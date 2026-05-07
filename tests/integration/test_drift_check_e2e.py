"""End-to-end tests for ``apm audit`` drift detection.

These tests prove the user-observable contracts of the drift system:

  * No-write contract -- ``apm audit`` never mutates the working tree
    (snapshot/diff every file under the project root).
  * Air-gap proof -- a populated cache lets the entire flow
    (install + audit) run with subprocess sentinels installed against
    ``git``/``gh``/``curl``/``wget``.
  * Performance smoke -- a small project audits in well under the 30s
    budget the matrix calls out.
  * SARIF and JSON shapes are stable enough for CI consumers.
  * The full ``install -> tamper -> audit -> reinstall -> audit`` loop
    converges back to a clean state.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest
import yaml
from click.testing import CliRunner

from apm_cli.cli import cli

# ---------------------------------------------------------------------------
# Helpers (kept local so this file is self-contained)
# ---------------------------------------------------------------------------


_INSTRUCTION_BYTES = b'---\napplyTo: "**"\n---\n# Rules\n\nE2E fixture content.\n'


def _make_project(tmp_path: Path, name: str = "drift-e2e") -> Path:
    project = tmp_path / name
    project.mkdir()
    (project / "apm.yml").write_bytes(
        yaml.safe_dump({"name": name, "version": "1.0.0", "target": "copilot"}).encode()
    )
    inst_dir = project / ".apm" / "instructions"
    inst_dir.mkdir(parents=True)
    (inst_dir / "rules.instructions.md").write_bytes(_INSTRUCTION_BYTES)
    return project


def _run(args: list[str]) -> Any:
    return CliRunner().invoke(cli, args, catch_exceptions=False)


def _snapshot_tree(root: Path) -> dict[str, tuple[int, bytes]]:
    import hashlib

    snap: dict[str, tuple[int, bytes]] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        data = path.read_bytes()
        snap[rel] = (len(data), hashlib.sha256(data).digest())
    return snap


_NETWORK_BINARIES = {"gh", "curl", "wget"}
_orig_subprocess_run = subprocess.run
_orig_subprocess_popen = subprocess.Popen


def _network_sentinel_run(*args, **kwargs):
    cmd = args[0] if args else kwargs.get("args", [])
    if isinstance(cmd, (list, tuple)) and cmd:
        if os.path.basename(str(cmd[0])) in _NETWORK_BINARIES:
            raise AssertionError(f"Unexpected network subprocess: {os.path.basename(str(cmd[0]))}")
    elif isinstance(cmd, str) and cmd:
        first = cmd.split(maxsplit=1)[0]
        if os.path.basename(first) in _NETWORK_BINARIES:
            raise AssertionError(f"Unexpected network subprocess: {first}")
    return _orig_subprocess_run(*args, **kwargs)


def _network_sentinel_popen(*args, **kwargs):
    cmd = args[0] if args else kwargs.get("args", [])
    if isinstance(cmd, (list, tuple)) and cmd:
        if os.path.basename(str(cmd[0])) in _NETWORK_BINARIES:
            raise AssertionError(f"Unexpected network Popen: {os.path.basename(str(cmd[0]))}")
    return _orig_subprocess_popen(*args, **kwargs)


# ---------------------------------------------------------------------------
# E2E tests
# ---------------------------------------------------------------------------


class TestDriftE2E:
    def test_apm_audit_makes_no_writes_to_working_tree(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No-write contract -- audit must never mutate the project tree.

        Snapshot every file (size + SHA-256) before and after a
        clean-state ``apm audit --ci`` run; trees must be byte-equal.
        """
        project = _make_project(tmp_path)
        monkeypatch.chdir(project)
        assert _run(["install"]).exit_code == 0

        before = _snapshot_tree(project)
        result = _run(["audit", "--ci"])
        after = _snapshot_tree(project)

        assert result.exit_code == 0
        assert before == after, "apm audit mutated the working tree (no-write contract violated)"

    def test_apm_audit_makes_no_writes_when_drift_present(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Even with drift, audit only reports -- never auto-heals."""
        project = _make_project(tmp_path)
        monkeypatch.chdir(project)
        assert _run(["install"]).exit_code == 0

        deployed = project / ".github" / "instructions" / "rules.instructions.md"
        deployed.write_bytes(b"# tampered\n")

        before = _snapshot_tree(project)
        result = _run(["audit", "--ci"])
        after = _snapshot_tree(project)

        assert result.exit_code == 1
        assert before == after, "apm audit auto-healed drift -- contract violated"
        # Tampered bytes still on disk afterwards.
        assert deployed.read_bytes() == b"# tampered\n"

    def test_audit_runs_without_network_subprocesses(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Air-gap proof -- block ``gh``/``curl``/``wget``; audit must succeed.

        ``git`` is intentionally NOT blocked: ``audit --ci`` runs
        ``git remote get-url origin`` for local policy auto-discovery
        (no network involved). Real network calls would route through
        ``gh``, ``curl``, or ``wget``.
        """
        project = _make_project(tmp_path)
        monkeypatch.chdir(project)
        assert _run(["install"]).exit_code == 0

        monkeypatch.setattr("subprocess.run", _network_sentinel_run)
        monkeypatch.setattr("subprocess.Popen", _network_sentinel_popen)

        result = _run(["audit", "--ci", "-f", "json"])
        assert result.exit_code == 0, (
            f"audit failed under network sentinel: {result.stdout}\n{result.stderr}"
        )

    def test_audit_completes_within_smoke_budget(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Performance smoke: a small project must audit in well under 30s.

        Budget is generous -- a real regression would balloon to
        minutes via repeated cache lookups or accidental re-downloads.
        """
        project = _make_project(tmp_path)
        monkeypatch.chdir(project)
        assert _run(["install"]).exit_code == 0

        start = time.perf_counter()
        result = _run(["audit", "--ci"])
        elapsed = time.perf_counter() - start

        assert result.exit_code == 0
        assert elapsed < 30.0, f"audit took {elapsed:.2f}s, budget is 30s"

    def test_install_audit_tamper_audit_reinstall_audit_loop(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Round-trip: clean -> tampered (drift) -> reinstall -> clean."""
        project = _make_project(tmp_path)
        monkeypatch.chdir(project)
        assert _run(["install"]).exit_code == 0

        # Phase 1: clean.
        assert _run(["audit", "--ci"]).exit_code == 0

        # Phase 2: tamper -> drift detected.
        deployed = project / ".github" / "instructions" / "rules.instructions.md"
        deployed.write_bytes(b"# tampered\n")
        assert _run(["audit", "--ci"]).exit_code == 1

        # Phase 3: reinstall heals -> drift cleared.
        assert _run(["install"]).exit_code == 0
        assert _run(["audit", "--ci"]).exit_code == 0

    def test_audit_ci_json_payload_has_stable_top_level_keys(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """JSON consumers depend on the top-level shape."""
        project = _make_project(tmp_path)
        monkeypatch.chdir(project)
        assert _run(["install"]).exit_code == 0

        result = _run(["audit", "--ci", "-f", "json"])
        payload = json.loads(result.stdout)

        # Required keys for the CI schema.
        for key in ("passed", "checks", "summary"):
            assert key in payload, f"missing top-level key {key!r}"

        # ``drift`` section present in default mode (no --no-drift).
        assert "drift" in payload
        assert isinstance(payload["drift"].get("drift"), list)

    def test_audit_ci_sarif_payload_is_valid_sarif(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """SARIF output remains parseable and has the SARIF skeleton."""
        project = _make_project(tmp_path)
        monkeypatch.chdir(project)
        assert _run(["install"]).exit_code == 0
        # Introduce drift so SARIF carries a result.
        (project / ".github" / "instructions" / "rules.instructions.md").write_bytes(
            b"# tampered\n"
        )

        result = _run(["audit", "--ci", "-f", "sarif"])
        # SARIF must parse as JSON.
        payload = json.loads(result.stdout)
        assert payload.get("$schema", "").endswith(".json") or "schema" in payload
        assert payload.get("version") == "2.1.0"
        runs = payload.get("runs") or []
        assert runs and isinstance(runs, list)
        # At least one drift result with the expected rule id prefix.
        rule_ids = {r.get("ruleId", "") for r in (runs[0].get("results") or [])}
        assert any(rid.startswith("apm/drift/") for rid in rule_ids), (
            f"no drift rule ids in SARIF: {rule_ids}"
        )

    def test_audit_text_mode_drift_section_uses_ascii_only(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Cross-platform encoding -- output must stay within ASCII.

        Per ``encoding.instructions.md``: Windows cp1252 raises on
        non-ASCII, so drift output must avoid emoji and box characters.
        We tolerate the few Rich-table box chars that the *baseline*
        audit table uses, but the drift-specific summary section must
        be pure ASCII.
        """
        project = _make_project(tmp_path)
        monkeypatch.chdir(project)
        assert _run(["install"]).exit_code == 0
        (project / ".github" / "instructions" / "rules.instructions.md").write_bytes(
            b"# tampered\n"
        )

        result = _run(["audit"])
        # Bare `apm audit` is advisory: drift is rendered but does not
        # fail the run (closes contract bug from PR #1137 review). Use
        # `apm audit --ci` for gated drift.
        assert result.exit_code == 0

        combined = (result.stdout or "") + (result.stderr or "")
        # Filter to lines that contain drift-specific markers.
        for line in combined.splitlines():
            if "Drift detected" in line or ("modified" in line and ".github" in line):
                for ch in line:
                    assert ord(ch) < 128, f"non-ASCII char {ch!r} in drift output line: {line!r}"

    def test_audit_with_force_install_cache_only_reuses_lockfile(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Audit-after-install must succeed without consulting the network.

        We do not patch subprocess here; instead, this is a sanity check
        that successive audits use only the lockfile + scratch area.
        """
        project = _make_project(tmp_path)
        monkeypatch.chdir(project)
        assert _run(["install"]).exit_code == 0

        for _ in range(3):
            r = _run(["audit", "--ci"])
            assert r.exit_code == 0

    def test_audit_cli_help_documents_drift_flag(self) -> None:
        """User-discoverable: ``apm audit --help`` mentions ``--no-drift``."""
        result = _run(["audit", "--help"])
        assert result.exit_code == 0
        assert "--no-drift" in result.stdout

    def test_bare_audit_with_drift_exits_zero_but_ci_audit_exits_one(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Drift exit-code contract (regression for PR #1137 review).

        * Bare ``apm audit`` is advisory: drift is rendered but exit
          stays 0 so users can run audit locally without their shell
          treating drift as a fatal error.
        * ``apm audit --ci`` is the explicit gate: the same drift state
          must produce exit code 1 so CI pipelines fail closed.
        """
        project = _make_project(tmp_path)
        monkeypatch.chdir(project)
        assert _run(["install"]).exit_code == 0

        # Tamper with a deployed file so drift is non-empty.
        (project / ".github" / "instructions" / "rules.instructions.md").write_bytes(
            b"# tampered locally\n"
        )

        bare = _run(["audit"])
        assert bare.exit_code == 0, (
            f"bare audit must be advisory on drift; got exit={bare.exit_code} "
            f"stdout={bare.stdout!r} stderr={bare.stderr!r}"
        )
        bare_combined = (bare.stdout or "") + (bare.stderr or "")
        assert "Drift detected" in bare_combined or "drift" in bare_combined.lower(), (
            "bare audit must still RENDER drift even though exit is 0"
        )

        ci = _run(["audit", "--ci"])
        assert ci.exit_code == 1, (
            f"--ci audit must gate on drift; got exit={ci.exit_code} "
            f"stdout={ci.stdout!r} stderr={ci.stderr!r}"
        )

    def test_apm_install_writes_cache_pin_marker_for_each_remote_dep(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """End-to-end proof of the cache-pin contract (PR #1137 follow-up).

        After ``apm install`` finishes, every cached non-local dep
        whose lockfile entry has a ``resolved_commit`` MUST carry a
        ``.apm-pin`` JSON marker recording that exact commit.

        This drives the supply-chain hardening: a future ``apm audit``
        on the same workspace will refuse to compare a stale cache
        against an updated lockfile because the marker will not
        match. We exercise the WRITE side here with a synthetic
        remote-style locked dep and the corresponding cached payload,
        because spinning up a real remote install is out of scope for
        a hermetic test.
        """
        from apm_cli.deps.lockfile import LockedDependency, LockFile, get_lockfile_path
        from apm_cli.install.cache_pin import (
            MARKER_FILENAME,
            SCHEMA_VERSION,
            verify_marker,
        )

        project = _make_project(tmp_path)
        monkeypatch.chdir(project)

        # Pre-populate apm_modules to simulate a previously-cached
        # remote dep whose marker is missing (e.g. cache pre-dates
        # this release of APM).
        apm_modules = project / "apm_modules"
        cached_pkg = apm_modules / "owner" / "repo"
        cached_pkg.mkdir(parents=True)
        (cached_pkg / "apm.yml").write_bytes(
            yaml.safe_dump({"name": "repo", "version": "1.0.0"}).encode()
        )
        assert not (cached_pkg / MARKER_FILENAME).exists()

        # Pre-seed apm.lock.yaml with a remote dep so LockfileBuilder
        # has something to mark (an `apm install` rebuild from manifest
        # alone would not include this dep).
        commit_sha = "deadc0de" * 5
        seed_lock = LockFile()
        seed_lock.add_dependency(
            LockedDependency(
                repo_url="owner/repo",
                host="github.com",
                resolved_commit=commit_sha,
                package_type="apm_package",
            )
        )
        seed_lock.save(get_lockfile_path(project))

        result = _run(["install"])
        assert result.exit_code == 0, (
            f"install failed: stdout={result.stdout!r} stderr={result.stderr!r}"
        )

        marker = cached_pkg / MARKER_FILENAME
        assert marker.exists(), "install must self-heal pre-existing caches by writing the marker"
        verify_marker(cached_pkg, commit_sha)
        payload = json.loads(marker.read_text(encoding="utf-8"))
        assert payload["schema_version"] == SCHEMA_VERSION
        assert payload["resolved_commit"] == commit_sha
