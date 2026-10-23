"""Explicitly registered local Markdown evidence; never follows document instructions."""
import time
from pathlib import Path
from .model import digest


def initialize(db):
    db.executescript('''
    CREATE TABLE IF NOT EXISTS memory_sources (
      matter TEXT, path TEXT, author TEXT, PRIMARY KEY(matter,path));
    CREATE TABLE IF NOT EXISTS memory_versions (
      matter TEXT, path TEXT, hash TEXT, body TEXT, modified REAL, observed REAL,
      PRIMARY KEY(matter,path,hash));
    ''')


def register(m, matter, path, author):
    mid=m.get(matter)['id'];path=Path(path).expanduser().resolve()
    if path.suffix.lower()!='.md' or not path.is_file():
        raise ValueError('Register an existing Markdown file')
    with m.db:
        m.db.execute('INSERT OR REPLACE INTO memory_sources VALUES (?,?,?)',(mid,str(path),author))
        m.audit('user','register_memory',{'matter':mid,'path':str(path),'author':author})


def read(m, mid):
    result=[];budget=36000
    for row in m.db.execute('SELECT * FROM memory_sources WHERE matter=? ORDER BY path',(mid,)).fetchall():
        source=dict(row);path=Path(source['path'])
        try:
            if path.is_symlink():raise ValueError('Source replaced by symlink')
            stat=path.stat()
            if stat.st_size>512000:raise ValueError('Source exceeds 512 KB limit')
            body=path.read_text(encoding='utf-8');sha=digest(body)
            with m.db:
                m.db.execute('INSERT OR IGNORE INTO memory_versions VALUES (?,?,?,?,?,?)',
                             (mid,str(path),sha,body,stat.st_mtime,time.time()))
            excerpt=body[:min(16000,budget)];budget-=len(excerpt)
            result.append({**source,'text':excerpt,'hash':sha,'modified':stat.st_mtime,
                           'truncated':len(excerpt)<len(body),'total_chars':len(body),
                           'status':'read','untrusted':True,
                           'interpretation':'作者的历史记忆，日期以正文为准；不代表当前事实或已验证结论。'})
        except (OSError,ValueError) as e:
            result.append({**source,'status':'unavailable','error':str(e)[:200]})
        if budget<=0:break
    return result
