"""Canonical tool → Codex wire format translation.

See docs/tool-mapping.md §3.
"""
# Adapted from opal-bridge (1va7/opal-bridge, MIT-style personal project by the
# same author). Renamed: canonical->session_ir, ingest/render->restore/emit,
# apply_patch->patch_io, pair_map->mirror_map. Field semantics unchanged.

from __future__ import annotations

import json
import shlex
from typing import Any

from . import patch_io as ap
from .patch_io import FileStateCache, to_relative


def render_tool_call(
    tool: str,
    args: dict[str, Any],
    call_id: str,
    cwd: str,
    *,
    file_state: FileStateCache | None = None,
) -> dict[str, Any]:
    """
    Return the Codex `response_item.payload` dict for a canonical tool call.
    Most map to function_call (exec_command); apply_patch family go to custom_tool_call.
    """
    if tool == "shell":
        cmd = args.get("command", "")
        wd = args.get("workdir", cwd)
        codex_args = {"cmd": cmd, "workdir": wd, "yield_time_ms": 1000}
        # max_output_tokens omitted unless specified — Codex default kicks in
        return {
            "type": "function_call",
            "name": "exec_command",
            "arguments": json.dumps(codex_args, ensure_ascii=False),
            "call_id": call_id,
        }
    if tool == "read_file":
        path = args.get("path", "")
        offset = args.get("offset")
        limit = args.get("limit")
        if offset and limit:
            cmd = f"sed -n '{offset},{offset + limit - 1}p' {shlex.quote(path)}"
        elif offset:
            cmd = f"tail -n +{offset} {shlex.quote(path)}"
        elif limit:
            cmd = f"head -n {limit} {shlex.quote(path)}"
        else:
            cmd = f"cat -n {shlex.quote(path)}"
        return {
            "type": "function_call",
            "name": "exec_command",
            "arguments": json.dumps({"cmd": cmd, "workdir": cwd, "yield_time_ms": 1000}, ensure_ascii=False),
            "call_id": call_id,
        }
    if tool == "find_files":
        pattern = args.get("pattern", "")
        path = args.get("path", ".")
        cmd = f"rg --files -g {shlex.quote(pattern)} {shlex.quote(path)}"
        return {
            "type": "function_call",
            "name": "exec_command",
            "arguments": json.dumps({"cmd": cmd, "workdir": cwd, "yield_time_ms": 5000}, ensure_ascii=False),
            "call_id": call_id,
        }
    if tool == "search_text":
        cmd = _build_rg_command(args)
        return {
            "type": "function_call",
            "name": "exec_command",
            "arguments": json.dumps({"cmd": cmd, "workdir": cwd, "yield_time_ms": 10000}, ensure_ascii=False),
            "call_id": call_id,
        }
    if tool == "web_search":
        # Codex web_search is a Responses-builtin; we approximate by exec_command curl in MVP
        # because we can't actually invoke OpenAI's builtin from a translated session.
        # The historical record just preserves the call as a comment.
        return {
            "type": "function_call",
            "name": "exec_command",
            "arguments": json.dumps(
                {
                    "cmd": f"echo '(translated from CC WebSearch query: {args.get('query','')})'",
                    "workdir": cwd,
                    "yield_time_ms": 1000,
                },
                ensure_ascii=False,
            ),
            "call_id": call_id,
        }
    if tool in {"edit_file", "write_file", "multi_edit_file", "delete_file", "move_file"}:
        return _render_apply_patch(tool, args, call_id, cwd, file_state)
    # Fallback: passthrough as exec_command echo so call_id pairing remains intact
    return {
        "type": "function_call",
        "name": "exec_command",
        "arguments": json.dumps(
            {
                "cmd": f"echo '(translated {tool} call; canonical args: {json.dumps(args, ensure_ascii=False)})'",
                "workdir": cwd,
                "yield_time_ms": 1000,
            },
            ensure_ascii=False,
        ),
        "call_id": call_id,
    }


def render_tool_result(call_id: str, output_text: str, is_error: bool) -> dict[str, Any]:
    """Return the Codex response_item.payload for a tool_result."""
    return {
        "type": "function_call_output",
        "call_id": call_id,
        "output": output_text,
    }


