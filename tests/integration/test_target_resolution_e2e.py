"""E2E integration tests for target resolution overhaul (#1154).

Closes: #1154, #805, #650, #1056, #888, #891, #1122, #1130, #518, #1138
Anti-regression: AGENTS.md is NOT a codex signal; .github/ alone is NOT
a copilot signal; empty repo does NOT silently fall back to copilot.

Uses local bundles only (no network, no GITHUB_APM_PAT required).

# Implementation contract assumed by these tests (TDD-red):
# - Provenance line printed BEFORE any mutation by every install/compile/
#   targets command, format:
#     [i] Targets: <sorted, comma-space list>  (source: <descriptor>)
#   where <descriptor> is one of:
#     '--target flag', 'apm.yml', or 'auto-detect from <signal_list>'.
# - Exit code 2 for every target-resolution user error (no harness,
#   ambiguous, unknown target, mutex schema, missing manifest).
# - Error renderer produces a 3-section block: (a) what APM saw,
#   (b) 3 actionable commands, (c) an apm.yml snippet.
# - 'apm targets' is a read-only command that works without apm.yml.
# - 'apm install --dry-run' resolves and prints a planned-writes table
#   without touching disk.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from apm_cli.cli import cli

pytestmark = pytest.mark.integration

FIXTURES = Path(__file__).parent / "fixtures" / "target_resolution"

PROVENANCE_RE = re.compile(r"\[i\] Targets: (?P<targets>[\w, ]+?)\s+\(source: (?P<source>[^)]+)\)")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Strip APM env vars that could leak from the developer environment."""
    for var in ("APM_TARGET", "APM_CONFIG", "APM_HOME"):
        monkeypatch.delenv(var, raising=False)


def _setup(tmp_path: Path, scenario: str) -> Path:
    """Copy a scenario fixture (and the shared bundle) into ``tmp_path``."""
    project = tmp_path / scenario
    shutil.copytree(FIXTURES / scenario, project)
    shared_dst = tmp_path / "shared"
    if not shared_dst.exists():
        shutil.copytree(FIXTURES / "shared", shared_dst)
    return project


def assert_provenance(output: str, *, targets: list[str], source: str) -> None:
    """Assert provenance line is present with exact targets and source."""
    match = PROVENANCE_RE.search(output)
    assert match, f"No provenance line in output:\n{output[:800]}"
    actual = sorted(t.strip() for t in match.group("targets").split(","))
    assert actual == sorted(targets), f"Targets mismatch: got {actual}, expected {sorted(targets)}"
    assert source in match.group("source"), (
        f"Source mismatch: '{match.group('source')}' missing {source!r}"
    )


def _assert_error_three_sections(output: str) -> None:
    """Assert the error-renderer's 3-section structure is present.

    (a) what APM saw    -- 'detected' or 'looked' or 'no harness' phrasing
    (b) 3 actionable commands -- at least three lines starting with 'apm '
    (c) apm.yml snippet -- contains 'targets:' or 'target:' yaml-block
    """
    lower = output.lower()
    saw_section = any(
        marker in lower
        for marker in (
            "no harness detected",
            "multiple harnesses",
            "unknown target",
            "cannot use both",
            "no apm.yml",
        )
    )
    assert saw_section, "Error output missing 'what APM saw' section:\n" + output[:800]
    cmd_lines = [ln for ln in output.splitlines() if ln.strip().startswith("apm ")]
    assert len(cmd_lines) >= 3, (
        f"Error output missing 3 actionable 'apm ...' commands; "
        f"found {len(cmd_lines)}:\n{output[:800]}"
    )
    assert "targets:" in output or "target:" in output, (
        "Error output missing apm.yml snippet:\n" + output[:800]
    )


def _invoke(args: list[str], cwd: Path) -> CliRunner.invoke:
    """Run a CliRunner.invoke with cwd set via isolated_filesystem-like env."""
    runner = CliRunner()
    cur = os.getcwd()
    try:
        os.chdir(cwd)
        return runner.invoke(cli, args, catch_exceptions=False)
    finally:
        os.chdir(cur)


# ---------------------------------------------------------------------------
# S1 - S22: auto-detect signal whitelist + override
# ---------------------------------------------------------------------------


