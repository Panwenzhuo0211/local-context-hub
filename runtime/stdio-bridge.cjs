'use strict';
// Transparent native stdio relay with a local, authenticated refresh channel.
// Refresh reads existing history only. It never sends a prompt or starts a model.
const fs=require('node:fs'),path=require('node:path'),http=require('node:http');
const crypto=require('node:crypto'),readline=require('node:readline');
const {spawn}=require('node:child_process');
const own=process.argv.slice(2),split=own.indexOf('--');
if(split<0)throw Error('Missing native backend command');
const options={};for(let i=0;i<split;i+=2)options[own[i].replace(/^--/,'')]=own[i+1];
const command=own.slice(split+1),adapter=options.adapter;
if(!['codex','zcode'].includes(adapter)||!command.length)throw Error('Invalid native bridge configuration');
const hub=path.resolve(options.hub),runtime=path.resolve(options.runtime),home=path.resolve(options.home);
const rpcPrefix='context-hub:'+crypto.randomUUID()+':';
const env={...process.env};delete env.CODEX_CLI_PATH;
const child=spawn(command[0],command.slice(1),{env,stdio:['pipe','pipe','pipe'],windowsHide:true});
child.stderr.pipe(process.stderr);
const isServer=command.includes('app-server')&&!command.includes('daemon');
let ready=adapter==='zcode',closed=false,next=0,server,descriptorFile;
const internal=new Map(),parent=new Map(),sessions=new Map(),busy=new Set(),operations=new Map();
const blockedParent=new Map();let outputTail=Promise.resolve();
const overlay=adapter==='codex'?require('./history-overlay.cjs')({hub,runtime,home,request,sendParent,state,digest}):null;
function sendChild(obj){child.stdin.write(JSON.stringify(obj)+'\n');}
function sendParent(obj){process.stdout.write(JSON.stringify(obj)+'\n');}
function state(sid){if(!sessions.has(sid))sessions.set(sid,{seen:new Map(),resume:null,snapshot:null});return sessions.get(sid);}
function digest(value){return crypto.createHash('sha256').update(JSON.stringify(value)).digest('hex');}
function observeResult(method,result,sid){
 if(!result||result.error)return;
 const thread=result.thread;
 if(adapter==='codex'&&thread?.id&&['thread/resume','thread/start'].includes(method)){
  const s=state(thread.id);s.snapshot=thread;
  for(const turn of thread.turns||[])for(const item of turn.items||[])s.seen.set(item.id,digest(item));
 }
 if(adapter==='zcode'&&result.session?.sessionId&&['session/resume','session/create'].includes(method)){
  const s=state(result.session.sessionId);s.snapshot=result;
  for(const m of result.messages||[])for(const part of m.parts||[])s.seen.set(part.partId,digest(part));
 }
 if(sid&&sessions.has(sid)&&method==='session/subscribe'&&result.snapshot){
  const s=state(sid);s.snapshot=result.snapshot;
  for(const m of result.snapshot.messages||[])for(const part of m.parts||[])s.seen.set(part.partId,digest(part));
 }
}
function observeNotification(obj){
 const p=obj.params||{};
 if(adapter==='codex'){
  const sid=p.threadId;if(!sid)return;
  if(obj.method==='turn/started')busy.add(sid);
  if(obj.method==='turn/completed')busy.delete(sid);
  if(obj.method==='item/completed'&&p.item&&sessions.has(sid))state(sid).seen.set(p.item.id,digest(p.item));
  if(obj.method==='thread/status/changed'){
   if(p.status?.type==='active')busy.add(sid);
   else if(p.status?.type==='idle'||p.status?.type==='notLoaded')busy.delete(sid);
  }
 }else if(obj.method==='session/event'){
  const sid=p.sessionId;
  if(p.type==='turn.started')busy.add(sid);
  if(['turn.completed','turn.failed'].includes(p.type))busy.delete(sid);
  if(p.type==='part.upserted'&&p.payload?.part&&sessions.has(sid)){
   const part=p.payload.part;state(sid).seen.set(part.partId,digest(part));
  }
 }
}
function request(method,params){
 // Explicitly enrolled history is recorded by the native writer, without
 // starting a model turn. Submission and steering APIs remain excluded.
 const allowed=adapter==='codex'?['thread/turns/list','thread/read','thread/inject_items']:['session/resume','session/read','session/messages'];
 if(!allowed.includes(method))return Promise.reject(Error('bridge method is not a history operation'));
 const id=rpcPrefix+(++next);
 return new Promise((resolve,reject)=>{
  const timer=setTimeout(()=>{internal.delete(id);reject(Error('native refresh timed out'));},10000);
  internal.set(id,{resolve:r=>{clearTimeout(timer);resolve(r);},reject:e=>{clearTimeout(timer);reject(e);}});
  sendChild({...(adapter==='codex'?{jsonrpc:'2.0'}:{}),id,method,params});
 });
}
function paired(sid){
 try{
  const allow=JSON.parse(fs.readFileSync(path.join(hub,'runtime-pairs.json'),'utf8'));
  return allow.schema===1&&allow.pairs.some(p=>p.enabled&&p[adapter]===sid&&
   path.resolve(p.homes[adapter]).toLowerCase()===home.toLowerCase());
 }catch{return false;}
}
function eventId(sid,revision,part){return crypto.createHash('sha256').update([adapter,sid,revision,part].join(':')).digest('hex');}
async function refreshCodex(body){
 const delivered=await overlay.refresh(body);if(delivered)return delivered;
 const s=state(body.session),wanted=new Set(body.refs||[]),found=new Set();let cursor=null,sent=0;
 function consume(turns){
  for(const turn of turns){
   if(turn.status==='inProgress')continue;
   let changed=false;
   for(const item of turn.items||[]){
    if(!wanted.has(item.id))continue;found.add(item.id);
    if(s.seen.get(item.id)===digest(item))continue;
    sendParent({method:'item/completed',params:{threadId:body.session,turnId:turn.id,item,
     ...(turn.completedAt!=null?{completedAtMs:turn.completedAt*1000}:{})}});
    s.seen.set(item.id,digest(item));sent++;changed=true;
   }
   if(changed)sendParent({method:'turn/completed',params:{threadId:body.session,turn}});
  }
 }
 for(let page=0;page<20;page++){
  const reply=await request('thread/turns/list',{threadId:body.session,limit:30,sortDirection:'desc',itemsView:'full',...(cursor?{cursor}:{})});
  if(reply.error)throw Error('native history read rejected');
  const data=reply.result||reply;
  consume(data.data||[]);
  if([...wanted].every(id=>found.has(id)))break;
  cursor=data.nextCursor;if(!cursor)break;
 }
 if([...wanted].some(id=>!found.has(id))){
  // Installed paginated readers may retain a stale page after an external
  // append. The native full reader parses fresh persisted presentation events.
  const reply=await request('thread/read',{threadId:body.session,includeTurns:true});
  if(reply.error)throw Error('native full history read rejected');
  consume((reply.result||reply).thread?.turns||[]);
 }
 if([...wanted].some(id=>!found.has(id)))throw Error('native history has not exposed all synchronized items');
 return {status:'notifications_sent',sent,model_started:false,desktop_visible:'UNVERIFIED'};
}
async function refreshZcode(body){
 const s=state(body.session),params={...(s.resume||{}),sessionId:body.session};
 const reply=await request('session/resume',params);
 if(reply.error)throw Error('native snapshot read rejected');
 const snapshot=reply.result||reply;
 if(snapshot.projection?.status!=='idle')return {status:'waiting_idle',model_started:false};
 const wanted=new Set(body.refs||[]),found=new Set();let sent=0;
 // desktop-continuous explicitly supports unsequenced (seq=0) events. The
 // installed host assigns sequence numbers and deduplicates stable event IDs.
 const emit=(type,payload,key)=>sendParent({method:'session/event',params:{
  sessionId:body.session,traceId:snapshot.session.traceId,eventId:eventId(body.session,body.revision,key),
  seq:0,timestamp:Date.now(),deliveryKind:'desktop-continuous',type,payload}});
 const changed=[];
 for(const m of snapshot.messages||[]){
  const mid=m.info?.messageId;if(!wanted.has(mid))continue;found.add(mid);
  for(const part of m.parts||[]){
   const kind=part.metadata?.context_hub_sync?.kind;
   if(kind&&!['user_text','assistant_text','tool_call'].includes(kind))continue;
   if(s.seen.get(part.partId)===digest(part))continue;
   changed.push(part);
  }
 }
 if([...wanted].some(id=>!found.has(id)))throw Error('native snapshot has not exposed all synchronized messages');
 if(changed.length){
  emit('session.resumed',{directory:snapshot.session.workspace.workspacePath,interruptedToolCount:0,
   messageCount:snapshot.messages.length,partCount:snapshot.messages.reduce((n,m)=>n+(m.parts||[]).length,0)},'resume');
  for(const part of changed){emit('part.upserted',{part},part.partId);s.seen.set(part.partId,digest(part));sent++;}
 }
 s.snapshot=snapshot;
 return {status:'notifications_sent',sent,model_started:false,desktop_visible:'UNVERIFIED',
  limitation:'native UI rendering of replayed part events requires desktop acceptance'};
}
async function apply(body,key){
 if(!paired(body.session)||busy.has(body.session)){
  const result={status:paired(body.session)?'waiting_idle':'paused',model_started:false};operations.set(key,result);return result;
 }
 const op={status:'refreshing',model_started:false};operations.set(key,op);
 try{
  // Gate parent submissions for this session during a read/notification batch.
  blockedParent.set(body.session,[]);
  const result=await(adapter==='codex'?refreshCodex(body):refreshZcode(body));
  operations.set(key,result);return result;
 }catch(error){if(process.env.CONTEXT_HUB_BRIDGE_DEBUG==='1')process.stderr.write('ContextHub refresh: '+String(error.message)+'\n');const result={status:'retry_pending',error:'native_refresh_failed',model_started:false};operations.set(key,result);return result;}
 finally{for(const obj of blockedParent.get(body.session)||[])sendChild(obj);blockedParent.delete(body.session);}
}
function valid(body){
 return body&&typeof body.session==='string'&&typeof body.revision==='string'&&body.revision.length<=200&&
  Array.isArray(body.refs)&&body.refs.length<=500&&body.refs.every(x=>typeof x==='string'&&x.length<300)&&
  Object.keys(body).every(k=>['session','revision','refs'].includes(k));
}
function descriptor(){
 if(!server||closed)return;
 const doc={schema:1,adapter,pid:process.pid,port:server.address().port,token:secret,
  home,ready,sessions:[...sessions.keys()],heartbeat:Date.now()/1000};
 const temp=descriptorFile+'.tmp';fs.writeFileSync(temp,JSON.stringify(doc),{mode:0o600});fs.renameSync(temp,descriptorFile);
}
const secret=crypto.randomBytes(32).toString('hex');
if(isServer){
 fs.mkdirSync(runtime,{recursive:true});descriptorFile=path.join(runtime,adapter+'-'+process.pid+'.json');
 server=http.createServer((req,res)=>{
  const deny=(code)=>{res.writeHead(code);res.end();};
  const auth=String(req.headers.authorization||'').replace(/^Bearer /,'');
  if(req.headers.origin||Buffer.byteLength(auth)!==secret.length||!crypto.timingSafeEqual(Buffer.from(auth),Buffer.from(secret)))return deny(403);
  if(req.method==='GET'&&req.url==='/status'){res.setHeader('Content-Type','application/json');return res.end(JSON.stringify({ready,sessions:[...sessions.keys()],model_started:false}));}
  if(req.method!=='POST'||req.url!=='/refresh')return deny(404);
  let raw='';req.on('data',chunk=>{raw+=chunk;if(Buffer.byteLength(raw)>200000){res.writeHead(413);res.end();req.destroy();}});
  req.on('end',()=>{
   let body;try{body=JSON.parse(raw);}catch{return deny(400);}
   if(!valid(body)||!paired(body.session))return deny(403);
   const respond=result=>{res.setHeader('Content-Type','application/json');res.end(JSON.stringify(result));};
   if(!ready||!sessions.has(body.session))return respond({status:'waiting_session_open',model_started:false});
   const key=body.session+':'+body.revision,old=operations.get(key);
   if(old?.status==='notifications_sent'||old?.status==='refreshing')return respond(old);
   if(busy.has(body.session)||blockedParent.has(body.session))return respond({status:'waiting_idle',model_started:false});
   // Acknowledge immediately; subsequent polls obtain the final result.
   operations.set(key,{status:'refreshing',model_started:false});respond(operations.get(key));
   outputTail=outputTail.then(()=>apply(body,key)).catch(()=>{});
   if(operations.size>1000)operations.delete(operations.keys().next().value);
  });
 });
 server.listen(0,'127.0.0.1',()=>descriptor());
 const heartbeat=setInterval(descriptor,3000);heartbeat.unref();
}
const input=readline.createInterface({input:process.stdin});
input.on('line',line=>{
 let obj;try{obj=JSON.parse(line);}catch{child.stdin.write(line+'\n');return;}
 const p=obj.params||{},sid=p.threadId||p.sessionId;
 if(obj.method&&obj.id!=null){
  parent.set(String(obj.id),{method:obj.method,sid,params:p});
  if(sid&&['thread/resume','session/resume'].includes(obj.method))state(sid).resume=p;
  if(sid&&['turn/start','turn/addUserMessage','session/send','session/compact'].includes(obj.method))busy.add(sid);
 }
 if(sid&&blockedParent.has(sid)){blockedParent.get(sid).push(obj);return;}
 sendChild(obj);
});
input.on('close',()=>child.stdin.end());
readline.createInterface({input:child.stdout}).on('line',line=>{
 let obj;try{obj=JSON.parse(line);}catch{process.stdout.write(line+'\n');return;}
 if(obj.id!=null&&internal.has(obj.id)&&!obj.method){const entry=internal.get(obj.id);internal.delete(obj.id);entry.resolve(obj);return;}
 if(obj.id!=null&&!obj.method){
  const req=parent.get(String(obj.id));
  if(req){parent.delete(String(obj.id));if(overlay)obj=overlay.response(req.method,obj,req.params);if(req.method==='initialize'&&!obj.error)ready=true;
   if(obj.error&&req.sid)busy.delete(req.sid);
   if(!obj.error)observeResult(req.method,obj.result,req.sid);
  }
 }else if(obj.method&&obj.id==null)observeNotification(obj);
 sendParent(obj);
});
function cleanup(){closed=true;server?.close();if(descriptorFile)try{fs.unlinkSync(descriptorFile);}catch{}
 for(const entry of internal.values())entry.reject(Error('backend exited'));internal.clear();}
child.on('error',()=>{cleanup();process.exitCode=1;});
child.on('exit',code=>{cleanup();input.close();process.stdin.destroy();process.exit(code||0);});
process.on('SIGTERM',()=>{cleanup();child.kill();});
