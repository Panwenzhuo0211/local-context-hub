"""Codex wire records -> session IR.

Adapted from 1va7/opal-bridge adapters/codex/ingest.py. The mapping is
historical only: no tool is executed while importing it.
"""
from __future__ import annotations
import json
import logging
import re
from pathlib import Path
from typing import Any
from .ids import iso_now_z, uuid7_str
from .schema import (
    AssistantText,
    Attachment,
    Error,
    ModeChange,
    Moment,
    Notification,
    PlanUpdate,
    Session,
    SummaryCompaction,
    Thinking,
    ToolCall,
    ToolResult,
    UserText,
)

from .patch_io import FileOp, parse_apply_patch, ApplyPatchParseError


logger = logging.getLogger(__name__)

# Codex top-level types we don't translate to moments (metadata only)
_SKIP_TYPES = {"session_meta", "turn_context"}

# event_msg payload types that are duplicates of response_item — we drop them
_EVENT_MSG_DUPLICATES = {
    "user_message",       # mirror of response_item.message role=user
    "agent_message",      # mirror of response_item.message role=assistant
    "exec_command_end",   # mirror of function_call_output
    "patch_apply_end",    # mirror of custom_tool_call_output
    "web_search_end",     # mirror of web_search_call output
    "task_started", "task_complete", "turn_started", "turn_complete",
    "token_count",
    "thread_name_updated", "thread_rolled_back", "turn_aborted",
    "view_image_tool_call",
    "context_compacted",  # already covered by RolloutItem.compacted
}


def restore_rollout(path, *, include_sync_echo=True):
    """Translate a whole Codex rollout jsonl into ordered session IR moments.

    Sync echo rows (context_hub_sync markers) are often the only copy of
    ZCode-originated messages inside a mirror, so by default they are
    imported with the marker stripped. session_meta/turn_context are skipped.
    """
    path = Path(path)
    with path.open('rb') as f:
        meta = json.loads(f.readline())['payload']
    moments = []
    with path.open('r', encoding='utf-8') as f:
        for raw in f:
            if not raw.strip():
                continue
            row = json.loads(raw)
            echoed = row.pop('context_hub_sync', None)
            if echoed is not None and not include_sync_echo:
                continue
            moments.extend(_translate_line(row, src_file=path.name, cwd=meta.get('cwd', '')))
    return meta, moments


def _translate_line(line: dict[str, Any], src_file: str, cwd: str) -> list[Moment]:
    line_type = line.get("type")
    if line_type in _SKIP_TYPES:
        return []
    ts = line.get("timestamp") or ""
    payload = line.get("payload", {}) or {}
    src_ref = {"file": src_file}

    if line_type == "compacted":
        return _translate_compacted(payload, ts, src_ref)

    if line_type == "event_msg":
        sub = payload.get("type")
        if sub in _EVENT_MSG_DUPLICATES:
            return []
        if sub == "error":
            return [Error(ts=ts, source_ref=src_ref, message=payload.get("message", ""))]
        return []  # unknown event_msg; drop

    if line_type == "response_item":
        return _translate_response_item(payload, ts, src_ref, cwd)

    return []


def _translate_response_item(payload: dict[str, Any], ts: str, src_ref: dict, cwd: str) -> list[Moment]:
    rt = payload.get("type")
    if rt == "message":
        return _translate_message(payload, ts, src_ref)
    if rt == "agent_message":
        return [Attachment(ts=ts, source_ref=src_ref, subtype="subagent_message",
                           data={"record": payload})]
    if rt == "reasoning":
        return _translate_reasoning(payload, ts, src_ref)
    if rt == "function_call":
        return _translate_function_call(payload, ts, src_ref, cwd)
    if rt == "function_call_output":
        return [
            ToolResult(
                ts=ts,
                source_ref=src_ref,
                call_id=payload.get("call_id", ""),
                output_text=_normalize_output(payload.get("output", "")),
                extra={"native_record":payload},
            )
        ]
    if rt == "custom_tool_call":
        return _translate_custom_tool_call(payload, ts, src_ref, cwd)
    if rt == "custom_tool_call_output":
        return [
            ToolResult(
                ts=ts,
                source_ref=src_ref,
                call_id=payload.get("call_id", ""),
                output_text=_normalize_output(payload.get("output", "")),
                extra={"native_record":payload},
            )
        ]
    if rt == "web_search_call":
        action = payload.get("action", {}) or {}
        if action.get("type") == "search":
            return [
                ToolCall(
                    ts=ts, source_ref=src_ref,
                    tool="web_search", call_id=payload.get("call_id") or f"ws_{ts}",
                    args={"query": action.get("query", "")},
                    wire_native={"harness": "codex", "name": "web_search", "input": payload},
                )
            ]
        # open_page / find_in_page → web_fetch fallback
        return [
            ToolCall(
                ts=ts, source_ref=src_ref,
                tool="web_fetch", call_id=payload.get("call_id") or f"wf_{ts}",
                args={"url": action.get("url", "")},
                wire_native={"harness": "codex", "name": "web_search", "input": payload},
            )
        ]
    if rt == "image_generation_call":
        return [Attachment(ts=ts, source_ref=src_ref, subtype="native_record", data={"record": payload})]
    if rt in {"compaction", "context_compaction", "compaction_summary"}:
        return [
            SummaryCompaction(
                ts=ts, source_ref=src_ref,
                summary_text="(codex remote compaction; encrypted_content not portable)",
            )
        ]
    return [Attachment(ts=ts, source_ref=src_ref, subtype="native_record", data={"record": payload})]


