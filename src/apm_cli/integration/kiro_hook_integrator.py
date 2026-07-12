"""Kiro hook transformation helpers.

Kiro stores each hook as its own JSON document under ``.kiro/hooks/``.
This module keeps the target-specific expansion out of ``hook_integrator.py``
so the shared integrator stays under the source-length guardrail.
"""

from __future__ import annotations

import json
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
from apm_cli.integration.hook_ir import HookBinding, HookHandler
from apm_cli.integration.hook_native_formats import _entries_to_ir
from apm_cli.utils.atomic_io import atomic_write_text
from apm_cli.utils.path_security import ensure_path_within
from apm_cli.utils.paths import portable_relpath

if TYPE_CHECKING:
    from apm_cli.integration.hook_integrator import HookIntegrator

_KIRO_EVENT_MAP = _HOOK_EVENT_MAP["kiro"]


def _safe_hook_slug(value: str, fallback: str = "hook") -> str:
    """Return a stable lowercase slug for generated Kiro hook filenames."""
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip(".-_").lower()
    return safe or fallback


def _kiro_matcher(binding: HookBinding) -> str | None:
    """Render a neutral matcher into Kiro's scalar matcher field."""
    if binding.matcher:
        return binding.matcher
    patterns = binding.metadata.get("patterns")
    if isinstance(patterns, str) and patterns.strip():
        return patterns.strip()
    if isinstance(patterns, (list, tuple)):
        values = [str(item).strip() for item in patterns if str(item).strip()]
        return "|".join(values) if values else None
    return None


def _kiro_action(handler: HookHandler) -> dict | None:
    """Render one neutral handler in Kiro v1 action form."""
    if handler.command:
        action: dict = {"type": "command", "command": handler.command}
    else:
        prompt = handler.metadata.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            return None
        action = {"type": "agent", "prompt": prompt}
    if handler.timeout_seconds is not None:
        action["timeout"] = handler.timeout_seconds
    return action


def _kiro_hook_document(
    *,
    name: str,
    event_name: str,
    matcher: str | None,
    action: dict,
) -> dict:
    """Build one current Kiro v1 hook JSON document."""
    hook = {
        "name": name,
        "trigger": event_name,
        "action": action,
    }
    if matcher:
        hook["matcher"] = matcher
    return {"version": "v1", "hooks": [hook]}


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
    for raw_event_name, entries in hooks.items():
        if not isinstance(entries, list):
            continue
        event_name = _KIRO_EVENT_MAP.get(raw_event_name, raw_event_name)
        event_slug = _safe_hook_slug(event_name)
        document = _entries_to_ir(entries, event_name)
        for binding in document.bindings:
            matcher = _kiro_matcher(binding)
            for handler in binding.handlers:
                action = _kiro_action(handler)
                if action is None:
                    continue
                per_event_counts[event_name] = per_event_counts.get(event_name, 0) + 1
                index = per_event_counts[event_name]
                doc = _kiro_hook_document(
                    name=f"{package_name} {event_name} {index}",
                    event_name=event_name,
                    matcher=matcher,
                    action=action,
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
                        action,
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
        data = integrator._parse_hook_json(hook_file)
        if data is None:
            continue

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
