"""Kiro hook transformation helpers.

Kiro stores each hook as its own JSON document under ``.kiro/hooks/``.
This module keeps the target-specific expansion out of ``hook_integrator.py``
so the shared integrator stays under the source-length guardrail.
"""

from __future__ import annotations

import json
import logging
import os
import re
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


def _with_kiro_hook_fields(converted: dict, source: dict) -> dict:
    """Carry hook-level Kiro fields beside an internal action."""
    timeout = source.get("timeout", source.get("timeoutSec"))
    if isinstance(timeout, (int, float)) and not isinstance(timeout, bool):
        converted["_kiro_timeout"] = timeout
    for field in ("description", "enabled"):
        if field in source:
            converted[f"_kiro_{field}"] = source[field]
    # Native v1 normalization has already namespaced these hook-level fields.
    for field in ("_kiro_description", "_kiro_timeout", "_kiro_enabled"):
        if field in source:
            converted[field] = source[field]
    return converted


def _kiro_action_from_action(action: dict, command_keys: tuple[str, ...]) -> dict | None:
    """Convert one APM hook action to a Kiro v1 action."""
    prompt = action.get("prompt")
    if action.get("type") in {"agent", "askAgent"} or isinstance(prompt, str):
        prompt_text = prompt if isinstance(prompt, str) else action.get("command")
        if isinstance(prompt_text, str) and prompt_text.strip():
            return _with_kiro_hook_fields(
                {"type": "agent", "prompt": prompt_text},
                action,
            )
        return None

    for key in command_keys:
        command = action.get(key)
        if isinstance(command, str) and command.strip():
            converted = {"type": "command", "command": command}
            return _with_kiro_hook_fields(converted, action)
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
    event_name: str,
    matcher: str | None,
    action: dict,
) -> dict:
    """Build one Kiro v1 hook JSON document."""
    action = dict(action)
    hook = {
        "name": name,
        "trigger": event_name,
        "action": action,
    }
    for field in ("description", "timeout", "enabled"):
        value = action.pop(f"_kiro_{field}", None)
        if value is not None:
            hook[field] = value
    if matcher:
        hook["matcher"] = matcher
    return {"version": "v1", "hooks": [hook]}


def _normalize_kiro_v1(data: dict) -> dict:
    """Convert a native Kiro v1 document to the internal event-map shape."""
    events: dict[str, list[dict]] = {}
    for hook in data.get("hooks", []):
        if not isinstance(hook, dict):
            continue
        trigger = hook.get("trigger")
        action = hook.get("action")
        if not isinstance(trigger, str) or not isinstance(action, dict):
            continue
        native_action = dict(action)
        if isinstance(hook.get("name"), str):
            native_action["_kiro_name"] = hook["name"]
        if isinstance(hook.get("description"), str):
            native_action["_kiro_description"] = hook["description"]
        timeout = hook.get("timeout")
        if isinstance(timeout, (int, float)) and not isinstance(timeout, bool):
            native_action["_kiro_timeout"] = timeout
        if isinstance(hook.get("enabled"), bool):
            native_action["_kiro_enabled"] = hook["enabled"]
        matcher: dict[str, object] = {"hooks": [native_action]}
        if isinstance(hook.get("matcher"), str):
            matcher["matcher"] = hook["matcher"]
        canonical_trigger = _KIRO_EVENT_MAP.get(trigger, trigger)
        events.setdefault(canonical_trigger, []).append(matcher)
    return {"hooks": events}


def _write_kiro_hook_docs(
    integrator: HookIntegrator,
    hook_file: Path,
    rewritten: dict,
    hooks_dir: Path,
    project_root: Path,
    package_name: str,
    force: bool,
    managed_files: set | None,
    diagnostics,
    target_paths: list[Path],
    display_payloads: list,
) -> tuple[int, int, int]:
    """Write Kiro hook docs from one source hook file."""
    files_integrated = 0
    files_skipped = 0
    files_adopted = 0
    hooks = rewritten.get("hooks", {})
    _emit_hook_event_diagnostics(list(hooks.keys()), "kiro", _KIRO_EVENT_MAP)
    per_event_counts: dict[str, int] = {}
    for raw_event_name, matchers in hooks.items():
        if not isinstance(matchers, list):
            continue
        event_name = _KIRO_EVENT_MAP.get(raw_event_name, raw_event_name)
        event_slug = _safe_hook_slug(event_name)
        for matcher in matchers:
            if not isinstance(matcher, dict):
                continue
            kiro_matcher = _kiro_matcher_from_matcher(matcher)
            for action in _kiro_actions_from_matcher(matcher, integrator.HOOK_COMMAND_KEYS):
                kiro_action = _kiro_action_from_action(action, integrator.HOOK_COMMAND_KEYS)
                if kiro_action is None:
                    _log.debug(
                        "Skipping unsupported Kiro hook action from %s",
                        hook_file.name,
                    )
                    continue
                per_event_counts[event_name] = per_event_counts.get(event_name, 0) + 1
                index = per_event_counts[event_name]
                doc = _kiro_hook_document(
                    name=action.get("_kiro_name", f"{package_name} {event_name} {index}"),
                    event_name=event_name,
                    matcher=kiro_matcher,
                    action=kiro_action,
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
                        event_name,
                        kiro_action,
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
        if isinstance(data.get("hooks"), list):
            _log.debug(
                "Normalizing %d native Kiro v1 hook(s) from %s",
                len(data["hooks"]),
                hook_file.name,
            )
            data = _normalize_kiro_v1(data)

        rewritten, scripts = integrator._rewrite_hooks_data(
            data,
            package_info.install_path,
            package_name,
            "kiro",
            hook_file_dir=hook_file.parent,
            root_dir=root_dir,
            deploy_root=deploy_root_for_rewrite,
        )
        written, skipped, adopted = _write_kiro_hook_docs(
            integrator,
            hook_file,
            rewritten,
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
                "Kiro hook file %s contributed no supported command or agent actions",
                hook_file,
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
