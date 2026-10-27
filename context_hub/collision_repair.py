"""Offline removal of *only* verified bridge records that collided with a writer.

The launcher runs this after the desktop/backend have exited, before reopening.
Native-authored records and their ordinal values are never changed.
"""
import hashlib,json,os,sqlite3,subprocess,time
from pathlib import Path
from .store import atomic,exclusive
from .session_ir.live import MARKER

def _sha(data):return hashlib.sha256(data).hexdigest()

def _closed():
    if os.name!='nt':return
    command="@(Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'codex.exe' -and $_.CommandLine -match '\\bapp-server\\b' }).Count"
    result=subprocess.run(['powershell.exe','-NoProfile','-Command',command],capture_output=True,text=True,check=True)
    if result.stdout.strip()!='0':raise ValueError('请先退出 Codex 及其后台，再修复原生记录序号。')

def _plan(root,state,pair,config):
    path=Path(pair['path']);raw=path.read_bytes();stack=[];removed=[];base=None
    offset=0
    for line in raw.splitlines(keepends=True):
        if not line.endswith(b'\n'):raise ValueError('Native file has an unfinished record')
        row=json.loads(line)
        if base is None:base=row.get('ordinal')
        if not isinstance(base,int):return None
        actual=row.get('ordinal');expected=base+len(stack)
        if actual!=expected:
            count=expected-actual if isinstance(actual,int) else 0
            if count<=0 or count>len(stack):raise ValueError('Ordinal fault is not a removable bridge collision')
            candidates=stack[-count:]
            if any((r[2].get(MARKER) or {}).get('pair')!=pair['id'] for r in candidates):
                raise ValueError('Refusing to remove or renumber a native-authored record')
            removed.extend(candidates);del stack[-count:]
        stack.append((offset,line,row));offset+=len(line)
    if not removed:return None
    events=sorted({r[2][MARKER]['event'] for r in removed})
    native=sqlite3.connect((Path(config['sources']['zcode'])/'cli/db/db.sqlite').as_uri()+'?mode=ro',uri=True)
    try:
        for event in events:
            if not event.startswith('zcode:') or not native.execute('SELECT 1 FROM part WHERE session_id=? AND id=?',(pair['zcode_id'],event[6:])).fetchone():
                raise ValueError('Original ZCode evidence missing; bridge history cannot be removed safely')
    finally:native.close()
    new=b''.join(r[1] for r in stack);backup=Path(root)/'backups'/('ordinal-collision-'+str(time.time_ns()));backup.mkdir(parents=True)
    atomic(backup/'before.jsonl',raw)
    # Byte-cursor translation keeps already delivered native messages from
    # being delivered again under new byte-derived identities.
    cursor=pair['offset']-sum(len(line) for start,line,row in removed if start<pair['offset'])
    items=[r[2]['payload']['item']['id'] for r in removed if r[2].get('payload',{}).get('type')=='item_completed']
    turns=sorted({r[2]['payload']['turn_id'] for r in removed if r[2].get('payload',{}).get('turn_id')})
    plan={'pair':pair['id'],'thread':pair['codex_id'],'path':str(path),'home':config['sources']['codex'],'backup':str(backup),
          'old_hash':_sha(raw),'new_hash':_sha(new),'new_cursor':cursor,'events':events,'item_ids':items,'turn_ids':turns,'phase':'planned'}
    atomic(backup/'repaired.jsonl',new);atomic(Path(root)/'native-repair-pending.json',json.dumps(plan,ensure_ascii=False))
    return plan

def repair(root):
    _closed();root=Path(root)
    from .session_sync import _db,_config
    config=_config(root);pending=root/'native-repair-pending.json';reports=[]
    with exclusive(root,'session-sync.lock'):
        state=_db(root)
        try:
            for pair in state.execute('SELECT * FROM pairs WHERE enabled=1').fetchall():
                plan=json.loads(pending.read_text(encoding='utf-8')) if pending.exists() else _plan(root,state,pair,config)
                if not plan:continue
                if plan['pair']!=pair['id']:raise ValueError('Another pair has an unfinished repair')
                path=Path(plan['path']);raw=path.read_bytes();backup=Path(plan['backup']);new=(backup/'repaired.jsonl').read_bytes()
                if _sha(raw)==plan['old_hash']:
                    _closed()
                    with path.open('r+b',buffering=0) as f:
                        current=f.read()
                        if _sha(current)!=plan['old_hash']:raise ValueError('Native source changed; repair deferred')
                        f.seek(0);f.write(new);f.truncate();os.fsync(f.fileno())
                elif _sha(raw)!=plan['new_hash']:raise ValueError('Native source changed after repair planning')
                h=sqlite3.connect(Path(plan['home'])/'thread_history_1.sqlite')
                try:
                    with h:
                        h.executemany('DELETE FROM thread_items WHERE thread_id=? AND item_id=?',[(pair['codex_id'],iid) for iid in plan['item_ids']])
                        h.executemany('DELETE FROM thread_turns WHERE thread_id=? AND turn_id=?',[(pair['codex_id'],tid) for tid in plan['turn_ids']])
                        h.execute('UPDATE thread_history_projection_state SET next_rollout_byte_offset=0,next_rollout_ordinal=? WHERE thread_id=?',(json.loads(new.splitlines()[0])['ordinal'],pair['codex_id']))
                finally:h.close()
                from .sync_tail import read,_CACHE
                from .session_ir.native_reconcile import reconcile
                _CACHE.pop(str(path),None)
                tail=read(path,len(new),_sha(new),paginated=True,ordinal_base=json.loads(new.splitlines()[0])['ordinal'])
                if not reconcile(plan['home'],pair['codex_id'],path,tail):raise ValueError('Native projection repair incomplete; reopen deferred')
                with state:
                    state.execute('UPDATE pairs SET offset=?,prefix=?,error=NULL,repair_required=0,status=? WHERE id=?',
                        (plan['new_cursor'],_sha(new[:plan['new_cursor']]),'collision_repaired',pair['id']))
                    for event in plan['events']:
                        state.execute('DELETE FROM parts WHERE pair_id=? AND part_id=?',(pair['id'],event[6:]))
                        state.execute('UPDATE deliveries SET written_at=NULL,projected_at=NULL WHERE pair_id=? AND event=?',(pair['id'],event))
                    state.execute("UPDATE live_refresh SET status='superseded' WHERE pair_id=? AND refs NOT LIKE '%context-hub-sync:%'",(pair['id'],))
                plan['phase']='complete';plan['native_records_changed']=0;plan['removed_bridge_events']=len(plan['events'])
                atomic(backup/'receipt.json',json.dumps(plan,ensure_ascii=False,indent=2));pending.unlink();reports.append(plan)
        finally:state.close()
    return {'status':'repaired' if reports else 'no_collision','repairs':reports}

if __name__=='__main__':
    import argparse
    p=argparse.ArgumentParser();p.add_argument('--data-dir',type=Path,required=True);args=p.parse_args()
    print(json.dumps(repair(args.data_dir),ensure_ascii=False))
