"""Read-only operational health. Never opens a model or migrates a database."""
import json
import time
from pathlib import Path
from . import __version__


def inspect(root, app=None, now=None):
    root=Path(root).expanduser().resolve()
    now=time.time() if now is None else now
    def read(path):
        try:
            value=json.loads(path.read_text(encoding='utf-8'))
            return value if isinstance(value,dict) else {}
        except (OSError,ValueError):return {}
    state=read(root/'background-status.json')
    state.setdefault('status','not_started')
    config=read(root/'config.json')
    deployment=read(Path(app or Path(__file__).resolve().parent.parent)/'deployment.json')
    warnings=state.get('sync',{}).get('warnings',[])
    sources={name:{'enrolled':True,'available':Path(path).expanduser().is_dir()}
             for name,path in config.get('sources',{}).items()}
    last=state.get('last_success')
    age=max(0,round(now-last,1)) if isinstance(last,(int,float)) else None
    checked=state.get('last_checked',last)
    checked_age=max(0,round(now-checked,1)) if isinstance(checked,(int,float)) else None
    alarms=state.get('alarms',[])
    stale=checked_age is None or checked_age>420
    health=('error' if state.get('status')=='error' or alarms or any(not s['available'] for s in sources.values()) else
            'stale' if stale else 'attention' if warnings else 'ok')
    return {**state,'health':health,'code_version':__version__,
            'deployed_version':deployment.get('version'),
            'deployment_commit':deployment.get('commit'),
            'last_success_age_seconds':age,'stale':stale,
            'last_checked_age_seconds':checked_age,
            'archive_format':'segmented-v3' if (root/'state.sqlite').exists() else 'legacy',
            'sources':sources,
            'warnings_count':len(warnings)+state.get('sync',{}).get('warnings_omitted',0),
            'missing_log_warnings':sum('indexed log missing' in w for w in warnings),
            'warning_examples':warnings[:3],
            'capture':'local_source_archive','semantic_summary':'agent_assisted',
            'chatgpt_full_transcript_auto_archive':False,
            'handoff_verified':False,
            'note':'归档状态不代表另一端已读取；交接需核对来源和恢复结果。'}
