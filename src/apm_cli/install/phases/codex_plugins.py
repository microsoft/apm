"""Register scanned Codex-native bundles only after canonical persistence and audit."""

from __future__ import annotations

from typing import TYPE_CHECKING

from apm_cli.codex_plugins.client import CodexNativeError
from apm_cli.codex_plugins.registrar import synchronize_codex_plugins
from apm_cli.copilot_plugins.registrar import candidates_from_lockfile
from apm_cli.deps.lockfile import LockFile, get_lockfile_path

if TYPE_CHECKING:
    from apm_cli.install.context import InstallContext


def run(ctx: InstallContext) -> None:
    """Consume the shared locked-candidate authority, never derive another closure."""
    if ctx.dry_run or ctx.lockfile_only or ctx.apm_modules_dir is None:
        return
    saved = LockFile.read(get_lockfile_path(ctx.apm_dir))
    candidates = candidates_from_lockfile(saved, ctx.apm_modules_dir)
    retained = {candidate.dependency_key for candidate in candidates}
    if not set(ctx.codex_native_plugins).issubset(retained):
        raise CodexNativeError(
            "Codex admission no longer matches the saved executable/ownership state. "
            "Review approval and rerun apm install."
        )
    for key in sorted(ctx.codex_native_plugins):
        retained_files = ctx.package_deployed_files.get(key)
        if retained_files:
            raise CodexNativeError(
                f"Codex native migration for {key} has retained loose deployment files: "
                f"{', '.join(sorted(retained_files))}. "
                "Preserve or relocate your edits, resolve cleanup conflicts, and rerun "
                "'apm install -g --target codex'; native registration was not changed."
            )
    retained.difference_update(set(ctx.package_deployed_files) - set(ctx.codex_native_plugins))
    changed = synchronize_codex_plugins(
        list(ctx.codex_native_plugins.values()),
        modules_dir=ctx.apm_modules_dir,
        cwd=ctx.apm_dir,
        scope=ctx.scope,
        retained_keys=retained,
    )
    if changed and ctx.logger:
        ctx.logger.success(
            f"Reconciled {len(changed)} Codex native plugin registration(s) at user scope. "
            "New plugins stay disabled; activation and MCP authorization remain with Codex."
        )
