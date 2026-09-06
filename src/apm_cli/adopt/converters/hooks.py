"""Native hook configs -> ``.apm/hooks/<tool>-native.json`` (neutral grammar).

Only host-authored entries reach this converter (the hooks scanner already
dropped ``_apm_source``-marked ones). Scripts are referenced, never executed;
``--include-hook-scripts`` copies in-project scripts next to the hook file.
"""

from __future__ import annotations

import json
import re
import shlex
from collections.abc import Callable
from pathlib import Path
from typing import Any

from apm_cli.hook_contract import HookDocument
from apm_cli.integration.hook_integrator import _HOOK_EVENT_MAP
from apm_cli.integration.hook_native_formats import (
    _document_to_entries,
    _from_antigravity_hook_entries,
    _from_claude_hook_entries,
    _from_copilot_hook_file,
    _from_cursor_hook_entries,
    _from_gemini_hook_entries,
    _from_kiro_hook_docs,
    _from_windsurf_hook_entries,
)
from apm_cli.security.gate import SecurityGate
from apm_cli.utils.atomic_io import write_text_lf
from apm_cli.utils.path_security import PathTraversalError, ensure_path_within

from ..model import Finding
from . import ConvertContext, ConvertError, ConvertResult
from .base import read_text, refuse_credentials

CANONICAL_EVENTS: frozenset[str] = frozenset(
    {
        "PreToolUse",
        "PostToolUse",
        "UserPromptSubmit",
        "SessionStart",
        "Stop",
        "PreTaskExecution",
        "PostTaskExecution",
    }
)
_PROJECT_DIR_VARS = re.compile(
    r"""^(?:"?\$\{?CLAUDE_PROJECT_DIR\}?"?|\$env:CLAUDE_PROJECT_DIR|\$\{?workspaceFolder\}?)[\\/]"""
)
_MERGED_READERS: dict[str, Callable[[list, str], HookDocument]] = {
    "claude": _from_claude_hook_entries,
    "codex": _from_claude_hook_entries,
    "cursor": _from_cursor_hook_entries,
    "windsurf": _from_windsurf_hook_entries,
    "gemini": _from_gemini_hook_entries,
    "antigravity": _from_antigravity_hook_entries,
}
_MAX_SCRIPT_BYTES = 1_048_576


def event_portability(event: str) -> tuple[list[str], list[str]]:
    """Return (targets that render *event* natively, targets that pass it through)."""
    native: list[str] = []
    passthrough: list[str] = []
    for target, mapping in _HOOK_EVENT_MAP.items():
        if event in mapping or (target == "claude" and event in CANONICAL_EVENTS):
            native.append(target)
        else:
            passthrough.append(target)
    for target in ("cursor", "codex", "windsurf", "antigravity"):
        (native if event in CANONICAL_EVENTS else passthrough).append(target)
    return sorted(native), sorted(passthrough)


def _rewrite_script_tokens(
    command: str,
    finding: Finding,
    dest_dir: Path,
    ctx: ConvertContext,
    result: ConvertResult,
    event: str,
) -> str:
    """Normalise project-dir variables; optionally copy in-project scripts."""
    try:
        argv = shlex.split(command, posix=True)
    except ValueError:
        return command
    changed = False
    for index, token in enumerate(argv):
        stripped = _PROJECT_DIR_VARS.sub("", token)
        if stripped != token:
            argv[index] = f"./{stripped}" if not stripped.startswith(("./", "/")) else stripped
            changed = True
            result.transform(
                f"hooks.{event}.command", "project-dir variable rewritten to a relative path"
            )
        candidate = ctx.project_root / argv[index]
        if ctx.include_hook_scripts and not Path(argv[index]).is_absolute() and candidate.is_file():
            try:
                ensure_path_within(
                    candidate.resolve(strict=False), ctx.project_root.resolve(strict=False)
                )
            except PathTraversalError:
                continue
            if candidate.is_symlink() or candidate.stat().st_size > _MAX_SCRIPT_BYTES:
                result.drop(f"hooks.{event}.script", "script skipped: symlink or oversize")
                continue
            text = candidate.read_bytes()
            if b"\x00" not in text:
                verdict = SecurityGate.scan_text(
                    text.decode("utf-8", errors="ignore"), candidate.name
                )
                if verdict.should_block:
                    raise ConvertError("hook script blocked by the security scan")
            scripts_dir = dest_dir / "scripts"
            scripts_dir.mkdir(parents=True, exist_ok=True)
            target = scripts_dir / candidate.name
            if not target.exists():
                import shutil

                shutil.copy2(candidate, target)
                result.written.append(target)
            argv[index] = f"./scripts/{candidate.name}"
            changed = True
            result.transform(f"hooks.{event}.command", "script copied into .apm/hooks/scripts/")
        elif (
            not Path(argv[index]).is_absolute()
            and argv[index].startswith("./")
            and not candidate.exists()
        ):
            result.transform(
                f"hooks.{event}.command", "referenced script not found in project", "warning"
            )
        elif Path(argv[index]).is_absolute() or argv[index].startswith("~"):
            if index == 0 or argv[index].endswith((".sh", ".py", ".ps1", ".js")):
                result.transform(
                    f"hooks.{event}.command", "machine-local script path is not portable", "warning"
                )
    return shlex.join(argv) if changed else command


