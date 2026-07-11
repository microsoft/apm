"""End-to-end regression coverage for dev dependency orphan detection."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from apm_cli.deps.lockfile import LockFile

CLI = [sys.executable, "-m", "apm_cli.cli"]
TIMEOUT = 180


def _run(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run the APM CLI in *cwd* and return the completed subprocess."""
    return subprocess.run(
        CLI + list(args),
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=TIMEOUT,
        check=False,
    )


@pytest.mark.integration
def test_dev_dependency_survives_audit_and_prune(tmp_path: Path) -> None:
    """A clean install must not classify its dev dependency as orphaned."""
    package = tmp_path / "dev-package"
    package.mkdir()
    (package / "apm.yml").write_text(
        yaml.safe_dump({"name": "dev-package", "version": "1.0.0"}),
        encoding="utf-8",
    )

    project = tmp_path / "consumer"
    project.mkdir()
    (project / "apm.yml").write_text(
        yaml.safe_dump(
            {
                "name": "consumer",
                "version": "1.0.0",
                "target": "copilot",
                "devDependencies": {"apm": [{"path": "../dev-package"}]},
            }
        ),
        encoding="utf-8",
    )

    install = _run(project, "install")
    assert install.returncode == 0, (
        f"install stdout:\n{install.stdout}\ninstall stderr:\n{install.stderr}"
    )

    installed_package = project / "apm_modules" / "_local" / "dev-package"
    assert installed_package.is_dir()
    lockfile = LockFile.read(project / "apm.lock.yaml")
    assert lockfile is not None
    assert len(lockfile.dependencies) == 1
    locked_dependency = next(iter(lockfile.dependencies.values()))
    assert locked_dependency.is_dev is True

    audit = _run(project, "audit", "--ci")
    assert audit.returncode == 0, f"audit stdout:\n{audit.stdout}\naudit stderr:\n{audit.stderr}"

    prune = _run(project, "prune")
    assert prune.returncode == 0, f"prune stdout:\n{prune.stdout}\nprune stderr:\n{prune.stderr}"
    assert installed_package.is_dir(), (
        "apm prune removed a package declared under devDependencies.apm"
    )
