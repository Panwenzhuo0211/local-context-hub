"""Local MCP stdio / single-call wrapper; no bearer secret on the command line."""
import argparse
import json
import os
import sys
from pathlib import Path
from .agents import Registry
from .agent_server import AgentServer
from .cli import DEFAULT,setup_console
from .model import use_utf8_stdio


def main(argv=None):
    os.umask(0o077);setup_console();use_utf8_stdio()
    p=argparse.ArgumentParser()
    p.add_argument('--data-dir',type=Path,default=DEFAULT)
    p.add_argument('--agent',required=True)
    p.add_argument('--call',help='Single tool call with JSON arguments from stdin; otherwise MCP stdio')
    a=p.parse_args(argv)
    try:
        registry=Registry(a.data_dir)
        token=json.loads(registry.credential_path(a.agent).read_text(encoding='utf-8'))['token']
        server=AgentServer(a.data_dir,a.agent,token)
        if a.call:
            raw=sys.stdin.read(1024*1024+1)
            if len(raw)>1024*1024:raise ValueError('Request too large')
            result=server.call(a.call,json.loads(raw))
            print(json.dumps(result,ensure_ascii=False));return 0
        for line in iter(lambda:sys.stdin.readline(1024*1024+1),''):
            try:
                if len(line)>1024*1024:raise ValueError('Request too large')
                response=server.handle(json.loads(line))
            except (ValueError,TypeError,KeyError,AttributeError):
                response={'jsonrpc':'2.0','id':None,'error':{'code':-32600,'message':'Request or access refused'}}
            if response is not None:print(json.dumps(response,ensure_ascii=False),flush=True)
        return 0
    except (ValueError,OSError,KeyError):
        print('Context Hub: agent access unavailable; ask owner to check enrollment',file=sys.stderr);return 1


if __name__=='__main__':raise SystemExit(main())
