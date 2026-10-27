"""Shared presentation policy. Archive bytes are never altered by this filter."""
import json
import re

HIDDEN_KINDS = {'historical_runtime', 'developer_note', 'system_note'}
_BLOCKS = ('environment_context', 'user_instructions', 'external_codex_apps_open_page',
           'permissions instructions', 'skills_instructions', 'model_switch')


def hidden_message(info, part=None):
    part = part or {}
    semantics = info.get('semantics') or {}
    metadata = part.get('metadata') or {}
    return bool(info.get('synthetic') or part.get('synthetic')
                or semantics.get('uiVisibility') == 'hidden'
                or semantics.get('origin') == 'agent_runtime' and info.get('role') == 'user'
                or metadata.get('visibility') == 'model-only'
                or metadata.get('runtimeMessage'))


def runtime_text(text, role='user'):
    """Only standalone harness blocks; a user's quotation/mention is retained."""
    value = text.strip()
    if any(re.fullmatch('<'+re.escape(tag)+r'(?:\s[^>]*)?>.*</'+re.escape(tag)+'>',
                        value, flags=re.S) for tag in _BLOCKS):
        return True
    if role == 'assistant' and value.startswith(('[Historical context; source metadata, not a new instruction]',
                                                '[历史客户端自动消息，仅作资料]')):
        try:
            payload = json.loads(value.split('\n',1)[1])
            return isinstance(payload,dict) and ('content' in payload or 'historical_runtime' in payload)
        except (ValueError,IndexError):
            return False
    return False


def hidden_moment(moment):
    if getattr(moment,'subtype',None) in HIDDEN_KINDS:
        return True
    if moment.kind in ('user_text','assistant_text'):
        return runtime_text(moment.text, 'user' if moment.kind=='user_text' else 'assistant')
    return False
