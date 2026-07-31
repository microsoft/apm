"""Hook integration facade for APM packages."""

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from apm_cli.core.deployment_ledger import DeploymentLedgerCodec
from apm_cli.core.deployment_state import (
    MaterializationResult,
    MaterializationStatus,
    NativePayloadValidation,
)
from apm_cli.core.scope import InstallScope
from apm_cli.integration.base_integrator import BaseIntegrator, IntegrationResult
from apm_cli.integration.hook_bundle import copy_deployed_hook_bundle
from apm_cli.integration.hook_file_routing import filter_hook_files_for_target
from apm_cli.utils.atomic_io import atomic_write_text
from apm_cli.utils.console import (
    _rich_warning,  # noqa: F401  (testability seam -- tests monkeypatch this)
)
from apm_cli.utils.paths import portable_relpath

if TYPE_CHECKING:
    from apm_cli.deps.lockfile import LockFile

from .hook_merge import (
    _clean_apm_entries_from_json,
    _dependency_hook_sources,
    _get_hook_source_marker,
    _is_root_local_package,
    _load_merged_config_and_sidecar,
    _merge_hook_file_entries,
    _warn_empty_hook_file,
    _write_merged_config,
)
from .hook_merge import (
    _get_package_name as _get_package_name_impl,
)
from .hook_merge import (
    _parse_hook_json as _parse_hook_json_impl,
)
from .hook_native_formats import (  # noqa: F401
    _to_antigravity_hook_entries,
    _to_claude_hook_entries,
    _to_gemini_hook_entries,
)
from .hook_transforms import (
    _APM_HOOKS_SIDECAR,
    _HOOK_EVENT_MAP,
    _MERGE_HOOK_TARGETS,
    _emit_hook_event_diagnostics,
    _MergeHookConfig,
    _rewrite_hooks_data,
    _validate_copilot_payload,
)

# ---------------------------------------------------------------------------
# Re-exports: symbols imported by external callers / tests from this module.
# The ``X as X`` form marks them as intentional public re-exports (PEP 484).
# ---------------------------------------------------------------------------
from .hook_transforms import _HOOK_EVENT_EXPECTED_CASING as _HOOK_EVENT_EXPECTED_CASING
from .hook_transforms import (
    _build_display_payload as _build_display_payload_impl,
)
from .hook_transforms import _copilot_keys_to_gemini as _copilot_keys_to_gemini
from .hook_transforms import _detect_event_casing as _detect_event_casing
from .hook_transforms import (
    _iter_hook_entries as _iter_hook_entries_impl,
)
from .hook_transforms import _reinject_apm_source_from_sidecar as _reinject_apm_source_from_sidecar
from .hook_transforms import (
    _rewrite_command_for_target as _rewrite_command_for_target_impl,
)
from .hook_transforms import (
    _summarize_command as _summarize_command_impl,
)

# Testability seam: tests can patch deprecated filename routing without
# replacing the imported helper for every call site.
_filter_hook_files_for_target = filter_hook_files_for_target

_log = logging.getLogger(__name__)


# DEPRECATED -- use IntegrationResult directly for new code.
# Backward-compatible shim: accepts hooks_integrated= kwarg and
# exposes a hooks_integrated property for consumers of the old API.
class HookIntegrationResult(IntegrationResult):
    def __init__(self, *args, hooks_integrated=None, **kwargs):
        if hooks_integrated is not None:
            kwargs.setdefault("files_integrated", hooks_integrated)
            kwargs.setdefault("files_updated", 0)
            kwargs.setdefault("files_skipped", 0)
            kwargs.setdefault("target_paths", [])
        super().__init__(*args, **kwargs)

    @property
    def hooks_integrated(self):
        return self.files_integrated


