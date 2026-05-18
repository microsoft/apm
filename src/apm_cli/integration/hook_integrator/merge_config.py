"""Hook integration functionality for APM packages.

Integrates hook JSON files and their referenced scripts during package
installation. Supports VSCode Copilot (.github/hooks/), Claude Code
(.claude/settings.json), and Cursor (.cursor/hooks.json) targets.

Hook JSON format (Claude Code  -- nested matcher groups):
    {
        "hooks": {
            "PreToolUse": [
                {
                    "hooks": [
                        {"type": "command", "command": "./scripts/validate.sh", "timeout": 10}
                    ]
                }
            ]
        }
    }

Hook JSON format (GitHub Copilot  -- flat arrays with bash/powershell keys):
    {
        "version": 1,
        "hooks": {
            "preToolUse": [
                {"type": "command", "bash": "./scripts/validate.sh", "timeoutSec": 10}
            ]
        }
    }

Hook JSON format (Cursor  -- flat arrays with command key):
    {
        "hooks": {
            "afterFileEdit": [
                {"command": "./hooks/format.sh"}
            ]
        }
    }

Script path handling:
    - ${CLAUDE_PLUGIN_ROOT}/path, ${CURSOR_PLUGIN_ROOT}/path, ${PLUGIN_ROOT}/path
      -> resolved relative to package root, rewritten for target
    - ./path -> relative path, resolved from hook file's parent directory, rewritten for target
    - System commands (no path separators) -> passed through unchanged
"""

import json
import logging
import shutil
from pathlib import Path

from apm_cli.utils.path_security import ensure_path_within
from apm_cli.utils.paths import portable_relpath

from ._hook_config import (
    _copy_hook_scripts,
    _empty_hook_result,
    _load_hook_json_config,
    _merge_hook_events,
    _rewrite_hook_file,
)
from ._opts import HookIntegrateOpts, HookRewriteOpts
from ._sidecar import _reinject_apm_source_from_sidecar
from .class_ import (
    HookIntegrationResult,
    _filter_hook_files_for_target,
    _MergeHookConfig,
    _to_gemini_hook_entries,
)

_APM_HOOKS_SIDECAR = "apm-hooks.json"
_log = logging.getLogger(__name__)

_MERGE_HOOK_TARGETS: dict[str, _MergeHookConfig] = {
    "claude": _MergeHookConfig(
        config_filename="settings.json",
        target_key="claude",
        require_dir=False,
        schema_strict=True,
    ),
    "cursor": _MergeHookConfig(
        config_filename="hooks.json",
        target_key="cursor",
        require_dir=True,
    ),
    "codex": _MergeHookConfig(
        config_filename="hooks.json",
        target_key="codex",
        require_dir=True,
    ),
    "gemini": _MergeHookConfig(
        config_filename="settings.json",
        target_key="gemini",
        require_dir=True,
    ),
    "windsurf": _MergeHookConfig(
        config_filename="hooks.json",
        target_key="windsurf",
        require_dir=True,
    ),
}
_HOOK_FILE_TARGET_SUFFIXES: dict[str, set[str]] = {
    "copilot-hooks": {"copilot", "vscode"},
    "cursor-hooks": {"cursor"},
    "claude-hooks": {"claude"},
    "codex-hooks": {"codex"},
    "gemini-hooks": {"gemini"},
    "windsurf-hooks": {"windsurf"},
}