def _translate_message(payload: dict[str, Any], ts: str, src_ref: dict) -> list[Moment]:
    role = payload.get("role", "user")
    content = payload.get("content", []) or []
    if isinstance(content,str): content=[{"type":"input_text","text":content}]
    text_parts: list[str] = []
    attachments=[]
    for c in content:
        ctype = c.get("type")
        if ctype in ("input_text", "output_text"):
            text_parts.append(c.get("text", ""))
        else:
            attachments.append(c)
    text = "\n".join(text_parts)
    if role == "user":
        return [UserText(ts=ts, source_ref=src_ref, text=text, attachments=attachments)]
    if role == "assistant":
        phase = payload.get("phase")
        return [AssistantText(ts=ts, source_ref=src_ref, text=text, phase=phase)]
    if role == "developer":
        # treat as Attachment subtype="developer_note" (renderable as system-reminder)
        return [Attachment(ts=ts, source_ref=src_ref, subtype="developer_note", data={"text": text})]
    if role == "system":
        return [Attachment(ts=ts, source_ref=src_ref, subtype="system_note", data={"text": text})]
    return []


def _translate_reasoning(payload: dict[str, Any], ts: str, src_ref: dict) -> list[Moment]:
    summary = payload.get("summary") or []
    parts = [s.get("text", "") for s in summary if isinstance(s, dict)]
    text = "\n".join(p for p in parts if p)
    if not text:
        # Empty reasoning — drop silently
        return []
    return [Thinking(ts=ts, source_ref=src_ref, text=text, format="summary")]


def _translate_function_call(payload: dict[str, Any], ts: str, src_ref: dict, cwd: str) -> list[Moment]:
    name = payload.get("name", "")
    call_id = payload.get("call_id", "")
    raw_args = payload.get("arguments", "{}")
    namespace = payload.get("namespace")
    try:
        args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
    except json.JSONDecodeError:
        args = {"_raw": raw_args}

    canonical, mapped_args = _codex_fn_to_canonical(name, args, cwd, namespace)
    return [
        ToolCall(
            ts=ts, source_ref=src_ref,
            tool=canonical, call_id=call_id,
            args=mapped_args,
            wire_native={"harness": "codex", "name": name, "namespace": namespace, "input": args},
        )
    ]


def _codex_fn_to_canonical(name: str, args: dict, cwd: str, namespace: str | None) -> tuple[str, dict]:
    if namespace:
        return "mcp_call", {"server": namespace, "tool": name, "args": args}
    if name in {"exec_command", "shell", "shell_command", "local_shell"}:
        cmd = args.get("cmd") or args.get("command") or ""
        if isinstance(cmd, list):
            cmd = " ".join(cmd)
        # Try to recognize the canonical tool from the command pattern
        canonical, mapped = _classify_shell_cmd(cmd, args, cwd)
        return canonical, mapped
    if name == "write_stdin":
        return "shell", {"command": f"echo {args.get('chars','')!r} | (existing pty stdin)", "_codex_pty": True}
    if name == "update_plan":
        plan = args.get("plan", [])
        return "update_plan", {"items": [
            {"title": p.get("step", ""), "status": p.get("status", "pending")} for p in plan
        ]}
    if name == "view_image":
        return "view_image", {"path": args.get("path", "")}
    if name in {"spawn_agent", "spawn_agent_v2"}:
        return "subagent_dispatch", {
            "agent_type": args.get("agent_type", "general-purpose"),
            "task": args.get("task_name", ""),
            "prompt": args.get("message", ""),
        }
    if name == "request_user_input":
        return "ask_user", args
    # Unknown native tools remain historical calls, never generated shell text.
    return "native_tool", args


