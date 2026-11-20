"""Isolated installed-CLI scenario shared by lifecycle contract tests (#4114).

One ``IsolatedApmEnvironment`` with its subprocess environment, local package
factories for sources and consumers, a production-shaped local Git repository
factory, and an installed-CLI runner.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_git_repository import LocalGitRepositoryFactory
from tests.utils.local_package import LocalPackageFactory


@dataclass(frozen=True)
class LifecycleScenario:
    """One isolated installed-CLI lifecycle workspace."""

    isolated: IsolatedApmEnvironment
    environment: dict[str, str]
    sources: LocalPackageFactory
    consumers: LocalPackageFactory
    repositories: LocalGitRepositoryFactory
    runner: ApmLifecycleRunner


def new_lifecycle_scenario(root: Path, apm_binary_path: Path) -> LifecycleScenario:
    """Create one isolated installed-CLI scenario under *root*."""
    isolated = IsolatedApmEnvironment.create(root, base_env=dict(os.environ))
    environment = isolated.subprocess_env()
    return LifecycleScenario(
        isolated=isolated,
        environment=environment,
        sources=LocalPackageFactory(isolated.package_root),
        consumers=LocalPackageFactory(isolated.work_root),
        repositories=LocalGitRepositoryFactory(
            isolated.repository_root,
            env=environment,
        ),
        runner=ApmLifecycleRunner(
            (str(apm_binary_path),),
            timeout_seconds=90,
            scenario_timeout_seconds=300,
        ),
    )
