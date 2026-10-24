"""Session IR types — stdlib dataclasses, no third-party deps.

Field-for-field port of opal-bridge canonical/schema.py (pydantic v2),
replacing BaseModel with dataclasses plus an `extra` dict so unknown
fields survive (lossless-first: even what we cannot interpret is kept).

Semantics reference: opal-bridge docs/ARCHITECTURE.md §3.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, fields, asdict
from typing import Any


def _fmt(value: Any) -> Any:
    if hasattr(value, 'to_dict'):
        return value.to_dict()
    if isinstance(value, list):
        return [_fmt(x) for x in value]
    if isinstance(value, dict):
        return {k: _fmt(v) for k, v in value.items()}
    return value


@dataclass
class _Base:
    """Dataclass base with an opaque `extra` bag and to_dict/from_dict."""

    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        out = {}
        for f in fields(self):
            if f.name == 'extra':
                continue
            out[f.name] = _fmt(getattr(self, f.name))
        for k, v in getattr(self, 'extra', {}).items():
            out.setdefault(k, v)
        return out

    @classmethod
    def _build(cls, data: dict):
        wire_aliases = getattr(cls, '_WIRE_ALIASES', {})
        known = {f.name for f in fields(cls)} - {'extra'}
        kwargs = {}
        extra = {}
        for k, v in dict(data).items():
            local = None
            for lname, wname in wire_aliases.items():
                if k == wname:
                    local = lname
                    break
            if local is None and k in known:
                local = k
            if local is None:
                extra[k] = v
            else:
                kwargs[local] = v
        instance = cls(**kwargs)
        instance.extra = extra
        return instance


def _dataclass(cls):
    return dataclass(cls)


# ---------- Session header ---------- #

@_dataclass
class GitInfo(_Base):
    branch: str | None = None
    commit: str | None = None
    repo_url: str | None = None


@_dataclass
class ModelHint(_Base):
    provider: str | None = None
    name: str | None = None
    reasoning_effort: str | None = None


@_dataclass
class Permissions(_Base):
    approval: str | None = None
    sandbox: str | None = None
    writable_roots: list = field(default_factory=list)
    network_access: bool = False


@_dataclass
class Session(_Base):
    schema_version: str = "1.0.0"
    id: str = ""
    source_harness: str = "other"
    source_session_id: str = ""
    source_session_path: str | None = None
    cwd: str = ""
    git: GitInfo = field(default_factory=GitInfo)
    model_hint: ModelHint = field(default_factory=ModelHint)
    started_at: str = ""
    ended_at: str | None = None
    permissions: Permissions = field(default_factory=Permissions)
    skills_inventory: list = field(default_factory=list)
    mcp_servers: list = field(default_factory=list)
    memory_files: list = field(default_factory=list)
    moments: list = field(default_factory=list)
    subagent_transcripts: dict = field(default_factory=dict)
    artifacts: list = field(default_factory=list)
    source_metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        out = super().to_dict()
        out['moments'] = [m.to_dict() for m in self.moments]
        out['subagent_transcripts'] = {k: [m.to_dict() for m in v]
                                       for k, v in self.subagent_transcripts.items()}
        return out


# ---------- Moments (tagged by `kind`) ---------- #

@_dataclass
class _MomentBase(_Base):
    ts: str = ""
    source_ref: dict | None = None
    agent_scope: str = "main"
    lossy: bool = False
    lossy_reason: str | None = None


@_dataclass
class UserText(_MomentBase):
    kind: str = "user_text"
    text: str = ""
    prompt_id: str | None = None
    attachments: list = field(default_factory=list)


@_dataclass
class AssistantText(_MomentBase):
    kind: str = "assistant_text"
    text: str = ""
    phase: str | None = None


@_dataclass
class Thinking(_MomentBase):
    kind: str = "thinking"
    text: str = ""
    format: str = "plaintext"
    lossy: bool = True
    lossy_reason: str = "harness-specific signature/encrypted_content not portable"


@_dataclass
class ToolCall(_MomentBase):
    kind: str = "tool_call"
    tool: str = ""
    call_id: str = ""
    args: dict = field(default_factory=dict)
    wire_native: dict | None = None


@_dataclass
class ToolResult(_MomentBase):
    kind: str = "tool_result"
    call_id: str = ""
    output_text: str = ""
    output_blocks: list = field(default_factory=list)
    is_error: bool = False
    exit_code: int | None = None
    duration_ms: int | None = None
    external_ref: str | None = None


@_dataclass
class Attachment(_MomentBase):
    kind: str = "attachment"
    subtype: str = ""
    data: dict = field(default_factory=dict)


@_dataclass
class ModeChange(_MomentBase):
    kind: str = "mode_change"
    from_mode: str | None = None
    to_mode: str = ""
    fields_changed: dict = field(default_factory=dict)
    _WIRE_ALIASES = {'from_mode': 'from', 'to_mode': 'to'}


@_dataclass
class PlanUpdate(_MomentBase):
    kind: str = "plan_update"
    items: list = field(default_factory=list)
    diff: dict | None = None


@_dataclass
class Error(_MomentBase):
    kind: str = "error"
    message: str = ""
    subtype: str | None = None
    retry_info: dict | None = None


@_dataclass
class Notification(_MomentBase):
    kind: str = "notification"
    subtype: str = ""
    content: str = ""
    ref: str | None = None


@_dataclass
class SummaryCompaction(_MomentBase):
    kind: str = "summary_compaction"
    trigger: str = "auto"
    before_tokens: int | None = None
    after_tokens: int | None = None
    summary_text: str = ""
    lossy: bool = True
    lossy_reason: str = "pre-compaction history not preserved across harnesses"


@_dataclass
class Metadata(_MomentBase):
    kind: str = "metadata"
    subtype: str = ""
    data: dict = field(default_factory=dict)


MOMENT_CLASSES = {
    c().kind: c for c in (UserText, AssistantText, Thinking, ToolCall, ToolResult,
                          Attachment, ModeChange, PlanUpdate, Error, Notification,
                          SummaryCompaction, Metadata)
}


def moment_from_dict(data: dict):
    """Build the right moment subclass from a tagged dict."""
    cls = MOMENT_CLASSES.get(data.get('kind'))
    if cls is None:
        raise ValueError('Unknown session_ir moment kind: ' + str(data.get('kind')))
    return cls._build(data)


Moment = tuple(MOMENT_CLASSES.values())  # runtime union of all moment classes