_RG_FILES_RE = re.compile(r"^rg\s+--files(\s|$)")
_RG_RE = re.compile(r"^rg\s+")
_CAT_RE = re.compile(r"^cat\s+(-n\s+)?")
_HEAD_RE = re.compile(r"^head\s+-n\s+(\d+)\s+(.+)$")
_TAIL_RE = re.compile(r"^tail\s+-n\s+\+?(\d+)\s+(.+)$")
_SED_RE = re.compile(r"^sed\s+-n\s+'(\d+),(\d+)p'\s+(.+)$")


def _classify_shell_cmd(cmd: str, args: dict, cwd: str) -> tuple[str, dict]:
    """Recognize idiomatic shells we generate during CC→Codex (round-trip)."""
    cmd_s = cmd.strip()
    if _RG_FILES_RE.match(cmd_s):
        m = re.search(r"-g\s+(\S+)", cmd_s)
        return "find_files", {"pattern": _unquote(m.group(1)) if m else "*", "path": "."}
    if _RG_RE.match(cmd_s):
        # leave as full shell (Grep arg recovery is fragile); keep canonical=shell
        pass
    if _SED_RE.match(cmd_s):
        m = _SED_RE.match(cmd_s)
        a = int(m.group(1)); b = int(m.group(2)); path = _unquote(m.group(3))
        return "read_file", {"path": path, "offset": a, "limit": b - a + 1}
    if _HEAD_RE.match(cmd_s):
        m = _HEAD_RE.match(cmd_s)
        return "read_file", {"path": _unquote(m.group(2)), "limit": int(m.group(1))}
    if _TAIL_RE.match(cmd_s):
        m = _TAIL_RE.match(cmd_s)
        return "read_file", {"path": _unquote(m.group(2)), "offset": int(m.group(1))}
    if _CAT_RE.match(cmd_s):
        rest = cmd_s[len(re.match(_CAT_RE, cmd_s).group(0)):].strip()
        return "read_file", {"path": _unquote(rest)}
    return "shell", {
        "command": cmd,
        "workdir": args.get("workdir", cwd),
        **({"timeout_ms": args["yield_time_ms"]} if args.get("yield_time_ms") else {}),
    }


def _unquote(s: str) -> str:
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        return s[1:-1]
    return s


def _translate_custom_tool_call(payload: dict[str, Any], ts: str, src_ref: dict, cwd: str) -> list[Moment]:
    """custom_tool_call is almost always apply_patch in current Codex versions."""
    name = payload.get("name", "")
    call_id = payload.get("call_id", "")
    # Native custom tools (including the JS exec orchestrator) have arbitrary
    # raw input. Preserve one call and its original result identity; do not
    # manufacture shell commands or split a single native call into fake calls.
    return [ToolCall(ts=ts, source_ref=src_ref, tool="native_tool", call_id=call_id,
                     args={"input": payload.get("input", "")},
                     wire_native={"harness":"codex","name":name,
                                  "input":payload.get("input", ""),"record":payload})]


def _abspath(rel_or_abs: str, cwd: str) -> str:
    if rel_or_abs.startswith("/"):
        return rel_or_abs
    return f"{cwd.rstrip('/')}/{rel_or_abs}"


def _translate_compacted(payload: dict[str, Any], ts: str, src_ref: dict) -> list[Moment]:
    msg = payload.get("message", "")
    replacement=payload.get("replacement_history")
    return [SummaryCompaction(ts=ts, source_ref=src_ref, summary_text=msg,
                              extra={"replacement_history":replacement,"native_record":payload})]


def _normalize_output(out: Any) -> str:
    if isinstance(out, str):
        return out
    if isinstance(out, list):
        # FunctionCallOutputBody::ContentItems form
        parts = []
        for item in out:
            if isinstance(item, dict):
                parts.append(item.get("text") or json.dumps(item))
            else:
                parts.append(str(item))
        return "\n".join(parts)
    return json.dumps(out, ensure_ascii=False)
