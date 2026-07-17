"""Owned GitHub-shaped package fixture for hermetic packaged-binary tests."""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_git_repository import LocalGitRepositoryFactory
from tests.utils.local_package import LocalPackage, LocalPackageFactory

DEPENDENCY = "microsoft/apm-sample-package"
REMOTE_URL = f"https://github.com/{DEPENDENCY}"


@dataclass(frozen=True)
class HermeticPackagedSample:
    """A consumer and local origin exposed as the sample package's GitHub URL."""

    project: LocalPackage
    environment: dict[str, str]
    runner: ApmLifecycleRunner

    def run(self, args: Sequence[str], *, scenario_id: str) -> CommandResult:
        """Run one packaged-binary command against the isolated consumer."""
        return self.runner.run(
            args,
            scenario_id=scenario_id,
            cwd=self.project.root,
            env=self.environment,
        )


def create_hermetic_packaged_sample(
    root: Path,
    *,
    apm_binary_path: Path,
    project_name: str,
) -> HermeticPackagedSample:
    """Create a realistic source package behind an owned GitHub-shaped remote."""
    # Inject deliberately-unusable ("POISONED") values for every APM credential
    # variable. IsolatedApmEnvironment.create strips them all (asserted below), proving
    # the packaged binary reaches the owned origin with no usable credentials -- only via
    # the file:// rewrite.
    inherited_environment = {
        **os.environ,
        "GITHUB_APM_PAT": "POISONED_DO_NOT_USE_GITHUB_APM_PAT",
        "GITHUB_TOKEN": "POISONED_DO_NOT_USE_GITHUB_TOKEN",
        "GH_TOKEN": "POISONED_DO_NOT_USE_GH_TOKEN",
        "ADO_APM_PAT": "POISONED_DO_NOT_USE_ADO_APM_PAT",
    }
    isolated = IsolatedApmEnvironment.create(root / "isolated", base_env=inherited_environment)
    base_environment = isolated.subprocess_env()
    # Fail loudly if the isolation contract regresses: no APM auth variable may survive,
    # and only file:// Git transport is permitted. GIT_ALLOW_PROTOCOL=file (not the
    # Python-only PYTHONPATH network guard, which the PyInstaller binary ignores) is the
    # load-bearing fail-closed gate for the packaged binary.
    assert {
        "GITHUB_APM_PAT",
        "GITHUB_TOKEN",
        "GH_TOKEN",
        "ADO_APM_PAT",
    }.isdisjoint(base_environment)
    assert base_environment["GIT_ALLOW_PROTOCOL"] == "file"
    packages = LocalPackageFactory(isolated.package_root)
    source = packages.create("apm-sample-package", targets=("copilot",))
    packages.add_prompt(
        source,
        "fixture-prompt",
        "---\ndescription: Hermetic packaged prompt\n---\n# Fixture prompt\n",
    )
    packages.add_agent(
        source,
        "fixture-agent",
        "---\nname: fixture-agent\ndescription: Hermetic packaged agent\n---\n# Fixture agent\n",
    )
    packages.add_instruction(
        source,
        "fixture-instruction",
        (
            "---\napplyTo: '**'\ndescription: Hermetic packaged instruction\n---\n"
            "# Fixture instruction\n"
        ),
    )
    packages.add_skill(
        source,
        "fixture-skill",
        "---\nname: fixture-skill\ndescription: Hermetic packaged skill\n---\n# Fixture skill\n",
    )

    repositories = LocalGitRepositoryFactory(
        isolated.repository_root,
        env=base_environment,
    )
    repository = repositories.create(source.name, source_tree=source.root)
    repositories.commit(repository, message="seed hermetic packaged sample")
    environment = repositories.url_rewrite_subprocess_env(repository, REMOTE_URL)
    # Two rewrite rules (bare + bare.git form) prove the GitHub-shaped URL is redirected
    # to the owned local origin rather than reaching github.com.
    assert environment["GIT_CONFIG_COUNT"] == "2"

    project = LocalPackageFactory(isolated.work_root).create(
        project_name,
        dependencies=(
            {
                "git": REMOTE_URL,
            },
        ),
        targets=("copilot",),
    )
    # 240s per-scenario cap (below the 300s runner default): a hermetic file:// install
    # has no network variance, so a scenario exceeding 240s signals a real regression
    # rather than transient slowness.
    return HermeticPackagedSample(
        project=project,
        environment=environment,
        runner=ApmLifecycleRunner((str(apm_binary_path),), scenario_timeout_seconds=240),
    )
