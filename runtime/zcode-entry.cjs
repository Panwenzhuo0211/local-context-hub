'use strict';
// Entry selected by ZCode's built-in workspace runtime resolver. The original
// storage preparation protocol is passed through before any refresh relay starts.
const fs=require('node:fs'),path=require('node:path'),{spawn}=require('node:child_process');
const configPath=process.env.CONTEXT_HUB_ZCODE_BRIDGE_CONFIG;
if(!configPath)throw Error('ContextHub ZCode launcher configuration is missing');
const cfg=JSON.parse(fs.readFileSync(configPath,'utf8')),args=process.argv.slice(2);
const server=args.includes('app-server')&&!args.includes('--prepare-storage');
const runtime=path.dirname(configPath),native=[cfg.backend,...args];
const command=server?[path.join(runtime,'stdio-bridge.cjs'),'--adapter','zcode','--hub',cfg.hub,
 '--runtime',cfg.runtime,'--home',cfg.home,'--',cfg.node,...native]:native;
const child=spawn(cfg.node,command,{env:process.env,stdio:'inherit',windowsHide:true});
child.on('error',()=>{process.stderr.write('ContextHub ZCode native entry failed\n');process.exitCode=1;});
child.on('exit',code=>process.exit(code??1));
process.on('SIGTERM',()=>child.kill());
