"""End-to-end regression coverage for shared-root skill contraction.

A skill deployed for a non-Claude target (e.g. Cursor) materializes into the
shared ``.agents/skills/<name>/SKILL.md`` root, while the Claude target owns its
private ``.claude/skills/<name>/SKILL.md`` copy. When the active target set
narrows back to Claude only, the shared-root copy must be removed through the
canonical cleanup chokepoint -- not left on disk after its lockfile/ledger
ownership row has been reconciled away.

This pins the fix for the shared-root physical orphan: previously the narrow
install correctly dropped the ``.agents/skills`` ledger row (Cursor is no longer
active/declared) yet the file lingered on disk because the reconciled value was
never routed to :func:`remove_stale_deployed_files`. That left a physical orphan
with no lockfile row, invisible to every manifest-driven audit gate.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

TIMEOUT = 180
_SKILL_NAME = "scope"
_CLAUDE_SKILL = Path(".claude/skills") / _SKILL_NAME / "SKILL.md"
_SHARED_SKILL = Path(".agents/skills") / _SKILL_NAME / "SKILL.md"


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
        "name": "shared-root-skill-contraction-consumer",
        "version": "1.0.0",
        "targets": targets,
        "dependencies": {"apm": [{"path": "../lifecycle-package"}]},
    }
    (project / "apm.yml").write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")


def _write_lifecycle_package(package: Path) -> None:
    """Write the local package whose skill materializes per target."""
    package.mkdir(parents=True, exist_ok=True)
    (package / "apm.yml").write_text(
        """name: lifecycle-package
version: 1.0.0
description: Shared-root skill contraction fixture
""",
        encoding="utf-8",
    )
    skill_dir = package / ".apm" / "skills" / _SKILL_NAME
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        """---
name: scope
description: Shared-root skill contraction fixture
---
# Scope skill
This skill materializes into every active target's skill root.
""",
        encoding="utf-8",
    )


def _deployed_files(project: Path) -> list[str]:
    """Return the fixture package's current lockfile paths."""
    lockfile = yaml.safe_load((project / "apm.lock.yaml").read_text(encoding="utf-8"))
    dependencies = lockfile.get("dependencies") or []
    return list(dependencies[0].get("deployed_files") or [])


@pytest.mark.integration
def test_widen_then_narrow_removes_shared_root_skill_orphan(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """Claude -> Claude+Cursor -> Claude removes the shared-root skill copy."""
    workspace = tmp_path / "workspace"
    consumer = workspace / "consumer"
    package = workspace / "lifecycle-package"
    _write_consumer(consumer, ["claude"])
    _write_lifecycle_package(package)

    initial = _run(apm_binary_path, consumer, "install", "--target", "claude", "--no-policy")
    assert initial.returncode == 0, initial.stderr
    assert (consumer / _CLAUDE_SKILL).is_file()
    assert not (consumer / _SHARED_SKILL).exists()
    claude_bytes = (consumer / _CLAUDE_SKILL).read_text(encoding="utf-8")

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
    assert (consumer / _CLAUDE_SKILL).is_file()
    assert (consumer / _SHARED_SKILL).is_file()

    _write_consumer(consumer, ["claude"])
    narrowed = _run(apm_binary_path, consumer, "install", "--target", "claude", "--no-policy")
    assert narrowed.returncode == 0, narrowed.stderr

    # The shared-root skill copy for the dropped Cursor target is gone; the
    # Claude-owned copy survives byte-for-byte.
    assert not (consumer / _SHARED_SKILL).exists()
    assert (consumer / _CLAUDE_SKILL).is_file()
    assert (consumer / _CLAUDE_SKILL).read_text(encoding="utf-8") == claude_bytes

    # No stale ledger/manifest row is left behind for the removed shared copy.
    files = _deployed_files(consumer)
    assert _CLAUDE_SKILL.as_posix() in files
    assert _SHARED_SKILL.as_posix() not in files

    # Prune is idempotent and leaves the reconciled state untouched.
    pruned = _run(apm_binary_path, consumer, "prune")
    assert pruned.returncode == 0, pruned.stderr
    assert not (consumer / _SHARED_SKILL).exists()
    assert (consumer / _CLAUDE_SKILL).is_file()

    # The manifest-driven audit gate is clean -- no orphan blind spot remains.
    audit = _run(apm_binary_path, consumer, "audit", "--ci", "--no-policy")
    assert audit.returncode == 0, audit.stdout + audit.stderr


@pytest.mark.integration
def test_narrowing_preserves_a_user_edited_shared_root_skill(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """A user-edited shared-root skill survives the cleanup provenance gate."""
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
    shared = consumer / _SHARED_SKILL
    original = shared.read_text(encoding="utf-8")
    edited = original + "\n# User edit\n"
    shared.write_text(edited, encoding="utf-8")

    _write_consumer(consumer, ["claude"])
    narrowed = _run(apm_binary_path, consumer, "install", "--target", "claude", "--no-policy")
    assert narrowed.returncode == 0, narrowed.stderr

    # The provenance gate refuses to delete user-modified bytes, and the row is
    # retained so the surviving file stays covered by the audit gates.
    assert (consumer / _CLAUDE_SKILL).is_file()
    assert shared.read_text(encoding="utf-8") == edited
    assert _SHARED_SKILL.as_posix() in _deployed_files(consumer)