def test_s01_claude_md_only_deploys_to_dot_claude(tmp_path):
    """S1: Closes #1154 - CLAUDE.md without .claude/ must deploy to .claude/."""
    project = _setup(tmp_path, "s01_claude_md_only")
    result = _invoke(["install"], project)
    assert result.exit_code == 0, result.output
    assert_provenance(result.output, targets=["claude"], source="CLAUDE.md")
    assert (project / ".claude").is_dir()


def test_s02_github_dir_only_errors_no_harness(tmp_path):
    """S2: Closes #805 - .github/ alone (no copilot-instructions.md) is NOT a signal."""
    project = _setup(tmp_path, "s02_github_dir_only")
    result = _invoke(["install"], project)
    assert result.exit_code == 2, result.output
    assert "no harness detected" in result.output.lower()
    _assert_error_three_sections(result.output)


def test_s02b_copilot_instructions_file_deploys_copilot(tmp_path):
    """S2b: .github/copilot-instructions.md is the canonical copilot signal."""
    project = _setup(tmp_path, "s02b_copilot_instructions")
    result = _invoke(["install"], project)
    assert result.exit_code == 0, result.output
    assert_provenance(
        result.output,
        targets=["copilot"],
        source=".github/copilot-instructions.md",
    )


def test_s03_ambiguous_multi_signals_error(tmp_path):
    """S3: .claude/ + .cursor/ both present must error with ambiguity guidance."""
    project = _setup(tmp_path, "s03_ambiguous_multi")
    result = _invoke(["install"], project)
    assert result.exit_code == 2, result.output
    out_lower = result.output.lower()
    assert "multiple harnesses" in out_lower or "ambiguous" in out_lower
    assert "claude" in out_lower and "cursor" in out_lower
    _assert_error_three_sections(result.output)


def test_s04_greenfield_explicit_target_creates_dir(tmp_path):
    """S4: --target claude in greenfield creates .claude/ and deploys."""
    project = _setup(tmp_path, "s04_greenfield_explicit")
    result = _invoke(["install", "--target", "claude"], project)
    assert result.exit_code == 0, result.output
    assert_provenance(result.output, targets=["claude"], source="--target flag")
    assert (project / ".claude").is_dir()


def test_s05_apm_yml_targets_list_deploys_both(tmp_path):
    """S5: apm.yml targets: [claude, copilot] deploys to both, source=apm.yml."""
    project = _setup(tmp_path, "s05_apm_yml_multi")
    result = _invoke(["install"], project)
    assert result.exit_code == 0, result.output
    assert_provenance(result.output, targets=["claude", "copilot"], source="apm.yml")


def test_s05b_apm_yml_singular_target_sugar(tmp_path):
    """S5b: apm.yml legacy 'target: claude' still works as eternal sugar."""
    project = _setup(tmp_path, "s05b_apm_yml_singular")
    result = _invoke(["install"], project)
    assert result.exit_code == 0, result.output
    assert_provenance(result.output, targets=["claude"], source="apm.yml")


def test_s05c_apm_yml_both_target_and_targets_error(tmp_path):
    """S5c: apm.yml with BOTH 'target:' and 'targets:' is a validation error."""
    project = _setup(tmp_path, "s05c_apm_yml_both")
    result = _invoke(["install"], project)
    assert result.exit_code == 2, result.output
    assert "cannot use both" in result.output.lower() or (
        "target" in result.output.lower() and "targets" in result.output.lower()
    )
    _assert_error_three_sections(result.output)


def test_s06_dry_run_no_disk_writes(tmp_path):
    """S6: --dry-run resolves and prints planned writes; no files materialized."""
    project = _setup(tmp_path, "s06_dry_run")
    pre = {p for p in project.rglob("*") if p.is_file()}
    result = _invoke(["install", "--dry-run"], project)
    assert result.exit_code == 0, result.output
    assert "dry run" in result.output.lower() or "dry-run" in result.output.lower()
    post = {p for p in project.rglob("*") if p.is_file()}
    assert pre == post, f"Dry-run wrote files: {post - pre}"


