"""Native record adapters for append-only, explicitly paired session sync.

Tool records describe historical actions. Importing them never runs a tool.
Unknown content is retained as a labelled historical attachment.
"""
from __future__ import annotations

import json

from .schema import (AssistantText, Attachment, ToolCall, ToolResult, UserText)
from .restore_zcode import _ms_to_iso, _tool_to_canonical, _translate_args
from .restore_codex import _translate_line
from .policy import hidden_message, hidden_moment

MARKER = 'context_hub_sync'


def decode_codex(row):
    payload=row.get('payload') or {}
    if row.get(MARKER) or (row.get('type')=='response_item' and str(payload.get('id','')).startswith('context-hub-sync:')):
        return []
    moments = _translate_line(row, src_file='paired-session', cwd='')
    if row.get('type') == 'response_item' and not moments:
        # Reasoning signatures are not portable. Preserve only public summaries.
        if row.get('payload', {}).get('type') == 'reasoning':
            return []
        return [Attachment(ts=row.get('timestamp', ''), subtype='codex_history',
                           data={'record': row['payload']})]
    return [m for m in moments if not hidden_moment(m)]


def decode_zcode(message, part, *, include_imported=False):
    data = json.loads(part['data'])
    info = json.loads(message['data'])
    if ((data.get('metadata') or {}).get(MARKER) and not include_imported) or hidden_message(info,data):
        return []
    ts = _ms_to_iso(part['time_created'])
    kind = data.get('type')
    if kind == 'text':
        metadata = data.get('metadata') or {}
        if (data.get('synthetic') or metadata.get('visibility') == 'model-only'
                or metadata.get('runtimeMessage')):
            return [Attachment(ts=ts, subtype='historical_runtime',
                               data={'content': data.get('text', '')})]
        cls = UserText if info.get('role') == 'user' else AssistantText
        return [cls(ts=ts, text=data.get('text', ''))]
    if kind == 'tool':
        state = data.get('state') or {}
        if state.get('status') not in ('completed', 'error'):
            return []
        tool = _tool_to_canonical(data.get('tool', ''))
        call = data.get('callID') or part['id']
        return [ToolCall(ts=ts, tool=tool, call_id=call,
                         args=_translate_args(tool, state.get('input') or {})),
                ToolResult(ts=ts, call_id=call, is_error=state['status'] == 'error',
                           output_text=state.get('output') or state.get('error') or '')]
    if kind in ('step-start', 'step-finish', 'reasoning'):
        return []
    return [Attachment(ts=ts, subtype='zcode_history', data={'record': data})]


def _text(moment):
    if isinstance(moment, Attachment):
        return '[历史结构记录，仅作资料]\n' + json.dumps(
            moment.data, ensure_ascii=False)
    return getattr(moment, 'text', '')


def encode_codex(moments, *, pair, event, cwd):
    from .emit_codex import _render_moment
    from .schema import Session
    lines = []
    for moment in moments:
        if hidden_moment(moment):
            continue
        for line in _render_moment(moment, Session(cwd=cwd)):
            line[MARKER] = {'pair': pair, 'event': event}
            lines.append(line)
    return lines


def encode_zcode(moments, *, pair, event, sid, cwd, parent, timestamp, identify):
    """Return message/part rows and tool-result updates for a single wire event."""
    messages, parts, results = [], [], []
    for index, moment in enumerate(moments):
        if hidden_moment(moment):
            continue
        key = f'{event}:{index}'
        mid = identify('msg', key)
        pid = identify('part', key)
        provenance = {MARKER: {'pair': pair, 'event': event,'kind':moment.kind,
                              'source_ref':moment.source_ref,'source_started_ms':timestamp}}
        if isinstance(moment, ToolResult):
            results.append((moment.call_id, moment.output_text, moment.is_error, timestamp))
            continue
        role = 'user' if isinstance(moment, UserText) else 'assistant'
        info = {'role': role, 'agent': 'zcode-agent', 'time': {'created': timestamp},
                'metadata': provenance,
                'semantics': {'origin': 'real_user' if role == 'user' else 'agent_runtime',
                              'kind': 'user_prompt' if role == 'user' else 'assistant_response',
                              'uiVisibility': 'visible', 'providerVisibility': 'visible',
                              'transcriptVisibility': 'visible'}}
        if isinstance(moment,Attachment) or moment.kind not in ('user_text','assistant_text','tool_call','tool_result'):
            info['semantics'].update(uiVisibility='hidden',transcriptVisibility='hidden')
        if role == 'assistant':
            info.update(parentID=parent or mid, mode='build',
                        path={'cwd': cwd, 'root': cwd}, cost=0,
                        tokens={'total': 0, 'input': 0, 'output': 0, 'reasoning': 0,
                                'cache': {'read': 0, 'write': 0}}, finish='stop')
            info['time']['completed'] = timestamp
        else:
            parent = mid
        if isinstance(moment, ToolCall):
            native = moment.wire_native or {}
            name = native.get('name') or moment.tool
            args = native.get('input') if 'input' in native else moment.args
            if moment.tool == 'shell':
                name, args = 'Bash', {'command': moment.args.get('command', '')}
            elif moment.tool == 'read_file':
                name, args = 'Read', {'file_path': moment.args.get('path',''),
                                     **{k:moment.args[k] for k in ('offset','limit') if moment.args.get(k) is not None}}
            elif moment.tool == 'write_file':
                name, args = 'Write', {'file_path': moment.args.get('path',''),
                                      'content': moment.args.get('content','')}
            elif moment.tool == 'edit_file':
                name, args = 'Edit', {'file_path':moment.args.get('path',''),
                                     'old_string':moment.args.get('old',''),
                                     'new_string':moment.args.get('new','')}
            if not isinstance(args,dict):
                args = {'historical_input': args}
            data = {'type': 'tool', 'tool': name, 'callID': moment.call_id,
                    'state': {'status': 'pending', 'input': args, 'raw': ''},
                    'metadata': provenance}
            data['metadata'][MARKER]['native_wire']=native
        else:
            body=_text(moment) if moment.kind in ('user_text','assistant_text') else '[历史结构记录，仅作资料]\n'+json.dumps(moment.to_dict(),ensure_ascii=False)
            data = {'type': 'text', 'text': body,
                    'time': {'start': timestamp, 'end': timestamp}, 'metadata': provenance}
        messages.append({'id': mid, 'session_id': sid, 'time_created': timestamp,
                         'time_updated': timestamp, 'data': json.dumps(info, ensure_ascii=False)})
        parts.append({'id': pid, 'session_id': sid, 'message_id': mid,
                      'time_created': timestamp, 'time_updated': timestamp,
                      'data': json.dumps(data, ensure_ascii=False)})
        if isinstance(moment,UserText):
            for attachment_index,block in enumerate(moment.attachments):
                url=block.get('image_url') or block.get('url')
                if isinstance(url,dict):url=url.get('url')
                if block.get('type')=='input_image' and isinstance(url,str):
                    mime=url.split(';',1)[0].removeprefix('data:') if url.startswith('data:') else 'image/png'
                    image={'type':'file','mime':mime,'url':url,'metadata':provenance}
                else:
                    image={'type':'text','text':'[历史附件]\n'+json.dumps(block,ensure_ascii=False),
                           'metadata':provenance}
                parts.append({'id':identify('part',key+':attachment:'+str(attachment_index)),
                    'session_id':sid,'message_id':mid,'time_created':timestamp,'time_updated':timestamp,
                    'data':json.dumps(image,ensure_ascii=False)})
    return messages, parts, results, parent