def _integrate_merged_hooks(
    self,
    config: "_MergeHookConfig",
    package_info,
    project_root: Path,
    opts: HookIntegrateOpts | None = None,
) -> HookIntegrationResult:
    """Integrate hooks by merging into a target-specific JSON config.

    This is the shared implementation for Claude, Cursor, and Codex
    targets that merge hook entries into a single JSON file (as
    opposed to Copilot which uses individual JSON files).
    """
    resolved_opts = opts or HookIntegrateOpts()
    target = resolved_opts.target
    root_dir = target.root_dir if target else f".{config.target_key}"
    target_dir = project_root / root_dir

    if config.require_dir and not target_dir.exists():
        return _empty_hook_result()

    hook_files = self.find_hook_files(package_info.install_path)
    hook_files = _filter_hook_files_for_target(hook_files, config.target_key)
    if not hook_files:
        return _empty_hook_result()

    package_name = self._get_package_name(package_info)
    hooks_integrated = 0
    scripts_copied = 0
    scripts_adopted = 0
    target_paths: list[Path] = []
    cleared_events: set = set()
    json_path = target_dir / config.config_filename
    json_config = _load_hook_json_config(json_path)

    # Load sidecar ownership metadata (schema-strict targets)
    sidecar_path = target_dir / _APM_HOOKS_SIDECAR
    sidecar_data: dict = {}
    if config.schema_strict and sidecar_path.exists():
        try:
            with open(sidecar_path, encoding="utf-8") as f:
                _raw = json.load(f)
            if isinstance(_raw, dict):
                sidecar_data = _raw
            else:
                _log.warning(
                    "Sidecar file %s contains non-dict JSON; treating as empty.",
                    sidecar_path,
                )
                sidecar_data = {}
        except (json.JSONDecodeError, OSError) as exc:
            _log.warning("Failed to read sidecar %s: %s; treating as empty.", sidecar_path, exc)
            sidecar_data = {}

        if sidecar_data and "hooks" in json_config:
            _reinject_apm_source_from_sidecar(json_config["hooks"], sidecar_data)

    for hook_file in hook_files:
        rewritten_bundle = _rewrite_hook_file(
            self,
            package_info,
            hook_file,
            root_dir,
            config.target_key,
            deploy_root=project_root,
        )
        if rewritten_bundle is None:
            continue

        rewritten, scripts = rewritten_bundle
        _merge_hook_events(
            json_config,
            rewritten.get("hooks", {}),
            config.target_key,
            package_name,
            cleared_events,
        )
        hooks_integrated += 1

        copied_paths, copied_count, adopted_count = _copy_hook_scripts(
            self,
            scripts,
            project_root,
            resolved_opts,
        )
        target_paths.extend(copied_paths)
        scripts_copied += copied_count
        scripts_adopted += adopted_count

    json_path.parent.mkdir(parents=True, exist_ok=True)

    if config.schema_strict:
        # Build sidecar from entries that have _apm_source
        sidecar_out: dict = {}
        for event_name, entries_list in json_config.get("hooks", {}).items():
            if not isinstance(entries_list, list):
                continue
            owned = [e for e in entries_list if isinstance(e, dict) and "_apm_source" in e]
            if owned:
                sidecar_out[event_name] = [dict(e) for e in owned]

        # Strip _apm_source from entries before writing to disk
        for entries_list in json_config.get("hooks", {}).values():
            if isinstance(entries_list, list):
                for entry in entries_list:
                    if isinstance(entry, dict):
                        entry.pop("_apm_source", None)

        # Write sidecar
        sidecar_path = target_dir / _APM_HOOKS_SIDECAR
        if sidecar_out:
            try:
                with open(sidecar_path, "w", encoding="utf-8") as f:
                    json.dump(sidecar_out, f, indent=2)
                    f.write("\n")
            except OSError as exc:
                _log.warning("Failed to write sidecar %s: %s", sidecar_path, exc)
        elif sidecar_path.exists():
            sidecar_path.unlink()

    with open(json_path, "w", encoding="utf-8") as handle:
        json.dump(json_config, handle, indent=2)
        handle.write("\n")

    return HookIntegrationResult(
        files_integrated=hooks_integrated,
        files_updated=0,
        files_skipped=0,
        target_paths=target_paths,
        scripts_copied=scripts_copied,
        files_adopted=scripts_adopted,
    )


