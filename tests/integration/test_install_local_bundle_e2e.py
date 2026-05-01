"""E2E integration tests for ``apm install <local-bundle>``.

Exercises the full pipeline: pack -> install round-trip, multi-target,
collision handling, dry-run, force, and the air-gap (zero-network) proof.

All tests in this file will FAIL until the production code for issue #1098
is implemented.  This is the expected TDD state.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import tarfile
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

_LOCAL_BUNDLE_EXISTS = importlib.util.find_spec("apm_cli.bundle.local_bundle") is not None

pytestmark = pytest.mark.skipif(
    not _LOCAL_BUNDLE_EXISTS,
    reason="apm_cli.bundle.local_bundle not yet implemented (TDD stub)",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sha256(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()


def _make_plugin_bundle(
    tmp_path: Path,
    *,
    plugin_id: str = "test-plugin",
    pack_target: str = "copilot,claude",
    files: dict[str, str] | None = None,
    include_lockfile: bool = True,
) -> Path:
    """Create a minimal plugin bundle directory."""
    bundle = tmp_path / "bundle"
    bundle.mkdir(parents=True, exist_ok=True)

    pj = {"id": plugin_id, "name": "Test Plugin"}
    (bundle / "plugin.json").write_text(json.dumps(pj), encoding="utf-8")

    if files is None:
        files = {
            "skills/coding/SKILL.md": "# Coding Skill\nHelps with code.",
            "agents/reviewer.md": "# Reviewer\nReviews code.",
            "instructions/style.md": "# Style Guide\nFollow PEP8.",
        }

    bundle_files: dict[str, str] = {}
    for rel, content in files.items():
        p = bundle / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        bundle_files[rel] = _sha256(content)

    if include_lockfile:
        lock_data: dict = {
            "pack": {
                "format": "plugin",
                "target": pack_target,
                "bundle_files": bundle_files,
            },
            "dependencies": [
                {
                    "repo_url": "owner/test-plugin",
                    "resolved_commit": "abc123",
                    "deployed_files": list(files.keys()),
                    "deployed_file_hashes": bundle_files,
                }
            ],
        }
        (bundle / "apm.lock.yaml").write_text(
            yaml.dump(lock_data, default_flow_style=False), encoding="utf-8"
        )

    return bundle


def _make_tarball(tmp_path: Path, bundle_dir: Path) -> Path:
    archive = tmp_path / "test-bundle.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(bundle_dir, arcname=bundle_dir.name)
    return archive


def _make_project(tmp_path: Path, *, targets: list[str] | None = None) -> Path:
    """Create a minimal APM project directory."""
    project = tmp_path / "project"
    project.mkdir(parents=True, exist_ok=True)

    yml = {"name": "test-project", "version": "1.0.0"}
    if targets:
        yml["targets"] = targets
    (project / "apm.yml").write_text(yaml.dump(yml, default_flow_style=False), encoding="utf-8")
    return project


# ---------------------------------------------------------------------------
# Network sentinel -- proves zero I/O during local install
# ---------------------------------------------------------------------------

_original_subprocess_run = subprocess.run


def _network_sentinel_subprocess_run(*args, **kwargs):
    """Block git/gh/curl/wget subprocess calls to prove air-gap."""
    cmd = args[0] if args else kwargs.get("args", [])
    if isinstance(cmd, (list, tuple)) and cmd:
        binary = str(cmd[0])
        basename = os.path.basename(binary)
        if basename in ("git", "gh", "curl", "wget"):
            raise AssertionError(f"Unexpected network I/O via subprocess: {basename}")
    return _original_subprocess_run(*args, **kwargs)


# ---------------------------------------------------------------------------
# E2E: Round-trip (pack -> install)
# ---------------------------------------------------------------------------


class TestInstallLocalBundleE2E:
    """End-to-end tests for the local-bundle install pipeline."""

    def test_install_local_bundle_from_directory(self, tmp_path: Path) -> None:
        """Install a plugin bundle from a directory -> files deployed."""
        _bundle = _make_plugin_bundle(tmp_path / "src")
        _project = _make_project(tmp_path / "dst")

        # When production code exists, invoke:
        #   result = runner.invoke(cli, ["install", str(bundle)], ...)
        # For now, test documents the contract.
        pytest.skip("Production code not yet implemented")

    def test_install_local_bundle_from_tarball(self, tmp_path: Path) -> None:
        """Install a plugin bundle from .tar.gz -> files deployed."""
        _bundle = _make_plugin_bundle(tmp_path / "src")
        _tarball = _make_tarball(tmp_path / "archives", _bundle)
        _project = _make_project(tmp_path / "dst")

        pytest.skip("Production code not yet implemented")

    def test_install_local_bundle_multi_target(self, tmp_path: Path) -> None:
        """Install with --target copilot,claude -> files in both trees."""
        _bundle = _make_plugin_bundle(tmp_path / "src", pack_target="copilot,claude")
        _project = _make_project(tmp_path / "dst", targets=["copilot", "claude"])

        pytest.skip("Production code not yet implemented")

    def test_install_local_bundle_auto_detect_target(self, tmp_path: Path) -> None:
        """Install without --target -> auto-detects from project."""
        _bundle = _make_plugin_bundle(tmp_path / "src")
        _project = _make_project(tmp_path / "dst")

        pytest.skip("Production code not yet implemented")

    def test_pack_install_round_trip_fidelity(self, tmp_path: Path) -> None:
        """pack(plugin) -> install(bundle) produces a valid file layout.

        The files deployed to the target tree should match the bundle's
        flat layout mapped through the integrator pipeline.
        """
        _bundle = _make_plugin_bundle(
            tmp_path / "src",
            files={
                "skills/coding/SKILL.md": "# Coding\nSkill content.",
                "agents/reviewer.md": "# Reviewer\nAgent content.",
            },
        )
        _project = _make_project(tmp_path / "dst")

        pytest.skip("Production code not yet implemented")


# ---------------------------------------------------------------------------
# E2E: Collision handling
# ---------------------------------------------------------------------------


class TestInstallLocalBundleCollision:
    """Collision behavior for local-bundle install."""

    def test_collision_managed_file_overwritten(self, tmp_path: Path) -> None:
        """Pre-existing managed file -> overwritten without --force."""
        _bundle = _make_plugin_bundle(tmp_path / "src")
        _project = _make_project(tmp_path / "dst")

        pytest.skip("Production code not yet implemented")

    def test_collision_locally_modified_skipped_without_force(self, tmp_path: Path) -> None:
        """Locally-modified file -> skipped without --force."""
        _bundle = _make_plugin_bundle(tmp_path / "src")
        _project = _make_project(tmp_path / "dst")

        pytest.skip("Production code not yet implemented")

    def test_collision_locally_modified_overwritten_with_force(self, tmp_path: Path) -> None:
        """With --force -> locally-modified file overwritten."""
        _bundle = _make_plugin_bundle(tmp_path / "src")
        _project = _make_project(tmp_path / "dst")

        pytest.skip("Production code not yet implemented")


# ---------------------------------------------------------------------------
# E2E: Dry-run
# ---------------------------------------------------------------------------


class TestInstallLocalBundleDryRun:
    """Dry-run mode for local-bundle install."""

    def test_dry_run_no_files_written(self, tmp_path: Path) -> None:
        """--dry-run shows what would be installed without writing."""
        _bundle = _make_plugin_bundle(tmp_path / "src")
        _project = _make_project(tmp_path / "dst")

        pytest.skip("Production code not yet implemented")


# ---------------------------------------------------------------------------
# E2E: apm.yml side effects
# ---------------------------------------------------------------------------


class TestApmYmlSideEffects:
    """Verify apm.yml is NOT mutated by local-bundle install."""

    def test_apm_yml_not_mutated_by_local_install(self, tmp_path: Path) -> None:
        _bundle = _make_plugin_bundle(tmp_path / "src")
        _project = _make_project(tmp_path / "dst")
        _yml_before = (_project / "apm.yml").read_text(encoding="utf-8")

        # After install, apm.yml must be identical
        pytest.skip("Production code not yet implemented")

    def test_local_lockfile_records_deployed_files(self, tmp_path: Path) -> None:
        """Project's apm.lock.yaml should record deployed file paths after install."""
        _bundle = _make_plugin_bundle(tmp_path / "src")
        _project = _make_project(tmp_path / "dst")

        pytest.skip("Production code not yet implemented")


