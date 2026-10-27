// Isolated protocol fixture, never connects to a model or a user's native home.
const fs=require('node:fs'),readline=require('node:readline');
const file=process.argv[2],log=process.argv[3],adapter=process.argv[4];
function emit(x){process.stdout.write(JSON.stringify(x)+'\n');}
readline.createInterface({input:process.stdin}).on('line',line=>{
 const req=JSON.parse(line);fs.appendFileSync(log,JSON.stringify(req)+'\n');
 const data=JSON.parse(fs.readFileSync(file,'utf8'));let result={};
 if(req.method==='initialize')result={serverInfo:{name:'fixture'}};
 if(req.method==='thread/resume')result={thread:{id:'fixture',turns:[]}};
 if(req.method==='thread/turns/list')result={data:data.stalePage?[]:data.turns,nextCursor:null};
 if(req.method==='thread/read')result={thread:{id:'fixture',turns:data.turns}};
 if(req.method==='session/resume')result=data.snapshot;
 emit({id:req.id,result});
 if(req.method==='fixture/busy'||req.method==='fixture/idle'){
  const busy=req.method==='fixture/busy';
  emit(adapter==='codex'?{method:busy?'turn/started':'turn/completed',params:{threadId:'fixture',turn:{id:'native'}}}:
   {method:'session/event',params:{sessionId:'fixture',type:busy?'turn.started':'turn.completed',payload:{}}});
 }
});
