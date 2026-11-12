"""End-to-end regression coverage for target-specific instruction contraction."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

TIMEOUT = 180
_RULE_NAME = "scope"
_CLAUDE_RULE = Path(".claude/rules") / f"{_RULE_NAME}.md"
_CURSOR_RULE = Path(".cursor/rules") / f"{_RULE_NAME}.mdc"


def _run(apm_binary_path: Path, cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run the installed CLI in *cwd* and capture its result."""
    return subprocess.run(
        [str(apm_binary_path), *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=TIMEOUT,
        check=False,
    )


def _write_consumer(project: Path, targets: list[str]) -> None:
    """Write a consumer manifest that depends on the adjacent fixture package."""
    project.mkdir(parents=True, exist_ok=True)
    manifest = {
        "name": "target-instruction-contraction-consumer",
        "version": "1.0.0",
        "targets": targets,
        "dependencies": {"apm": [{"path": "../lifecycle-package"}]},
    }
    (project / "apm.yml").write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")


def _write_lifecycle_package(package: Path) -> None:
    """Write the local package whose instruction materializes per target."""
    package.mkdir(parents=True, exist_ok=True)
    (package / "apm.yml").write_text(
        """name: lifecycle-package
version: 1.0.0
description: Target-specific instruction contraction fixture
""",
        encoding="utf-8",
    )
    instructions = package / ".apm" / "instructions"
    instructions.mkdir(parents=True, exist_ok=True)
    (instructions / f"{_RULE_NAME}.instructions.md").write_text(
        """---
description: Target-specific instruction contraction fixture
applyTo: '**/*.py'
---
# Scope rule
This rule is deployed only to active targets.
""",
        encoding="utf-8",
    )


def _deployed_files(project: Path) -> list[str]:
    """Return the fixture package's current lockfile paths."""
    lockfile = yaml.safe_load((project / "apm.lock.yaml").read_text(encoding="utf-8"))
    dependencies = lockfile.get("dependencies") or []
    return list(dependencies[0].get("deployed_files") or [])


@pytest.mark.integration
def test_widen_then_narrow_removes_dropped_cursor_instruction(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """Claude -> Claude+Cursor -> Claude removes only Cursor's managed rule."""
    workspace = tmp_path / "workspace"
    consumer = workspace / "consumer"
    package = workspace / "lifecycle-package"
    _write_consumer(consumer, ["claude"])
    _write_lifecycle_package(package)

    initial = _run(apm_binary_path, consumer, "install", "--target", "claude", "--no-policy")
    assert initial.returncode == 0, initial.stderr
    assert (consumer / _CLAUDE_RULE).is_file()
    assert not (consumer / _CURSOR_RULE).exists()

    _write_consumer(consumer, ["claude", "cursor"])
    widened = _run(
        apm_binary_path,
        consumer,
        "install",
        "--target",
        "claude,cursor",
        "--no-policy",
    )
    assert widened.returncode == 0, widened.stderr
    assert (consumer / _CLAUDE_RULE).is_file()
    assert (consumer / _CURSOR_RULE).is_file()

    shared_skill = consumer / ".agents" / "skills" / "scope" / "SKILL.md"
    shared_skill.parent.mkdir(parents=True)
    shared_skill.write_text("# Shared user content\n", encoding="utf-8")

    _write_consumer(consumer, ["claude"])
    narrowed = _run(apm_binary_path, consumer, "install", "--target", "claude", "--no-policy")
    assert narrowed.returncode == 0, narrowed.stderr

    assert (consumer / _CLAUDE_RULE).is_file()
    assert not (consumer / _CURSOR_RULE).exists()
    assert shared_skill.read_text(encoding="utf-8") == "# Shared user content\n"
    assert _CLAUDE_RULE.as_posix() in _deployed_files(consumer)
    assert _CURSOR_RULE.as_posix() not in _deployed_files(consumer)

    pruned = _run(apm_binary_path, consumer, "prune")
    assert pruned.returncode == 0, pruned.stderr
    assert (consumer / _CLAUDE_RULE).is_file()
    assert not (consumer / _CURSOR_RULE).exists()

    audit = _run(apm_binary_path, consumer, "audit", "--ci", "--no-policy")
    assert audit.returncode == 0, audit.stdout + audit.stderr


@pytest.mark.integration
def test_narrowing_preserves_a_user_edited_cursor_instruction(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """A changed dropped-target rule survives the cleanup provenance gate."""
    workspace = tmp_path / "workspace"
    consumer = workspace / "consumer"
    package = workspace / "lifecycle-package"
    _write_consumer(consumer, ["claude", "cursor"])
    _write_lifecycle_package(package)

    initial = _run(
        apm_binary_path,
        consumer,
        "install",
        "--target",
        "claude,cursor",
        "--no-policy",
    )
    assert initial.returncode == 0, initial.stderr
    cursor_rule = consumer / _CURSOR_RULE
    original = cursor_rule.read_text(encoding="utf-8")
    edited = original + "\n# User edit\n"
    cursor_rule.write_text(edited, encoding="utf-8")

    _write_consumer(consumer, ["claude"])
    narrowed = _run(apm_binary_path, consumer, "install", "--target", "claude", "--no-policy")
    assert narrowed.returncode == 0, narrowed.stderr

    assert (consumer / _CLAUDE_RULE).is_file()
    assert cursor_rule.read_text(encoding="utf-8") == edited
    assert _CURSOR_RULE.as_posix() in _deployed_files(consumer)