def _load_json(path: Path, limit: int) -> Any:
    try:
        return json.loads(read_text(path, limit))
    except ValueError as exc:
        raise ConvertError(f"invalid JSON: {type(exc).__name__}") from exc


class HooksConverter:
    """Merged hook configs and per-file hook documents -> neutral hook files."""

    id = "hooks->apm_hooks"

    def handles(self, converter_id: str) -> bool:
        return converter_id.endswith("->apm_hooks")

    def convert(self, finding: Finding, dest: Path, *, ctx: ConvertContext) -> ConvertResult:
        result = ConvertResult()
        tool = finding.tool
        documents: dict[str, dict[str, HookDocument]] = {}
        if isinstance(finding.payload, dict) and "hooks" in finding.payload:
            reader = _MERGED_READERS.get(tool, _from_claude_hook_entries)
            for key, entries in finding.payload["hooks"].items():
                container, _, event = key.rpartition(":") if ":" in key else ("", "", key)
                documents.setdefault(container or "", {})[event] = reader(entries, event)
        elif finding.abs_path is not None:
            payload = _load_json(finding.abs_path, ctx.limits.max_file_bytes)
            if tool == "kiro":
                docs = _from_kiro_hook_docs(payload)
                if not docs:
                    raise ConvertError("legacy Kiro hook shape is not importable")
            else:
                docs = _from_copilot_hook_file(payload)
            documents[""] = docs
        else:
            raise ConvertError("no hook data")
        if not any(documents.values()):
            raise ConvertError("no host-authored hook entries")

        for container, per_event in documents.items():
            hooks_out: dict[str, list] = {}
            for event, document in sorted(per_event.items()):
                entries = _document_to_entries(document)
                for entry in entries:
                    if not isinstance(entry, dict):
                        continue
                    for handler in entry.get("hooks", []):
                        if isinstance(handler, dict) and handler.get("command"):
                            handler["command"] = _rewrite_script_tokens(
                                str(handler["command"]), finding, dest.parent, ctx, result, event
                            )
                        if isinstance(handler, dict) and handler.get("type") not in (
                            None,
                            "command",
                        ):
                            result.transform(
                                f"hooks.{event}.type",
                                f"'{handler.get('type')}' handlers only fire on {tool}",
                                "warning",
                            )
                if entries:
                    hooks_out[event] = entries
                native, passthrough = event_portability(event)
                if passthrough:
                    where = (
                        f"native on {', '.join(native)}" if native else "not native on any target"
                    )
                    result.transform(
                        f"hooks.{event}",
                        f"{where}; passed through unmapped on {', '.join(passthrough)}",
                        "info" if event in CANONICAL_EVENTS else "warning",
                    )
            if not hooks_out:
                continue
            target = dest if not container else dest.with_name(f"{tool}-{container}-native.json")
            rendered = json.dumps({"hooks": hooks_out}, indent=2) + "\n"
            refuse_credentials(rendered)
            target.parent.mkdir(parents=True, exist_ok=True)
            write_text_lf(target, rendered)
            result.written.append(target)
        if not result.written:
            raise ConvertError("no hook entries produced")
        return result


CONVERTERS = (HooksConverter(),)