class HookIntegrator(BaseIntegrator):
    """Handles integration of APM package hooks into target locations."""

    def __init__(self) -> None:
        super().__init__()
        self._deprecated_hook_routing_warnings: set[str] = set()

    @staticmethod
    def _get_package_name(package_info, project_root: Path | None = None) -> str:
        return _get_package_name_impl(package_info, project_root)

    @staticmethod
    def _rewrite_command_for_target(
        command: str,
        package_path,
        package_name: str,
        target: str,
        deploy_root=None,
        hook_file_dir=None,
        root_dir: str | None = None,
        _warn=None,
    ):
        if _warn is None:
            import apm_cli.integration.hook_integrator as _hi

            _warn = _hi._rich_warning
        return _rewrite_command_for_target_impl(
            command,
            package_path,
            package_name,
            target,
            deploy_root=deploy_root,
            hook_file_dir=hook_file_dir,
            root_dir=root_dir,
            _warn=_warn,
        )

    @staticmethod
    def _rewrite_hooks_data(
        data: dict,
        package_path,
        package_name: str,
        target: str,
        hook_file_dir=None,
        root_dir: str | None = None,
        deploy_root=None,
    ):
        return _rewrite_hooks_data(
            data,
            package_path,
            package_name,
            target,
            hook_file_dir=hook_file_dir,
            root_dir=root_dir,
            deploy_root=deploy_root,
        )

    @staticmethod
    def _root_local_identity_root(package_info, project_root: Path | None) -> Path | None:
        return getattr(package_info, "root_local_project_root", None) or project_root

    @staticmethod
    def _deploy_root_for_hook_rewrite(project_root: Path, user_scope: bool) -> Path | None:
        return project_root if user_scope else None

    @staticmethod
    def _is_root_local_package(package_info, project_root: Path | None) -> bool:
        return _is_root_local_package(package_info, project_root)

    @staticmethod
    def _get_hook_source_marker(package_info, project_root: Path, package_name: str) -> str:
        return _get_hook_source_marker(package_info, project_root, package_name)

    @staticmethod
    def _dependency_hook_sources(project_root: Path) -> set[str]:
        return _dependency_hook_sources(project_root)

    @staticmethod
    def _clean_apm_entries_from_json(
        json_path: Path,
        stats: dict[str, int],
        container: str = "hooks",
        sidecar_path: Path | None = None,
    ) -> None:
        _clean_apm_entries_from_json(
            json_path, stats, container=container, sidecar_path=sidecar_path
        )

    @staticmethod
    def _iter_hook_entries(payload: dict) -> list[tuple[str, dict]]:
        return _iter_hook_entries_impl(payload)

    @staticmethod
    def _summarize_command(entry: dict) -> str:
        return _summarize_command_impl(entry)

    def _build_display_payload(
        self,
        target_label: str,
        output_path: str,
        source_hook_file: Any,
        rewritten: dict,
    ) -> dict:
        return _build_display_payload_impl(target_label, output_path, source_hook_file, rewritten)

    def find_hook_files(self, package_path: Path) -> list[Path]:
        hook_files: list[Path] = []
        seen_stems: set[str] = set()

        # Search in .apm/hooks/ (APM convention)
        apm_hooks = package_path / ".apm" / "hooks"
        if apm_hooks.exists():
            for f in sorted(apm_hooks.glob("*.json")):
                if f.is_symlink():
                    continue
                stem_key = f.stem.lower()
                if stem_key not in seen_stems:
                    seen_stems.add(stem_key)
                    hook_files.append(f)

        # Search in hooks/ (Claude-native convention)
        hooks_dir = package_path / "hooks"
        if hooks_dir.exists():
            for f in sorted(hooks_dir.glob("*.json")):
                if f.is_symlink():
                    continue
                stem_key = f.stem.lower()
                if stem_key not in seen_stems:
                    seen_stems.add(stem_key)
                    hook_files.append(f)

        return hook_files

    def _parse_hook_json(self, hook_file: Path) -> dict | None:
        return _parse_hook_json_impl(hook_file)

    # -- Copilot (individual-file) integration --

    def integrate_package_hooks(
        self,
        package_info,
        project_root: Path,
        force: bool = False,
        managed_files: set = None,  # noqa: RUF013
        diagnostics=None,
        target=None,
        *,
        user_scope: bool = False,
    ) -> HookIntegrationResult:
        hook_files = self.find_hook_files(package_info.install_path)
        package_name = self._get_package_name(package_info, project_root)
        hook_files = _filter_hook_files_for_target(
            hook_files,
            "copilot",
            package_name=package_name,
            warned_packages=self._deprecated_hook_routing_warnings,
            package_identity=package_info.get_canonical_dependency_string(),
        )

        if not hook_files:
            return HookIntegrationResult(
                files_integrated=0, files_updated=0, files_skipped=0, target_paths=[]
            )

        root_dir = target.root_dir if target else ".github"
        hooks_dir = project_root / root_dir / "hooks"
        hooks_dir.mkdir(parents=True, exist_ok=True)
        deploy_root_for_rewrite = self._deploy_root_for_hook_rewrite(project_root, user_scope)

        hooks_integrated = 0
        scripts_copied = 0
        scripts_adopted = 0
        target_paths: list[Path] = []
        display_payloads: list = []
        materializations: list[MaterializationResult] = []
        from apm_cli.integration.targets import KNOWN_TARGETS
        from apm_cli.utils.content_hash import compute_file_hash

        _copilot_target = KNOWN_TARGETS["copilot"]

        for hook_file in hook_files:
            data = self._parse_hook_json(hook_file)
            if data is None:
                continue

            rewritten, scripts = _rewrite_hooks_data(
                data,
                package_info.install_path,
                package_name,
                "vscode",
                hook_file_dir=hook_file.parent,
                root_dir=root_dir,
                deploy_root=deploy_root_for_rewrite,
            )

            # Apply Copilot event name normalisation
            hooks = rewritten.get("hooks", {})
            event_map = _HOOK_EVENT_MAP.get("copilot", {})
            if isinstance(hooks, dict):
                renamed_hooks: dict = {}
                for raw_event_name, entries in hooks.items():
                    event_name = event_map.get(raw_event_name, raw_event_name)
                    if event_name in renamed_hooks and isinstance(renamed_hooks[event_name], list):
                        if isinstance(entries, list):
                            renamed_hooks[event_name].extend(entries)
                            continue
                    renamed_hooks[event_name] = entries
                rewritten["hooks"] = renamed_hooks

            rewritten.setdefault("version", 1)
            errors = _validate_copilot_payload(rewritten)
            validation = NativePayloadValidation(
                valid=not errors, contract="copilot-hooks-v1", errors=tuple(errors)
            )

            # Generate target filename
            stem = hook_file.stem
            target_filename = f"{package_name}-{stem}.json"
            target_path = hooks_dir / target_filename
            rel_path = portable_relpath(target_path, project_root)

            if not validation.valid:
                if diagnostics is not None:
                    diagnostics.error(
                        f"Invalid Copilot hook payload for {rel_path}",
                        package=package_name,
                        detail="; ".join(validation.errors),
                    )
                materializations.append(
                    MaterializationResult(
                        locator=DeploymentLedgerCodec.locator_for_path(
                            target_path,
                            project_root=project_root,
                            target=_copilot_target,
                            scope=InstallScope.PROJECT,
                        ),
                        owners=frozenset({package_info.get_canonical_dependency_string()}),
                        status=MaterializationStatus.FAILED,
                        content_hash=None,
                        validation=validation,
                    )
                )
                continue

            if self.check_collision(
                target_path, rel_path, managed_files, force, diagnostics=diagnostics
            ):
                continue

            _emit_hook_event_diagnostics(list(hooks.keys()), "copilot", event_map)
            atomic_write_text(target_path, json.dumps(rewritten, indent=2) + "\n")
            hooks_integrated += 1
            target_paths.append(target_path)
            materializations.append(
                MaterializationResult(
                    locator=DeploymentLedgerCodec.locator_for_path(
                        target_path,
                        project_root=project_root,
                        target=_copilot_target,
                        scope=InstallScope.PROJECT,
                    ),
                    owners=frozenset({package_info.get_canonical_dependency_string()}),
                    status=MaterializationStatus.WRITTEN,
                    content_hash=compute_file_hash(target_path),
                    validation=validation,
                )
            )
            display_payloads.append(
                self._build_display_payload(
                    f"{root_dir}/hooks/", target_filename, hook_file, rewritten
                )
            )
            copy_result = copy_deployed_hook_bundle(
                self,
                package_path=package_info.install_path,
                hook_file_dir=hook_file.parent,
                project_root=project_root,
                scripts=scripts,
                managed_files=managed_files,
                force=force,
                diagnostics=diagnostics,
                target_paths=target_paths,
                hook_descriptor_files=set(hook_files),
                exclude_json_files=True,
            )
            scripts_copied += copy_result.scripts_copied
            scripts_adopted += copy_result.files_adopted

        return HookIntegrationResult(
            files_integrated=hooks_integrated,
            files_updated=0,
            files_skipped=0,
            target_paths=target_paths,
            scripts_copied=scripts_copied,
            files_adopted=scripts_adopted,
            display_payloads=display_payloads,
            materializations=tuple(materializations),
        )

    # -- Shared JSON-merge implementation (Claude / Cursor / Codex) --

    def _integrate_merged_hooks(
        self,
        config: "_MergeHookConfig",
        package_info,
        project_root: Path,
        *,
        force: bool = False,
        managed_files: set = None,  # noqa: RUF013
        diagnostics=None,
        target=None,
        user_scope: bool = False,
    ) -> HookIntegrationResult:
        _empty = HookIntegrationResult(
            files_integrated=0,
            files_updated=0,
            files_skipped=0,
            target_paths=[],
        )

        root_dir = target.root_dir if target else f".{config.target_key}"
        target_dir = project_root / root_dir

        # Opt-in check: some targets only deploy when their dir exists
        if config.require_dir and not target_dir.exists():
            return _empty

        # Absolutize hook commands only for user-scope deploys.
        _deploy_root_for_rewrite = self._deploy_root_for_hook_rewrite(project_root, user_scope)

        hook_files = self.find_hook_files(package_info.install_path)
        package_name = self._get_package_name(package_info, project_root)
        hook_files = _filter_hook_files_for_target(
            hook_files,
            config.target_key,
            package_name=package_name,
            warned_packages=self._deprecated_hook_routing_warnings,
            package_identity=package_info.get_canonical_dependency_string(),
        )
        if not hook_files:
            return _empty

        source_marker = _get_hook_source_marker(package_info, project_root, package_name)
        heal_stale = _is_root_local_package(package_info, project_root)
        dep_sources = _dependency_hook_sources(project_root) if heal_stale else set()

        hooks_integrated = 0
        scripts_copied = 0
        scripts_adopted = 0
        target_paths: list[Path] = []
        display_payloads: list = []
        pending_display: list = []
        cleared_events: set = set()

        json_path = target_dir / config.config_filename
        sidecar_path = target_dir / _APM_HOOKS_SIDECAR
        json_config = _load_merged_config_and_sidecar(
            json_path, sidecar_path, config.schema_strict, container=config.event_container_key
        )

        injected_keys: list[str] = []
        for key, value in config.top_level_defaults.items():
            if key not in json_config:
                json_config[key] = value
                injected_keys.append(key)
        if injected_keys:
            _log.debug(
                "Injected top_level_defaults into %s: %s", config.config_filename, injected_keys
            )

        for hook_file in hook_files:
            data = self._parse_hook_json(hook_file)
            if data is None:
                continue

            rewritten, scripts = _rewrite_hooks_data(
                data,
                package_info.install_path,
                package_name,
                config.target_key,
                hook_file_dir=hook_file.parent,
                root_dir=root_dir,
                deploy_root=_deploy_root_for_rewrite,
            )

            container = config.event_container_key
            hooks = rewritten.get("hooks", {})  # source files always use "hooks" key
            event_map = _HOOK_EVENT_MAP.get(config.target_key, {})
            _emit_hook_event_diagnostics(list(hooks.keys()), config.target_key, event_map)

            file_event_entries: dict = {}
            appended = _merge_hook_file_entries(
                json_config,
                hooks,
                config.target_key,
                event_map,
                source_marker,
                cleared_events,
                heal_stale_root_source=heal_stale,
                dependency_sources=dep_sources,
                capture_entries=file_event_entries,
                container=container,
            )

            if appended:
                hooks_integrated += 1
                pending_display.append(
                    (
                        config.config_filename,
                        config.config_filename,
                        hook_file,
                        file_event_entries,
                    )
                )
            else:
                _warn_empty_hook_file(hook_file, config.target_key)

            copy_result = copy_deployed_hook_bundle(
                self,
                package_path=package_info.install_path,
                hook_file_dir=hook_file.parent,
                project_root=project_root,
                scripts=scripts,
                managed_files=managed_files,
                force=force,
                diagnostics=diagnostics,
                target_paths=target_paths,
                hook_descriptor_files=set(hook_files),
            )
            scripts_copied += copy_result.scripts_copied
            scripts_adopted += copy_result.files_adopted

        json_path.parent.mkdir(parents=True, exist_ok=True)
        _write_merged_config(json_path, sidecar_path, json_config, config.schema_strict)

        for _label, _path, _hook_file, _file_event_entries in pending_display:
            display_payloads.append(
                self._build_display_payload(
                    _label,
                    _path,
                    _hook_file,
                    {config.event_container_key: _file_event_entries},
                )
            )

        return HookIntegrationResult(
            files_integrated=hooks_integrated,
            files_updated=0,
            files_skipped=0,
            target_paths=target_paths,
            scripts_copied=scripts_copied,
            files_adopted=scripts_adopted,
            display_payloads=display_payloads,
        )

    def integrate_package_hooks_claude(
        self,
        package_info,
        project_root: Path,
        force: bool = False,
        managed_files: set = None,  # noqa: RUF013
        diagnostics=None,
        *,
        user_scope: bool = False,
    ) -> HookIntegrationResult:
        return self._integrate_merged_hooks(
            _MERGE_HOOK_TARGETS["claude"],
            package_info,
            project_root,
            force=force,
            managed_files=managed_files,
            diagnostics=diagnostics,
            user_scope=user_scope,
        )

    def integrate_package_hooks_cursor(
        self,
        package_info,
        project_root: Path,
        force: bool = False,
        managed_files: set = None,  # noqa: RUF013
        diagnostics=None,
        *,
        user_scope: bool = False,
    ) -> HookIntegrationResult:
        return self._integrate_merged_hooks(
            _MERGE_HOOK_TARGETS["cursor"],
            package_info,
            project_root,
            force=force,
            managed_files=managed_files,
            diagnostics=diagnostics,
            user_scope=user_scope,
        )

    def integrate_package_hooks_codex(
        self,
        package_info,
        project_root: Path,
        force: bool = False,
        managed_files: set = None,  # noqa: RUF013
        diagnostics=None,
        *,
        user_scope: bool = False,
    ) -> HookIntegrationResult:
        return self._integrate_merged_hooks(
            _MERGE_HOOK_TARGETS["codex"],
            package_info,
            project_root,
            force=force,
            managed_files=managed_files,
            diagnostics=diagnostics,
            user_scope=user_scope,
        )

    # -- Target-driven API --

    def integrate_hooks_for_target(
        self,
        target,
        package_info,
        project_root: Path,
        *,
        force: bool = False,
        managed_files: set = None,  # noqa: RUF013
        diagnostics=None,
        scope=None,
        user_scope: bool = False,
        dep_targets_active: bool = False,
        allowed_targets: "set[str] | None" = None,
    ) -> "HookIntegrationResult":
        if dep_targets_active and (not allowed_targets or target.name not in allowed_targets):
            raise AssertionError(f"BUG: target {target.name} bypassed chokepoint filter")

        if target.name == "copilot":
            return self.integrate_package_hooks(
                package_info,
                project_root,
                force=force,
                managed_files=managed_files,
                diagnostics=diagnostics,
                target=target,
                user_scope=user_scope,
            )

        if target.name == "kiro":
            from apm_cli.integration.kiro_hook_integrator import integrate_kiro_hooks

            return integrate_kiro_hooks(
                self,
                package_info,
                project_root,
                force=force,
                managed_files=managed_files,
                diagnostics=diagnostics,
                target=target,
                user_scope=user_scope,
            )

        config = _MERGE_HOOK_TARGETS.get(target.name)
        if config is not None:
            return self._integrate_merged_hooks(
                config,
                package_info,
                project_root,
                force=force,
                managed_files=managed_files,
                diagnostics=diagnostics,
                target=target,
                user_scope=user_scope,
            )

        return HookIntegrationResult(
            files_integrated=0,
            files_updated=0,
            files_skipped=0,
            target_paths=[],
        )

    def sync_integration(
        self,
        apm_package,
        project_root: Path,
        managed_files: set = None,  # noqa: RUF013
        targets=None,
    ) -> dict:
        from .targets import KNOWN_TARGETS

        stats: dict[str, int] = {"files_removed": 0, "errors": 0}

        # Derive hook prefixes dynamically from targets
        source = targets if targets is not None else list(KNOWN_TARGETS.values())
        hook_prefixes = []
        for t in source:
            if t.supports("hooks"):
                sm = t.primitives["hooks"]
                effective_root = sm.deploy_root or t.root_dir
                hook_prefixes.append(f"{effective_root}/hooks/")
        hook_prefix_tuple = tuple(hook_prefixes)

        if managed_files is not None:
            deleted: list = []
            for rel_path in managed_files:
                normalized = rel_path.replace("\\", "/")
                if not normalized.startswith(hook_prefix_tuple) or ".." in rel_path:
                    continue
                target_file = project_root / rel_path
                if target_file.exists() and target_file.is_file():
                    try:
                        target_file.unlink()
                        stats["files_removed"] += 1
                        deleted.append(target_file)
                    except Exception:
                        stats["errors"] += 1
            self.cleanup_empty_parents(deleted, stop_at=project_root)
        else:
            hooks_dir = project_root / ".github" / "hooks"
            if hooks_dir.exists():
                for hook_file in hooks_dir.glob("*-apm.json"):
                    try:
                        hook_file.unlink()
                        stats["files_removed"] += 1
                    except Exception:
                        stats["errors"] += 1

        # Clean APM entries from merged-hook JSON configs (uses _apm_source marker)
        for t in source:
            config = _MERGE_HOOK_TARGETS.get(t.name)
            if config is not None:
                root = t.root_dir if hasattr(t, "root_dir") else f".{t.name}"
                json_path = project_root / root / config.config_filename
                sidecar_path = json_path.parent / _APM_HOOKS_SIDECAR
                self._clean_apm_entries_from_json(
                    json_path,
                    stats,
                    container=config.event_container_key,
                    sidecar_path=sidecar_path,
                )

        return stats

    def reconcile_after_removal(
        self,
        apm_package,
        project_root: Path,
        *,
        user_scope: bool = False,
        lockfile: "LockFile | None" = None,
    ) -> dict:
        """Reconcile merged-hook ownership after packages leave apm.yml (#2254)."""
        from apm_cli.constants import APM_MODULES_DIR
        from apm_cli.models.apm_package import (
            build_installed_package_info,
            surviving_dependency_refs_for_reintegration,
        )

        from .targets import resolve_targets

        config_target = list(apm_package.canonical_targets)
        targets = resolve_targets(
            project_root, user_scope=user_scope, explicit_target=config_target or None
        )
        surviving_deps = surviving_dependency_refs_for_reintegration(
            apm_package, project_root, lockfile=lockfile
        )
        stats = self.sync_integration(
            apm_package, project_root, managed_files=set(), targets=targets
        )
        for dep_ref in surviving_deps:
            pkg_info = build_installed_package_info(dep_ref, project_root / APM_MODULES_DIR)
            if pkg_info is None:
                continue
            try:
                for target in targets:
                    self.integrate_hooks_for_target(
                        target, pkg_info, project_root, user_scope=user_scope
                    )
            except Exception as e:
                stats["errors"] = stats.get("errors", 0) + 1
                pkg_id = (
                    dep_ref.get_identity() if hasattr(dep_ref, "get_identity") else str(dep_ref)
                )
                _log.warning("Best-effort hook re-integration skipped for %s: %s", pkg_id, e)
        return stats

    def reconcile_dropped_targets(
        self,
        project_root: Path,
        dropped_target_names: "list[str] | set[str]",
        *,
        user_scope: bool = False,
    ) -> "dict[str, int]":
        """Clean merge-hook state for targets no longer declared (#2253)."""
        from ._hook_dropped_targets import reconcile_dropped_targets as _impl

        return _impl(project_root, dropped_target_names, user_scope=user_scope)
