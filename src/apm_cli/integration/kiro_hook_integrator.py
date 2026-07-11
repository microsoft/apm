"""Kiro hook transformation helpers.

Kiro stores each hook as its own JSON document under ``.kiro/hooks/``.
This module keeps the target-specific expansion out of ``hook_integrator.py``
so the shared integrator stays under the source-length guardrail.

Design: the Kiro target consumes APM's declared hook input *directly* into a
Kiro-native result. Native Kiro v1 documents are read and re-emitted without
round-tripping through any foreign (Claude-shaped) event map, and no internal
side-channel keys are used to smuggle Kiro fields across a normalization step.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from apm_cli.integration.hook_bundle import copy_deployed_hook_bundle
from apm_cli.integration.hook_integrator import (
    _HOOK_EVENT_MAP,
    HookIntegrationResult,
    _emit_hook_event_diagnostics,
    _filter_hook_files_for_target,
)
from apm_cli.utils.atomic_io import atomic_write_text
from apm_cli.utils.path_security import ensure_path_within
from apm_cli.utils.paths import portable_relpath

if TYPE_CHECKING:
    from apm_cli.integration.hook_integrator import HookIntegrator

_KIRO_EVENT_MAP = _HOOK_EVENT_MAP["kiro"]
_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class _ResolvedKiroHook:
    """One fully-resolved Kiro v1 hook, ready to serialize.

    Every field already holds its final Kiro-native value (commands are
    path-rewritten, triggers canonicalized). There is no side channel: what
    is stored here is exactly what is written to disk. Frozen because a
    resolver builds it once and the writer only ever reads it.
    """

    trigger: str
    action: dict
    name: str | None = None
    matcher: str | None = None
    description: str | None = None
    timeout: int | float | None = None
    enabled: bool | None = None


def _safe_hook_slug(value: str, fallback: str = "hook") -> str:
    """Return a stable lowercase slug for generated Kiro hook filenames."""
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip(".-_").lower()
    return safe or fallback


def _kiro_matcher_from_matcher(matcher: dict) -> str | None:
    """Extract a Kiro v1 matcher from an APM hook matcher, if present."""
    patterns = matcher.get("patterns")
    if isinstance(patterns, str) and patterns.strip():
        return patterns.strip()
    if isinstance(patterns, list):
        values = [str(item).strip() for item in patterns if str(item).strip()]
        return "|".join(values) or None
    matcher_value = matcher.get("matcher")
    if isinstance(matcher_value, str) and matcher_value.strip():
        return matcher_value.strip()
    return None


def _numeric_timeout(value: object) -> int | float | None:
    """Return a numeric timeout, or None when absent/non-numeric."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    return None


def _kiro_action_from_action(action: dict, command_keys: tuple[str, ...]) -> dict | None:
    """Convert one APM hook action to a Kiro v1 action.

    Pure mapping: it produces the Kiro-native ``{"type": ...}`` action and
    never mutates or annotates the input. Command path rewriting is the
    caller's responsibility (it happens before this call for native input and
    inside ``_rewrite_hooks_data`` for portable input).
    """
    prompt = action.get("prompt")
    if action.get("type") in {"agent", "askAgent"} or isinstance(prompt, str):
        prompt_text = prompt if isinstance(prompt, str) else action.get("command")
        if isinstance(prompt_text, str) and prompt_text.strip():
            return {"type": "agent", "prompt": prompt_text}
        return None

    for key in command_keys:
        command = action.get(key)
        if isinstance(command, str) and command.strip():
            return {"type": "command", "command": command}
    return None


def _kiro_actions_from_matcher(matcher: dict, command_keys: tuple[str, ...]) -> list[dict]:
    """Return flat action dicts from both Copilot-flat and Claude-nested shapes."""
    actions: list[dict] = []
    if any(isinstance(matcher.get(key), str) for key in command_keys):
        actions.append(matcher)
    if isinstance(matcher.get("prompt"), str):
        actions.append(matcher)
    nested_hooks = matcher.get("hooks", [])
    if isinstance(nested_hooks, list):
        actions.extend(hook for hook in nested_hooks if isinstance(hook, dict))
    return actions


