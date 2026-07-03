"""End-to-end coverage for target-scoped local path dependencies.

Issue #1982 regressed at the model-to-install boundary: object-form local
path dependencies accepted ``targets:`` but failed to carry that subset into
the install pipeline. This hermetic test builds a consumer and a sibling
package on disk, runs the real install and compile commands, and verifies a
dependency scoped to Copilot never deploys its instructions to Claude.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
import yaml

CLI = [sys.executable, "-m", "apm_cli.cli"]
TIMEOUT = 180

DEPENDENCY_INSTRUCTION = """---
description: Local dependency target subset proof
applyTo: '**/*.py'
---
# Local target subset proof
This instruction must deploy only to Copilot.
"""
DEPENDENCY_SENTINEL = "This instruction must deploy only to Copilot."


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


def _write_consumer(consumer: Path) -> None:
    """Create a consumer manifest with a target-scoped local path dependency."""
    consumer.mkdir(parents=True)
    (consumer / ".github").mkdir()
    (consumer / ".claude").mkdir()
    (consumer / "apm.yml").write_text(
        """name: local-path-targets-consumer
version: 1.0.0
targets:
  - copilot
  - claude
dependencies:
  apm:
    - path: ../targeted-package
      targets:
        - copilot
""",
        encoding="utf-8",
    )


def _write_dependency(package_dir: Path) -> None:
    """Create the sibling package whose primitives would deploy to both targets."""
    package_dir.mkdir(parents=True)
    (package_dir / "apm.yml").write_text(
        """name: targeted-package
version: 1.0.0
description: Local path dependency target subset proof
author: Test
""",
        encoding="utf-8",
    )
    instructions_dir = package_dir / ".apm" / "instructions"
    instructions_dir.mkdir(parents=True)
    (instructions_dir / "local-target.instructions.md").write_text(
        DEPENDENCY_INSTRUCTION,
        encoding="utf-8",
    )


@pytest.fixture
def local_path_targets_workspace(tmp_path: Path) -> Path:
    """Build workspace/{consumer,targeted-package} with a path dependency."""
    workspace = tmp_path / "workspace"
    consumer = workspace / "consumer"
    dependency = workspace / "targeted-package"
    _write_consumer(consumer)
    _write_dependency(dependency)
    return consumer


@pytest.mark.integration
def test_path_dependency_targets_deploy_only_to_declared_target(
    local_path_targets_workspace: Path,
) -> None:
    """A path dependency with targets: [copilot] must not deploy to Claude."""
    consumer = local_path_targets_workspace

    install_res = _run(consumer, "install", "--target", "copilot,claude")
    assert install_res.returncode == 0, (
        f"install stdout:\n{install_res.stdout}\ninstall stderr:\n{install_res.stderr}"
    )
    lockfile = yaml.safe_load((consumer / "apm.lock.yaml").read_text(encoding="utf-8"))
    locked_deps = lockfile.get("dependencies") or []
    assert any(dep.get("target_subset") == ["copilot"] for dep in locked_deps), (
        "Lockfile must preserve the dependency-level target_subset for audit "
        f"and replay. Lockfile dependencies: {locked_deps}"
    )

    compile_res = _run(consumer, "compile", "--target", "copilot,claude")
    assert compile_res.returncode == 0, (
        f"compile stdout:\n{compile_res.stdout}\ncompile stderr:\n{compile_res.stderr}"
    )

    copilot_instruction = consumer / ".github" / "instructions" / "local-target.instructions.md"
    assert copilot_instruction.exists(), (
        "Copilot is in the dependency-level targets subset and must receive "
        "the local dependency instruction."
    )
    assert DEPENDENCY_SENTINEL in copilot_instruction.read_text(encoding="utf-8")

    claude_rules_dir = consumer / ".claude" / "rules"
    claude_rule = claude_rules_dir / "local-target.md"
    assert not claude_rule.exists(), (
        "Claude is an active install target but is outside the dependency-level "
        "targets subset, so the local dependency instruction must not deploy "
        f"there. Install stdout:\n{install_res.stdout}\nInstall stderr:\n{install_res.stderr}"
    )
    if claude_rules_dir.exists():
        deployed_rule_names = sorted(path.name for path in claude_rules_dir.glob("*.md"))
        assert "local-target.md" not in deployed_rule_names