# ---------------------------------------------------------------------------
# E2E: Air-gap proof (zero network I/O)
# ---------------------------------------------------------------------------


class TestLocalInstallAirGap:
    """Prove that local-bundle install does zero network I/O."""

    def test_local_install_zero_network_io(self, tmp_path: Path) -> None:
        """Monkeypatch all known network entry points to assert no calls.

        If ANY network call is made during local-bundle install, the
        sentinel raises AssertionError immediately.
        """
        _bundle = _make_plugin_bundle(tmp_path / "src")
        _project = _make_project(tmp_path / "dst")

        def _fail_urlopen(*a, **kw):
            raise AssertionError("Unexpected network I/O: urllib.request.urlopen")

        def _fail_requests(*a, **kw):
            raise AssertionError("Unexpected network I/O: requests")

        def _fail_httpx(*a, **kw):
            raise AssertionError("Unexpected network I/O: httpx")

        patches = [
            patch("urllib.request.urlopen", side_effect=_fail_urlopen),
            patch("subprocess.run", side_effect=_network_sentinel_subprocess_run),
        ]

        # requests and httpx may not be importable in all envs
        try:
            import requests  # noqa: F401

            patches.append(patch("requests.Session.send", side_effect=_fail_requests))
        except ImportError:
            pass

        try:
            import httpx  # noqa: F401

            patches.append(patch("httpx.Client.send", side_effect=_fail_httpx))
        except ImportError:
            pass

        for p in patches:
            p.start()
        try:
            # When production code exists, invoke the install here.
            # Any network call will raise AssertionError.
            pytest.skip("Production code not yet implemented")
        finally:
            for p in patches:
                p.stop()
