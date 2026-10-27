"""Reload an atomically published local deployment between MCP requests.

The current call always finishes on its original implementation. A deployment
marker is published last, with hashes for every replacement Python file. No
native conversation, user configuration, prompt or sync pair is modified here.
"""
import hashlib
import importlib
import importlib.util
import json
import sys
from pathlib import Path


class Runtime:
    def __init__(self,server,app_root):
        self.server=server;self.app_root=Path(app_root).resolve()
        self.marker=self.app_root/'runtime-generation.json'
        self.last=self._digest();self.error=None

    def _digest(self):
        return hashlib.sha256(self.marker.read_bytes()).hexdigest() if self.marker.exists() else None

    def _validate(self,raw):
        doc=json.loads(raw)
        if not isinstance(doc.get('version'),str) or not doc.get('files'):raise ValueError('Invalid runtime deployment marker')
        for relative,digest in doc['files'].items():
            path=self.app_root/relative
            if not relative.startswith('context_hub/') or path.suffix!='.py' or not path.resolve().is_relative_to(self.app_root/'context_hub'):
                raise ValueError('Runtime deployment path is outside its package')
            if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest()!=digest:
                raise ValueError('Runtime deployment is incomplete; retaining the loaded service')
        return doc

    def _load_server(self):
        # Keep this small bootstrap controller stable. All work modules are
        # imported anew after their publication hashes have been checked.
        prefix='context_hub.'
        previous={k:v for k,v in sys.modules.items() if k.startswith(prefix) and k!=__name__}
        try:
            for name in previous:sys.modules.pop(name,None)
            importlib.invalidate_caches()
            module=importlib.import_module('context_hub.mcp_server')
            newer=module.Server(self.server.root,self.server.client)
            newer.initialized=self.server.initialized
            return newer
        except BaseException:
            for name in list(sys.modules):
                if name.startswith(prefix) and name!=__name__:sys.modules.pop(name,None)
            sys.modules.update(previous)
            for name,module in previous.items():
                parent,_,attr=name.rpartition('.')
                if parent in sys.modules:setattr(sys.modules[parent],attr,module)
            raise

    def refresh(self):
        changed=False
        try:
            raw=self.marker.read_bytes() if self.marker.exists() else None
            digest=hashlib.sha256(raw).hexdigest() if raw else None
            if digest and digest!=self.last:
                doc=self._validate(raw)
                # Timestamp-based .pyc validation can reuse old code after a
                # same-size update within one second. Drop only derived caches
                # corresponding to hash-verified, package-local source files.
                for relative in doc['files']:
                    cache=Path(importlib.util.cache_from_source(str(self.app_root/relative)))
                    if cache.exists() and cache.resolve().is_relative_to(self.app_root/'context_hub') and not cache.is_symlink():cache.unlink()
                self.server=self._load_server();self.last=digest;self.error=None;changed=True
        except Exception as e:self.error=str(e)[:500]
        self.server.runtime_reload_status={'supported':True,'deployment_marker':self.last,'error':self.error,
                                           'boundary':'between MCP requests'}
        return changed