def _build_rg_command(args: dict[str, Any]) -> str:
    parts = ["rg"]
    if args.get("output_mode") == "files_with_matches":
        parts.append("-l")
    elif args.get("output_mode") == "count":
        parts.append("-c")
    if args.get("-i"):
        parts.append("-i")
    if args.get("multiline"):
        parts.extend(["-U", "--multiline-dotall"])
    for f in ("-A", "-B", "-C"):
        if f in args:
            parts.extend([f, str(args[f])])
    if "context" in args:
        parts.extend(["-C", str(args["context"])])
    if "type" in args:
        parts.extend(["--type", str(args["type"])])
    if "glob" in args:
        parts.extend(["-g", shlex.quote(args["glob"])])
    parts.append(shlex.quote(args.get("pattern", "")))
    parts.append(shlex.quote(args.get("path", ".")))
    cmd = " ".join(parts)
    if "head_limit" in args:
        cmd += f" | head -n {int(args['head_limit'])}"
    if "offset" in args:
        cmd += f" | tail -n +{int(args['offset'])}"
    return cmd


def _render_apply_patch(
    tool: str,
    args: dict[str, Any],
    call_id: str,
    cwd: str,
    file_state: FileStateCache | None,
) -> dict[str, Any]:
    """Render Edit/Write/MultiEdit/Delete/Move as a custom_tool_call apply_patch."""
    path = args.get("path", "")
    rel = to_relative(path, cwd) if path else path

    state = file_state.contents if file_state else {}
    file_content = state.get(path)

    if tool == "write_file":
        content = args.get("content", "")
        existed = path in state
        patch = ap.patch_for_write(rel, content, file_existed=existed)
    elif tool == "edit_file":
        patch = ap.patch_for_edit(
            rel,
            args.get("old", ""),
            args.get("new", ""),
            file_content=file_content,
            replace_all=args.get("replace_all", False),
        )
    elif tool == "multi_edit_file":
        patch = ap.patch_for_multi_edit(
            rel,
            args.get("edits", []),
            file_content=file_content,
        )
    elif tool == "delete_file":
        patch = ap.patch_for_delete(rel)
    elif tool == "move_file":
        old_rel = to_relative(args.get("from", path), cwd)
        new_rel = to_relative(args.get("to", path), cwd)
        patch = ap.patch_for_move(old_rel, new_rel)
    else:  # safety
        patch = ap.compose_patch_envelope(f"# unknown tool: {tool}\n")

    return {
        "type": "custom_tool_call",
        "status": "completed",
        "call_id": call_id,
        "name": "apply_patch",
        "input": patch,
    }

# --- Native (Claude Code / ZCode family) tool name -> canonical ---
# Ported from opal-bridge adapters/claude_code/tool_map.py; ZCode shares the
# same wire tool family (Bash/Read/Glob/Grep/Edit/...), so the table applies.

CC_TO_CANONICAL: dict = {
    "Bash": "shell",
    "Read": "read_file",
    "Write": "write_file",
    "Edit": "edit_file",
    "MultiEdit": "multi_edit_file",
    "Glob": "find_files",
    "Grep": "search_text",
    "WebFetch": "web_fetch",
    "WebSearch": "web_search",
    "NotebookEdit": "notebook_edit",
    "TodoWrite": "update_plan",
    "TaskCreate": "update_plan",
    "TaskUpdate": "update_plan",
    "TaskList": "update_plan",
    "TaskGet": "update_plan",
    "TaskOutput": "update_plan",
    "TaskStop": "update_plan",
    "Agent": "subagent_dispatch",
    "Skill": "skill_invoke",
    "AskUserQuestion": "ask_user",
    "EnterWorktree": "worktree_enter",
    "ExitWorktree": "worktree_exit",
    "ScheduleWakeup": "schedule_task",
    "CronCreate": "schedule_task",
    "CronList": "schedule_task",
    "CronDelete": "schedule_task",
}


def translate_args(canonical_name: str, native_input: dict) -> dict:
    """Normalize native input to canonical args (mostly passthrough)."""
    if not isinstance(native_input, dict):
        return {}
    if canonical_name == "shell":
        return {
            "command": native_input.get("command", ""),
            "description": native_input.get("description"),
            "workdir": native_input.get("workdir") or native_input.get("cwd"),
        }
    if canonical_name == "read_file":
        return {
            "path": native_input.get("file_path") or native_input.get("path", ""),
            "offset": native_input.get("offset"),
            "limit": native_input.get("limit"),
        }
    if canonical_name == "edit_file":
        return {
            "path": native_input.get("file_path") or native_input.get("path", ""),
            "old": native_input.get("old_string", ""),
            "new": native_input.get("new_string", ""),
            "replace_all": native_input.get("replace_all", False),
        }
    if canonical_name == "write_file":
        return {
            "path": native_input.get("file_path") or native_input.get("path", ""),
            "content": native_input.get("content", ""),
            "overwrite": True,
        }
    if canonical_name == "find_files":
        return {"pattern": native_input.get("pattern", ""), "path": native_input.get("path")}
    return dict(native_input)
