"""Resolve user-supplied session names against native metadata, never chat bodies."""
import re
import sqlite3
import unicodedata
from pathlib import Path
from .native_source import readonly


def canonical(value):
    return re.sub(r'\s+','',unicodedata.normalize('NFKC',str(value))).casefold()


def _request(query):
    query=query.strip()
    explicit=re.fullmatch(r'(zcode|codex):([^\s:]+)',query,re.I)
    if explicit:return explicit[1].lower(),explicit[2],True
    # Quoted titles may themselves mention another client; only the surrounding
    # request determines which native source was requested.
    quote_pattern=r'《([^》]+)》|“([^”]+)”|「([^」]+)」|"([^"]+)"'
    quoted=re.findall(quote_pattern,query)
    titles=[next(s for s in group if s) for group in quoted]
    if len(titles)>1:raise ValueError('请一次指定一个会话名称。')
    outer=re.sub(quote_pattern,'',query)
    adapters={x.lower() for x in re.findall(r'(?<![A-Za-z])(zcode|codex)(?![A-Za-z])',outer,re.I)}
    if len(adapters)>1:raise ValueError('请明确原会话来自 ZCode 还是 Codex。')
    adapter=next(iter(adapters),None)
    if titles:return adapter,titles[0].strip(),False
    name=re.sub(r'^(?:请|帮我|麻烦|现在|先|一下|\s)*(?:加载|读取|接入|恢复|接续|继续)(?:一下)?\s*','',query)
    if adapter:
        name=re.sub(r'^(?:在|来自)?\s*'+adapter+r'\s*(?:里面|上面|之中|中|里|上)?\s*(?:的)?\s*','',name,flags=re.I)
    name=re.sub(r'\s*(?:的)?(?:会话|对话)?\s*(?:的)?(?:原生上下文|原始上下文|上下文|原生记录)\s*[。！!]?\s*$','',name)
    name=re.sub(r'\s*(?:会话|对话)\s*$','',name).strip(' ：:。')
    if not name:raise ValueError('请指定要加载的原会话名称。')
    return adapter,name,False


def _catalogue(config,adapter):
    home=Path(config['sources'][adapter]).expanduser().resolve()
    path=home/('cli/db/db.sqlite' if adapter=='zcode' else 'state_5.sqlite')
    con=readonly(path)
    try:
        table='session' if adapter=='zcode' else 'threads'
        columns={r['name'] for r in con.execute(f'PRAGMA table_info({table})')}
        if not {'id','title'}<=columns:raise ValueError('原生会话名称索引不可用：'+adapter)
        fields=[k for k in ('id','title','directory','cwd','time_updated','updated_at_ms','updated_at','archived','time_archived') if k in columns]
        for row in con.execute(f'SELECT {",".join(fields)} FROM {table}'):
            row=dict(row)
            stamp=row.get('time_updated') or row.get('updated_at_ms') or row.get('updated_at') or 0
            if stamp and stamp<10**12:stamp*=1000
            title=row.get('title') or ''
            yield {'session':adapter+':'+row['id'],'title':title,'adapter':adapter,
                   'cwd':row.get('directory') or row.get('cwd') or '',
                   'updated_at_ms':stamp,'archived':bool(row.get('archived') or row.get('time_archived')),
                   'generated_copy':bool(re.match(r'^\[(?:from zcode|来自\s*Codex)\]',title,re.I))}
    finally:con.close()


def resolve(config,query,limit=12):
    adapter,name,explicit=_request(query)
    if explicit:
        if adapter not in config.get('sources',{}):raise ValueError('原生来源尚未接入：'+adapter)
        return {'status':'ready','selected':{'session':adapter+':'+name},'match':'explicit_id'}
    sources=[adapter] if adapter else [a for a in ('zcode','codex') if a in config.get('sources',{})]
    exact=[];partial=[];errors=[];needle=canonical(name)
    for source in sources:
        try:
            if source not in config.get('sources',{}):raise ValueError('原生来源尚未接入：'+source)
            for item in _catalogue(config,source):
                title=canonical(item['title'])
                if title==needle:exact.append(item)
                elif needle in title:partial.append(item)
        except (OSError,sqlite3.Error,ValueError) as e:errors.append({'adapter':source,'error':str(e)})
    if len(exact)==1 and not errors:
        return {'status':'ready','selected':exact[0],'match':'exact_native_title','query_title':name}
    matches=exact or partial
    matches.sort(key=lambda i:(-i['updated_at_ms'],i['session']))
    return {'status':'source_unavailable' if errors else 'ambiguous' if len(exact)>1 else 'candidates' if partial else 'not_found',
            'query_title':name,'candidates':matches[:limit],'total_candidates':len(matches),'source_errors':errors,
            'read_only':True,'imported':False,'sync_enabled':False,'untrusted':True,
            'note':'只根据原生会话标题定位；同名或仅部分匹配时需选定来源，不自动选择最新同名会话，也不合并事项。'}
