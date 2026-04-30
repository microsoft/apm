"""InstalledPackage: a record of a successfully installed dependency.

Used to accumulate install results during ``apm install`` before writing
the final lockfile.  Previously represented as an ad hoc positional tuple;
using a dataclass eliminates positional-index brittleness and makes each
field self-documenting.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional  # noqa: F401

if TYPE_CHECKING:
    from apm_cli.deps.registry_proxy import RegistryConfig
    from apm_cli.models.dependency.reference import DependencyReference


@dataclass
class InstalledPackage:
    """Record of a single successfully-installed dependency.

    Accumulated by ``install_command()`` and consumed by
    :meth:`~apm_cli.deps.lockfile.LockFile.from_installed_packages` to
    generate the lock file.

    Attributes
    ----------
    dep_ref:
        The resolved :class:`~apm_cli.models.dependency.reference.DependencyReference`
        that was installed.
    resolved_commit:
        The exact commit SHA that was installed, or ``None`` for local / Artifactory
        packages where no commit is available.
    depth:
        Dependency tree depth (1 = direct, 2 = transitive, ...).
    resolved_by:
        ``repo_url`` of the parent that introduced this dependency, or ``None``
        for direct dependencies.
    is_dev:
        ``True`` when the package is a dev-only dependency.
    registry_config:
        The :class:`~apm_cli.deps.registry_proxy.RegistryConfig` that was active
        when this package was downloaded, or ``None`` for direct VCS installs.
        When present, the lockfile stores the proxy host (FQDN) and prefix so
        that subsequent installs replay through the same proxy.
    """

    dep_ref: DependencyReference
    resolved_commit: str | None
    depth: int
    resolved_by: str | None
    is_dev: bool = False
    registry_config: RegistryConfig | None = None
