"""Tests for the baseline CI checks engine (``apm_cli.policy.ci_checks``)."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from apm_cli.policy.ci_checks import (
    _check_config_consistency,
    _check_content_integrity,
    _check_deployed_files_present,
    _check_lockfile_exists,
    _check_no_orphans,
    _check_ref_consistency,
    run_baseline_checks,
)
from apm_cli.policy.models import CIAuditResult, CheckResult
from apm_cli.models.apm_package import clear_apm_yml_cache


# -- Helpers --------------------------------------------------------


def _write_apm_yml(project: Path, *, deps: list[str] | None = None, mcp: list | None = None) -> None:
    """Write a minimal apm.yml with optional dependencies."""
    lines = ["name: test-project", "version: '1.0.0'"]
    if deps or mcp:
        lines.append("dependencies:")
    if deps:
        lines.append("  apm:")
        for d in deps:
            lines.append(f"    - {d}")
    if mcp:
        lines.append("  mcp:")
        for m in mcp:
            if isinstance(m, str):
                lines.append(f"    - {m}")
            elif isinstance(m, dict):
                # Write dict form
                first_key = True
                for k, v in m.items():
                    prefix = "    - " if first_key else "      "
                    lines.append(f"{prefix}{k}: {v}")
                    first_key = False
    (project / "apm.yml").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_lockfile(project: Path, content: str) -> None:
    """Write apm.lock.yaml."""
    (project / "apm.lock.yaml").write_text(content, encoding="utf-8")


def _make_deployed_file(project: Path, rel_path: str, content: str = "clean\n") -> None:
    """Create a file at the given relative path under project."""
    p = project / rel_path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


# -- Fixtures -------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_cache():
    """Clear the APMPackage parse cache between tests."""
    clear_apm_yml_cache()
    yield
    clear_apm_yml_cache()


# -- Lockfile exists ------------------------------------------------


class TestLockfileExists:
    def test_pass_lockfile_present(self, tmp_path):
        _write_apm_yml(tmp_path, deps=["owner/repo"])
        _write_lockfile(
            tmp_path,
            textwrap.dedent("""\
                lockfile_version: '1'
                generated_at: '2025-01-01T00:00:00Z'
                dependencies:
                  - repo_url: owner/repo
                    resolved_ref: main
            """),
        )
        result = _check_lockfile_exists(tmp_path)
        assert result.passed
        assert result.name == "lockfile-exists"

    def test_fail_lockfile_missing(self, tmp_path):
        _write_apm_yml(tmp_path, deps=["owner/repo"])
        result = _check_lockfile_exists(tmp_path)
        assert not result.passed
        assert "missing" in result.message.lower()
        assert len(result.details) > 0

    def test_pass_no_deps_no_lockfile(self, tmp_path):
        _write_apm_yml(tmp_path)  # no deps
        result = _check_lockfile_exists(tmp_path)
        assert result.passed
        assert "not required" in result.message.lower()

    def test_pass_no_apm_yml(self, tmp_path):
        result = _check_lockfile_exists(tmp_path)
        assert result.passed


# -- Ref consistency ------------------------------------------------


class TestRefConsistency:
    def test_pass_refs_match(self, tmp_path):
        _write_apm_yml(tmp_path, deps=["owner/repo#v1.0.0"])
        _write_lockfile(
            tmp_path,
            textwrap.dedent("""\
                lockfile_version: '1'
                generated_at: '2025-01-01T00:00:00Z'
                dependencies:
                  - repo_url: owner/repo
                    resolved_ref: v1.0.0
                    deployed_files: []
            """),
        )
        from apm_cli.models.apm_package import APMPackage
        from apm_cli.deps.lockfile import LockFile, get_lockfile_path

        manifest = APMPackage.from_apm_yml(tmp_path / "apm.yml")
        lock = LockFile.read(get_lockfile_path(tmp_path))
        result = _check_ref_consistency(manifest, lock)
        assert result.passed

    def test_fail_ref_mismatch(self, tmp_path):
        _write_apm_yml(tmp_path, deps=["owner/repo#v2.0.0"])
        _write_lockfile(
            tmp_path,
            textwrap.dedent("""\
                lockfile_version: '1'
                generated_at: '2025-01-01T00:00:00Z'
                dependencies:
                  - repo_url: owner/repo
                    resolved_ref: v1.0.0
                    deployed_files: []
            """),
        )
        from apm_cli.models.apm_package import APMPackage
        from apm_cli.deps.lockfile import LockFile, get_lockfile_path

        manifest = APMPackage.from_apm_yml(tmp_path / "apm.yml")
        lock = LockFile.read(get_lockfile_path(tmp_path))
        result = _check_ref_consistency(manifest, lock)
        assert not result.passed
        assert any("v2.0.0" in d and "v1.0.0" in d for d in result.details)

    def test_fail_dep_not_in_lockfile(self, tmp_path):
        _write_apm_yml(tmp_path, deps=["owner/repo"])
        _write_lockfile(
            tmp_path,
            textwrap.dedent("""\
                lockfile_version: '1'
                generated_at: '2025-01-01T00:00:00Z'
                dependencies: []
            """),
        )
        from apm_cli.models.apm_package import APMPackage
        from apm_cli.deps.lockfile import LockFile, get_lockfile_path

        manifest = APMPackage.from_apm_yml(tmp_path / "apm.yml")
        lock = LockFile.read(get_lockfile_path(tmp_path))
        result = _check_ref_consistency(manifest, lock)
        assert not result.passed
        assert any("not found" in d for d in result.details)


# -- Deployed files present -----------------------------------------


class TestDeployedFilesPresent:
    def test_pass_all_present(self, tmp_path):
        _make_deployed_file(tmp_path, ".github/prompts/test.md")
        _write_lockfile(
            tmp_path,
            textwrap.dedent("""\
                lockfile_version: '1'
                generated_at: '2025-01-01T00:00:00Z'
                dependencies:
                  - repo_url: owner/repo
                    deployed_files:
                      - .github/prompts/test.md
            """),
        )
        from apm_cli.deps.lockfile import LockFile, get_lockfile_path

        lock = LockFile.read(get_lockfile_path(tmp_path))
        result = _check_deployed_files_present(tmp_path, lock)
        assert result.passed

    def test_fail_file_missing(self, tmp_path):
        _write_lockfile(
            tmp_path,
            textwrap.dedent("""\
                lockfile_version: '1'
                generated_at: '2025-01-01T00:00:00Z'
                dependencies:
                  - repo_url: owner/repo
                    deployed_files:
                      - .github/prompts/missing.md
            """),
        )
        from apm_cli.deps.lockfile import LockFile, get_lockfile_path

        lock = LockFile.read(get_lockfile_path(tmp_path))
        result = _check_deployed_files_present(tmp_path, lock)
        assert not result.passed
        assert ".github/prompts/missing.md" in result.details


# -- No orphaned packages ------------------------------------------


class TestNoOrphans:
    def test_pass_no_orphans(self, tmp_path):
        _write_apm_yml(tmp_path, deps=["owner/repo"])
        _write_lockfile(
            tmp_path,
            textwrap.dedent("""\
                lockfile_version: '1'
                generated_at: '2025-01-01T00:00:00Z'
                dependencies:
                  - repo_url: owner/repo
                    deployed_files: []
            """),
        )
        from apm_cli.models.apm_package import APMPackage
        from apm_cli.deps.lockfile import LockFile, get_lockfile_path

        manifest = APMPackage.from_apm_yml(tmp_path / "apm.yml")
        lock = LockFile.read(get_lockfile_path(tmp_path))
        result = _check_no_orphans(manifest, lock)
        assert result.passed

    def test_fail_orphan_in_lockfile(self, tmp_path):
        _write_apm_yml(tmp_path, deps=["owner/repo"])
        _write_lockfile(
            tmp_path,
            textwrap.dedent("""\
                lockfile_version: '1'
                generated_at: '2025-01-01T00:00:00Z'
                dependencies:
                  - repo_url: owner/repo
                    deployed_files: []
                  - repo_url: extra/orphan
                    deployed_files: []
            """),
        )
        from apm_cli.models.apm_package import APMPackage
        from apm_cli.deps.lockfile import LockFile, get_lockfile_path

        manifest = APMPackage.from_apm_yml(tmp_path / "apm.yml")
        lock = LockFile.read(get_lockfile_path(tmp_path))
        result = _check_no_orphans(manifest, lock)
        assert not result.passed
        assert "extra/orphan" in result.details


# -- Config consistency ---------------------------------------------


class TestConfigConsistency:
    def test_pass_no_mcp(self, tmp_path):
        _write_apm_yml(tmp_path, deps=["owner/repo"])
        _write_lockfile(
            tmp_path,
            textwrap.dedent("""\
                lockfile_version: '1'
                generated_at: '2025-01-01T00:00:00Z'
                dependencies:
                  - repo_url: owner/repo
                    deployed_files: []
            """),
        )
        from apm_cli.models.apm_package import APMPackage
        from apm_cli.deps.lockfile import LockFile, get_lockfile_path

        manifest = APMPackage.from_apm_yml(tmp_path / "apm.yml")
        lock = LockFile.read(get_lockfile_path(tmp_path))
        result = _check_config_consistency(manifest, lock)
        assert result.passed

    def test_pass_mcp_configs_match(self, tmp_path):
        _write_apm_yml(tmp_path, mcp=["my-server"])
        _write_lockfile(
            tmp_path,
            textwrap.dedent("""\
                lockfile_version: '1'
                generated_at: '2025-01-01T00:00:00Z'
                dependencies: []
                mcp_configs:
                  my-server:
                    name: my-server
            """),
        )
        from apm_cli.models.apm_package import APMPackage
        from apm_cli.deps.lockfile import LockFile, get_lockfile_path

        manifest = APMPackage.from_apm_yml(tmp_path / "apm.yml")
        lock = LockFile.read(get_lockfile_path(tmp_path))
        result = _check_config_consistency(manifest, lock)
        assert result.passed

    def test_fail_mcp_config_drift(self, tmp_path):
        # Manifest declares server with transport override, lockfile has plain
        _write_apm_yml(
            tmp_path,
            mcp=[{"name": "my-server", "transport": "stdio"}],
        )
        _write_lockfile(
            tmp_path,
            textwrap.dedent("""\
                lockfile_version: '1'
                generated_at: '2025-01-01T00:00:00Z'
                dependencies: []
                mcp_configs:
                  my-server:
                    name: my-server
            """),
        )
        from apm_cli.models.apm_package import APMPackage
        from apm_cli.deps.lockfile import LockFile, get_lockfile_path

        manifest = APMPackage.from_apm_yml(tmp_path / "apm.yml")
        lock = LockFile.read(get_lockfile_path(tmp_path))
        result = _check_config_consistency(manifest, lock)
        assert not result.passed
        assert any("my-server" in d and "differs" in d for d in result.details)


# -- Content integrity ----------------------------------------------


class TestContentIntegrity:
    def test_pass_clean_files(self, tmp_path):
        _make_deployed_file(tmp_path, ".github/prompts/clean.md", "Clean content\n")
        _write_lockfile(
            tmp_path,
            textwrap.dedent("""\
                lockfile_version: '1'
                generated_at: '2025-01-01T00:00:00Z'
                dependencies:
                  - repo_url: owner/repo
                    deployed_files:
                      - .github/prompts/clean.md
            """),
        )
        from apm_cli.deps.lockfile import LockFile, get_lockfile_path

        lock = LockFile.read(get_lockfile_path(tmp_path))
        result = _check_content_integrity(tmp_path, lock)
        assert result.passed

    def test_fail_critical_unicode(self, tmp_path):
        _make_deployed_file(
            tmp_path,
            ".github/prompts/evil.md",
            "Normal text\U000E0001\U000E0068hidden\n",
        )
        _write_lockfile(
            tmp_path,
            textwrap.dedent("""\
                lockfile_version: '1'
                generated_at: '2025-01-01T00:00:00Z'
                dependencies:
                  - repo_url: owner/repo
                    deployed_files:
                      - .github/prompts/evil.md
            """),
        )
        from apm_cli.deps.lockfile import LockFile, get_lockfile_path

        lock = LockFile.read(get_lockfile_path(tmp_path))
        result = _check_content_integrity(tmp_path, lock)
        assert not result.passed
        assert any("evil.md" in d for d in result.details)

    # -- Hash verification ----------------------------------------------

    def test_hash_pass_when_all_match(self, tmp_path):
        from apm_cli.utils.content_hash import compute_file_hash

        _make_deployed_file(tmp_path, ".github/prompts/clean.md", "Clean content\n")
        actual_hash = compute_file_hash(tmp_path / ".github/prompts/clean.md")
        _write_lockfile(
            tmp_path,
            textwrap.dedent(f"""\
                lockfile_version: '1'
                generated_at: '2025-01-01T00:00:00Z'
                dependencies:
                  - repo_url: owner/repo
                    deployed_files:
                      - .github/prompts/clean.md
                    deployed_file_hashes:
                      .github/prompts/clean.md: '{actual_hash}'
            """),
        )
        from apm_cli.deps.lockfile import LockFile, get_lockfile_path

        lock = LockFile.read(get_lockfile_path(tmp_path))
        result = _check_content_integrity(tmp_path, lock)
        assert result.passed, result.details

    def test_hash_fail_on_hand_edit(self, tmp_path):
        from apm_cli.utils.content_hash import compute_file_hash

        _make_deployed_file(
            tmp_path, ".github/prompts/installed.md", "Original content\n"
        )
        recorded_hash = compute_file_hash(tmp_path / ".github/prompts/installed.md")
        _write_lockfile(
            tmp_path,
            textwrap.dedent(f"""\
                lockfile_version: '1'
                generated_at: '2025-01-01T00:00:00Z'
                dependencies:
                  - repo_url: owner/repo
                    deployed_files:
                      - .github/prompts/installed.md
                    deployed_file_hashes:
                      .github/prompts/installed.md: '{recorded_hash}'
            """),
        )
        # Simulate hand edit after install
        (tmp_path / ".github/prompts/installed.md").write_text(
            "Tampered content\n", encoding="utf-8"
        )

        from apm_cli.deps.lockfile import LockFile, get_lockfile_path

        lock = LockFile.read(get_lockfile_path(tmp_path))
        result = _check_content_integrity(tmp_path, lock)
        assert not result.passed
        assert any(
            "hash-drift" in d and "installed.md" in d for d in result.details
        ), result.details

    def test_hash_skips_missing_file(self, tmp_path):
        # Lockfile records a file with a hash, but the file is missing on
        # disk -- _check_deployed_files_present owns that signal, so
        # content-integrity must not double-report it.
        _write_lockfile(
            tmp_path,
            textwrap.dedent("""\
                lockfile_version: '1'
                generated_at: '2025-01-01T00:00:00Z'
                dependencies:
                  - repo_url: owner/repo
                    deployed_files:
                      - .github/prompts/missing.md
                    deployed_file_hashes:
                      .github/prompts/missing.md: 'sha256:deadbeef'
            """),
        )
        from apm_cli.deps.lockfile import LockFile, get_lockfile_path

        lock = LockFile.read(get_lockfile_path(tmp_path))
        result = _check_content_integrity(tmp_path, lock)
        assert result.passed, result.details

    def test_hash_skips_entry_without_hash(self, tmp_path):
        # File listed in deployed_files but with no entry in
        # deployed_file_hashes (e.g. directories) must not raise.
        _make_deployed_file(tmp_path, ".github/prompts/no-hash.md", "stuff\n")
        _write_lockfile(
            tmp_path,
            textwrap.dedent("""\
                lockfile_version: '1'
                generated_at: '2025-01-01T00:00:00Z'
                dependencies:
                  - repo_url: owner/repo
                    deployed_files:
                      - .github/prompts/no-hash.md
            """),
        )
        from apm_cli.deps.lockfile import LockFile, get_lockfile_path

        lock = LockFile.read(get_lockfile_path(tmp_path))
        result = _check_content_integrity(tmp_path, lock)
        assert result.passed, result.details

    def test_hash_skips_symlink(self, tmp_path):
        import os
        from apm_cli.utils.content_hash import compute_file_hash

        # Create a real target file outside the deployed path
        target = tmp_path / "target.md"
        target.write_text("target content\n", encoding="utf-8")
        # Place a symlink at the deployed path
        link_path = tmp_path / ".github/prompts/link.md"
        link_path.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(target, link_path)

        # Record an obviously wrong hash -- symlinks must be skipped, not
        # flagged.
        _write_lockfile(
            tmp_path,
            textwrap.dedent("""\
                lockfile_version: '1'
                generated_at: '2025-01-01T00:00:00Z'
                dependencies:
                  - repo_url: owner/repo
                    deployed_files:
                      - .github/prompts/link.md
                    deployed_file_hashes:
                      .github/prompts/link.md: 'sha256:deadbeef'
            """),
        )
        from apm_cli.deps.lockfile import LockFile, get_lockfile_path

        lock = LockFile.read(get_lockfile_path(tmp_path))
        result = _check_content_integrity(tmp_path, lock)
        assert result.passed, result.details

    def test_hash_covers_self_entry_local_files(self, tmp_path):
        # Local content lives in lock.local_deployed_files / hashes; the
        # LockFile loader synthesizes a self-entry into lock.dependencies,
        # so iterating dependencies.items() must catch drift here too.
        from apm_cli.utils.content_hash import compute_file_hash

        _make_deployed_file(tmp_path, ".github/prompts/local.md", "local v1\n")
        recorded_hash = compute_file_hash(tmp_path / ".github/prompts/local.md")
        _write_lockfile(
            tmp_path,
            textwrap.dedent(f"""\
                lockfile_version: '1'
                generated_at: '2025-01-01T00:00:00Z'
                local_deployed_files:
                  - .github/prompts/local.md
                local_deployed_file_hashes:
                  .github/prompts/local.md: '{recorded_hash}'
            """),
        )
        # Mutate local file
        (tmp_path / ".github/prompts/local.md").write_text(
            "local v2 tampered\n", encoding="utf-8"
        )

        from apm_cli.deps.lockfile import LockFile, get_lockfile_path

        lock = LockFile.read(get_lockfile_path(tmp_path))
        result = _check_content_integrity(tmp_path, lock)
        assert not result.passed
        assert any(
            "hash-drift" in d and "local.md" in d for d in result.details
        ), result.details


# -- Aggregate runner ----------------------------------------------


class TestRunBaselineChecks:
    def test_all_pass(self, tmp_path):
        _write_apm_yml(tmp_path, deps=["owner/repo#v1.0.0"])
        _make_deployed_file(tmp_path, ".github/prompts/test.md")
        _write_lockfile(
            tmp_path,
            textwrap.dedent("""\
                lockfile_version: '1'
                generated_at: '2025-01-01T00:00:00Z'
                dependencies:
                  - repo_url: owner/repo
                    resolved_ref: v1.0.0
                    deployed_files:
                      - .github/prompts/test.md
            """),
        )
        result = run_baseline_checks(tmp_path)
        assert result.passed
        assert len(result.checks) == 7  # all 7 checks ran (incl. includes-consent)

    def test_mixed_pass_fail(self, tmp_path):
        # Ref mismatch (fail) + missing file (fail) + clean otherwise
        # Use fail_fast=False to let all checks run
        _write_apm_yml(tmp_path, deps=["owner/repo#v2.0.0"])
        _write_lockfile(
            tmp_path,
            textwrap.dedent("""\
                lockfile_version: '1'
                generated_at: '2025-01-01T00:00:00Z'
                dependencies:
                  - repo_url: owner/repo
                    resolved_ref: v1.0.0
                    deployed_files:
                      - .github/prompts/gone.md
            """),
        )
        result = run_baseline_checks(tmp_path, fail_fast=False)
        assert not result.passed
        assert len(result.failed_checks) >= 2
        failed_names = {c.name for c in result.failed_checks}
        assert "ref-consistency" in failed_names
        assert "deployed-files-present" in failed_names

    def test_no_apm_yml(self, tmp_path):
        result = run_baseline_checks(tmp_path)
        assert result.passed
        assert len(result.checks) == 1  # only lockfile-exists

    def test_stops_early_on_lockfile_missing(self, tmp_path):
        _write_apm_yml(tmp_path, deps=["owner/repo"])
        result = run_baseline_checks(tmp_path)
        assert not result.passed
        assert len(result.checks) == 1
        assert result.checks[0].name == "lockfile-exists"

    def test_fail_fast_stops_after_first_failure(self, tmp_path):
        """fail_fast=True (default) stops after the first failing check."""
        _write_apm_yml(tmp_path, deps=["owner/repo#v2.0.0"])
        _write_lockfile(
            tmp_path,
            textwrap.dedent("""\
                lockfile_version: '1'
                generated_at: '2025-01-01T00:00:00Z'
                dependencies:
                  - repo_url: owner/repo
                    resolved_ref: v1.0.0
                    deployed_files:
                      - .github/prompts/gone.md
            """),
        )
        result = run_baseline_checks(tmp_path, fail_fast=True)
        assert not result.passed
        # Should stop after ref-consistency (first failure), not run deployed-files
        assert len(result.failed_checks) == 1
        assert result.failed_checks[0].name == "ref-consistency"

    def test_fail_fast_false_runs_all_checks(self, tmp_path):
        """fail_fast=False runs all checks even after a failure."""
        _write_apm_yml(tmp_path, deps=["owner/repo#v2.0.0"])
        _write_lockfile(
            tmp_path,
            textwrap.dedent("""\
                lockfile_version: '1'
                generated_at: '2025-01-01T00:00:00Z'
                dependencies:
                  - repo_url: owner/repo
                    resolved_ref: v1.0.0
                    deployed_files:
                      - .github/prompts/gone.md
            """),
        )
        result = run_baseline_checks(tmp_path, fail_fast=False)
        assert not result.passed
        assert len(result.failed_checks) >= 2


# -- Serialization -------------------------------------------------


class TestSerialization:
    def test_to_json(self):
        result = CIAuditResult(
            checks=[
                CheckResult(name="a", passed=True, message="ok"),
                CheckResult(name="b", passed=False, message="bad", details=["x"]),
            ]
        )
        j = result.to_json()
        assert j["passed"] is False
        assert j["summary"]["total"] == 2
        assert j["summary"]["passed"] == 1
        assert j["summary"]["failed"] == 1
        assert len(j["checks"]) == 2

    def test_to_sarif(self):
        result = CIAuditResult(
            checks=[
                CheckResult(name="a", passed=True, message="ok"),
                CheckResult(name="b", passed=False, message="bad", details=["detail1"]),
            ]
        )
        s = result.to_sarif()
        assert s["version"] == "2.1.0"
        runs = s["runs"]
        assert len(runs) == 1
        assert len(runs[0]["results"]) == 1
        assert runs[0]["results"][0]["ruleId"] == "b"
        assert runs[0]["results"][0]["message"]["text"] == "detail1"

    def test_passed_property_all_pass(self):
        result = CIAuditResult(
            checks=[
                CheckResult(name="a", passed=True, message="ok"),
                CheckResult(name="b", passed=True, message="ok"),
            ]
        )
        assert result.passed is True

    def test_passed_property_one_fails(self):
        result = CIAuditResult(
            checks=[
                CheckResult(name="a", passed=True, message="ok"),
                CheckResult(name="b", passed=False, message="bad"),
            ]
        )
        assert result.passed is False

    def test_sarif_no_results_when_all_pass(self):
        result = CIAuditResult(
            checks=[
                CheckResult(name="a", passed=True, message="ok"),
            ]
        )
        s = result.to_sarif()
        assert s["runs"][0]["results"] == []
        assert s["runs"][0]["tool"]["driver"]["rules"] == []

    def test_sarif_uses_message_when_no_details(self):
        result = CIAuditResult(
            checks=[
                CheckResult(name="c", passed=False, message="the message"),
            ]
        )
        s = result.to_sarif()
        assert s["runs"][0]["results"][0]["message"]["text"] == "the message"


# -- Local-only repo support (issue #887) --------------------------


class TestLocalOnlyRepoSupport:
    """Audit must support repos with only local content (no remote deps).

    A local-only repo declares no APM/MCP deps in apm.yml but the lockfile
    records the project's own local content via ``local_deployed_files``,
    which the LockFile loader synthesizes as a "." self-entry in
    ``lock.dependencies``.
    """

    def test_lockfile_exists_passes_when_local_content_recorded(self, tmp_path):
        """(a) Empty manifest deps + non-empty local_deployed_files in
        lockfile must not short-circuit with 'no dependencies declared'.
        It must require/accept the lockfile so downstream checks run."""
        _make_deployed_file(tmp_path, ".github/prompts/local.prompt.md", "# main\n")
        _write_apm_yml(tmp_path)  # no deps
        _write_lockfile(
            tmp_path,
            textwrap.dedent("""\
                lockfile_version: '1'
                generated_at: '2025-01-01T00:00:00Z'
                dependencies: []
                local_deployed_files:
                  - .github/prompts/local.prompt.md
            """),
        )
        result = _check_lockfile_exists(tmp_path)
        assert result.passed
        assert "lockfile present" in result.message.lower()
        # Must NOT have been short-circuited as "no dependencies declared"
        assert "not required" not in result.message.lower()

    def test_lockfile_exists_still_passes_when_no_deps_and_no_local(self, tmp_path):
        """Regression guard: empty manifest + empty/absent lockfile still
        returns the 'no dependencies declared' fast-path (no false fail)."""
        _write_apm_yml(tmp_path)  # no deps
        # No lockfile on disk at all.
        result = _check_lockfile_exists(tmp_path)
        assert result.passed
        assert "not required" in result.message.lower()

    def test_aggregate_runs_deployed_files_check_for_local_only_repo(self, tmp_path):
        """(c) Aggregate must NOT short-circuit before deployed-files-present
        runs against the synthesized self-entry."""
        # File declared in lockfile but missing on disk -> deployed check fails.
        _write_apm_yml(tmp_path)  # no deps
        _write_lockfile(
            tmp_path,
            textwrap.dedent("""\
                lockfile_version: '1'
                generated_at: '2025-01-01T00:00:00Z'
                dependencies: []
                local_deployed_files:
                  - .github/prompts/missing.prompt.md
            """),
        )
        result = run_baseline_checks(tmp_path, fail_fast=False)
        check_names = {c.name for c in result.checks}
        # Aggregate must execute deployed-files-present, not stop after
        # lockfile-exists.
        assert "deployed-files-present" in check_names
        # And it must FAIL because the self-entry's file is missing.
        deployed = next(c for c in result.checks if c.name == "deployed-files-present")
        assert not deployed.passed
        assert ".github/prompts/missing.prompt.md" in deployed.details

    def test_aggregate_passes_for_clean_local_only_repo(self, tmp_path):
        """End-to-end happy path: local-only repo with file on disk passes."""
        _make_deployed_file(tmp_path, ".github/prompts/local.prompt.md", "# main\n")
        _write_apm_yml(tmp_path)  # no deps
        _write_lockfile(
            tmp_path,
            textwrap.dedent("""\
                lockfile_version: '1'
                generated_at: '2025-01-01T00:00:00Z'
                dependencies: []
                local_deployed_files:
                  - .github/prompts/local.prompt.md
            """),
        )
        result = run_baseline_checks(tmp_path, fail_fast=False)
        assert result.passed, [
            (c.name, c.message, c.details) for c in result.failed_checks
        ]
        check_names = {c.name for c in result.checks}
        assert "deployed-files-present" in check_names
        assert "no-orphaned-packages" in check_names

    def test_no_orphans_self_entry_alone_not_flagged(self, tmp_path):
        """(b) Lockfile with only the '.' self-entry + manifest with no deps
        must not flag the self-entry as orphaned."""
        _write_apm_yml(tmp_path)  # no deps
        _write_lockfile(
            tmp_path,
            textwrap.dedent("""\
                lockfile_version: '1'
                generated_at: '2025-01-01T00:00:00Z'
                dependencies: []
                local_deployed_files:
                  - .github/prompts/local.prompt.md
            """),
        )
        from apm_cli.models.apm_package import APMPackage
        from apm_cli.deps.lockfile import LockFile, get_lockfile_path, _SELF_KEY

        manifest = APMPackage.from_apm_yml(tmp_path / "apm.yml")
        lock = LockFile.read(get_lockfile_path(tmp_path))
        # Sanity: the loader synthesized the self-entry.
        assert _SELF_KEY in lock.dependencies
        result = _check_no_orphans(manifest, lock)
        assert result.passed, result.details

    def test_no_orphans_self_entry_with_declared_local_dep(self, tmp_path):
        """(b) Self-entry + a declared local-path dep both present -> no orphans."""
        # Create the local package directory so manifest parsing accepts it.
        local_pkg = tmp_path / "packages" / "shared"
        local_pkg.mkdir(parents=True)
        (local_pkg / "apm.yml").write_text(
            "name: shared\nversion: '1.0.0'\n", encoding="utf-8"
        )

        _write_apm_yml(tmp_path, deps=["./packages/shared"])
        _write_lockfile(
            tmp_path,
            textwrap.dedent("""\
                lockfile_version: '1'
                generated_at: '2025-01-01T00:00:00Z'
                dependencies:
                  - repo_url: <local>
                    source: local
                    local_path: ./packages/shared
                    deployed_files: []
                local_deployed_files:
                  - .github/prompts/local.prompt.md
            """),
        )
        from apm_cli.models.apm_package import APMPackage
        from apm_cli.deps.lockfile import LockFile, get_lockfile_path

        manifest = APMPackage.from_apm_yml(tmp_path / "apm.yml")
        lock = LockFile.read(get_lockfile_path(tmp_path))
        result = _check_no_orphans(manifest, lock)
        assert result.passed, result.details

    def test_no_orphans_still_detects_real_orphan_with_self_entry(self, tmp_path):
        """(b) Negative: self-entry must not mask a genuine remote orphan."""
        _write_apm_yml(tmp_path)  # manifest declares nothing
        _write_lockfile(
            tmp_path,
            textwrap.dedent("""\
                lockfile_version: '1'
                generated_at: '2025-01-01T00:00:00Z'
                dependencies:
                  - repo_url: extra/orphan
                    deployed_files: []
                local_deployed_files:
                  - .github/prompts/local.prompt.md
            """),
        )
        from apm_cli.models.apm_package import APMPackage
        from apm_cli.deps.lockfile import LockFile, get_lockfile_path, _SELF_KEY

        manifest = APMPackage.from_apm_yml(tmp_path / "apm.yml")
        lock = LockFile.read(get_lockfile_path(tmp_path))
        result = _check_no_orphans(manifest, lock)
        assert not result.passed
        assert "extra/orphan" in result.details
        # Self-entry must NOT appear in the orphan list.
        assert _SELF_KEY not in result.details


# -- Includes consent advisory (issue #887) ------------------------


class TestIncludesConsent:
    """Advisory check that nudges maintainers to declare 'includes:' when
    the lockfile records local content. Never hard-fails."""

    def _write_manifest(self, project: Path, includes_line: str | None) -> None:
        lines = ["name: test-project", "version: '1.0.0'"]
        if includes_line is not None:
            lines.append(includes_line)
        (project / "apm.yml").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _write_local_lock(self, project: Path, files: list[str]) -> None:
        if files:
            file_lines = "\n".join(f"  - {f}" for f in files)
            body = textwrap.dedent("""\
                lockfile_version: '1'
                generated_at: '2025-01-01T00:00:00Z'
                dependencies: []
                local_deployed_files:
                """) + file_lines + "\n"
        else:
            body = textwrap.dedent("""\
                lockfile_version: '1'
                generated_at: '2025-01-01T00:00:00Z'
                dependencies: []
                """)
        _write_lockfile(project, body)

    def _load(self, project: Path):
        from apm_cli.models.apm_package import APMPackage
        from apm_cli.deps.lockfile import LockFile, get_lockfile_path

        manifest = APMPackage.from_apm_yml(project / "apm.yml")
        lock = LockFile.read(get_lockfile_path(project))
        return manifest, lock

    def test_auto_with_local_content_passes_silently(self, tmp_path):
        from apm_cli.policy.ci_checks import _check_includes_consent

        _make_deployed_file(tmp_path, ".github/prompts/local.prompt.md")
        self._write_manifest(tmp_path, "includes: auto")
        self._write_local_lock(tmp_path, [".github/prompts/local.prompt.md"])

        manifest, lock = self._load(tmp_path)
        result = _check_includes_consent(manifest, lock)
        assert result.passed
        assert "[!]" not in result.message

    def test_absent_with_local_content_passes_with_advisory(self, tmp_path):
        from apm_cli.policy.ci_checks import _check_includes_consent

        _make_deployed_file(tmp_path, ".github/prompts/local.prompt.md")
        self._write_manifest(tmp_path, includes_line=None)
        self._write_local_lock(tmp_path, [".github/prompts/local.prompt.md"])

        manifest, lock = self._load(tmp_path)
        result = _check_includes_consent(manifest, lock)
        assert result.passed  # advisory, not a hard failure
        assert "consider adding 'includes: auto'" in result.message
        assert "includes:" in result.message
        assert "consent" in result.message.lower()
        # ASCII-only convention: no unicode warning glyphs.
        assert "\u26a0" not in result.message  # warning sign
        assert "\ufe0f" not in result.message  # variation selector

    def test_absent_with_no_local_content_skipped(self, tmp_path):
        from apm_cli.policy.ci_checks import _check_includes_consent

        self._write_manifest(tmp_path, includes_line=None)
        self._write_local_lock(tmp_path, files=[])

        manifest, lock = self._load(tmp_path)
        result = _check_includes_consent(manifest, lock)
        assert result.passed
        assert "[!]" not in result.message
        assert "skipped" in result.message.lower()

    def test_list_with_local_content_passes_silently(self, tmp_path):
        from apm_cli.policy.ci_checks import _check_includes_consent

        _make_deployed_file(tmp_path, ".github/prompts/local.prompt.md")
        self._write_manifest(
            tmp_path,
            "includes:\n  - .github/prompts/local.prompt.md\n  - .github/prompts/other.md",
        )
        self._write_local_lock(tmp_path, [".github/prompts/local.prompt.md"])

        manifest, lock = self._load(tmp_path)
        result = _check_includes_consent(manifest, lock)
        assert result.passed
        assert "[!]" not in result.message

    def test_auto_with_no_local_content_passes_silently(self, tmp_path):
        from apm_cli.policy.ci_checks import _check_includes_consent

        self._write_manifest(tmp_path, "includes: auto")
        self._write_local_lock(tmp_path, files=[])

        manifest, lock = self._load(tmp_path)
        result = _check_includes_consent(manifest, lock)
        assert result.passed
        assert "[!]" not in result.message

    def test_aggregate_runner_includes_consent_check_last(self, tmp_path):
        """Aggregate must run the consent check after content-integrity,
        producing a 7th check entry with the [!] advisory when applicable."""
        _make_deployed_file(tmp_path, ".github/prompts/local.prompt.md")
        self._write_manifest(tmp_path, includes_line=None)  # no includes:
        self._write_local_lock(tmp_path, [".github/prompts/local.prompt.md"])

        result = run_baseline_checks(tmp_path, fail_fast=False)
        assert result.passed, [
            (c.name, c.message, c.details) for c in result.failed_checks
        ]
        names = [c.name for c in result.checks]
        assert "includes-consent" in names
        assert names[-1] == "includes-consent"  # appears last
        consent = next(c for c in result.checks if c.name == "includes-consent")
        assert consent.passed
        assert "consider adding 'includes: auto'" in consent.message
