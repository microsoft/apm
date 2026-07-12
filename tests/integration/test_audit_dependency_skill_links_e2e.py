"""End-to-end audit replay coverage for dependency skill links."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

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


def _write_project(project: Path) -> None:
    """Create a project with a nested local dependency skill."""
    project.mkdir()
    (project / ".claude").mkdir()
    (project / "apm.yml").write_text(
        """name: dependency-link-consumer
version: 1.0.0
target: claude
dependencies:
  apm:
    - path: ./package
""",
        encoding="utf-8",
    )

    package = project / "package"
    package.mkdir()
    (package / "apm.yml").write_text(
        """name: linked-skill-package
version: 1.0.0
description: Dependency skill link audit proof
author: Test
""",
        encoding="utf-8",
    )
    (package / "MANIFESTO.md").write_text("# Manifesto\n", encoding="utf-8")
    skill = package / ".apm" / "skills" / "demo" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        """---
name: demo
description: Dependency skill with a package-root link
---
# Demo

See [the manifesto](../../../MANIFESTO.md).
""",
        encoding="utf-8",
    )


@pytest.mark.integration
def test_audit_is_clean_after_installing_dependency_skill_with_relative_link(
    tmp_path: Path,
) -> None:
    """Replay must preserve the install-time dependency path in skill links."""
    project = tmp_path / "project"
    _write_project(project)

    install = _run(project, "install")
    assert install.returncode == 0, (
        f"install stdout:\n{install.stdout}\ninstall stderr:\n{install.stderr}"
    )

    deployed = project / ".claude" / "skills" / "demo" / "SKILL.md"
    assert "../../../apm_modules/_local/package/MANIFESTO.md" in deployed.read_text(
        encoding="utf-8"
    )

    audit = _run(project, "audit", "--ci", "--no-policy")
    assert audit.returncode == 0, f"audit stdout:\n{audit.stdout}\naudit stderr:\n{audit.stderr}"
