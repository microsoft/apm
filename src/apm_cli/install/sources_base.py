"""Base types for the install dependency-source strategy.

Holds the pieces shared by every concrete ``DependencySource`` so the
source modules (``sources``, ``sources_fresh``) can depend on them without
importing each other:

- :class:`Materialization` -- the value object returned by ``acquire()``.
- :class:`DependencySource` -- the strategy base class, including the
  helpers that fold the per-source lockfile bookkeeping (which was
  previously copy-pasted into each ``acquire()``).

See ``apm_cli.install.sources`` for the module-level overview of the
strategy flow.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from apm_cli.install.context import InstallContext
    from apm_cli.models.apm_package import PackageInfo


def _record_declared_license(ctx, dep_key: str, install_path) -> None:
    """Backfill ctx.package_declared_licenses from the resolved dep's manifest.

    Reads the DECLARED license (apm.yml ``license:`` or plugin.json
    ``license``) at the install path. APM never reads the LICENSE file text or
    concludes a license -- this is a passthrough of an author claim. When no
    manifest declares one, the key is left ABSENT (not declared == unknown);
    no sentinel is stored. Best-effort: any read error leaves the key absent.
    """
    try:
        from apm_cli.export.declared_license import read_declared_license

        declared = read_declared_license(install_path)
    except Exception:
        declared = None
    if declared:
        ctx.package_declared_licenses[dep_key] = declared


@dataclass
class Materialization:
    """Outcome of ``DependencySource.acquire()``.

    Carries everything the integration template needs to run the security
    gate + primitive integration on a freshly-acquired package.
    """

    package_info: PackageInfo | None
    install_path: Path
    dep_key: str
    deltas: dict[str, int] = field(default_factory=lambda: {"installed": 1})


class DependencySource(ABC):
    """Strategy: acquire one dependency and prepare it for integration.

    Subclasses encapsulate source-specific concerns (filesystem copy,
    cache reuse, fresh download with progress + hash verification).
    The post-acquire template flow is the same for every source.
    """

    INTEGRATE_ERROR_PREFIX: str = "Failed to integrate primitives"
    """Per-source error wording used by the integration template when
    ``integrate_package_primitives`` raises.  Subclasses override to
    preserve the legacy diagnostic text shown to users."""

    def __init__(
        self,
        ctx: InstallContext,
        dep_ref: Any,
        install_path: Path,
        dep_key: str,
    ):
        self.ctx = ctx
        self.dep_ref = dep_ref
        self.install_path = install_path
        self.dep_key = dep_key

    @abstractmethod
    def acquire(self) -> Materialization | None:
        """Materialise the dependency on disk and build PackageInfo.

        Returns ``None`` to skip integration entirely (e.g. local dep at
        user scope, copy/download failure).  Otherwise returns a
        ``Materialization`` consumed by the integration template.
        """

    def _lockfile_node_fields(self) -> tuple[int, str | None, bool]:
        """Return ``(depth, resolved_by, is_dev)`` for this dep's lockfile entry.

        Shared by every source: looks up the dependency-tree node and reads
        the three fields the ``InstalledPackage`` record needs, defaulting
        gracefully when the node is absent (depth 1, no parent, not dev).
        """
        node = self.ctx.dependency_graph.dependency_tree.get_node(self.dep_key)
        depth = node.depth if node else 1
        resolved_by = node.parent.dependency_ref.repo_url if node and node.parent else None
        is_dev = node.is_dev if node else False
        return depth, resolved_by, is_dev

    def _skip_integration(self, deltas: dict[str, int]) -> Materialization:
        """Return a ``Materialization`` that signals 'skip integration'.

        Used when the target set is empty: the package is recorded in
        lockfile-bound state but no files are deployed.  ``package_info=None``
        is the agreed signal to the template to skip the integration pass.
        """
        return Materialization(
            package_info=None,
            install_path=self.install_path,
            dep_key=self.dep_key,
            deltas=deltas,
        )