def _kiro_hook_document(
    *,
    name: str,
    trigger: str,
    action: dict,
    matcher: str | None = None,
    description: str | None = None,
    timeout: int | float | None = None,
    enabled: bool | None = None,
) -> dict:
    """Build one Kiro v1 hook JSON document from explicit native fields."""
    hook: dict[str, object] = {"name": name, "trigger": trigger}
    if description is not None:
        hook["description"] = description
    if matcher:
        hook["matcher"] = matcher
    if timeout is not None:
        hook["timeout"] = timeout
    if enabled is not None:
        hook["enabled"] = enabled
    hook["action"] = dict(action)
    return {"version": "v1", "hooks": [hook]}


def _resolve_native_v1_hooks(
    integrator: HookIntegrator,
    hooks: list,
    *,
    package_path: Path,
    package_name: str,
    root_dir: str | None,
    deploy_root: Path | None,
    hook_file_dir: Path,
) -> tuple[list[_ResolvedKiroHook], list[tuple[Path, str]]]:
    """Resolve native Kiro v1 hooks directly into Kiro-native results."""
    resolved: list[_ResolvedKiroHook] = []
    scripts: list[tuple[Path, str]] = []
    for raw in hooks:
        if not isinstance(raw, dict):
            continue
        trigger = raw.get("trigger")
        action = raw.get("action")
        if not isinstance(trigger, str) or not isinstance(action, dict):
            continue
        kiro_action = _kiro_action_from_action(action, integrator.HOOK_COMMAND_KEYS)
        if kiro_action is None:
            continue
        if kiro_action["type"] == "command":
            new_cmd, act_scripts = integrator._rewrite_command_for_target(
                kiro_action["command"],
                package_path,
                package_name,
                "kiro",
                hook_file_dir=hook_file_dir,
                root_dir=root_dir,
                deploy_root=deploy_root,
            )
            kiro_action = {"type": "command", "command": new_cmd}
            scripts.extend(act_scripts)
        resolved.append(
            _ResolvedKiroHook(
                # Native triggers pass through unchanged when unmapped. This is
                # safe: _safe_hook_slug sanitizes the value for the filename,
                # ensure_path_within bounds the output path, and the trigger is
                # data-only in the emitted JSON (no execution semantics).
                trigger=_KIRO_EVENT_MAP.get(trigger, trigger),
                action=kiro_action,
                name=raw.get("name") if isinstance(raw.get("name"), str) else None,
                matcher=raw.get("matcher") if isinstance(raw.get("matcher"), str) else None,
                description=(
                    raw.get("description") if isinstance(raw.get("description"), str) else None
                ),
                timeout=_numeric_timeout(raw.get("timeout")),
                enabled=raw.get("enabled") if isinstance(raw.get("enabled"), bool) else None,
            )
        )
    return resolved, _dedup_scripts(scripts)


def _dedup_scripts(scripts: list[tuple[Path, str]]) -> list[tuple[Path, str]]:
    """Drop duplicate (source, target) script pairs, preserving order."""
    seen: set[tuple[str, str]] = set()
    unique: list[tuple[Path, str]] = []
    for source, target_rel in scripts:
        key = (str(source), target_rel)
        if key in seen:
            continue
        seen.add(key)
        unique.append((source, target_rel))
    return unique


def _resolve_portable_hooks(
    integrator: HookIntegrator,
    rewritten: dict,
) -> list[_ResolvedKiroHook]:
    """Resolve APM's portable (path-rewritten) hook map into Kiro-native results."""
    resolved: list[_ResolvedKiroHook] = []
    hooks = rewritten.get("hooks", {})
    _emit_hook_event_diagnostics(list(hooks.keys()), "kiro", _KIRO_EVENT_MAP)
    for raw_event_name, matchers in hooks.items():
        if not isinstance(matchers, list):
            continue
        trigger = _KIRO_EVENT_MAP.get(raw_event_name, raw_event_name)
        for matcher in matchers:
            if not isinstance(matcher, dict):
                continue
            kiro_matcher = _kiro_matcher_from_matcher(matcher)
            for action in _kiro_actions_from_matcher(matcher, integrator.HOOK_COMMAND_KEYS):
                kiro_action = _kiro_action_from_action(action, integrator.HOOK_COMMAND_KEYS)
                if kiro_action is None:
                    continue
                resolved.append(
                    _ResolvedKiroHook(
                        trigger=trigger,
                        action=kiro_action,
                        matcher=kiro_matcher,
                        timeout=_numeric_timeout(action.get("timeout", action.get("timeoutSec"))),
                    )
                )
    return resolved


