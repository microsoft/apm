"""Integration tests for ``apm audit`` drift detection (Phase D).

Covers:
  * Section A -- 9 drift cases (modified / unintegrated / orphaned, plus
    normalization-driven false-positive guards for CRLF, BOM, and the
    injected ``Build ID`` marker).
  * Section B -- regression traps tied to specific past PRs.
  * Section C -- edge cases (no lockfile, corrupt lockfile, empty
    primitives, untracked governed files).
  * Section D -- multi-target projects (copilot + claude).
  * Section E -- ``--no-drift`` flag, mutex with ``--strip``/``--file``,
    warning routing, and CI JSON / text output shapes.

Anti-patterns enforced (per matrix Section 10):
  * ``write_bytes`` only -- never ``write_text`` (avoids platform line-ending
    surprises).
  * No URL substring matching (we do not assert on URLs at all here).
  * ``catch_exceptions=False`` so Click's exception chaining surfaces.
  * ASCII-only assertions (no Unicode box characters or emoji).
  * ``CliRunner()`` without ``mix_stderr=`` -- Click 8.x removed that kwarg
    and always provides ``result.stdout`` / ``result.stderr`` separately.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
import yaml
from click.testing import CliRunner

from apm_cli.cli import cli

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_DEFAULT_INSTRUCTION = (
    b"---\n"
    b'applyTo: "**"\n'
    b"---\n"
    b"# Rules\n"
    b"\n"
    b"Follow these conventions strictly.\n"
    b"Never commit secrets.\n"
)


def _make_apm_project(
    tmp_path: Path,
    *,
    name: str = "drift-fixture",
    version: str = "1.0.0",
    target: str | None = "copilot",
    files: Mapping[str, bytes] | None = None,
) -> Path:
    """Create a minimal APM project rooted under ``tmp_path``.

    The project has an ``apm.yml`` plus ``.apm/`` source content. After
    ``apm install`` runs against it the lockfile records a synthetic
    self-entry (``_SELF_KEY``) under ``local_deployed_files`` -- which is
    what the drift replay engine walks.

    ``files`` keys are paths *relative to ``.apm/``* (e.g.
    ``"instructions/rules.instructions.md"``).
    """
    project = tmp_path / name
    project.mkdir(parents=True, exist_ok=False)

    manifest: dict[str, Any] = {"name": name, "version": version}
    if target is not None:
        manifest["target"] = target
    (project / "apm.yml").write_bytes(yaml.safe_dump(manifest).encode("utf-8"))

    payload = (
        files
        if files is not None
        else {
            "instructions/rules.instructions.md": _DEFAULT_INSTRUCTION,
        }
    )
    for rel, content in payload.items():
        dest = project / ".apm" / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(content)

    return project


def _install(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Run ``apm install`` in ``project``; assert success."""
    monkeypatch.chdir(project)
    runner = CliRunner()
    result = runner.invoke(cli, ["install"], catch_exceptions=False)
    assert result.exit_code == 0, (
        f"apm install failed: exit={result.exit_code}\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


def _audit(
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
    *args: str,
) -> Any:
    """Run ``apm audit`` and return the Click result."""
    monkeypatch.chdir(project)
    runner = CliRunner()
    return runner.invoke(cli, ["audit", *args], catch_exceptions=False)


def _snapshot_tree(root: Path) -> dict[str, tuple[int, bytes]]:
    """Snapshot every file under ``root`` as ``{relpath: (size, sha256-bytes)}``.

    Used to prove the no-write contract -- ``apm audit`` MUST NOT mutate
    the working tree.
    """
    import hashlib

    snap: dict[str, tuple[int, bytes]] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        data = path.read_bytes()
        snap[rel] = (len(data), hashlib.sha256(data).digest())
    return snap


def _drift_paths(result_stdout: str) -> list[str]:
    """Extract drift entry paths from a ``--ci -f json`` payload."""
    payload = json.loads(result_stdout)
    drift_section = payload.get("drift") or {}
    entries = drift_section.get("drift") or []
    return [entry.get("path", "") for entry in entries]


def _drift_kinds(result_stdout: str) -> list[tuple[str, str]]:
    """Extract ``(path, kind)`` tuples from a ``--ci -f json`` payload."""
    payload = json.loads(result_stdout)
    entries = (payload.get("drift") or {}).get("drift") or []
    return [(e.get("path", ""), e.get("kind", "")) for e in entries]


def _checks_by_name(result_stdout: str) -> dict[str, dict[str, Any]]:
    payload = json.loads(result_stdout)
    return {c["name"]: c for c in payload.get("checks", [])}


def _assert_text_safe(result: Any, *needles: str) -> None:
    """Assert each needle appears in stdout-or-stderr (text mode)."""
    blob = (result.stdout or "") + "\n" + (result.stderr or "")
    for needle in needles:
        assert needle in blob, (
            f"missing {needle!r}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )


# ---------------------------------------------------------------------------
# Section A -- 9 drift cases
# ---------------------------------------------------------------------------


class TestSectionADriftCases:
    """Each test exercises one drift kind or one false-positive guard."""

    def test_a1_modified_simple_edit_to_deployed_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = _make_apm_project(tmp_path)
        _install(project, monkeypatch)

        deployed = project / ".github" / "instructions" / "rules.instructions.md"
        assert deployed.exists(), "fixture pre-condition: install deploys to .github/"
        deployed.write_bytes(b"# tampered\n")

        result = _audit(project, monkeypatch, "--ci", "-f", "json")
        assert result.exit_code == 1
        kinds = dict(_drift_kinds(result.stdout))
        assert kinds.get(".github/instructions/rules.instructions.md") == "modified"

    def test_a2_unintegrated_new_source_added_no_install(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = _make_apm_project(tmp_path)
        _install(project, monkeypatch)

        extra = project / ".apm" / "instructions" / "extra.instructions.md"
        extra.write_bytes(b'---\napplyTo: "**"\n---\n# Extra\nNew rule, never integrated.\n')

        result = _audit(project, monkeypatch, "--ci", "-f", "json")
        assert result.exit_code == 1
        kinds = dict(_drift_kinds(result.stdout))
        # The diff engine reports the *output* path that would have been
        # produced, not the source path.
        assert kinds.get(".github/instructions/extra.instructions.md") == "unintegrated"

    def test_a3_unintegrated_when_deployed_file_deleted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Deployed file deleted from disk -> scratch still produces it.

        The diff engine reports this as ``unintegrated`` (file present in
        scratch, absent from project) rather than ``orphaned`` -- the
        orphaned kind is reserved for files in project + lockfile but
        absent from the scratch reproduction.
        """
        project = _make_apm_project(tmp_path)
        _install(project, monkeypatch)

        deployed = project / ".github" / "instructions" / "rules.instructions.md"
        deployed.unlink()

        result = _audit(project, monkeypatch, "--ci", "-f", "json")
        assert result.exit_code == 1
        kinds = dict(_drift_kinds(result.stdout))
        assert kinds.get(".github/instructions/rules.instructions.md") in {
            "unintegrated",
            "orphaned",
        }

    def test_a4_orphaned_source_removed_but_output_remains(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Source file removed from ``.apm/`` after install.

        Scratch no longer reproduces the integrated output, but the file
        still lives on disk and is recorded in the lockfile -- this is
        the canonical ``orphaned`` case.
        """
        project = _make_apm_project(tmp_path)
        _install(project, monkeypatch)

        source = project / ".apm" / "instructions" / "rules.instructions.md"
        source.unlink()

        result = _audit(project, monkeypatch, "--ci", "-f", "json")
        assert result.exit_code == 1
        kinds = dict(_drift_kinds(result.stdout))
        assert kinds.get(".github/instructions/rules.instructions.md") == "orphaned"

    def test_a5_clean_state_no_drift_after_install(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = _make_apm_project(tmp_path)
        _install(project, monkeypatch)

        result = _audit(project, monkeypatch, "--ci", "-f", "json")
        assert result.exit_code == 0
        checks = _checks_by_name(result.stdout)
        assert checks["drift"]["passed"] is True
        assert _drift_paths(result.stdout) == []

    def test_a6_modified_multiple_files_all_reported(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = _make_apm_project(
            tmp_path,
            files={
                "instructions/a.instructions.md": (b'---\napplyTo: "**"\n---\n# A\nfirst.\n'),
                "instructions/b.instructions.md": (b'---\napplyTo: "**"\n---\n# B\nsecond.\n'),
            },
        )
        _install(project, monkeypatch)

        for name in ("a", "b"):
            (project / ".github" / "instructions" / f"{name}.instructions.md").write_bytes(
                f"# tampered-{name}\n".encode()
            )

        result = _audit(project, monkeypatch, "--ci", "-f", "json")
        assert result.exit_code == 1
        paths = sorted(_drift_paths(result.stdout))
        assert paths == [
            ".github/instructions/a.instructions.md",
            ".github/instructions/b.instructions.md",
        ]

    def test_a7_crlf_only_change_is_not_drift(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Normalization strips line endings before diff."""
        project = _make_apm_project(tmp_path)
        _install(project, monkeypatch)

        deployed = project / ".github" / "instructions" / "rules.instructions.md"
        original = deployed.read_bytes()
        assert b"\r\n" not in original, "fixture must start LF-only"
        deployed.write_bytes(original.replace(b"\n", b"\r\n"))

        # Bare audit (no --ci) -- avoids the unrelated content-integrity
        # baseline check which compares raw byte hashes.
        result = _audit(project, monkeypatch)
        assert result.exit_code == 0, (
            f"CRLF normalization regressed: stdout={result.stdout}\nstderr={result.stderr}"
        )
        _assert_text_safe(result, "no issues found")

    def test_a8_bom_only_change_is_not_drift(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = _make_apm_project(tmp_path)
        _install(project, monkeypatch)

        deployed = project / ".github" / "instructions" / "rules.instructions.md"
        original = deployed.read_bytes()
        deployed.write_bytes(b"\xef\xbb\xbf" + original)

        result = _audit(project, monkeypatch)
        assert result.exit_code == 0
        # Drift specifically must report no findings; the content-scan may
        # surface an info-level "unusual characters" finding which we
        # tolerate explicitly.
        assert "Drift detected" not in (result.stdout + result.stderr)

    def test_a9_build_id_only_change_is_not_drift(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Injected ``<!-- Build ID: ... -->`` lines are stripped pre-diff."""
        project = _make_apm_project(tmp_path)
        _install(project, monkeypatch)

        deployed = project / ".github" / "instructions" / "rules.instructions.md"
        original = deployed.read_bytes()
        deployed.write_bytes(original + b"<!-- Build ID: deadbeef0123 -->\n")

        result = _audit(project, monkeypatch)
        assert result.exit_code == 0
        assert "Drift detected" not in (result.stdout + result.stderr)


# ---------------------------------------------------------------------------
# Section B -- regression traps for past PRs
# ---------------------------------------------------------------------------


class TestSectionBRegressions:
    """Behaviours we have explicitly broken before."""

    def test_b1_self_entry_replays_local_apm_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression: drift must walk the synthetic ``_SELF_KEY`` dep.

        Without it the project's own ``.apm/`` content would be
        invisible to replay and any drift would silently pass.
        """
        project = _make_apm_project(tmp_path)
        _install(project, monkeypatch)

        deployed = project / ".github" / "instructions" / "rules.instructions.md"
        deployed.write_bytes(b"# tampered\n")

        result = _audit(project, monkeypatch, "--ci", "-f", "json")
        assert result.exit_code == 1
        # If the self-entry walk regressed, the drift list would be empty.
        assert _drift_paths(result.stdout), (
            "self-entry replay regressed -- drift returned no findings"
        )

    def test_b2_audit_ci_exit_code_propagates_drift_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression: ``--ci`` must exit non-zero when drift is found."""
        project = _make_apm_project(tmp_path)
        _install(project, monkeypatch)
        (project / ".github" / "instructions" / "rules.instructions.md").write_bytes(
            b"# tampered\n"
        )

        result = _audit(project, monkeypatch, "--ci")
        assert result.exit_code == 1

    def test_b3_text_renderer_uses_ascii_status_symbols(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression: drift output must stay within printable ASCII."""
        project = _make_apm_project(tmp_path)
        _install(project, monkeypatch)
        (project / ".github" / "instructions" / "rules.instructions.md").write_bytes(
            b"# tampered\n"
        )

        result = _audit(project, monkeypatch)
        # Scope ASCII assertion to drift-specific output lines (the
        # baseline audit table uses Rich box-drawing chars that are
        # tolerated for that table but forbidden in drift output).
        combined = (result.stdout or "") + (result.stderr or "")
        for line in combined.splitlines():
            if "Drift detected" not in line and not ("modified" in line and ".github" in line):
                continue
            for ch in line:
                assert ord(ch) < 128, f"non-ASCII char {ch!r} in drift line: {line!r}"
        # Strict: drift-specific markers must appear and be ASCII.
        for needle in ("[!]", "modified"):
            assert needle in combined

    def test_b4_clean_install_does_not_emit_false_drift_warning(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression: a clean tree must not log ``Drift detected``."""
        project = _make_apm_project(tmp_path)
        _install(project, monkeypatch)

        result = _audit(project, monkeypatch)
        assert result.exit_code == 0
        combined = (result.stdout or "") + (result.stderr or "")
        assert "Drift detected" not in combined


# ---------------------------------------------------------------------------
# Section C -- edge cases
# ---------------------------------------------------------------------------


class TestSectionCEdgeCases:
    def test_c1_no_lockfile_bare_audit_skips_silently(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fresh repo with no lockfile -- bare ``apm audit`` exits 0.

        The current implementation treats ``apm.lock.yaml`` absence as
        "nothing to scan" rather than a usage error. This test pins that
        behaviour so a future change is intentional.
        """
        project = tmp_path / "fresh"
        project.mkdir()
        (project / "apm.yml").write_bytes(b"name: fresh\nversion: 1.0.0\n")

        result = _audit(project, monkeypatch)
        assert result.exit_code == 0
        assert "nothing to scan" in result.stdout.lower() or "no apm.lock" in result.stdout

    def test_c2_no_lockfile_ci_audit_passes_baseline(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No declared dependencies -> ``--ci`` baseline passes."""
        project = tmp_path / "fresh-ci"
        project.mkdir()
        (project / "apm.yml").write_bytes(b"name: fresh\nversion: 1.0.0\n")

        result = _audit(project, monkeypatch, "--ci")
        assert result.exit_code == 0

    def test_c3_corrupt_lockfile_yaml_skips_drift(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Bad YAML in lockfile -> ``LockFile.read`` returns ``None``.

        The audit command then skips drift silently. Pinning this so a
        future ``raise`` doesn't surprise users.
        """
        project = _make_apm_project(tmp_path)
        _install(project, monkeypatch)
        (project / "apm.lock.yaml").write_bytes(b"!!!{not valid yaml: [\n")

        result = _audit(project, monkeypatch)
        # Either passes silently (no parseable lockfile -> nothing to
        # scan) or emits a warning -- both acceptable, but no traceback.
        assert result.exit_code in {0, 1}
        assert "Traceback" not in result.stdout
        assert "Traceback" not in result.stderr

    def test_c4_empty_apm_directory_no_drift(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Project with no governed primitives -> no drift possible."""
        project = tmp_path / "empty"
        project.mkdir()
        (project / "apm.yml").write_bytes(b"name: empty\nversion: 1.0.0\n")
        # No .apm/ content at all.

        result = _audit(project, monkeypatch, "--ci")
        assert result.exit_code == 0

    def test_c5_untracked_governed_file_silently_ignored(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A hand-authored file in ``.github/`` that the lockfile does not
        track must NOT be reported as drift.

        This is the contract that lets users keep CODEOWNERS, manual
        instruction files, and other ungoverned content alongside APM
        deploys.
        """
        project = _make_apm_project(tmp_path)
        _install(project, monkeypatch)

        manual = project / ".github" / "instructions" / "manual.instructions.md"
        manual.write_bytes(b"# hand authored, not from APM\n")

        result = _audit(project, monkeypatch, "--ci", "-f", "json")
        assert result.exit_code == 0
        paths = _drift_paths(result.stdout)
        assert ".github/instructions/manual.instructions.md" not in paths

    def test_c6_apm_audit_makes_no_writes_to_working_tree(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No-write contract -- audit is read-only."""
        project = _make_apm_project(tmp_path)
        _install(project, monkeypatch)
        # Introduce drift so the replay engine actually does work.
        (project / ".github" / "instructions" / "rules.instructions.md").write_bytes(
            b"# tampered\n"
        )

        before = _snapshot_tree(project)
        result = _audit(project, monkeypatch, "--ci")
        after = _snapshot_tree(project)
        assert before == after, (
            "apm audit mutated the working tree (no-write contract violated)\n"
            f"exit={result.exit_code}\nstdout={result.stdout[:400]}"
        )

    def test_c7_repeated_audits_are_idempotent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = _make_apm_project(tmp_path)
        _install(project, monkeypatch)

        first = _audit(project, monkeypatch, "--ci", "-f", "json")
        second = _audit(project, monkeypatch, "--ci", "-f", "json")
        assert first.exit_code == second.exit_code == 0
        assert _drift_paths(first.stdout) == _drift_paths(second.stdout) == []


# ---------------------------------------------------------------------------
# Section D -- multi-target projects
# ---------------------------------------------------------------------------


class TestSectionDMultiTarget:
    """Multi-target apm.yml support.

    Pin: ``apm install`` honours apm.yml's ``target: a,b`` and writes to
    every named target's root_dir. The drift replay engine, however,
    currently re-integrates per-package using only the first/auto-detected
    target. Secondary targets (``.claude`` etc.) therefore appear as
    ``orphaned`` -- their on-disk files exist but the scratch reproduction
    does not produce them. These tests pin that observed behaviour so a
    future fix surfaces as an intentional change rather than silent drift.
    """

    def test_d1_multi_target_install_creates_all_target_outputs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = _make_apm_project(tmp_path, target="copilot,claude")
        _install(project, monkeypatch)
        # Sanity: both targets received the integrated file.
        assert (project / ".github" / "instructions" / "rules.instructions.md").exists()
        assert (project / ".claude" / "rules" / "rules.md").exists()

    def test_d2_multi_target_drift_reports_secondary_target_files(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Secondary target files are visible to the drift engine.

        Whether reported as ``orphaned`` (current replay limitation) or
        ``modified`` (after a future fix), the secondary target's
        deployed file MUST appear in the drift findings if it is
        tampered with -- it must never be silently ignored.
        """
        project = _make_apm_project(tmp_path, target="copilot,claude")
        _install(project, monkeypatch)
        (project / ".claude" / "rules" / "rules.md").write_bytes(b"# tampered\n")

        result = _audit(project, monkeypatch, "--ci", "-f", "json")
        assert result.exit_code == 1
        kinds = dict(_drift_kinds(result.stdout))
        assert ".claude/rules/rules.md" in kinds, (
            f"secondary target was silently ignored: kinds={kinds}"
        )
        # Acceptable kinds today: orphaned (replay missed it) or modified
        # (replay reproduced it and content differs). Both prove the
        # secondary target is not invisible.
        assert kinds[".claude/rules/rules.md"] in {"modified", "orphaned"}

    def test_d3_multi_target_primary_target_drift_detected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Primary (auto-detected) target drift is always reported."""
        project = _make_apm_project(tmp_path, target="copilot,claude")
        _install(project, monkeypatch)
        (project / ".github" / "instructions" / "rules.instructions.md").write_bytes(
            b"# tampered-copilot\n"
        )

        result = _audit(project, monkeypatch, "--ci", "-f", "json")
        assert result.exit_code == 1
        kinds = dict(_drift_kinds(result.stdout))
        assert kinds.get(".github/instructions/rules.instructions.md") == "modified"


# ---------------------------------------------------------------------------
# Section E -- --no-drift opt-out and CLI surface
# ---------------------------------------------------------------------------


class TestSectionENoDriftFlag:
    def test_e1_no_drift_with_strip_is_usage_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = _make_apm_project(tmp_path)
        _install(project, monkeypatch)

        result = _audit(project, monkeypatch, "--no-drift", "--strip")
        assert result.exit_code == 2
        assert "no-drift" in result.stderr.lower()
        assert "strip" in result.stderr.lower()

    def test_e2_no_drift_with_file_is_usage_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = _make_apm_project(tmp_path)
        _install(project, monkeypatch)

        result = _audit(
            project,
            monkeypatch,
            "--no-drift",
            "--file",
            ".github/instructions/rules.instructions.md",
        )
        assert result.exit_code == 2
        assert "no-drift" in result.stderr.lower()

    def test_e3_no_drift_warning_routed_to_stderr_text_mode(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The warning must appear on stderr (not stdout) in text mode."""
        project = _make_apm_project(tmp_path)
        _install(project, monkeypatch)

        result = _audit(project, monkeypatch, "--no-drift")
        assert result.exit_code == 0
        assert "drift detection skipped" in result.stderr.lower()
        assert "drift detection skipped" not in result.stdout.lower()

    def test_e4_no_drift_suppresses_warning_in_json_mode(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """JSON consumers must not see the human warning."""
        project = _make_apm_project(tmp_path)
        _install(project, monkeypatch)

        result = _audit(project, monkeypatch, "--ci", "--no-drift", "-f", "json")
        # JSON must be parseable and must NOT contain the drift section.
        data = json.loads(result.stdout)
        assert "drift" not in data, (
            f"expected no drift section under --no-drift, got: {list(data.keys())}"
        )
        assert "drift detection skipped" not in result.stderr.lower()

    def test_e5_no_drift_with_modified_file_skips_drift_check(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """--no-drift removes the ``drift`` check from the CI gate.

        Note: ``content-integrity`` (a separate baseline check, PR #889)
        ALSO catches modified files via hashes, so the overall exit code
        may still be 1. The point of this test is that the *drift* check
        is absent, not that exit becomes 0.
        """
        project = _make_apm_project(tmp_path)
        _install(project, monkeypatch)
        (project / ".github" / "instructions" / "rules.instructions.md").write_bytes(
            b"# tampered\n"
        )

        result = _audit(project, monkeypatch, "--ci", "--no-drift", "-f", "json")
        checks = _checks_by_name(result.stdout)
        assert "drift" not in checks, (
            f"--no-drift should remove the drift check, got: {list(checks)}"
        )

    def test_e6_default_audit_runs_drift_check(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Drift must be on by default in ``--ci`` mode."""
        project = _make_apm_project(tmp_path)
        _install(project, monkeypatch)

        result = _audit(project, monkeypatch, "--ci", "-f", "json")
        checks = _checks_by_name(result.stdout)
        assert "drift" in checks, "drift check missing from default --ci payload"
        assert checks["drift"]["passed"] is True

    def test_e7_default_audit_emits_no_skip_warning(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = _make_apm_project(tmp_path)
        _install(project, monkeypatch)

        result = _audit(project, monkeypatch)
        combined = (result.stdout or "") + (result.stderr or "")
        assert "drift detection skipped" not in combined.lower()

    def test_e8_no_drift_help_text_documents_flag(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["audit", "--help"], catch_exceptions=False)
        assert result.exit_code == 0
        assert "--no-drift" in result.stdout

    def test_e9_text_mode_drift_summary_includes_kind_and_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = _make_apm_project(tmp_path)
        _install(project, monkeypatch)
        (project / ".github" / "instructions" / "rules.instructions.md").write_bytes(
            b"# tampered\n"
        )

        result = _audit(project, monkeypatch)
        combined = result.stdout + result.stderr
        assert "modified" in combined
        assert ".github/instructions/rules.instructions.md" in combined

    def test_e10_bare_audit_surfaces_cache_miss_on_stderr(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When drift fails (CacheMissError, CachePinError) and produces no
        findings, bare ``apm audit`` MUST tell the user on stderr -- silence
        is a UX trap (user cannot tell "drift was clean" from "drift never
        ran"). Per dev-ux + cli-logging-ux panel feedback (PR #1137)."""
        from apm_cli.install.drift import CacheMissError

        project = _make_apm_project(tmp_path)
        _install(project, monkeypatch)

        def _boom(*_args, **_kwargs):
            raise CacheMissError(
                "cache miss for org/foo@deadbeef: expected /tmp/x; "
                "run 'apm install' to populate the cache"
            )

        # Patch the symbol where ci_checks._check_drift looks it up.
        monkeypatch.setattr("apm_cli.install.drift.run_replay", _boom)

        result = _audit(project, monkeypatch)
        # Bare audit is advisory: exit code is not gated on drift failure.
        assert result.exit_code in {0, 2}
        # The stderr-warning contract.
        assert "drift check could not run" in result.stderr.lower()
        assert "cache miss" in result.stderr.lower()
