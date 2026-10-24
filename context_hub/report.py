"""Static read-only HTML report; no server, no port, opens in any browser."""
import json
import shutil
import sqlite3
import time
from pathlib import Path

PAGE = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>Context Hub 报告</title>
<style>
 body{font-family:"Segoe UI",system-ui,sans-serif;background:#101418;color:#d8dee6;margin:0;padding:32px}
 h1{font-size:20px;margin:0 0 4px} .sub{color:#7f8b99;font-size:12px;margin-bottom:24px}
 .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:12px;margin-bottom:28px}
 .card{background:#1a2027;border-radius:10px;padding:14px}
 .card .k{color:#7f8b99;font-size:12px} .card .v{font-size:22px;font-weight:600;margin-top:4px}
 .ok{color:#5ec26a}.warn{color:#e2b93b}.bad{color:#e2635e}
 h2{font-size:15px;color:#9fb0c0;border-bottom:1px solid #2a323c;padding-bottom:6px;margin:28px 0 12px}
 table{width:100%;border-collapse:collapse;font-size:13px}
 td,th{padding:7px 10px;border-bottom:1px solid #232b34;text-align:left;vertical-align:top}
 th{color:#7f8b99;font-weight:500}
 .note{color:#a8b6c4} .author{color:#6fa8d6} .muted{color:#66727f}
 .empty{color:#66727f;padding:12px 0}
</style></head><body>
<h1>Context Hub 运行报告</h1>
<div class="sub">__GENERATED__ · 数据目录 __ROOT__</div>
__HEALTH__
<h2>事项（__MCOUNT__）</h2>
__MATTERS__
<h2>自动关联候选（auto 标记 __ACOUNT__ 条）</h2>
__CANDIDATES__
<h2>最近一次同步警告</h2>
__WARNINGS__
</body></html>"""

def _cards(items):
    cells=''.join(f'<div class="card"><div class="k">{k}</div><div class="v {cls}">{v}</div></div>'
                  for k,v,cls in items)
    return f'<div class="grid">{cells}</div>'

def _health(root,status,mstate,db):
    def read_status(key,default='—'):
        return status.get(key,default) if isinstance(status,dict) else default
    lag=read_status('index_lag_seconds',None); pending=read_status('pending_heads')
    tick=read_status('status')
    free=shutil.disk_usage(root).free/1024**3
    backup_at=None
    row=db.execute("SELECT value FROM cursors WHERE key='auto-backup'").fetchone()
    if row:
        try:backup_at=json.loads(row[0])['at']
        except (ValueError,KeyError):pass
    backup_age='从未' if not backup_at else f'{(time.time()-backup_at)/86400:.1f} 天前'
    segs=list((root/'log').glob('seg-*.log'))
    seg_mb=sum(p.stat().st_size for p in segs)//1024//1024
    cls=lambda bad,warning: 'bad' if bad else ('warn' if warning else 'ok')
    def fmt_lag():
        if lag is None:return ('—','')
        v=float(lag)
        return (f'{v:.0f} 秒',cls(v>600,v>120))
    lag_text,lag_cls=fmt_lag()
    return _cards([
        ('后台状态',str(tick),cls(tick=='error',tick!='ok' and tick!='running')),
        ('投影滞后',lag_text,lag_cls),
        ('待投影记录',str(pending),'bad' if isinstance(pending,int) and pending>500 else 'ok'),
        ('修订总数',f"{db.execute('SELECT count(*) FROM revisions').fetchone()[0]:,}",''),
        ('检索文本',f"{mstate.get('docs',0):,}",''),
        ('日志段',f'{len(segs)} 个 / {seg_mb}MB',''),
        ('上次备份',backup_age,'bad' if backup_at and time.time()-backup_at>8*86400 else 'warn' if not backup_at else 'ok'),
        ('磁盘空闲',f'{free:.1f} GB','bad' if free<2 else ('warn' if free<8 else 'ok')),
    ])

def render(root):
    root=Path(root)
    db=sqlite3.connect((root/'state.sqlite').as_uri()+'?mode=ro',uri=True)
    db.row_factory=sqlite3.Row
    m=sqlite3.connect((root/'matters.sqlite').as_uri()+'?mode=ro',uri=True)
    m.row_factory=sqlite3.Row
    try:
        status={}
        path=root/'background-status.json'
        if path.exists():
            try:status=json.loads(path.read_text(encoding='utf-8'))
            except ValueError:pass
        mstate={'docs':m.execute('SELECT count(*) FROM docs').fetchone()[0]}
        matters=[dict(r) for r in m.execute('SELECT * FROM matters ORDER BY created')]
        rows=[]
        for mat in matters:
            notes=m.execute('SELECT count(*) FROM entries WHERE matter=?',(mat['id'],)).fetchone()[0]
            open_tasks=[dict(r) for r in m.execute(
                "SELECT * FROM entries WHERE matter=? AND kind='task' ORDER BY created DESC",(mat['id'],))]
            latest={}
            for n in open_tasks:latest.setdefault((n['author'],n['topic']),n)
            open_n=len([n for n in latest.values() if n['status'] not in ('done','rejected')])
            links=m.execute("SELECT count(*) FROM links WHERE matter=? AND status!='rejected'",(mat['id'],)).fetchone()[0]
            recent=m.execute('SELECT * FROM entries WHERE matter=? ORDER BY created DESC LIMIT 2',(mat['id'],)).fetchall()
            notes_html=''.join(f'<div class="note">· <span class="author">{n["author"]}</span> [{n["kind"]}] {n["topic"]}：{n["body"][:80]}</div>'
                               for n in recent) or '<div class="muted">尚无笔记</div>'
            rows.append(f'<tr><td><b>{mat["name"]}</b><div class="muted">{mat["description"][:60]}</div></td>'
                        f'<td>{notes}</td><td>{open_n}</td><td>{links}</td><td>{notes_html}</td></tr>')
        matters_html=('<table><tr><th>事项</th><th>笔记</th><th>未结任务</th><th>关联</th><th>最近笔记</th></tr>'
                      +''.join(rows)+'</table>') if matters else \
                      '<div class="empty">尚无事项。在 ZCode/Codex 里说「新建事项 XX」即可建立。</div>'
        autos=[dict(r) for r in m.execute("SELECT * FROM links WHERE status='auto' ORDER BY updated DESC LIMIT 20")]
        auto_html=('<table><tr><th>事项</th><th>类型</th><th>目标</th><th>理由</th></tr>'+
                   ''.join(f'<tr><td>{a["matter"][:12]}</td><td>{a["kind"]}</td>'
                           f'<td class="muted">{a["target"][:60]}</td><td class="note">{a["reason"]}</td></tr>' for a in autos)
                   +'</table>') if autos else '<div class="empty">无待确认的自动关联。</div>'
        warnings=[]
        row=db.execute('SELECT result FROM runs ORDER BY id DESC LIMIT 1').fetchone()
        if row:
            try:warnings=json.loads(row[0]).get('warnings',[])[:10]
            except ValueError:pass
        warn_html=''.join(f'<div class="note">· {w}</div>' for w in warnings) or '<div class="empty">最近一次同步无警告。</div>'
        page=(PAGE.replace('__GENERATED__',time.strftime('%Y-%m-%d %H:%M:%S'))
                  .replace('__ROOT__',str(root))
                  .replace('__HEALTH__',_health(root,status,mstate,db))
                  .replace('__MCOUNT__',str(len(matters)))
                  .replace('__MATTERS__',matters_html)
                  .replace('__ACOUNT__',str(len(autos)))
                  .replace('__CANDIDATES__',auto_html)
                  .replace('__WARNINGS__',warn_html))
        out=root/'report.html'
        out.write_text(page,encoding='utf-8')
        return out
    finally:
        db.close();m.close()