def _write_resolved_hooks(
    integrator: HookIntegrator,
    resolved: list[_ResolvedKiroHook],
    hook_file: Path,
    hooks_dir: Path,
    project_root: Path,
    package_name: str,
    force: bool,
    managed_files: set | None,
    diagnostics,
    target_paths: list[Path],
    display_payloads: list,
) -> tuple[int, int, int]:
    """Serialize resolved Kiro hooks to one v1 document per hook."""
    files_integrated = 0
    files_skipped = 0
    files_adopted = 0
    per_event_counts: dict[str, int] = {}
    for hook in resolved:
        per_event_counts[hook.trigger] = per_event_counts.get(hook.trigger, 0) + 1
        index = per_event_counts[hook.trigger]
        event_slug = _safe_hook_slug(hook.trigger)
        doc = _kiro_hook_document(
            name=hook.name or f"{package_name} {hook.trigger} {index}",
            trigger=hook.trigger,
            action=hook.action,
            matcher=hook.matcher,
            description=hook.description,
            timeout=hook.timeout,
            enabled=hook.enabled,
        )
        target_filename = (
            f"{_safe_hook_slug(package_name)}-{_safe_hook_slug(hook_file.stem)}-"
            f"{event_slug}-{index}.json"
        )
        target_path = hooks_dir / target_filename
        ensure_path_within(target_path, hooks_dir)
        rel_path = portable_relpath(target_path, project_root)
        rendered = json.dumps(doc, indent=2) + "\n"

        if target_path.exists() and target_path.read_text(encoding="utf-8") == rendered:
            os.chmod(target_path, 0o600)
            files_adopted += 1
            target_paths.append(target_path)
            continue
        if integrator.check_collision(
            target_path,
            rel_path,
            managed_files,
            force,
            diagnostics=diagnostics,
        ):
            files_skipped += 1
            continue

        atomic_write_text(target_path, rendered, new_file_mode=0o600)
        # Keep existing hook files private after updates too.
        os.chmod(target_path, 0o600)
        files_integrated += 1
        target_paths.append(target_path)
        display_payloads.append(
            _display_payload(
                integrator,
                target_filename,
                hook_file,
                hook.trigger,
                hook.action,
                rendered,
            )
        )
    return files_integrated, files_skipped, files_adopted


def _display_payload(
    integrator: HookIntegrator,
    target_filename: str,
    hook_file: Path,
    event_name: str,
    action: dict,
    rendered: str,
) -> dict:
    """Build install-log metadata for one generated Kiro hook file."""
    summary = (
        integrator._summarize_command({"command": action.get("command", "")})
        if action.get("type") == "command"
        else "asks agent"
    )
    return {
        "target_label": ".kiro/hooks/",
        "output_path": target_filename,
        "source_hook_file": hook_file.name,
        "actions": [{"event": event_name, "summary": summary}],
        "rendered_json": rendered.rstrip("\n"),
    }


def _copy_scripts(
    integrator: HookIntegrator,
    scripts,
    package_path: Path,
    hook_file_dir: Path,
    project_root: Path,
    managed_files,
    force: bool,
    diagnostics,
    target_paths: list[Path],
    hook_descriptor_files: set[Path],
) -> tuple[int, int]:
    """Copy Kiro hook scripts and return copied/adopted counts."""
    copy_result = copy_deployed_hook_bundle(
        integrator,
        package_path=package_path,
        hook_file_dir=hook_file_dir,
        project_root=project_root,
        scripts=scripts,
        managed_files=managed_files,
        force=force,
        diagnostics=diagnostics,
        target_paths=target_paths,
        hook_descriptor_files=hook_descriptor_files,
    )
    return copy_result.scripts_copied, copy_result.files_adopted


