"""Install opt-in launchers, without changing native clients or restarting them."""
import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

def install(target,hub,node,codex_backend,codex_app,zcode_app,zcode_backend,codex_home,zcode_home):
    target=Path(target).resolve();target.mkdir(parents=True,exist_ok=True)
    source=Path(__file__).resolve().parent
    build_relay=not (target/'codex-relay.exe').is_file() or not (target/'CodexRelay.cs').is_file() or (target/'CodexRelay.cs').read_bytes()!=(source/'CodexRelay.cs').read_bytes()
    for name in ('stdio-bridge.cjs','history-overlay.cjs','CodexRelay.cs','launch-desktop.ps1','zcode-entry.cjs'):
        shutil.copy2(source/name,target/name)
    for value in (node,codex_backend,codex_app,zcode_app,zcode_backend):
        if not Path(value).is_file():raise ValueError('Missing native application: '+str(value))
    compiler=Path(r'C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe')
    if build_relay:
        subprocess.run([str(compiler),'/nologo','/target:exe','/r:System.Web.Extensions.dll',
            '/out:'+str(target/'codex-relay.exe'),str(target/'CodexRelay.cs')],check=True,capture_output=True)
    connections=target/'connections';connections.mkdir(exist_ok=True)
    cfg={'adapter':'codex','node':str(node),'backend':str(codex_backend),'hub':str(hub),
         'runtime':str(connections),'home':str(codex_home)}
    (target/'codex-relay.json').write_text(json.dumps(cfg,ensure_ascii=False),encoding='utf-8')
    zcode_workspace=target/'zcode-workspace'
    entry=zcode_workspace/'apps/zcode-cli/packages/cli/dist/zcode.cjs'
    entry.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(source/'zcode-entry.cjs',entry)
    (target/'zcode-relay.json').write_text(json.dumps({'node':str(node),'backend':str(zcode_backend),
        'hub':str(hub),'runtime':str(connections),'home':str(zcode_home)},ensure_ascii=False),encoding='utf-8')
    desktop={'codex':{'executable':str(codex_app),'environment':{'CODEX_CLI_PATH':str(target/'codex-relay.exe')}},
        'zcode':{'executable':str(zcode_app),'workingDirectory':str(zcode_workspace),
            'environment':{'ZCODE_AGENT_SERVER_COMMAND':None,'ZCODE_AGENT_SERVER_ARGS_JSON':None,'ZCODE_AGENT_SERVER_CWD':None,
            'ZCODE_DESKTOP_AGENT_BYTECODE':'0','CONTEXT_HUB_ZCODE_BRIDGE_CONFIG':str(target/'zcode-relay.json')}}}
    desktop.update(python=sys.executable,hub=str(hub))
    (target/'desktop-launch.json').write_text(json.dumps(desktop,ensure_ascii=False,indent=2),encoding='utf-8')
    # Windows PowerShell 5.1 requires a BOM to read non-ASCII source text.
    ps=target/'launch-desktop.ps1';ps.write_text(ps.read_text(encoding='utf-8'),encoding='utf-8-sig')
    for adapter,label in (('codex','Codex'),('zcode','ZCode')):
        command='@echo off\r\npowershell.exe -NoProfile -ExecutionPolicy Bypass -File "'+str(ps)+'" -Adapter '+adapter+'\r\nif errorlevel 1 pause\r\n'
        (Path(hub)/('启动 '+label+' 同步桥.cmd')).write_text(command,encoding='ascii')
    return {'runtime':str(target),'clients_restarted':False,'vendor_files_modified':False}

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('target','hub','node','codex-backend','codex-app','zcode-app','zcode-backend','codex-home','zcode-home'):
        p.add_argument('--'+name,required=True,type=Path)
    args=vars(p.parse_args());args={k.replace('-','_'):v for k,v in args.items()}
    print(json.dumps(install(**args),ensure_ascii=False))
