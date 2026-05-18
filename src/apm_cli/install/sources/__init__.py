"""Dependency sources -- Strategy pattern for the install pipeline.

Each ``DependencySource`` knows how to *acquire* one dependency: bring its
files onto disk, build a ``PackageInfo``, register it in the lockfile-bound
state, and return the metadata the integration template needs.

After ``acquire()``, all sources flow through the same template
(``apm_cli.install.template.run_integration_template``) which handles the
security gate, primitive integration, and per-package diagnostics.

This package deliberately contains *only* source-specific logic.  Anything
shared across sources lives in the template.

Sources
-------
- ``LocalDependencySource``: ``file://`` deps copied from the workspace.
- ``CachedDependencySource``: deps already extracted in ``apm_modules/``.
- ``FreshDependencySource``: deps that need a network download (with
  supply-chain hash verification on top of the existing lockfile entry).

The root-project integration (``<project_root>/.apm/``) follows a
substantially different shape (no PackageInfo, dedicated tracking on
``ctx.local_deployed_files``) and is handled separately in
``phases/integrate.py``.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from apm_cli.install.sources._base import (
    DependencySource,
    Materialization,
    _format_package_type_label,
)
from apm_cli.install.sources._cached import CachedDependencySource, _CachedSourceExtras
from apm_cli.install.sources._fresh import FreshDependencySource, _FreshSourceExtras
from apm_cli.install.sources._local import LocalDependencySource

if TYPE_CHECKING:
    from apm_cli.install.context import InstallContext

__all__ = [
    "CachedDependencySource",
    "DependencySource",
    "FreshDependencySource",
    "LocalDependencySource",
    "Materialization",
    "_format_package_type_label",
    "make_dependency_source",
]


def make_dependency_source(
    ctx: InstallContext,
    dep_ref: Any,
    install_path: Path,
    dep_key: str,
    **kwargs,
) -> DependencySource:
    """Factory: pick the right ``DependencySource`` for *dep_ref*.

    Caller is responsible for resolving the download strategy (cached vs
    fresh) before invoking the factory; the resolved-ref and
    locked-checksum data flow into the appropriate source.

    ``fetched_this_run`` (F2): when ``skip_download=True`` AND the
    package was actually downloaded earlier in this run by the resolver
    callback, set this to ``True`` so the cached source emits the
    download-complete line WITHOUT the misleading ``(cached)`` suffix.
    """
    resolved_ref = kwargs.get("resolved_ref")
    dep_locked_chk = kwargs.get("dep_locked_chk")
    ref_changed: bool = kwargs.get("ref_changed", False)
    skip_download: bool = kwargs.get("skip_download", False)
    fetched_this_run: bool = kwargs.get("fetched_this_run", False)
    progress = kwargs.get("progress")
    if dep_ref.is_local and dep_ref.local_path:
        return LocalDependencySource(ctx, dep_ref, install_path, dep_key)
    if skip_download:
        return CachedDependencySource(
            ctx,
            dep_ref,
            install_path,
            dep_key,
            _CachedSourceExtras(
                resolved_ref=resolved_ref,
                dep_locked_chk=dep_locked_chk,
                fetched_this_run=fetched_this_run,
            ),
        )
    return FreshDependencySource(
        ctx,
        dep_ref,
        install_path,
        dep_key,
        _FreshSourceExtras(
            resolved_ref=resolved_ref,
            dep_locked_chk=dep_locked_chk,
            ref_changed=ref_changed,
            progress=progress,
        ),
    )