def test_s07_compile_target_all_with_single_signal(tmp_path):
    """S7: 'apm compile --target all' with only CLAUDE.md expands to claude only."""
    project = _setup(tmp_path, "s07_compile_all_single")
    result = _invoke(["compile", "--target", "all"], project)
    # Either succeeds expanding to just claude, or errors cleanly.
    assert result.exit_code in (0, 2), result.output
    if result.exit_code == 0:
        # Provenance must mention claude and NOT pretend other targets.
        assert "claude" in result.output.lower()
        for absent in ("copilot", "cursor", "gemini", "windsurf", "opencode"):
            # Tolerate listing in help text; ensure provenance row is honest.
            match = PROVENANCE_RE.search(result.output)
            if match:
                resolved = match.group("targets")
                assert absent not in resolved.lower(), (
                    f"compile lied: claimed {absent} in {resolved}"
                )


def test_s07b_target_all_deprecation_visible(tmp_path):
    """S7b: '--target all' emits a user-visible deprecation warning.

    Anti-regression for convergence item 9: the deprecation must surface
    via the standard CLI warning channel, not warnings.warn (which is
    silenced by default in CLI output and would invisibly disappear).
    """
    project = _setup(tmp_path, "s07_compile_all_single")
    result = _invoke(["compile", "--target", "all"], project)
    # Exit code is permissive (compile may succeed or 2 on this fixture).
    out_lower = result.output.lower()
    assert "deprecated" in out_lower, (
        "deprecation warning must be visible in CLI output, not silenced via warnings.warn; "
        f"got: {result.output!r}"
    )
    assert "--all" in result.output, "deprecation must point at the replacement flag"


def test_s08_targets_command_table(tmp_path):
    """S8: Closes #1122/#1130/#518 - 'apm targets' shows discoverability table."""
    project = _setup(tmp_path, "s08_targets_cmd")
    result = _invoke(["targets"], project)
    assert result.exit_code == 0, result.output
    out_lower = result.output.lower()
    # Table must surface both detected harnesses and column headers.
    assert "claude" in out_lower
    assert "cursor" in out_lower
    assert "target" in out_lower and "status" in out_lower


def test_s08b_targets_json_output(tmp_path):
    """S8b: 'apm targets --json' returns parseable array of per-target objects.

    Anti-regression for the documented contract: array (not object envelope),
    one entry per canonical harness target, ordered by canonical order. CI
    scripts depend on this shape, so a regression here silently breaks every
    consumer that parses the JSON.
    """
    import json as _json

    project = _setup(tmp_path, "s08_targets_cmd")
    result = _invoke(["targets", "--json"], project)
    assert result.exit_code == 0, result.output
    payload = _json.loads(result.output)
    assert isinstance(payload, list), f"--json must emit a list, got {type(payload).__name__}"
    by_name = {row["target"]: row for row in payload}
    # claude is signalled (CLAUDE.md fixture), copilot is not.
    assert by_name["claude"]["status"] == "active"
    assert by_name["claude"]["source"], "active rows must report a source"
    assert by_name["copilot"]["status"] == "inactive"
    assert by_name["copilot"]["needs"] == ".github/copilot-instructions.md"
    # Default JSON must not include the agent-skills meta-target.
    assert "agent-skills" not in by_name
    # --all --json must add the meta-target row.
    result_all = _invoke(["targets", "--all", "--json"], project)
    assert result_all.exit_code == 0, result_all.output
    payload_all = _json.loads(result_all.output)
    by_name_all = {row["target"]: row for row in payload_all}
    assert "agent-skills" in by_name_all
    assert by_name_all["agent-skills"].get("meta_target") is True


def test_s09_unknown_target_errors_with_valid_list(tmp_path):
    """S9: --target unknown emits a UsageError that lists valid targets."""
    project = _setup(tmp_path, "s09_unknown_target")
    result = _invoke(["install", "--target", "unknown"], project)
    assert result.exit_code == 2, result.output
    out_lower = result.output.lower()
    assert "unknown" in out_lower or "invalid" in out_lower
    # Must list at least one canonical target name in the suggestion.
    assert "claude" in out_lower or "copilot" in out_lower


