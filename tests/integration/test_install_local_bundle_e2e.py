"""E2E integration tests for ``apm install <local-bundle>``.

Exercises the full pipeline: pack -> install round-trip, multi-target,
collision handling, dry-run, force, and the air-gap (zero-network) proof.

The production code under test lives in:
- ``apm_cli.bundle.local_bundle`` (detection / integrity)
- ``apm_cli.install.local_bundle_handler`` (install_local_bundle)
- ``apm_cli.install.services.integrate_local_bundle``
- ``apm_cli.commands.install`` (local-bundle early-exit + ``--as`` flag)
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tarfile
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from click.testing import CliRunner

from apm_cli.cli import cli

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
    tmp_path.mkdir(parents=True, exist_ok=True)
    archive = tmp_path / "test-bundle.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(bundle_dir, arcname=bundle_dir.name)
    return archive


def _make_project(tmp_path: Path, *, targets: list[str] | None = None) -> Path:
    """Create a minimal APM project directory."""
    project = tmp_path / "project"
    project.mkdir(parents=True, exist_ok=True)

    yml: dict = {"name": "test-project", "version": "1.0.0"}
    if targets:
        yml["targets"] = targets
    (project / "apm.yml").write_text(yaml.dump(yml, default_flow_style=False), encoding="utf-8")
    return project


def _invoke_install(
    project_dir: Path,
    bundle_arg: str,
    *extra_args: str,
    monkeypatch: pytest.MonkeyPatch,
) -> object:
    """Run ``apm install <bundle_arg> [extra_args...]`` inside *project_dir*.

    Returns the ``click.testing.Result``.  Uses ``catch_exceptions=False``
    so production-code bugs propagate as real test failures with full
    traceback (instead of being swallowed into ``result.exception``).
    """
    monkeypatch.chdir(project_dir)
    runner = CliRunner()
    return runner.invoke(
        cli,
        ["install", bundle_arg, *extra_args],
        catch_exceptions=False,
    )


# ---------------------------------------------------------------------------
# Network sentinel -- proves zero I/O during local install
# ---------------------------------------------------------------------------

_original_subprocess_run = subprocess.run
_original_subprocess_popen = subprocess.Popen


def _network_sentinel_subprocess_run(*args, **kwargs):
    """Block git/gh/curl/wget subprocess calls to prove air-gap."""
    cmd = args[0] if args else kwargs.get("args", [])
    if isinstance(cmd, (list, tuple)) and cmd:
        binary = str(cmd[0])
        basename = os.path.basename(binary)
        if basename in ("git", "gh", "curl", "wget"):
            raise AssertionError(f"Unexpected network I/O via subprocess: {basename}")
    elif isinstance(cmd, str) and cmd:
        token = cmd.split()[0] if cmd.split() else ""
        basename = os.path.basename(token)
        if basename in ("git", "gh", "curl", "wget"):
            raise AssertionError(f"Unexpected network I/O via subprocess: {basename}")
    return _original_subprocess_run(*args, **kwargs)


def _network_sentinel_subprocess_popen(*args, **kwargs):
    cmd = args[0] if args else kwargs.get("args", [])
    if isinstance(cmd, (list, tuple)) and cmd:
        basename = os.path.basename(str(cmd[0]))
        if basename in ("git", "gh", "curl", "wget"):
            raise AssertionError(f"Unexpected network I/O via Popen: {basename}")
    elif isinstance(cmd, str) and cmd:
        token = cmd.split()[0] if cmd.split() else ""
        basename = os.path.basename(token)
        if basename in ("git", "gh", "curl", "wget"):
            raise AssertionError(f"Unexpected network I/O via Popen: {basename}")
    return _original_subprocess_popen(*args, **kwargs)


# ---------------------------------------------------------------------------
# E2E: Round-trip (pack -> install)
# ---------------------------------------------------------------------------


class TestInstallLocalBundleE2E:
    """End-to-end tests for the local-bundle install pipeline."""

    def test_install_local_bundle_from_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Install a plugin bundle from a directory -> files deployed."""
        bundle = _make_plugin_bundle(tmp_path / "src")
        project = _make_project(tmp_path / "dst")

        result = _invoke_install(
            project, str(bundle), "--target", "copilot", monkeypatch=monkeypatch
        )

        assert result.exit_code == 0, f"stdout={result.output!r}\nstderr={result.stderr!r}"
        # copilot project-scope root_dir is ".github"
        assert (project / ".github" / "skills" / "coding" / "SKILL.md").is_file()
        assert (project / ".github" / "agents" / "reviewer.md").is_file()
        assert (project / ".github" / "instructions" / "style.md").is_file()

    def test_install_local_bundle_from_tarball(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Install a plugin bundle from .tar.gz -> files deployed."""
        bundle = _make_plugin_bundle(tmp_path / "src")
        tarball = _make_tarball(tmp_path / "archives", bundle)
        project = _make_project(tmp_path / "dst")

        result = _invoke_install(
            project, str(tarball), "--target", "copilot", monkeypatch=monkeypatch
        )

        assert result.exit_code == 0, f"stdout={result.output!r}\nstderr={result.stderr!r}"
        assert (project / ".github" / "skills" / "coding" / "SKILL.md").is_file()
        assert (project / ".github" / "agents" / "reviewer.md").is_file()

    def test_install_local_bundle_multi_target(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Install with --target copilot,claude -> files in both trees."""
        bundle = _make_plugin_bundle(tmp_path / "src", pack_target="copilot,claude")
        project = _make_project(tmp_path / "dst", targets=["copilot", "claude"])

        result = _invoke_install(
            project, str(bundle), "--target", "copilot,claude", monkeypatch=monkeypatch
        )

        assert result.exit_code == 0, f"stdout={result.output!r}\nstderr={result.stderr!r}"
        # copilot root_dir = ".github" ; claude root_dir = ".claude"
        assert (project / ".github" / "skills" / "coding" / "SKILL.md").is_file()
        assert (project / ".claude" / "skills" / "coding" / "SKILL.md").is_file()
        assert (project / ".github" / "agents" / "reviewer.md").is_file()
        assert (project / ".claude" / "agents" / "reviewer.md").is_file()

    def test_install_local_bundle_auto_detect_target(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Install without --target -> auto-detects from project.

        With no detector dirs present, ``resolve_targets`` falls back to
        the universal copilot default, so files land under ``.github/``.
        """
        bundle = _make_plugin_bundle(tmp_path / "src")
        project = _make_project(tmp_path / "dst")

        result = _invoke_install(project, str(bundle), monkeypatch=monkeypatch)

        assert result.exit_code == 0, f"stdout={result.output!r}\nstderr={result.stderr!r}"
        assert (project / ".github" / "skills" / "coding" / "SKILL.md").is_file()

    def test_pack_install_round_trip_fidelity(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """pack(plugin) -> install(bundle) produces a valid file layout.

        The files deployed to the target tree should match the bundle's
        flat layout mapped through the integrator pipeline.  Verifies
        that deployed content is byte-identical to the source.
        """
        files = {
            "skills/coding/SKILL.md": "# Coding\nSkill content.",
            "agents/reviewer.md": "# Reviewer\nAgent content.",
        }
        bundle = _make_plugin_bundle(tmp_path / "src", files=files)
        project = _make_project(tmp_path / "dst")

        result = _invoke_install(
            project, str(bundle), "--target", "copilot", monkeypatch=monkeypatch
        )

        assert result.exit_code == 0, f"stdout={result.output!r}\nstderr={result.stderr!r}"
        for rel, expected_content in files.items():
            deployed = project / ".github" / rel
            assert deployed.is_file(), f"missing {deployed}"
            assert deployed.read_text(encoding="utf-8") == expected_content


# ---------------------------------------------------------------------------
# E2E: Collision handling
# ---------------------------------------------------------------------------


class TestInstallLocalBundleCollision:
    """Collision behavior for local-bundle install."""

    def test_collision_managed_file_overwritten(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Pre-existing file with identical content -> idempotent overwrite.

        Production-code logic (``services.integrate_local_bundle``) only
        skips when the on-disk file's hash differs from the bundle's
        expected hash.  When hashes match (i.e. the file is already what
        the bundle would deploy), the copy goes through silently and the
        path is recorded in ``deployed_files``.
        """
        bundle_content = "# Coding Skill\nHelps with code."
        bundle = _make_plugin_bundle(
            tmp_path / "src",
            files={"skills/coding/SKILL.md": bundle_content},
        )
        project = _make_project(tmp_path / "dst")

        # Pre-create the destination file with identical content.
        dest = project / ".github" / "skills" / "coding" / "SKILL.md"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(bundle_content, encoding="utf-8")

        result = _invoke_install(
            project, str(bundle), "--target", "copilot", monkeypatch=monkeypatch
        )

        assert result.exit_code == 0, f"stdout={result.output!r}\nstderr={result.stderr!r}"
        # File still exists with bundle content (overwrite was a no-op).
        assert dest.is_file()
        assert dest.read_text(encoding="utf-8") == bundle_content

    def test_collision_locally_modified_skipped_without_force(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Locally-modified file -> skipped without --force."""
        bundle = _make_plugin_bundle(
            tmp_path / "src",
            files={"skills/coding/SKILL.md": "# Bundle version\n"},
        )
        project = _make_project(tmp_path / "dst")

        dest = project / ".github" / "skills" / "coding" / "SKILL.md"
        dest.parent.mkdir(parents=True, exist_ok=True)
        local_content = "# Locally modified\nDo not overwrite me.\n"
        dest.write_text(local_content, encoding="utf-8")

        result = _invoke_install(
            project, str(bundle), "--target", "copilot", monkeypatch=monkeypatch
        )

        assert result.exit_code == 0, f"stdout={result.output!r}\nstderr={result.stderr!r}"
        assert dest.read_text(encoding="utf-8") == local_content

    def test_collision_locally_modified_overwritten_with_force(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With --force -> locally-modified file overwritten."""
        bundle_content = "# Bundle version\n"
        bundle = _make_plugin_bundle(
            tmp_path / "src",
            files={"skills/coding/SKILL.md": bundle_content},
        )
        project = _make_project(tmp_path / "dst")

        dest = project / ".github" / "skills" / "coding" / "SKILL.md"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text("# Locally modified\n", encoding="utf-8")

        result = _invoke_install(
            project,
            str(bundle),
            "--target",
            "copilot",
            "--force",
            monkeypatch=monkeypatch,
        )

        assert result.exit_code == 0, f"stdout={result.output!r}\nstderr={result.stderr!r}"
        assert dest.read_text(encoding="utf-8") == bundle_content

    def test_collision_managed_file_overwritten_with_force(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """IM10: identical pre-existing content + --force -> idempotent
        success, content byte-equal to the bundle file.

        Regression guard against accidental ``shutil.copy2`` errors or
        permission flips when the destination already matches the source
        and the user opts into the force path.
        """
        bundle_content = "# Bundle version\nIdentical pre-existing content.\n"
        bundle = _make_plugin_bundle(
            tmp_path / "src",
            files={"skills/coding/SKILL.md": bundle_content},
        )
        project = _make_project(tmp_path / "dst")

        dest = project / ".github" / "skills" / "coding" / "SKILL.md"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(bundle_content, encoding="utf-8")
        before_bytes = dest.read_bytes()

        result = _invoke_install(
            project,
            str(bundle),
            "--target",
            "copilot",
            "--force",
            monkeypatch=monkeypatch,
        )

        assert result.exit_code == 0, f"stdout={result.output!r}\nstderr={result.stderr!r}"
        assert dest.is_file()
        assert dest.read_bytes() == before_bytes, (
            "Managed file content changed under --force despite identical input"
        )


# ---------------------------------------------------------------------------
# E2E: Dry-run
# ---------------------------------------------------------------------------


class TestInstallLocalBundleDryRun:
    """Dry-run mode for local-bundle install."""

    def test_dry_run_no_files_written(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """--dry-run shows what would be installed without writing."""
        bundle = _make_plugin_bundle(tmp_path / "src")
        project = _make_project(tmp_path / "dst")

        result = _invoke_install(
            project,
            str(bundle),
            "--target",
            "copilot",
            "--dry-run",
            monkeypatch=monkeypatch,
        )

        assert result.exit_code == 0, f"stdout={result.output!r}\nstderr={result.stderr!r}"
        # No deployed files anywhere under .github/
        github_root = project / ".github"
        if github_root.exists():
            files = [p for p in github_root.rglob("*") if p.is_file()]
            assert files == [], f"dry-run wrote files: {files}"
        # Lockfile must not be created on dry-run.
        assert not (project / "apm.lock.yaml").exists()


# ---------------------------------------------------------------------------
# E2E: apm.yml side effects
# ---------------------------------------------------------------------------


class TestApmYmlSideEffects:
    """Verify apm.yml is NOT mutated by local-bundle install."""

    def test_apm_yml_not_mutated_by_local_install(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Bytes-identical apm.yml before and after local install."""
        bundle = _make_plugin_bundle(tmp_path / "src")
        project = _make_project(tmp_path / "dst")
        apm_yml = project / "apm.yml"
        before = apm_yml.read_bytes()

        result = _invoke_install(
            project, str(bundle), "--target", "copilot", monkeypatch=monkeypatch
        )

        assert result.exit_code == 0, f"stdout={result.output!r}\nstderr={result.stderr!r}"
        assert apm_yml.read_bytes() == before

    def test_local_lockfile_records_deployed_files(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Project's apm.lock.yaml records deployed paths and SHA-256s."""
        bundle = _make_plugin_bundle(tmp_path / "src")
        project = _make_project(tmp_path / "dst")

        result = _invoke_install(
            project, str(bundle), "--target", "copilot", monkeypatch=monkeypatch
        )

        assert result.exit_code == 0, f"stdout={result.output!r}\nstderr={result.stderr!r}"

        lockfile_path = project / "apm.lock.yaml"
        assert lockfile_path.is_file(), "lockfile not written"
        data = yaml.safe_load(lockfile_path.read_text(encoding="utf-8"))

        deployed_files = data.get("local_deployed_files") or []
        deployed_hashes = data.get("local_deployed_file_hashes") or {}

        assert isinstance(deployed_files, list)
        assert isinstance(deployed_hashes, dict)
        # All three bundle files should be recorded.
        assert len(deployed_files) == 3
        assert len(deployed_hashes) == 3

        # Every recorded path must have a matching hash entry.
        assert set(deployed_files) == set(deployed_hashes.keys())

        # Each recorded path's hash must match the on-disk file's actual SHA-256.
        for record_path, expected_hash in deployed_hashes.items():
            # Records may be absolute or project-relative; resolve both.
            candidate = Path(record_path)
            if not candidate.is_absolute():
                candidate = project / candidate
            assert candidate.is_file(), f"missing deployed file: {candidate}"
            actual_hash = hashlib.sha256(candidate.read_bytes()).hexdigest()
            assert actual_hash == expected_hash, f"hash mismatch for {candidate}"


# ---------------------------------------------------------------------------
# E2E: Air-gap proof (zero network I/O)
# ---------------------------------------------------------------------------


class TestLocalInstallAirGap:
    """Prove that local-bundle install does zero network I/O."""

    def test_local_install_zero_network_io(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Monkeypatch all known network entry points to assert no calls.

        If ANY network call is made during local-bundle install, the
        sentinel raises AssertionError immediately and propagates because
        the runner is invoked with ``catch_exceptions=False``.
        """
        bundle = _make_plugin_bundle(tmp_path / "src")
        tarball = _make_tarball(tmp_path / "archives", bundle)
        project = _make_project(tmp_path / "dst")

        def _fail_urlopen(*a, **kw):
            raise AssertionError("Unexpected network I/O: urllib.request.urlopen")

        def _fail_requests(*a, **kw):
            raise AssertionError("Unexpected network I/O: requests")

        def _fail_httpx(*a, **kw):
            raise AssertionError("Unexpected network I/O: httpx")

        def _fail_socket_connect(*a, **kw):
            raise AssertionError("Unexpected network I/O: socket.create_connection")

        def _fail_socket_socket(*a, **kw):
            raise AssertionError("Unexpected network I/O: socket.socket")

        patches = [
            patch("urllib.request.urlopen", side_effect=_fail_urlopen),
            patch("subprocess.run", side_effect=_network_sentinel_subprocess_run),
            patch("subprocess.Popen", side_effect=_network_sentinel_subprocess_popen),
            patch("socket.create_connection", side_effect=_fail_socket_connect),
            patch("socket.socket", side_effect=_fail_socket_socket),
        ]

        try:
            import requests  # noqa: F401

            patches.append(patch("requests.Session.send", side_effect=_fail_requests))
        except ImportError:
            pass

        try:
            import httpx  # noqa: F401

            patches.append(patch("httpx.Client.send", side_effect=_fail_httpx))
            patches.append(patch("httpx.AsyncClient.send", side_effect=_fail_httpx))
        except ImportError:
            pass

        for p in patches:
            p.start()
        try:
            result = _invoke_install(
                project,
                str(tarball),
                "--target",
                "copilot",
                monkeypatch=monkeypatch,
            )
            assert result.exit_code == 0, f"stdout={result.output!r}\nstderr={result.stderr!r}"
            # Files actually deployed (so we know the install ran end-to-end).
            assert (project / ".github" / "skills" / "coding" / "SKILL.md").is_file()
        finally:
            for p in patches:
                p.stop()


# ---------------------------------------------------------------------------
# Legacy --format apm tarball rejection
# ---------------------------------------------------------------------------


class TestInstallLegacyApmFormatBundle:
    """``apm install <legacy-bundle>`` must reject legacy --format apm tarballs
    with an actionable error pointing at ``apm unpack`` or ``--format plugin``."""

    def test_legacy_apm_tarball_rejected_with_actionable_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A tarball produced by ``apm pack --format apm --archive`` has
        ``apm.lock.yaml`` at the root but no ``plugin.json``.  The install
        command must reject it with a message that names the legacy format
        and suggests either repacking or using ``apm unpack``."""
        # Build a legacy apm-format bundle directory
        bundle = tmp_path / "test-pkg-0.1.0"
        bundle.mkdir(parents=True)

        files = {
            ".github/copilot-instructions.md": "# Copilot instructions\n",
            ".github/skills/coding/SKILL.md": "# Coding skill\n",
        }
        for rel, content in files.items():
            p = bundle / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")

        lock_data = {
            "pack": {"format": "apm", "target": "copilot"},
            "dependencies": [
                {
                    "repo_url": "owner/test-pkg",
                    "resolved_commit": "abc123",
                    "deployed_files": list(files.keys()),
                }
            ],
        }
        (bundle / "apm.lock.yaml").write_text(
            yaml.dump(lock_data, default_flow_style=False), encoding="utf-8"
        )

        # Archive (same as ``apm pack --format apm --archive``)
        archive = tmp_path / "test-pkg-0.1.0.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            tar.add(bundle, arcname=bundle.name)

        project = _make_project(tmp_path / "dst")
        result = _invoke_install(
            project, str(archive), "--target", "copilot", monkeypatch=monkeypatch
        )

        assert result.exit_code != 0, f"Expected failure, got exit 0: {result.output!r}"
        # The error must mention the legacy format
        assert "--format apm" in result.output or "legacy format" in result.output
        # The error must offer actionable guidance
        assert "apm unpack" in result.output or "--format plugin" in result.output
        # No files should be deployed
        assert not (project / ".github" / "copilot-instructions.md").exists()

    def test_legacy_apm_directory_falls_through_to_resolver(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A legacy directory (no plugin.json, not a tarball) is not caught
        by the tarball guard and falls through to the dependency-resolver
        pipeline.  This is expected: directories without plugin.json are
        treated as local-path deps, not bundles."""
        bundle = tmp_path / "test-pkg-0.1.0"
        bundle.mkdir(parents=True)
        (bundle / "apm.lock.yaml").write_text("pack:\n  format: apm\n", encoding="utf-8")

        project = _make_project(tmp_path / "dst")
        # This will fail in the resolver (not a valid local dep) but
        # must NOT hit the "legacy format" error path -- that's tarball-only.
        result = _invoke_install(
            project, str(bundle), "--target", "copilot", monkeypatch=monkeypatch
        )
        # Should NOT contain the legacy-tarball error message
        assert "--format apm" not in result.output
