'use strict';
const fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto'),readline=require('node:readline');
module.exports=function({hub,runtime,home,request,sendParent,state,digest}){
 const normalized=p=>path.resolve(p.replace(/^\\\\\?\\/,''));
 function load(sid){
  try{
   const pairs=JSON.parse(fs.readFileSync(path.join(hub,'runtime-pairs.json'),'utf8'));
   const pair=pairs.pairs.find(p=>p.enabled&&p.codex===sid&&normalized(p.homes.codex).toLowerCase()===normalized(home).toLowerCase());
   if(!pair||!/^[0-9a-f]{32}$/.test(pair.id))return null;
   const file=path.join(hub,'runtime-history',pair.id+'.json');
   if(!fs.existsSync(file))return null;
   if(fs.statSync(file).size>32*1024*1024)throw Error('History overlay exceeds supported size');
   const doc=JSON.parse(fs.readFileSync(file,'utf8'));
   if(doc.schema!==1||doc.thread!==sid||doc.pair!==pair.id)throw Error('History overlay identity mismatch');
   return doc;
  }catch(e){if(e.code==='ENOENT')return null;throw e;}
 }
 function receipt(sid){
  const file=path.join(runtime,'injected-'+crypto.createHash('sha256').update(home+sid).digest('hex')+'.json');
  let ids=[];try{ids=JSON.parse(fs.readFileSync(file,'utf8')).ids||[];}catch(e){if(e.code!=='ENOENT')throw e;}
  return {file,ids:new Set(ids)};
 }
 function save(r){const tmp=r.file+'.tmp';fs.writeFileSync(tmp,JSON.stringify({schema:1,ids:[...r.ids]}),{mode:0o600});fs.renameSync(tmp,r.file);}
 function available(sid){const doc=load(sid);if(!doc)return [];const r=receipt(sid);return doc.entries.filter(e=>e.model_items.every(i=>r.ids.has(i.id))).map(e=>e.turn);}
 function merge(native,extra,desc=false){
  const ids=new Set(native.map(t=>t.id));const itemIds=new Set(native.flatMap(t=>(t.items||[]).map(i=>i.id)));
  const all=[...native,...extra.filter(t=>!ids.has(t.id)).map(t=>({...t,items:t.items.filter(i=>!itemIds.has(i.id))})).filter(t=>t.items.length)];
  return all.sort((a,b)=>(desc?-1:1)*((a.startedAt||0)-(b.startedAt||0)));
 }
 function response(method,obj,params){
  const sid=params?.threadId||obj.result?.thread?.id;if(!sid)return obj;
  let turns;try{turns=available(sid);}catch{return obj;}
  if(!turns.length)return obj;
  if(method==='thread/items/list'){
   const turn=turns.find(t=>t.id===params.turnId);
   if(turn)return {id:obj.id,result:{data:turn.items,nextCursor:null}};
  }
  if(obj.error)return obj;
  if(['thread/resume','thread/read'].includes(method)&&obj.result?.thread){
   if(method==='thread/read'&&!params.includeTurns)return obj;
   obj.result.thread.turns=merge(obj.result.thread.turns||[],turns);
  }
  if(method==='thread/turns/list'&&!params.cursor&&Array.isArray(obj.result?.data))
   obj.result.data=merge(obj.result.data,turns,params.sortDirection!=='asc');
  return obj;
 }
 async function scan(file,wanted,found){
  if(!file)return;
  file=normalized(file);const relative=path.relative(normalized(home),file);
  if(relative.startsWith('..')||path.isAbsolute(relative))throw Error('Native history is outside the enrolled home');
  if(!fs.existsSync(file))return;
  const stream=fs.createReadStream(file);const lines=readline.createInterface({input:stream,crlfDelay:Infinity});
  try{for await(const line of lines){
   if(!line.includes('context-hub-sync:'))continue;
   let row;try{row=JSON.parse(line);}catch{continue;}
   const id=row.type==='response_item'?row.payload?.id:null;if(wanted.has(id))found.add(id);
  }}finally{lines.close();stream.destroy();}
 }
 async function refresh(body){
  const doc=load(body.session);if(!doc)return null;
  const wanted=new Set(body.refs),entries=doc.entries.filter(e=>e.turn.items.some(i=>wanted.has(i.id)));
  if(!entries.length)return null;
  if([...wanted].some(id=>!entries.some(e=>e.turn.items.some(i=>i.id===id))))throw Error('Refresh mixes unrelated delivery IDs');
  const r=receipt(body.session),all=entries.flatMap(e=>e.model_items),missing=new Set(all.filter(i=>!r.ids.has(i.id)).map(i=>i.id));
  if(missing.size){
   // A response can be lost after a successful native write. Recover from
   // native message IDs before retrying, rather than injecting another copy.
   const paths=new Set(entries.map(e=>e.source_rollout));paths.add(state(body.session).snapshot?.path);
   for(const file of paths)await scan(file,missing,r.ids);
   const inject=all.filter(i=>!r.ids.has(i.id));
   if(inject.length){
    const reply=await request('thread/inject_items',{threadId:body.session,items:inject});
    if(reply.error)throw Error('Native writer rejected history injection');
    for(const item of inject)r.ids.add(item.id);
   }
   save(r);
  }
  let sent=0;const s=state(body.session);
  for(const entry of entries){let changed=false;
   for(const item of entry.turn.items){
    if(!wanted.has(item.id)||s.seen.get(item.id)===digest(item))continue;
    sendParent({method:'item/completed',params:{threadId:body.session,turnId:entry.turn.id,item,completedAtMs:entry.turn.completedAt*1000}});
    s.seen.set(item.id,digest(item));sent++;changed=true;
   }
   if(changed)sendParent({method:'turn/completed',params:{threadId:body.session,turn:entry.turn}});
  }
  return {status:'notifications_sent',sent,native_writer:true,model_started:false,desktop_visible:'UNVERIFIED'};
 }
 return {refresh,response};
};