def test_s10_agents_md_only_errors_no_harness(tmp_path):
    """S10: AGENTS.md alone is NOT a codex signal (multi-harness output file)."""
    project = _setup(tmp_path, "s10_agents_md_only")
    result = _invoke(["install"], project)
    assert result.exit_code == 2, result.output
    assert "no harness detected" in result.output.lower()
    _assert_error_three_sections(result.output)


def test_s11_csv_multi_target_creates_both(tmp_path):
    """S11: --target claude,cursor (CSV) deploys to both, creates both dirs."""
    project = _setup(tmp_path, "s11_csv_multi_target")
    result = _invoke(["install", "--target", "claude,cursor"], project)
    assert result.exit_code == 0, result.output
    assert_provenance(result.output, targets=["claude", "cursor"], source="--target flag")
    assert (project / ".claude").is_dir()
    assert (project / ".cursor").is_dir()


def test_s12_empty_repo_errors_no_silent_copilot(tmp_path):
    """S12: empty repo + apm install must NOT silently fall back to copilot."""
    project = _setup(tmp_path, "s12_empty_repo")
    result = _invoke(["install"], project)
    assert result.exit_code == 2, result.output
    out_lower = result.output.lower()
    assert "no harness detected" in out_lower
    # Anti-regression: must not silently deploy to copilot.
    assert not (project / ".github" / "copilot-instructions.md").exists()
    _assert_error_three_sections(result.output)