def integrate_package_hooks(
    self,
    package_info,
    project_root: Path,
    opts: HookIntegrateOpts | None = None,
) -> HookIntegrationResult:
    """Integrate hooks from a package into hooks dir (Copilot target).

    Deploys hook JSON files with clean filenames and copies referenced
    script files. Skips user-authored files unless force=True.

    Args:
        package_info: PackageInfo with package metadata and install path
        project_root: Root directory of the project
        force: If True, overwrite user-authored files on collision
        managed_files: Set of relative paths known to be APM-managed
        target: Optional TargetProfile for scope-resolved root_dir

    Returns:
        HookIntegrationResult: Results of the integration operation
    """
    hook_files = self.find_hook_files(package_info.install_path)
    hook_files = _filter_hook_files_for_target(hook_files, "copilot")

    if not hook_files:
        return HookIntegrationResult(
            files_integrated=0,
            files_updated=0,
            files_skipped=0,
            target_paths=[],
        )

    resolved_opts = opts or HookIntegrateOpts()
    target = resolved_opts.target
    root_dir = target.root_dir if target else ".github"
    hooks_dir = project_root / root_dir / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)

    package_name = self._get_package_name(package_info)
    hooks_integrated = 0
    scripts_copied = 0
    scripts_adopted = 0
    target_paths: list[Path] = []

    for hook_file in hook_files:
        data = self._parse_hook_json(hook_file)
        if data is None:
            continue

        # Rewrite script paths for VSCode target
        rewritten, scripts = self._rewrite_hooks_data(
            data,
            HookRewriteOpts(
                package_path=package_info.install_path,
                package_name=package_name,
                target="vscode",
                hook_file_dir=hook_file.parent,
                root_dir=root_dir,
            ),
        )

        # Generate target filename (clean, no -apm suffix)
        stem = hook_file.stem
        target_filename = f"{package_name}-{stem}.json"
        target_path = hooks_dir / target_filename
        rel_path = portable_relpath(target_path, project_root)

        if self.check_collision(
            target_path,
            rel_path,
            resolved_opts.managed_files,
            resolved_opts.force,
            diagnostics=resolved_opts.diagnostics,
        ):
            continue

        # Write rewritten JSON
        with open(target_path, "w", encoding="utf-8") as f:
            json.dump(rewritten, f, indent=2)
            f.write("\n")

        hooks_integrated += 1
        target_paths.append(target_path)

        # Copy referenced scripts (individual file tracking)
        for source_file, target_rel in scripts:
            target_script = project_root / target_rel
            ensure_path_within(target_script, project_root)
            if self.is_content_identical_to_source(target_script, source_file):
                target_paths.append(target_script)
                scripts_adopted += 1
                continue
            if self.check_collision(
                target_script,
                target_rel,
                resolved_opts.managed_files,
                resolved_opts.force,
                diagnostics=resolved_opts.diagnostics,
            ):
                continue
            target_script.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_file, target_script)
            scripts_copied += 1
            target_paths.append(target_script)

    return HookIntegrationResult(
        files_integrated=hooks_integrated,
        files_updated=0,
        files_skipped=0,
        target_paths=target_paths,
        scripts_copied=scripts_copied,
        files_adopted=scripts_adopted,
    )


def integrate_hooks_for_target(
    self,
    target,
    package_info,
    project_root: Path,
    opts: HookIntegrateOpts | None = None,
) -> "HookIntegrationResult":
    """Integrate hooks for a single *target*.

    Copilot uses individual JSON files (genuinely different pattern).
    All other merge-based targets are dispatched via the
    ``_MERGE_HOOK_TARGETS`` registry.
    """
    if target.name == "copilot":
        return self.integrate_package_hooks(
            package_info,
            project_root,
            HookIntegrateOpts(
                force=(opts.force if opts else False),
                managed_files=(opts.managed_files if opts else None),
                diagnostics=(opts.diagnostics if opts else None),
                target=target,
            ),
        )

    config = _MERGE_HOOK_TARGETS.get(target.name)
    if config is not None:
        return self._integrate_merged_hooks(
            config,
            package_info,
            project_root,
            HookIntegrateOpts(
                force=(opts.force if opts else False),
                managed_files=(opts.managed_files if opts else None),
                diagnostics=(opts.diagnostics if opts else None),
                target=target,
            ),
        )

    return HookIntegrationResult(
        files_integrated=0,
        files_updated=0,
        files_skipped=0,
        target_paths=[],
    )
