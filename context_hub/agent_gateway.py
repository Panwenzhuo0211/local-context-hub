"""Authenticated stateless MCP over HTTP, bound only to loopback.

JSON response transport (no SSE/session storage). A separately configured trusted
HTTPS reverse proxy is needed for remote agents. No public tunnel or OAuth is
created here. Per-request bearer credentials bind identity and current policy.
"""
import argparse
import json
import os
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
from .agents import Registry
from .agent_server import AgentServer
from .cli import DEFAULT


class Gateway(ThreadingHTTPServer):
    daemon_threads=True
    allow_reuse_address=True

    def __init__(self,root,port=8765,public_origin=None):
        self.root=Path(root).resolve()
        self.public_origin=None
        if public_origin:
            u=urlsplit(public_origin)
            if u.scheme!='https' or not u.hostname or u.username or u.password or u.path not in ('','/') or u.query or u.fragment:
                raise ValueError('Public origin must be an HTTPS origin without path or credentials')
            self.public_origin=public_origin.rstrip('/')
        super().__init__(('127.0.0.1',port),Handler)


class Handler(BaseHTTPRequestHandler):
    server_version='ContextHub';sys_version=''
    protocol_version='HTTP/1.1'

    def setup(self):
        super().setup();self.connection.settimeout(15)

    def log_message(self,*args):pass  # Never log headers, tokens or private prompts.

    def respond(self,status,value=None,headers=None):
        body=b'' if value is None else json.dumps(value,ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type','application/json; charset=utf-8')
        self.send_header('Content-Length',str(len(body)))
        self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Connection','close')
        for k,v in (headers or {}).items():self.send_header(k,v)
        self.end_headers();self.wfile.write(body);self.close_connection=True

    def trusted_request(self):
        local='127.0.0.1:'+str(self.server.server_port)
        allowed_hosts={local,'localhost:'+str(self.server.server_port)}
        if self.server.public_origin:allowed_hosts.add(urlsplit(self.server.public_origin).netloc)
        if self.headers.get('Host') not in allowed_hosts:return False
        origin=self.headers.get('Origin')
        return not origin or origin in {'http://'+local,'http://localhost:'+str(self.server.server_port),self.server.public_origin}

    def authorize(self):
        header=self.headers.get('Authorization','')
        if not header.startswith('Bearer '):raise ValueError('Missing credential')
        token=header[7:]
        registry=Registry(self.server.root)
        name=registry.authenticate(token)
        return AgentServer(self.server.root,name,token)

    def do_POST(self):
        if not self.trusted_request():return self.respond(403,{'error':'Origin or host refused'})
        if self.path!='/mcp':return self.respond(404,{'error':'Not found'})
        try:agent=self.authorize()
        except (ValueError,OSError,KeyError):
            return self.respond(401,{'error':'Agent credential required'}, {'WWW-Authenticate':'Bearer realm="context-hub"'})
        content=self.headers.get('Content-Type','').split(';')[0].strip()
        if content!='application/json':return self.respond(415,{'error':'Use application/json'})
        if self.headers.get('Transfer-Encoding'):return self.respond(400,{'error':'Chunked requests unsupported'})
        try:length=int(self.headers.get('Content-Length',''))
        except ValueError:return self.respond(411,{'error':'Content-Length required'})
        if length<1 or length>1024*1024:return self.respond(413,{'error':'Request size limit'})
        try:
            payload=json.loads(self.rfile.read(length))
            if not isinstance(payload,dict) or payload.get('jsonrpc')!='2.0' or not isinstance(payload.get('method'),str):
                raise ValueError('Invalid request')
            version=self.headers.get('MCP-Protocol-Version')
            if version and version not in ('2024-11-05','2025-03-26','2025-06-18','2025-11-25'):
                return self.respond(400,{'error':'Unsupported protocol version'})
            # Stateless MCP omits Mcp-Session-Id; authorization is evaluated anew.
            agent.initialized=True
            response=agent.handle(payload)
            self.respond(202 if response is None else 200,response)
        except (ValueError,TypeError,AttributeError,KeyError,UnicodeError):
            self.respond(400,{'error':'Invalid request or revoked access'})
        except Exception:
            # Do not leak SQLite paths, tracebacks or other matter identities.
            self.respond(503,{'error':'Local service unavailable; retry later'})

    def do_GET(self):
        if not self.trusted_request():return self.respond(403,{'error':'Origin or host refused'})
        if self.path=='/healthz':return self.respond(200,{'service':'context-hub-agent-gateway','status':'listening'})
        self.respond(405 if self.path=='/mcp' else 404,{'error':'Use POST /mcp'},{'Allow':'POST'})

    def do_DELETE(self):self.respond(405,{'error':'Stateless transport has no sessions'},{'Allow':'POST'})


def main(argv=None):
    os.umask(0o077)
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--data-dir',type=Path,default=DEFAULT)
    p.add_argument('--port',type=int,default=8765)
    p.add_argument('--public-origin',help='Trusted HTTPS proxy origin; this does not create a tunnel')
    a=p.parse_args(argv)
    gateway=Gateway(a.data_dir,a.port,a.public_origin)
    try:gateway.serve_forever()
    except KeyboardInterrupt:pass
    finally:gateway.server_close()


if __name__=='__main__':main()
