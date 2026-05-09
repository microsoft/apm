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
        # write_bytes (not write_text) keeps newlines as `\n` on Windows.
        # Otherwise Path.write_text translates `\n` -> `\r\n` and the
        # on-disk bytes diverge from the in-memory `content` whose
        # sha256 we record below, causing verify_bundle_integrity to fail.
        p.write_bytes(content.encode("utf-8"))
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
        # copilot project-scope root_dir is ".github"; skills route to ".agents"
        assert (project / ".agents" / "skills" / "coding" / "SKILL.md").is_file()
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
        assert (project / ".agents" / "skills" / "coding" / "SKILL.md").is_file()
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
        # copilot routes skills to ".agents" ; claude root_dir = ".claude"
        assert (project / ".agents" / "skills" / "coding" / "SKILL.md").is_file()
        assert (project / ".claude" / "skills" / "coding" / "SKILL.md").is_file()
        assert (project / ".github" / "agents" / "reviewer.md").is_file()
        assert (project / ".claude" / "agents" / "reviewer.md").is_file()

    def test_install_local_bundle_auto_detect_target(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Install without --target -> auto-detects from project.

        With no detector dirs present, ``resolve_targets`` falls back to
        the universal copilot default. Skills route to ``.agents/`` while
        other primitives land under ``.github/``.
        """
        bundle = _make_plugin_bundle(tmp_path / "src")
        project = _make_project(tmp_path / "dst")

        result = _invoke_install(project, str(bundle), monkeypatch=monkeypatch)

        assert result.exit_code == 0, f"stdout={result.output!r}\nstderr={result.stderr!r}"
        assert (project / ".agents" / "skills" / "coding" / "SKILL.md").is_file()

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
            # copilot routes ``skills/`` to ``.agents/``; other primitives
            # stay under ``.github/``.
            root = ".agents" if rel.startswith("skills/") else ".github"
            deployed = project / root / rel
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
        dest = project / ".agents" / "skills" / "coding" / "SKILL.md"
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

        dest = project / ".agents" / "skills" / "coding" / "SKILL.md"
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

        dest = project / ".agents" / "skills" / "coding" / "SKILL.md"
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

        dest = project / ".agents" / "skills" / "coding" / "SKILL.md"
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
        # No deployed files anywhere under .github/ or .agents/
        for root_name in (".github", ".agents"):
            root = project / root_name
            if root.exists():
                files = [p for p in root.rglob("*") if p.is_file()]
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

        # Each recorded path's hash must match the on-disk file's actual
        # SHA-256, written in the canonical ``sha256:<hex>`` form so it
        # compares equal against ``compute_file_hash`` output (regression
        # guard: prior to 0.12.0 the local-bundle path wrote bare hex,
        # which mis-classified files as "user-edited" in stale-cleanup).
        for record_path, expected_hash in deployed_hashes.items():
            # Records may be absolute or project-relative; resolve both.
            candidate = Path(record_path)
            if not candidate.is_absolute():
                candidate = project / candidate
            assert candidate.is_file(), f"missing deployed file: {candidate}"
            assert expected_hash.startswith("sha256:"), (
                f"hash for {record_path!r} must use canonical 'sha256:<hex>' "
                f"form, got {expected_hash!r}"
            )
            actual_hash = "sha256:" + hashlib.sha256(candidate.read_bytes()).hexdigest()
            assert actual_hash == expected_hash, f"hash mismatch for {candidate}"


# ---------------------------------------------------------------------------
# E2E: Hash-format consistency across install flows (regression for 0.12.0)
# ---------------------------------------------------------------------------


class TestLocalBundleHashFormatCrossFlow:
    """Pin the hash format contract that ties ``apm install <bundle>`` to
    the stale-cleanup provenance check.

    Prior to the 0.12.0 fix, ``integrate_local_bundle`` wrote bare
    ``<hex>`` into ``local_deployed_file_hashes`` while
    ``compute_file_hash`` (used by ``cleanup.py``) emitted the canonical
    ``sha256:<hex>``.  An exact-match comparison in
    ``remove_stale_deployed_files`` then mis-classified every
    bundle-deployed file as "user-edited" and refused to remove stale
    entries on subsequent installs.
    """

    def test_local_bundle_hash_matches_compute_file_hash_format(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Hash recorded by local-bundle install must equal compute_file_hash output."""
        from apm_cli.utils.content_hash import compute_file_hash

        bundle = _make_plugin_bundle(tmp_path / "src")
        project = _make_project(tmp_path / "dst")

        result = _invoke_install(
            project, str(bundle), "--target", "copilot", monkeypatch=monkeypatch
        )
        assert result.exit_code == 0, f"stdout={result.output!r}\nstderr={result.stderr!r}"

        data = yaml.safe_load((project / "apm.lock.yaml").read_text(encoding="utf-8"))
        deployed_hashes = data.get("local_deployed_file_hashes") or {}
        assert deployed_hashes, "local_deployed_file_hashes is empty"

        for record_path, recorded in deployed_hashes.items():
            candidate = Path(record_path)
            if not candidate.is_absolute():
                candidate = project / candidate
            # Equality with compute_file_hash is the contract: this is the
            # exact comparison cleanup.py uses for stale-file provenance.
            assert recorded == compute_file_hash(candidate), (
                f"hash format drift for {record_path!r}: "
                f"recorded={recorded!r} vs compute_file_hash={compute_file_hash(candidate)!r}. "
                "Stale-cleanup provenance check would mis-classify this file as user-edited."
            )

    def test_recorded_hash_compares_equal_in_cleanup_provenance_check(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Hashes recorded by local-bundle install must NOT trip the
        ``cleanup.remove_stale_deployed_files`` "user-edited" guard
        when the deployed file is unchanged.

        This drives the actual code path that the regression broke:
        ``cleanup.py`` reads ``recorded_hashes`` from the lockfile (set
        by ``integrate_local_bundle``), recomputes via
        ``compute_file_hash``, and compares.  Prior to 0.12.0 the
        comparison always failed (bare hex vs ``sha256:<hex>``), so
        every bundle-deployed file was permanently classified as
        user-edited and stale-cleanup was a no-op.
        """
        from apm_cli.integration.cleanup import remove_stale_deployed_files
        from apm_cli.utils.diagnostics import DiagnosticCollector

        bundle = _make_plugin_bundle(tmp_path / "src")
        project = _make_project(tmp_path / "dst")
        result = _invoke_install(
            project, str(bundle), "--target", "copilot", monkeypatch=monkeypatch
        )
        assert result.exit_code == 0, f"install failed: {result.output}"

        data = yaml.safe_load((project / "apm.lock.yaml").read_text(encoding="utf-8"))
        deployed_files = list(data.get("local_deployed_files") or [])
        deployed_hashes = dict(data.get("local_deployed_file_hashes") or {})
        assert deployed_files and deployed_hashes

        # Pretend every file is now stale and ask cleanup to remove them.
        # The provenance gate should pass (file is unchanged), so cleanup
        # actually deletes them -- not skip them as "user-edited".
        diagnostics = DiagnosticCollector()
        cleanup_result = remove_stale_deployed_files(
            deployed_files,
            project,
            dep_key="<local-bundle-test>",
            targets=None,
            diagnostics=diagnostics,
            recorded_hashes=deployed_hashes,
        )

        assert not cleanup_result.skipped_user_edit, (
            "cleanup mis-classified bundle-deployed files as user-edited: "
            f"{cleanup_result.skipped_user_edit}. "
            "Likely a hash-format regression between integrate_local_bundle "
            "(write side) and compute_file_hash (read side in cleanup.py)."
        )
        # Every file passed the provenance check and was deleted.
        assert set(cleanup_result.deleted) == set(deployed_files)


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
            assert (project / ".agents" / "skills" / "coding" / "SKILL.md").is_file()
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


# ---------------------------------------------------------------------------
# E2E: Issue #1207 -- target-agnostic bundle install matrix
# ---------------------------------------------------------------------------


class TestInstallLocalBundleIssue1207:
    """End-to-end matrix for issue #1207.

    The same target-agnostic bundle (``pack.target == "all"``) must
    install correctly into projects configured for any target.  No
    "Install interrupted" line on success; ``plugin.json`` is never
    deployed; instructions land where the target's compile flow
    expects them.
    """

    @pytest.mark.parametrize(
        "consumer_target,expected_paths",
        [
            (
                "copilot",
                [
                    ".agents/skills/coding/SKILL.md",
                    ".github/agents/reviewer.md",
                    ".github/instructions/style.md",
                ],
            ),
            (
                "claude",
                [
                    ".claude/skills/coding/SKILL.md",
                    ".claude/agents/reviewer.md",
                    ".claude/instructions/style.md",
                ],
            ),
            (
                "cursor",
                [
                    ".agents/skills/coding/SKILL.md",
                    ".cursor/agents/reviewer.md",
                    ".cursor/instructions/style.md",
                ],
            ),
            (
                "opencode",
                [
                    ".agents/skills/coding/SKILL.md",
                    ".opencode/agents/reviewer.md",
                    "apm_modules/test-plugin/.apm/instructions/style.md",
                ],
            ),
            (
                "codex",
                [
                    ".agents/skills/coding/SKILL.md",
                    ".codex/agents/reviewer.md",
                    "apm_modules/test-plugin/.apm/instructions/style.md",
                ],
            ),
            (
                "gemini",
                [
                    ".agents/skills/coding/SKILL.md",
                    "apm_modules/test-plugin/.apm/instructions/style.md",
                ],
            ),
        ],
    )
    def test_target_agnostic_bundle_installs_per_consumer_target(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        consumer_target: str,
        expected_paths: list[str],
    ) -> None:
        """Bundle packed with ``pack.target == "all"`` deploys correctly
        into the consumer's resolved target without any pack-side
        target binding leaking into the deploy path.

        We exercise both detection mechanisms in a single matrix: the
        consumer's IDE is "configured" by pre-creating its ``root_dir``
        (matches what real users see -- they don't pass ``--target`` on
        every install).
        """
        from apm_cli.integration.targets import KNOWN_TARGETS

        bundle = _make_plugin_bundle(tmp_path / "src", pack_target="all")
        project = _make_project(tmp_path / "dst")
        # Pre-create the target's root_dir so resolve_targets picks it up
        # via directory detection (mirrors a project with the IDE set up).
        (project / KNOWN_TARGETS[consumer_target].root_dir).mkdir(parents=True, exist_ok=True)

        result = _invoke_install(project, str(bundle), monkeypatch=monkeypatch)

        assert result.exit_code == 0, f"target={consumer_target} stdout={result.output!r}"
        # D3: no false "Install interrupted" on success.
        assert "Install interrupted" not in result.output

        for rel in expected_paths:
            assert (project / rel).is_file(), (
                f"missing {rel} for target={consumer_target} in {result.output!r}"
            )

        # D2.a: plugin.json never deployed under the consumer's project
        # tree, regardless of casing.  PR #1217 review: walk the entire
        # project tree -- not just ``apm_modules/`` -- so a regression
        # that leaks ``plugin.json`` under ``.github/``, ``.claude/``,
        # ``.cursor/``, or any other target root is caught.
        for child in project.rglob("*"):
            if child.is_file() and child.name.lower() == "plugin.json":
                rel_to_project = child.relative_to(project)
                pytest.fail(f"plugin.json leaked into {rel_to_project}")

        # D2.b: compile-only targets must surface the compile hint so
        # users know to run ``apm compile`` to merge staged instructions.
        if consumer_target in {"opencode", "codex", "gemini"}:
            # CLI logger may line-wrap; collapse whitespace before
            # substring checks so the assertion survives terminal width
            # changes without coupling to render details.
            collapsed = " ".join(result.output.split())
            assert "apm compile" in collapsed, (
                f"compile hint missing for target={consumer_target}: {result.output!r}"
            )
            assert consumer_target in collapsed, (
                f"compile hint should name target={consumer_target}: {result.output!r}"
            )

    def test_multi_target_consumer_deploys_to_both(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A consumer project with both ``.github/`` and ``.opencode/``
        present receives the bundle in both layouts: native instructions
        for copilot, staged for opencode.
        """
        bundle = _make_plugin_bundle(tmp_path / "src", pack_target="all")
        project = _make_project(tmp_path / "dst")
        # Both targets configured.
        (project / ".github").mkdir()
        (project / ".github" / "copilot-instructions.md").write_text("# proj\n", encoding="utf-8")
        (project / ".opencode").mkdir()

        result = _invoke_install(project, str(bundle), monkeypatch=monkeypatch)

        assert result.exit_code == 0, f"stdout={result.output!r}"
        # copilot side: instructions deploy verbatim to .github/instructions.
        assert (project / ".github" / "instructions" / "style.md").is_file()
        # opencode side: instructions staged for apm compile.
        assert (
            project / "apm_modules" / "test-plugin" / ".apm" / "instructions" / "style.md"
        ).is_file()
        # Skills shared dir from both target profiles.
        assert (project / ".agents" / "skills" / "coding" / "SKILL.md").is_file()


# ---------------------------------------------------------------------------
# E2E: bundle .mcp.json wiring through MCPIntegrator
# ---------------------------------------------------------------------------


class TestBundleMcpWiringE2E:
    """End-to-end: a packed bundle carrying ``.mcp.json`` wires its
    servers through ``MCPIntegrator.install`` once after the deploy
    loop, with the resolved targets passed via ``explicit_target``.

    Per-target write paths (Claude project ``.mcp.json``,
    ``.vscode/mcp.json``, ``.cursor/mcp.json``, etc.) are covered by
    ``MCPIntegrator``'s own suite -- here we assert at the
    integrator boundary so the test is independent of installed
    runtime binaries (``claude``, ``cursor``, ...) on the CI host.
    """

    @staticmethod
    def _make_mcp_bundle(tmp_path: Path) -> Path:
        """Plugin bundle with skill + .mcp.json + matching bundle_files."""
        mcp_payload = json.dumps(
            {
                "mcpServers": {
                    "filesystem": {
                        "type": "stdio",
                        "command": "npx",
                        "args": ["-y", "@modelcontextprotocol/server-filesystem"],
                    },
                    "github": {
                        "type": "http",
                        "url": "https://api.githubcopilot.com/mcp/",
                    },
                }
            },
            indent=2,
        )
        files = {
            "skills/coding/SKILL.md": "# Coding Skill\nbody.",
            ".mcp.json": mcp_payload,
        }
        return _make_plugin_bundle(tmp_path / "src", files=files, pack_target="all")

    def test_bundle_mcp_servers_reach_integrator_with_resolved_targets(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Bundle ``.mcp.json`` is parsed and ``MCPIntegrator.install``
        is called once with the bundle's servers and the resolved
        targets as a CSV string in ``explicit_target``."""
        from apm_cli.integration.targets import KNOWN_TARGETS

        bundle = self._make_mcp_bundle(tmp_path)
        project = _make_project(tmp_path / "dst")
        # Mark the project as configured for both copilot and claude.
        (project / KNOWN_TARGETS["copilot"].root_dir).mkdir(parents=True, exist_ok=True)
        (project / KNOWN_TARGETS["claude"].root_dir).mkdir(parents=True, exist_ok=True)

        captured: dict = {}

        def _capture(mcp_deps, **kwargs):
            captured["deps"] = list(mcp_deps)
            captured["kwargs"] = dict(kwargs)
            return len(mcp_deps)

        with patch(
            "apm_cli.integration.mcp_integrator.MCPIntegrator.install",
            side_effect=_capture,
        ):
            result = _invoke_install(project, str(bundle), monkeypatch=monkeypatch)

        assert result.exit_code == 0, result.output
        # The skill still deploys verbatim.
        assert (project / ".agents" / "skills" / "coding" / "SKILL.md").is_file()
        # The bundle .mcp.json must NOT be deployed as a flat file
        # under any target root (it is metadata, routed through the
        # integrator instead).
        for child in project.rglob(".mcp.json"):
            # Tolerate the integrator legitimately writing one to the
            # project root; the regression we forbid is the bundle
            # file copied verbatim into a target tree.
            rel = child.relative_to(project)
            parts = rel.parts
            assert "skills" not in parts and "apm_modules" not in parts, (
                f".mcp.json leaked into target tree at {rel}"
            )

        # Integrator was invoked.
        assert "deps" in captured, "MCPIntegrator.install was not called"
        names = sorted(d.name for d in captured["deps"])
        assert names == ["filesystem", "github"]

        # Resolved targets reach the integrator as a CSV in
        # ``explicit_target``.  Both copilot and claude must appear.
        target_csv = captured["kwargs"].get("explicit_target") or ""
        target_set = {t.strip() for t in target_csv.split(",") if t.strip()}
        assert "copilot" in target_set
        assert "claude" in target_set

        # ``project_root`` is the consumer project, not the bundle.
        assert Path(captured["kwargs"]["project_root"]).resolve() == project.resolve()

    def test_bundle_without_mcp_does_not_call_integrator(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When the bundle has no ``.mcp.json``, the integrator is not
        invoked -- protects against spurious "No MCP dependencies
        found" warnings on every install."""
        from apm_cli.integration.targets import KNOWN_TARGETS

        bundle = _make_plugin_bundle(tmp_path / "src", pack_target="all")
        project = _make_project(tmp_path / "dst")
        (project / KNOWN_TARGETS["copilot"].root_dir).mkdir(parents=True, exist_ok=True)

        with patch("apm_cli.integration.mcp_integrator.MCPIntegrator.install") as mock_install:
            result = _invoke_install(project, str(bundle), monkeypatch=monkeypatch)

        assert result.exit_code == 0, result.output
        mock_install.assert_not_called()

    def test_bundle_mcp_integrator_failure_does_not_break_install(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If the integrator raises, the install still succeeds with
        a warning -- file deploys must not be undone by an MCP wiring
        hiccup."""
        from apm_cli.integration.targets import KNOWN_TARGETS

        bundle = self._make_mcp_bundle(tmp_path)
        project = _make_project(tmp_path / "dst")
        (project / KNOWN_TARGETS["copilot"].root_dir).mkdir(parents=True, exist_ok=True)

        with patch(
            "apm_cli.integration.mcp_integrator.MCPIntegrator.install",
            side_effect=RuntimeError("integrator blew up"),
        ):
            result = _invoke_install(project, str(bundle), monkeypatch=monkeypatch)

        assert result.exit_code == 0, result.output
        assert (project / ".agents" / "skills" / "coding" / "SKILL.md").is_file()
        # Warning should mention MCP wiring.
        assert "MCP" in result.output or "mcp" in result.output

    def test_bundle_mcp_dry_run_does_not_call_integrator(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``--dry-run`` must not fire the integrator: zero side
        effects on the consumer's MCP config."""
        from apm_cli.integration.targets import KNOWN_TARGETS

        bundle = self._make_mcp_bundle(tmp_path)
        project = _make_project(tmp_path / "dst")
        (project / KNOWN_TARGETS["copilot"].root_dir).mkdir(parents=True, exist_ok=True)

        with patch("apm_cli.integration.mcp_integrator.MCPIntegrator.install") as mock_install:
            result = _invoke_install(project, str(bundle), "--dry-run", monkeypatch=monkeypatch)

        assert result.exit_code == 0, result.output
        mock_install.assert_not_called()
