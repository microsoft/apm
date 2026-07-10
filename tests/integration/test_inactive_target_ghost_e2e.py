"""Hermetic install/audit regression for inactive-target lockfile ghosts."""

from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

import yaml
from click.testing import CliRunner

from apm_cli.cli import cli
from apm_cli.models.apm_package import (
    APMPackage,
    GitReferenceType,
    PackageInfo,
    ResolvedReference,
)
from apm_cli.models.dependency.reference import DependencyReference

GHOST = ".windsurf/rules/demo.md"
PACKAGE_REF = "acme/fixture-package"
_PATCH_UPDATES = "apm_cli.commands._helpers.check_for_updates"


class _HermeticDownloader:
    """Materialize a stable package while keeping network I/O outside the test."""

    def download_package(
        self, repo_ref: object, target_path: Path, *args: Any, **kwargs: Any
    ) -> PackageInfo:
        """Write the fixture package into the install cache."""
        dep_ref = (
            repo_ref
            if isinstance(repo_ref, DependencyReference)
            else DependencyReference.parse(str(repo_ref))
        )
        target_path = Path(target_path)
        target_path.mkdir(parents=True, exist_ok=True)
        (target_path / "apm.yml").write_text(
            """name: fixture-package
version: 1.0.0
description: Inactive target ghost regression fixture
""",
            encoding="utf-8",
        )
        instructions = target_path / ".apm" / "instructions"
        instructions.mkdir(parents=True, exist_ok=True)
        (instructions / "demo.instructions.md").write_text(
            "---\ndescription: Ghost regression fixture\napplyTo: '**'\n---\n# Demo\n",
            encoding="utf-8",
        )
        return PackageInfo(
            package=APMPackage.from_apm_yml(target_path / "apm.yml"),
            install_path=target_path,
            installed_at=datetime.now().isoformat(),
            dependency_ref=dep_ref,
            resolved_reference=ResolvedReference(
                original_ref="main",
                ref_type=GitReferenceType.BRANCH,
                resolved_commit="a" * 40,
                ref_name="main",
            ),
        )


def _write_fixture(workspace: Path) -> Path:
    """Create the consumer manifest used to seed the committed lockfile."""
    consumer = workspace / "seed-consumer"
    consumer.mkdir(parents=True)
    (consumer / "apm.yml").write_text(
        """name: inactive-target-ghost-consumer
version: 1.0.0
targets:
  - copilot
  - windsurf
dependencies:
  apm:
    - acme/fixture-package
""",
        encoding="utf-8",
    )
    return consumer


def _invoke(
    runner: CliRunner,
    project: Path,
    monkeypatch,
    *args: str,
):
    """Invoke the user-facing CLI from the fixture project."""
    monkeypatch.chdir(project)
    with patch(_PATCH_UPDATES, return_value=None):
        return runner.invoke(cli, list(args), catch_exceptions=False)


def test_install_repairs_ghost_before_fresh_checkout_audit(tmp_path: Path, monkeypatch) -> None:
    """A fresh checkout becomes audit-clean after one ordinary install."""
    workspace = tmp_path / "workspace"
    seed = _write_fixture(workspace)

    from apm_cli.deps import github_downloader

    downloader = _HermeticDownloader()
    monkeypatch.setattr(
        github_downloader.GitHubPackageDownloader,
        "download_package",
        downloader.download_package,
    )
    runner = CliRunner()

    initial_install = _invoke(runner, seed, monkeypatch, "install", "--target", "copilot,windsurf")
    assert initial_install.exit_code == 0, initial_install.output
    seed_lock = yaml.safe_load((seed / "apm.lock.yaml").read_text(encoding="utf-8"))
    seeded_deployed = (seed_lock.get("dependencies") or [])[0].get("deployed_files") or []
    assert GHOST in seeded_deployed
    (seed / "apm.yml").write_text(
        (seed / "apm.yml").read_text(encoding="utf-8").replace("  - windsurf\n", ""),
        encoding="utf-8",
    )

    checkout = workspace / "fresh-checkout"
    checkout.mkdir()
    shutil.copy2(seed / "apm.yml", checkout / "apm.yml")
    shutil.copy2(seed / "apm.lock.yaml", checkout / "apm.lock.yaml")

    failing_audit = _invoke(
        runner, checkout, monkeypatch, "audit", "--ci", "--no-policy", "--no-fail-fast"
    )
    assert failing_audit.exit_code != 0
    assert GHOST in failing_audit.output

    repair = _invoke(runner, checkout, monkeypatch, "install", "--target", "copilot", "--verbose")
    assert repair.exit_code == 0, repair.output
    assert f"Dropped inactive-target lockfile entry {GHOST}" in repair.output

    repaired_lock = yaml.safe_load((checkout / "apm.lock.yaml").read_text(encoding="utf-8"))
    deployed = (repaired_lock.get("dependencies") or [])[0].get("deployed_files") or []
    assert GHOST not in deployed
    assert GHOST not in (repaired_lock.get("local_deployed_files") or [])

    clean_audit = _invoke(
        runner, checkout, monkeypatch, "audit", "--ci", "--no-policy", "--no-fail-fast"
    )
    assert clean_audit.exit_code == 0, clean_audit.output