@pytest.mark.xfail(
    reason="#650 global copilot path resolution; handled by three-guard collapse",
    strict=False,
)
def test_s13_global_copilot_no_silent_skip(tmp_path, monkeypatch):
    """S13: Closes #650 - 'apm install -g --target copilot' must not silently skip."""
    project = _setup(tmp_path, "s13_global_copilot")
    fakehome = tmp_path / "fakehome"
    fakehome.mkdir()
    env = os.environ.copy()
    env["HOME"] = str(fakehome)
    env["USERPROFILE"] = str(fakehome)
    for var in ("APM_TARGET", "APM_CONFIG", "APM_HOME"):
        env.pop(var, None)
    result = subprocess.run(
        [sys.executable, "-m", "apm_cli", "install", "-g", "--target", "copilot"],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    # Either the user-scope copilot path or a clear deploy line must appear.
    combined = result.stdout + result.stderr
    assert "copilot" in combined.lower()


def test_s14_no_manifest_errors_not_silent(tmp_path):
    """S14: Closes #1056 - apm install with no apm.yml/no package is a user error."""
    project = _setup(tmp_path, "s14_no_manifest")
    result = _invoke(["install"], project)
    assert result.exit_code != 0, result.output
    out_lower = result.output.lower()
    assert "apm.yml" in out_lower or "manifest" in out_lower or "no package" in out_lower


def test_s15_no_double_emission_with_existing_rules(tmp_path):
    """S15: Closes #1138 (partial) - install must not double-emit when .claude/rules/ exists."""
    project = _setup(tmp_path, "s15_claude_rules_exists")
    result = _invoke(["install", "--target", "claude"], project)
    assert result.exit_code == 0, result.output
    # Pre-existing rule must remain.
    assert (project / ".claude" / "rules" / "existing-rule.md").exists()
    # No skill duplicate at project root.
    root_skill_files = list(project.glob("hello/SKILL.md"))
    assert root_skill_files == []


def test_s16a_targets_inactive_shows_reasons(tmp_path):
    """S16a: 'apm targets' shows inactive targets with a reason column."""
    project = _setup(tmp_path, "s16a_targets_inactive")
    result = _invoke(["targets"], project)
    assert result.exit_code == 0, result.output
    out_lower = result.output.lower()
    assert "claude" in out_lower
    # An inactive row must justify itself (e.g., 'no signal').
    assert "no signal" in out_lower or "inactive" in out_lower


@pytest.mark.xfail(
    reason="#888 cwd anchoring partial; provenance project-root display",
    strict=False,
)
def test_s16b_provenance_uses_project_root(tmp_path):
    """S16b: Closes #888 (partial) - provenance reflects project root, not cwd."""
    project = _setup(tmp_path, "s16b_provenance_cwd")
    subdir = project / "subdir"
    result = _invoke(["install"], subdir)
    assert result.exit_code == 0, result.output
    assert_provenance(result.output, targets=["claude"], source="CLAUDE.md")


def test_s17_cursorrules_file_detects_cursor(tmp_path):
    """S17: .cursorrules file (no .cursor/ dir) is a valid cursor signal."""
    project = _setup(tmp_path, "s17_cursorrules_file")
    result = _invoke(["install"], project)
    assert result.exit_code == 0, result.output
    assert_provenance(result.output, targets=["cursor"], source=".cursorrules")


def test_s18_gemini_dir_detects_gemini(tmp_path):
    """S18: .gemini/ dir is a valid gemini signal."""
    project = _setup(tmp_path, "s18_gemini_dir")
    result = _invoke(["install"], project)
    assert result.exit_code == 0, result.output
    assert_provenance(result.output, targets=["gemini"], source=".gemini/")


def test_s19_opencode_dir_detects_opencode(tmp_path):
    """S19: .opencode/ dir is a valid opencode signal."""
    project = _setup(tmp_path, "s19_opencode_dir")
    result = _invoke(["install"], project)
    assert result.exit_code == 0, result.output
    assert_provenance(result.output, targets=["opencode"], source=".opencode/")


def test_s20_windsurf_dir_detects_windsurf(tmp_path):
    """S20: .windsurf/ dir is a valid windsurf signal."""
    project = _setup(tmp_path, "s20_windsurf_dir")
    result = _invoke(["install"], project)
    assert result.exit_code == 0, result.output
    assert_provenance(result.output, targets=["windsurf"], source=".windsurf/")


def test_s21_codex_dir_detects_codex(tmp_path):
    """S21: .codex/ dir is the canonical codex signal (NOT AGENTS.md)."""
    project = _setup(tmp_path, "s21_codex_dir")
    result = _invoke(["install"], project)
    assert result.exit_code == 0, result.output
    assert_provenance(result.output, targets=["codex"], source=".codex/")


def test_s22_gemini_md_only_creates_gemini_dir(tmp_path):
    """S22: GEMINI.md (no .gemini/) parallels CLAUDE.md - creates .gemini/."""
    project = _setup(tmp_path, "s22_gemini_md_only")
    result = _invoke(["install"], project)
    assert result.exit_code == 0, result.output
    assert_provenance(result.output, targets=["gemini"], source="GEMINI.md")
    assert (project / ".gemini").is_dir()


# ---------------------------------------------------------------------------
# S23: error renderer structure (parametrized over 4 error paths)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("scenario", "argv"),
    [
        ("s02_github_dir_only", ["install"]),
        ("s03_ambiguous_multi", ["install"]),
        ("s09_unknown_target", ["install", "--target", "unknown"]),
        ("s05c_apm_yml_both", ["install"]),
    ],
    ids=["no_harness", "ambiguous", "unknown_target", "schema_mutex"],
)
def test_s23_error_renderer_three_sections(tmp_path, scenario, argv):
    """S23: every target-resolution error has the 3-section unified format."""
    project = _setup(tmp_path, scenario)
    result = _invoke(argv, project)
    assert result.exit_code == 2, result.output
    _assert_error_three_sections(result.output)


# ---------------------------------------------------------------------------
# S24, S25: priority and explicit override
# ---------------------------------------------------------------------------


def test_s24_priority_flag_over_yaml(tmp_path):
    """S24: --target cursor overrides apm.yml targets:[claude]; deploy=cursor only."""
    project = _setup(tmp_path, "s24_priority_override")
    result = _invoke(["install", "--target", "cursor"], project)
    assert result.exit_code == 0, result.output
    assert_provenance(result.output, targets=["cursor"], source="--target flag")
    assert (project / ".cursor").is_dir()


def test_s25_copilot_alias_in_greenfield(tmp_path):
    """S25: --target copilot in greenfield deploys to copilot path."""
    project = _setup(tmp_path, "s25_copilot_alias")
    result = _invoke(["install", "--target", "copilot"], project)
    assert result.exit_code == 0, result.output
    assert_provenance(result.output, targets=["copilot"], source="--target flag")
