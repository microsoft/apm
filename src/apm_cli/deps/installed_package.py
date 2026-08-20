"""InstalledPackage: a record of a successfully installed dependency.

Used to accumulate install results during ``apm install`` before writing
the final lockfile.  Previously represented as an ad hoc positional tuple;
using a dataclass eliminates positional-index brittleness and makes each
field self-documenting.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from apm_cli.deps.git_semver_resolver import GitSemverResolution
    from apm_cli.deps.registry.resolver import RegistryResolution
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
    registry_resolution:
        The :class:`~apm_cli.deps.registry.resolver.RegistryResolution` produced
        by the dedicated-registry resolver, or ``None`` for git/local/proxy
        installs. When present, the lockfile records ``resolved_url`` /
        ``resolved_hash`` / ``version`` from it so re-installs verify against
        the same content (design §6.1). Distinct concept from ``registry_config``
        (Artifactory VCS proxy).
    git_semver_resolution:
        The :class:`~apm_cli.deps.git_semver_resolver.GitSemverResolution` produced
        when a git-source dep used a semver range as ``ref:`` (issue #1488).
        When present the lockfile records ``constraint`` / ``resolved_tag`` /
        ``resolved_at`` and ``resolved_ref`` is set to the concrete tag.
    """

    dep_ref: DependencyReference
    resolved_commit: str | None
    depth: int
    resolved_by: str | None
    is_dev: bool = False
    registry_config: RegistryConfig | None = None
    registry_resolution: RegistryResolution | None = None
    git_semver_resolution: GitSemverResolution | None = None
    # Package-declared metadata read from apm.yml at resolution time (#1888).
    # Prefixed package_ to avoid collision with dep_ref identity fields.
    package_name: str | None = None
    package_version: str | None = None
