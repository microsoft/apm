"""Codex split-install report and one-time retirement of app-server registrations."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from apm_cli.codex_plugins.legacy_registrations import (
    report_legacy_retirement,
    retire_legacy_codex_registrations,
)
from apm_cli.core.scope import InstallScope

if TYPE_CHECKING:
    from apm_cli.install.context import InstallContext


def split_install_message(plugins: set[str], scope: object, *, dry_run: bool = False) -> str:
    """Name the target, scope, plugins and the unmet native capability (OpenAPM A3)."""
    scope_label = "user" if scope is InstallScope.USER else "project"
    split, deploy = (
        ("would split-install", "would deploy") if dry_run else ("split-installed", "deployed")
    )
    return (
        f"Codex: {split} {len(plugins)} plugin(s) at {scope_label} scope "
        f"({', '.join(sorted(plugins))}). Codex cannot enable a plugin from its settings "
        f"alone (native activation criterion A3, declarative enable), so APM {deploy} each "
        "plugin's skills, agents, hooks and MCP servers separately and does not register it "
        "with Codex."
    )


def run(ctx: InstallContext) -> None:
    """Report split installs once, then retire legacy registrations APM still owns."""
    if ctx.dry_run or ctx.lockfile_only:
        return
    if ctx.codex_split_plugins and ctx.diagnostics is not None:
        ctx.diagnostics.info(split_install_message(ctx.codex_split_plugins, ctx.scope))
    if ctx.apm_modules_dir is not None:
        report_legacy_retirement(
            retire_legacy_codex_registrations(ctx.apm_modules_dir, ctx.scope), ctx.logger
        )


def _dry_run_plugin_name(package_path: Path, locked: Any) -> str | None:
    """Name a plugin the way the real install types it, without normalizing it."""
    from apm_cli.deps.plugin_parser import parse_plugin_manifest
    from apm_cli.install.legacy_plugin_compat import preserve_normalized_marketplace_plugin_type
    from apm_cli.models.validation import PackageType, detect_package_type

    detected, manifest = detect_package_type(package_path)
    package_type = preserve_normalized_marketplace_plugin_type(package_path, locked, detected)
    if package_type is not PackageType.MARKETPLACE_PLUGIN:
        return None
    name = getattr(locked, "name", None)
    if not name and manifest is not None:
        try:
            name = parse_plugin_manifest(manifest).get("name")
        except (OSError, ValueError):
            name = None
    return str(name or package_path.name)


def report_codex_split_dry_run(ctx: Any, dependencies: list, *, apm_package: Any) -> None:
    """Report the split-install decision a real install would make, writing nothing."""
    from apm_cli.core.scope import get_modules_dir, is_user_scope
    from apm_cli.deps.lockfile import LockFile, get_lockfile_path
    from apm_cli.integration.targets import resolve_targets
    from apm_cli.models.apm_package import package_target_selection

    try:
        targets = resolve_targets(
            ctx.project_root,
            user_scope=is_user_scope(ctx.scope),
            explicit_target=ctx.target or package_target_selection(apm_package),
            create_config=False,
        )
    except Exception:
        targets = getattr(ctx, "targets", None) or []
    if ctx.logger is None or not any(target.name == "codex" for target in targets):
        return
    modules_dir = get_modules_dir(ctx.scope)
    try:
        lockfile = LockFile.read(get_lockfile_path(ctx.apm_dir))
    except (OSError, ValueError):
        lockfile = None
    plugins: set[str] = set()
    for dependency in dependencies:
        if dependency.is_marketplace:
            continue
        if dependency.is_local and dependency.local_path:
            package_path = Path(dependency.local_path).expanduser()
            if not package_path.is_absolute():
                package_path = (ctx.project_root / package_path).resolve()
        else:
            package_path = dependency.get_install_path(modules_dir)
        locked = lockfile.get_dependency(dependency.get_unique_key()) if lockfile else None
        if package_path.is_dir() and (name := _dry_run_plugin_name(package_path, locked)):
            plugins.add(name)
    if plugins:
        ctx.logger.info(split_install_message(plugins, ctx.scope, dry_run=True))
