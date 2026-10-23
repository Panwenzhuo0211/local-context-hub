#!/usr/bin/env python3
"""Local interactive launcher. Credentials stay out of arguments and files."""
import getpass
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

APP=Path(__file__).resolve().parent
DATA=Path.home()/'Library/Application Support/ContextHub'


def main():
    os.umask(0o077)
    binary=APP/'tunnel-client/tunnel-client'
    access=DATA/'chatgpt-access.json'
    if not binary.is_file() or not access.is_file():
        raise SystemExit('请先完成 Context Hub 本地接入端安装。')
    print('Context Hub → ChatGPT 官方安全隧道')
    print('只传输已开放事项的按需查询结果；不调用付费模型 API。隧道自身费用须以账号显示为准。')
    print('已开放事项配置：',access)
    saved_tunnel=DATA/'chatgpt-tunnel-id.txt'
    tunnel=(saved_tunnel.read_text().strip() if saved_tunnel.exists()
            else input('粘贴 Platform 隧道 ID（tunnel_...）：').strip())
    if not re.fullmatch(r'tunnel_[A-Za-z0-9_-]+',tunnel):raise SystemExit('隧道 ID 格式无效。')
    key=os.environ.get('CONTROL_PLANE_API_KEY') or getpass.getpass('输入仅有 Tunnels Read/Use 权限的运行密钥（不回显、不保存）：')
    if not key.strip():raise SystemExit('未提供运行密钥。')
    env=os.environ.copy();env['CONTROL_PLANE_API_KEY']=key
    env['XDG_CONFIG_HOME']=str(DATA/'tunnel-config')
    env['XDG_STATE_HOME']=str(DATA/'tunnel-state')
    command=shlex.join([sys.executable,str(APP/'chatgpt-entry.py'),'--access-file',str(access)])
    args=[str(binary),'runtimes','connect','--alias','context-hub-chatgpt','--profile','context-hub-chatgpt',
          '--profile-dir',str(DATA/'tunnel-profiles'),'--tunnel-id',tunnel,'--mcp-command',command,
          '--runtime-api-key','env:CONTROL_PLANE_API_KEY']
    subprocess.run(args,env=env,check=True)
    result=subprocess.run([str(binary),'runtimes','status','context-hub-chatgpt','--json'],env=env,check=True,capture_output=True,text=True)
    state=json.loads(result.stdout)
    print(json.dumps({k:state.get(k) for k in ('process_running','healthy','ready','runtime_state')},ensure_ascii=False))
    print('请确认状态为 ready，再到 ChatGPT 插件添加自定义 MCP → Tunnel，选择同一隧道。')
    print('运行密钥只在进程环境中保留；电脑重启或隧道进程停止后需重新运行此入口。')

if __name__=='__main__':main()
