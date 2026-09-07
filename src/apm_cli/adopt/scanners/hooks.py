"""Scanner for hook configuration files and the scripts they reference.

Merged-config targets (Claude, Cursor, Codex, Gemini, Windsurf, Antigravity)
keep every hook in one JSON document that users also edit by hand. APM marks
the entries it wrote with ``_apm_source`` (kept in a sidecar for schema-strict
targets), so ownership is decided *per entry*: the finding carries only the
host-authored slice and notes how many APM entries were skipped.
"""

from __future__ import annotations

import copy
import json
import re
import shlex
from collections.abc import Iterable
from pathlib import Path, PurePosixPath
from typing import Any

from apm_cli.hook_contract import walk_hook_commands
from apm_cli.integration.hook_integrator import _APM_HOOKS_SIDECAR, _MERGE_HOOK_TARGETS
from apm_cli.integration.hook_ownership import reinject_apm_source_from_sidecar
from apm_cli.integration.targets import KNOWN_TARGETS
from apm_cli.utils.path_security import PathTraversalError, ensure_path_within

from ..model import HarnessKind, Ownership, RawFinding, Risk, Scope
from ..registry import ScanContext, probe_text

_PROJECT_DIR_VARS = re.compile(
    r"""^(?:"?\$\{?CLAUDE_PROJECT_DIR\}?"?|\$env:CLAUDE_PROJECT_DIR|\$\{?workspaceFolder\}?)[\\/]"""
)
_EXECUTES = frozenset({Risk.EXECUTES_CODE})


def _load_json(path: Path, ctx: ScanContext) -> Any | None:
    if probe_text(path) is None:
        ctx.error(path, "binary or unreadable")
        return None
    try:
        if path.stat().st_size > ctx.limits.max_file_bytes:
            ctx.error(path, "oversize; not parsed")
            return None
        return json.loads(path.read_text(encoding="utf-8").removeprefix("\ufeff"))
    except (OSError, ValueError) as exc:
        ctx.error(path, f"invalid JSON: {type(exc).__name__}")
        return None


def split_hook_entries(hooks: dict[str, Any]) -> tuple[dict[str, list], int]:
    """Return (host-authored event map, count of APM-marked entries dropped)."""
    host: dict[str, list] = {}
    apm_count = 0
    for event, entries in hooks.items():
        if not isinstance(entries, list):
            continue
        kept: list = []
        for entry in entries:
            if isinstance(entry, dict) and "_apm_source" in entry:
                apm_count += 1
                continue
            kept.append(entry)
        if kept:
            host[event] = kept
    return host, apm_count


def _normalize_script_ref(token: str) -> str:
    return _PROJECT_DIR_VARS.sub("", token.strip().strip('"').strip("'"))


def script_findings(
    ctx: ScanContext,
    tool: str,
    hooks_doc: dict[str, Any],
    source_display: str,
    seen: set[str],
) -> Iterable[RawFinding]:
    """Yield a HOOK_SCRIPT finding for every in-root file a hook command runs."""
    for declaration in walk_hook_commands(hooks_doc):
        try:
            argv = shlex.split(declaration.command, posix=True)
        except ValueError:
            continue
        resolved: Path | None = None
        for token in argv:
            head = _normalize_script_ref(token)
            if not head or "$" in head or head.startswith("-"):
                continue
            candidate = Path(head)
            target = candidate if candidate.is_absolute() else ctx.root / candidate
            target = target.resolve(strict=False)
            try:
                ensure_path_within(target, ctx.root)
            except PathTraversalError:
                continue
            if target.is_file():
                resolved = target
                break
        if resolved is None:
            continue
        key = resolved.as_posix()
        if key in seen:
            continue
        seen.add(key)
        yield RawFinding(
            tool=tool,
            scope=ctx.scope,
            kind=HarnessKind.HOOK_SCRIPT,
            display_path=ctx.display(resolved),
            abs_path=resolved,
            size_bytes=resolved.stat().st_size,
            risk=_EXECUTES,
            notes=(f"referenced by {source_display}",),
            evidence=(f"hook-command:{PurePosixPath(source_display).name}",),
        )


class HooksScanner:
    """Read merged hook configs and enumerate referenced scripts."""

    name = "hooks"

    def scan(self, ctx: ScanContext) -> Iterable[RawFinding]:
        seen_scripts: set[str] = set()
        for tool, config in _MERGE_HOOK_TARGETS.items():
            profile = KNOWN_TARGETS.get(tool)
            if profile is None:
                continue
            scoped = profile.for_scope(user_scope=ctx.scope is Scope.USER)
            if scoped is None:
                continue
            config_path = ctx.root / scoped.root_dir / config.config_filename
            if not config_path.is_file():
                continue
            data = _load_json(config_path, ctx)
            if not isinstance(data, dict):
                continue
            display = ctx.display(config_path)
            if config.event_container_key == "hooks":
                containers = {"hooks": data.get("hooks")}
            else:
                containers = {
                    name: value
                    for name, value in data.items()
                    if name != config.event_container_key and isinstance(value, dict)
                }
            sidecar_path = config_path.parent / _APM_HOOKS_SIDECAR
            sidecar = _load_json(sidecar_path, ctx) if sidecar_path.is_file() else None
            host_events: dict[str, list] = {}
            apm_total = 0
            for container_name, hooks in containers.items():
                if not isinstance(hooks, dict) or not hooks:
                    continue
                working = copy.deepcopy(hooks)
                if isinstance(sidecar, dict) and container_name == "hooks":
                    reinject_apm_source_from_sidecar(working, sidecar)
                host, apm_count = split_hook_entries(working)
                apm_total += apm_count
                for event, entries in host.items():
                    key = event if container_name == "hooks" else f"{container_name}:{event}"
                    host_events.setdefault(key, []).extend(entries)
            if not host_events and apm_total == 0:
                continue
            host_count = sum(len(v) for v in host_events.values())
            notes = [f"host entries: {host_count}; apm-owned entries skipped: {apm_total}"]
            ownership = Ownership.HOST_OWNED if host_events else Ownership.APM_OWNED
            yield RawFinding(
                tool=tool,
                scope=ctx.scope,
                kind=HarnessKind.HOOK,
                display_path=display,
                abs_path=config_path,
                size_bytes=config_path.stat().st_size,
                format_id=f"{tool}_hooks",
                primitive="hooks",
                risk=_EXECUTES,
                notes=tuple(notes),
                evidence=("hooks:per-entry-ownership",),
                ownership=ownership,
                payload={"tool": tool, "hooks": host_events},
            )
            if host_events:
                yield from script_findings(ctx, tool, {"hooks": host_events}, display, seen_scripts)
