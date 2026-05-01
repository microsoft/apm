"""Unit tests asserting lockfile + pack metadata in plugin export output.

Tests that ``plugin_exporter.export_plugin_bundle()`` produces bundles with:
- ``apm.lock.yaml`` present at bundle root
- ``pack.target`` populated with the effective target(s)
- ``pack.bundle_files`` populated with SHA-256 hashes matching actual files

These tests will FAIL until the production changes to ``plugin_exporter.py``
(Change 1 from the design doc) are implemented.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
import yaml

from apm_cli.bundle.plugin_exporter import export_plugin_bundle
from apm_cli.deps.lockfile import LockedDependency, LockFile

# ---------------------------------------------------------------------------
# Helpers (reuse patterns from test_plugin_exporter.py)
# ---------------------------------------------------------------------------


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def _write_apm_yml(
    project: Path,
    *,
    name: str = "test-pkg",
    version: str = "1.0.0",
    deps: dict | None = None,
) -> Path:
    data: dict = {"name": name, "version": version}
    if deps:
        data["dependencies"] = deps
    path = project / "apm.yml"
    path.write_text(yaml.dump(data), encoding="utf-8")
    return path


def _write_lockfile(
    project: Path,
    deps: list[LockedDependency] | None = None,
) -> Path:
    lockfile = LockFile()
    for d in deps or []:
        lockfile.add_dependency(d)
    lockfile.write(project / "apm.lock.yaml")
    return project / "apm.lock.yaml"


def _setup_project_with_skill(tmp_path: Path, *, target: str = "copilot") -> Path:
    """Create a minimal project with one skill for packing."""
    project = tmp_path / "project"
    project.mkdir()

    _write_apm_yml(
        project,
        deps={"owner/test-skill": "main"},
    )

    # Simulate installed skill
    skill_dir = project / ".apm" / "skills" / "coding"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# Coding\nA skill.", encoding="utf-8")

    # Agent
    agent_dir = project / ".apm" / "agents"
    agent_dir.mkdir(parents=True)
    (agent_dir / "reviewer.md").write_text("# Reviewer", encoding="utf-8")

    # Create deployed files in target tree for lockfile
    gh_skill = project / ".github" / "skills" / "coding"
    gh_skill.mkdir(parents=True)
    (gh_skill / "SKILL.md").write_text("# Coding\nA skill.", encoding="utf-8")

    dep = LockedDependency(
        repo_url="owner/test-skill",
        resolved_commit="abc123",
        deployed_files=[
            ".github/skills/coding/SKILL.md",
        ],
        deployed_file_hashes={
            ".github/skills/coding/SKILL.md": _sha256_file(gh_skill / "SKILL.md"),
        },
    )
    _write_lockfile(project, [dep])

    return project


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestPluginExportIncludesLockfile:
    """Assert that plugin bundles include apm.lock.yaml after Change 1."""

    def test_plugin_export_includes_lockfile(self, tmp_path: Path) -> None:
        """apm.lock.yaml MUST be present at bundle root."""
        project = _setup_project_with_skill(tmp_path)
        output = tmp_path / "output"
        output.mkdir()

        result = export_plugin_bundle(
            project_root=project,
            output_dir=output,
            target="copilot",
        )

        bundle_root = result.bundle_path
        lockfile_path = bundle_root / "apm.lock.yaml"

        # This assertion will FAIL until plugin_exporter.py is changed
        # to include apm.lock.yaml in plugin bundles.
        assert lockfile_path.exists(), (
            "apm.lock.yaml not found in plugin bundle output. "
            "Change 1 (include lockfile in plugin bundles) not yet implemented."
        )

    def test_plugin_export_lockfile_has_pack_target(self, tmp_path: Path) -> None:
        """pack.target must be populated with the effective target."""
        project = _setup_project_with_skill(tmp_path)
        output = tmp_path / "output"
        output.mkdir()

        result = export_plugin_bundle(
            project_root=project,
            output_dir=output,
            target="copilot",
        )

        bundle_root = result.bundle_path
        lockfile_path = bundle_root / "apm.lock.yaml"

        if not lockfile_path.exists():
            pytest.skip("apm.lock.yaml not in bundle -- Change 1 not implemented")

        lock_data = yaml.safe_load(lockfile_path.read_text(encoding="utf-8"))
        assert "pack" in lock_data, "pack: section missing from bundle lockfile"
        assert "target" in lock_data["pack"], "pack.target missing"
        assert lock_data["pack"]["target"] == "copilot"

    def test_plugin_export_lockfile_multi_target(self, tmp_path: Path) -> None:
        """IM11: when packing for multiple targets, ``pack.target`` must
        be a comma-joined STRING (per ``lockfile_enrichment.py:175``).
        Regression guard: any change to dict/list serialisation here would
        break ``check_target_mismatch`` and the bundle-format contract."""
        project = _setup_project_with_skill(tmp_path)
        # Make sure both target trees exist with deployed files so the
        # exporter can include them in either output mode.
        for tgt_root in (".github", ".claude"):
            d = project / tgt_root / "skills" / "coding"
            d.mkdir(parents=True, exist_ok=True)
            (d / "SKILL.md").write_text("# Coding\nA skill.", encoding="utf-8")

        output = tmp_path / "output"
        output.mkdir()

        result = export_plugin_bundle(
            project_root=project,
            output_dir=output,
            target="copilot,claude",
        )
        lockfile_path = result.bundle_path / "apm.lock.yaml"
        if not lockfile_path.exists():
            pytest.skip("apm.lock.yaml not in bundle")

        lock_data = yaml.safe_load(lockfile_path.read_text(encoding="utf-8"))
        target = lock_data.get("pack", {}).get("target")
        assert isinstance(target, str), (
            f"pack.target must be a string (comma-joined), got {type(target).__name__}: {target!r}"
        )
        # Order is canonical via lockfile_enrichment.py:175 (input order
        # preserved when caller supplies a comma-joined string).
        parts = [p.strip() for p in target.split(",")]
        assert "copilot" in parts and "claude" in parts, (
            f"Multi-target string missing expected parts: {target!r}"
        )

    def test_plugin_export_lockfile_has_bundle_files(self, tmp_path: Path) -> None:
        """pack.bundle_files must map bundle-relative paths to SHA-256 hashes
        matching the actual file contents in the bundle."""
        project = _setup_project_with_skill(tmp_path)
        output = tmp_path / "output"
        output.mkdir()

        result = export_plugin_bundle(
            project_root=project,
            output_dir=output,
            target="copilot",
        )

        bundle_root = result.bundle_path
        lockfile_path = bundle_root / "apm.lock.yaml"

        if not lockfile_path.exists():
            pytest.skip("apm.lock.yaml not in bundle -- Change 1 not implemented")

        lock_data = yaml.safe_load(lockfile_path.read_text(encoding="utf-8"))
        pack = lock_data.get("pack", {})

        # bundle_files section must exist
        assert "bundle_files" in pack, (
            "pack.bundle_files missing from bundle lockfile. "
            "Bundle file manifest not yet implemented."
        )

        bundle_files = pack["bundle_files"]
        assert len(bundle_files) > 0, "bundle_files is empty"

        # Every listed file must exist and hash must match
        for rel_path, expected_hash in bundle_files.items():
            file_path = bundle_root / rel_path
            assert file_path.exists(), f"Bundle file listed but missing: {rel_path}"
            actual_hash = _sha256_file(file_path)
            assert actual_hash == expected_hash, (
                f"Hash mismatch for {rel_path}: "
                f"expected {expected_hash[:12]}..., got {actual_hash[:12]}..."
            )