def _resolve_hooks_for_file(
    integrator: HookIntegrator,
    data: dict,
    *,
    package_path: Path,
    package_name: str,
    root_dir: str | None,
    deploy_root: Path | None,
    hook_file_dir: Path,
    hook_file_name: str,
) -> tuple[list[_ResolvedKiroHook], list[tuple[Path, str]]]:
    """Resolve one parsed hook file into Kiro-native hooks plus scripts to copy."""
    if isinstance(data.get("hooks"), list):
        _log.debug(
            "Consuming %d native Kiro v1 hook(s) from %s",
            len(data["hooks"]),
            hook_file_name,
        )
        return _resolve_native_v1_hooks(
            integrator,
            data["hooks"],
            package_path=package_path,
            package_name=package_name,
            root_dir=root_dir,
            deploy_root=deploy_root,
            hook_file_dir=hook_file_dir,
        )

    rewritten, scripts = integrator._rewrite_hooks_data(
        data,
        package_path,
        package_name,
        "kiro",
        hook_file_dir=hook_file_dir,
        root_dir=root_dir,
        deploy_root=deploy_root,
    )
    return _resolve_portable_hooks(integrator, rewritten), scripts


def integrate_kiro_hooks(
    integrator: HookIntegrator,
    package_info,
    project_root: Path,
    *,
    force: bool = False,
    managed_files: set | None = None,
    diagnostics=None,
    target=None,
    user_scope: bool = False,
    dep_targets_active: bool = False,
) -> HookIntegrationResult:
    """Integrate hooks as one Kiro JSON file per hook action."""
    root_dir = target.root_dir if target else ".kiro"
    target_dir = project_root / root_dir
    if not target_dir.exists():
        return HookIntegrationResult(0, 0, 0, [])

    hook_files = integrator.find_hook_files(package_info.install_path)
    package_name = integrator._get_package_name(package_info, project_root)
    if not dep_targets_active:
        hook_files = _filter_hook_files_for_target(
            hook_files,
            "kiro",
            package_name=package_name,
            warned_packages=integrator._deprecated_hook_routing_warnings,
            package_identity=package_info.get_canonical_dependency_string(),
        )
    if not hook_files:
        return HookIntegrationResult(0, 0, 0, [])

    hooks_dir = target_dir / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    deploy_root_for_rewrite = project_root if user_scope else None

    files_integrated = 0
    files_skipped = 0
    files_adopted = 0
    scripts_copied = 0
    scripts_adopted = 0
    target_paths: list[Path] = []
    display_payloads: list = []

    for hook_file in hook_files:
        data = integrator._parse_hook_json(hook_file, allow_kiro_v1=True)
        if data is None:
            continue

        resolved, scripts = _resolve_hooks_for_file(
            integrator,
            data,
            package_path=package_info.install_path,
            package_name=package_name,
            root_dir=root_dir,
            deploy_root=deploy_root_for_rewrite,
            hook_file_dir=hook_file.parent,
            hook_file_name=hook_file.name,
        )
        written, skipped, adopted = _write_resolved_hooks(
            integrator,
            resolved,
            hook_file,
            hooks_dir,
            project_root,
            package_name,
            force,
            managed_files,
            diagnostics,
            target_paths,
            display_payloads,
        )
        files_integrated += written
        files_skipped += skipped
        files_adopted += adopted
        if written + skipped + adopted == 0:
            _log.warning(
                "Kiro hook file %s contributed no supported command or agent "
                'actions (supported: type "command" or type "agent")',
                hook_file.name,
            )
        copied, adopted_scripts = _copy_scripts(
            integrator,
            scripts,
            package_info.install_path,
            hook_file.parent,
            project_root,
            managed_files,
            force,
            diagnostics,
            target_paths,
            set(hook_files),
        )
        scripts_copied += copied
        scripts_adopted += adopted_scripts

    return HookIntegrationResult(
        files_integrated=files_integrated,
        files_updated=0,
        files_skipped=files_skipped,
        target_paths=target_paths,
        scripts_copied=scripts_copied,
        files_adopted=files_adopted + scripts_adopted,
        display_payloads=display_payloads,
    )
