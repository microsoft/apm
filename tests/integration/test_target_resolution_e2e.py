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
from pathlib import Path

import pytest
from click.testing import CliRunner

from apm_cli.cli import cli
from apm_cli.utils.yaml_io import dump_yaml, load_yaml

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


def test_repository_manifest_pin_overrides_second_harness_signal(tmp_path):
    """The repository target pin must override contributor-local harness signals."""
    project = _setup(tmp_path, "s02b_copilot_instructions")
    repository_manifest = load_yaml(Path(__file__).parents[2] / "apm.yml") or {}
    assert repository_manifest.get("targets") == ["copilot"]

    fixture_manifest = load_yaml(project / "apm.yml") or {}
    fixture_manifest["targets"] = repository_manifest["targets"]
    dump_yaml(fixture_manifest, project / "apm.yml")

    claude_settings = project / ".claude" / "settings.local.json"
    claude_settings.parent.mkdir()
    claude_settings.write_text("{}\n", encoding="utf-8")

    result = _invoke(["install"], project)

    assert result.exit_code == 0, result.output
    assert_provenance(result.output, targets=["copilot"], source="apm.yml")
    assert list((project / ".claude").rglob("*")) == [claude_settings]


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


def test_s13_global_copilot_no_silent_skip(tmp_path, monkeypatch, apm_binary_path: Path):
    """S13: Closes #650, #4544 - bare 'apm install -g' with no user manifest is a user error.

    Like S14 at project scope, it exits 1, names the missing user manifest and
    the 'apm install -g <org/repo>' next step, and deploys nothing.
    """
    project = _setup(tmp_path, "s13_global_copilot")
    fakehome = tmp_path / "fakehome"
    fakehome.mkdir()
    env = os.environ.copy()
    env["HOME"] = str(fakehome)
    env["USERPROFILE"] = str(fakehome)
    for var in ("APM_TARGET", "APM_CONFIG", "APM_HOME"):
        env.pop(var, None)
    result = subprocess.run(
        [str(apm_binary_path), "install", "-g", "--target", "copilot"],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
    )
    combined = " ".join((result.stdout + result.stderr).split())
    assert result.returncode == 1, combined
    user_manifest = fakehome / ".apm" / "apm.yml"
    assert f"No {user_manifest} found" in combined
    assert "'apm install -g <org/repo>'" in combined
    assert not user_manifest.exists()
    assert not (fakehome / ".apm" / "apm.lock.yaml").exists()
    assert not (fakehome / ".copilot").exists()
    # Only APM's own lock files and the update-check cache may appear.
    written = [
        path.relative_to(fakehome).as_posix()
        for path in fakehome.rglob("*")
        if path.is_file()
        and path.name != ".apm-lifecycle.lock"
        and path.relative_to(fakehome).parts[0] != ".cache"
    ]
    assert written == [], written


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


def _snapshot_dir(directory: Path) -> dict[str, bytes]:
    """Capture relative paths and bytes of all files in directory."""
    return {
        p.relative_to(directory).as_posix(): p.read_bytes()
        for p in directory.glob("**/*")
        if p.is_file()
    }


def test_s16b_provenance_uses_project_root(tmp_path):
    """S16b: Closes #888, #4545 - provenance reflects project root, not cwd."""
    project = _setup(tmp_path, "s16b_provenance_cwd")
    subdir = project / "subdir"
    before = _snapshot_dir(subdir)
    result = _invoke(["install"], subdir)
    assert result.exit_code == 0, result.output
    assert_provenance(result.output, targets=["claude"], source="CLAUDE.md")
    assert _snapshot_dir(subdir) == before


def test_s16c_installed_cli_subdirectory_install(tmp_path, apm_binary_path: Path):
    """S16c: Closes #4545 - installed CLI install from subdirectory matches root install."""
    root_project = _setup(tmp_path / "root_run", "s16b_provenance_cwd")
    sub_project = _setup(tmp_path / "sub_run", "s16b_provenance_cwd")
    subdir = sub_project / "subdir"
    before = _snapshot_dir(subdir)
    env = os.environ.copy()

    res_root = subprocess.run(
        [str(apm_binary_path), "install"],
        cwd=root_project,
        env=env,
        capture_output=True,
        text=True,
    )
    assert res_root.returncode == 0, res_root.stderr

    res_sub = subprocess.run(
        [str(apm_binary_path), "install"],
        cwd=subdir,
        env=env,
        capture_output=True,
        text=True,
    )
    assert res_sub.returncode == 0, res_sub.stderr

    combined = res_sub.stdout + res_sub.stderr
    assert "Using apm.yml at" in combined
    assert f"project root: {sub_project.resolve()}" in combined
    assert_provenance(combined, targets=["claude"], source="CLAUDE.md")

    # Nothing written under subdir/
    assert _snapshot_dir(subdir) == before

    # Lockfile and deployed files match root install
    assert (sub_project / "apm.lock.yaml").read_text(encoding="utf-8") == (
        root_project / "apm.lock.yaml"
    ).read_text(encoding="utf-8")
    # The same deployed files with the same bytes (#4545 scope).
    sub_claude = _snapshot_dir(sub_project / ".claude")
    assert sub_claude, "the subdirectory install deployed nothing"
    assert sub_claude == _snapshot_dir(root_project / ".claude")


def test_s16d_installed_cli_subdirectory_add_package(tmp_path, apm_binary_path: Path):
    """Installed CLI: apm install <package> from subdirectory updates parent apm.yml and lockfile."""
    project = _setup(tmp_path / "project", "s16b_provenance_cwd")
    subdir = project / "subdir"
    ext_pkg = tmp_path / "ext_pkg"
    ext_pkg.mkdir()
    (ext_pkg / "apm.yml").write_text("name: ext-pkg\nversion: 0.1.0\n", encoding="utf-8")
    before = _snapshot_dir(subdir)
    res = subprocess.run(
        [str(apm_binary_path), "install", str(ext_pkg)],
        cwd=subdir,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, res.stderr
    combined = res.stdout + res.stderr
    assert "Using apm.yml at" in combined
    assert f"project root: {project.resolve()}" in combined
    assert (project / "apm.lock.yaml").exists()
    root_yml = (project / "apm.yml").read_text(encoding="utf-8")
    assert "ext_pkg" in root_yml
    assert _snapshot_dir(subdir) == before


def test_s16e_installed_cli_subdirectory_add_relative_package(tmp_path, apm_binary_path: Path):
    """Installed CLI: apm install ./pkg with pkg under subdir/ records ./subdir/pkg in root apm.yml."""
    project = _setup(tmp_path / "project", "s16b_provenance_cwd")
    subdir = project / "subdir"
    pkg_dir = subdir / "pkg"
    pkg_dir.mkdir()
    (pkg_dir / "apm.yml").write_text("name: local-sub-pkg\nversion: 0.1.0\n", encoding="utf-8")
    before = _snapshot_dir(subdir)
    res = subprocess.run(
        [str(apm_binary_path), "install", "./pkg"],
        cwd=subdir,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, res.stderr
    combined = res.stdout + res.stderr
    assert "Using apm.yml at" in combined
    assert f"project root: {project.resolve()}" in combined
    root_yml = (project / "apm.yml").read_text(encoding="utf-8")
    assert "./subdir/pkg" in root_yml
    assert (project / "apm.lock.yaml").exists()
    assert _snapshot_dir(subdir) == before


def test_s16f_installed_cli_nested_project_is_not_swallowed_by_its_parent(
    tmp_path, apm_binary_path: Path
):
    """A nested project with its own apm.yml installs as itself from below it (#4545).

    The nearest apm.yml wins: the parent's manifest, lockfile and deployed
    files are untouched, and nothing is written under the invoking directory.
    """
    parent = _setup(tmp_path / "project", "s16b_provenance_cwd")
    nested = parent / "subdir" / "nested"
    shutil.copytree(FIXTURES / "s16b_provenance_cwd", nested)
    (nested / "apm.yml").write_text("name: nested-project\nversion: 0.1.0\n", encoding="utf-8")
    invoking = nested / "subdir"
    parent_before = {
        path: data for path, data in _snapshot_dir(parent).items() if not path.startswith("subdir/")
    }
    invoking_before = _snapshot_dir(invoking)

    res = subprocess.run(
        [str(apm_binary_path), "install"],
        cwd=invoking,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
    )

    assert res.returncode == 0, res.stdout + res.stderr
    combined = res.stdout + res.stderr
    assert f"project root: {nested.resolve()}" in combined, combined
    assert (nested / "apm.lock.yaml").is_file()
    assert not (parent / "apm.lock.yaml").exists()
    assert {
        path: data for path, data in _snapshot_dir(parent).items() if not path.startswith("subdir/")
    } == parent_before
    assert _snapshot_dir(invoking) == invoking_before


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlinks")
def test_s16g_installed_cli_subdirectory_symlinked_file_is_still_refused(
    tmp_path, apm_binary_path: Path
):
    """Rebasing ./link from a subdirectory keeps the link, so install refuses it (#4545)."""
    project = _setup(tmp_path / "project", "s16b_provenance_cwd")
    subdir = project / "subdir"
    (project / "actual.prompt.md").write_text("# actual\n", encoding="utf-8")
    (subdir / "sel.prompt.md").symlink_to("../actual.prompt.md")
    manifest_before = (project / "apm.yml").read_bytes()

    res = subprocess.run(
        [str(apm_binary_path), "install", "./sel.prompt.md"],
        cwd=subdir,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
    )

    combined = " ".join((res.stdout + res.stderr).split())
    assert res.returncode != 0, combined
    assert "symlink" in combined, combined
    assert (project / "apm.yml").read_bytes() == manifest_before


def test_s16_control_root_flag_from_subdirectory(tmp_path):
    """Control: --root DIR from a subdirectory keeps today's sources and writes."""
    project = _setup(tmp_path, "s16b_provenance_cwd")
    subdir = project / "subdir"
    before = _snapshot_dir(subdir)
    deploy_dir = tmp_path / "deploy_target"
    result = _invoke(["install", "--root", str(deploy_dir)], subdir)
    # Today's behavior: sources stay in cwd (subdir), which has no apm.yml, so it errors.
    assert result.exit_code == 1, result.output
    assert "No apm.yml found" in result.output
    assert "Using apm.yml at" not in result.output
    assert _snapshot_dir(subdir) == before


def test_s16_control_root_flag_bootstrap_in_subdirectory(tmp_path):
    """Control: apm install --root . <package> from subdirectory still bootstraps in the subdirectory."""
    project = _setup(tmp_path, "s16b_provenance_cwd")
    subdir = project / "subdir"
    ext_pkg = tmp_path / "ext_pkg"
    ext_pkg.mkdir()
    (ext_pkg / "apm.yml").write_text("name: ext_pkg\nversion: 0.1.0\n", encoding="utf-8")
    result = _invoke(["install", "--root", ".", "--target", "claude", str(ext_pkg)], subdir)
    assert result.exit_code == 0, result.output
    assert (subdir / "apm.yml").exists()
    assert "ext_pkg" in (subdir / "apm.yml").read_text(encoding="utf-8")


def test_s16_control_nested_manifest_uses_nearest(tmp_path):
    """Control: cwd with its own apm.yml inside a parent project uses the nearest one."""
    project = _setup(tmp_path, "s16b_provenance_cwd")
    subdir = project / "subdir"
    (subdir / "apm.yml").write_text("name: child-project\nversion: 1.0.0\n", encoding="utf-8")
    result = _invoke(["install"], subdir)
    assert result.exit_code == 0, result.output
    # Operates on the nearest manifest in subdir, not the parent manifest.
    assert "Using apm.yml at" not in result.output
    assert not (subdir / ".claude").exists()


def test_s16_control_no_ancestor_manifest_errors(tmp_path):
    """Control: no ancestor apm.yml keeps the current error, and positional package still bootstraps."""
    empty_dir = tmp_path / "isolated"
    empty_dir.mkdir()
    res_bare = _invoke(["install"], empty_dir)
    assert res_bare.exit_code == 1, res_bare.output
    assert "No apm.yml found" in res_bare.output

    ext_pkg = tmp_path / "ext_pkg"
    ext_pkg.mkdir()
    (ext_pkg / "apm.yml").write_text("name: ext_pkg\nversion: 0.1.0\n", encoding="utf-8")
    res_add = _invoke(["install", "--target", "claude", str(ext_pkg)], empty_dir)
    assert res_add.exit_code == 0, res_add.output
    assert (empty_dir / "apm.yml").exists()
    assert "ext_pkg" in (empty_dir / "apm.yml").read_text(encoding="utf-8")


def test_s16_control_dry_run_from_subdirectory_previews_root(tmp_path):
    """Control: apm install --dry-run from subdirectory previews root project."""
    project = _setup(tmp_path, "s16b_provenance_cwd")
    subdir = project / "subdir"
    before = _snapshot_dir(subdir)
    result = _invoke(["install", "--dry-run"], subdir)
    assert result.exit_code == 0, result.output
    assert "Using apm.yml at" in result.output
    assert "Dry run mode" in result.output
    assert not (project / "apm.lock.yaml").exists()
    assert not (subdir / "apm.lock.yaml").exists()
    assert _snapshot_dir(subdir) == before


def test_s16_control_global_ignores_walk(tmp_path, monkeypatch):
    """Control: apm install -g ignores ancestor walk."""
    project = _setup(tmp_path, "s16b_provenance_cwd")
    subdir = project / "subdir"
    before = _snapshot_dir(subdir)
    fakehome = tmp_path / "fakehome"
    fakehome.mkdir()
    monkeypatch.setenv("HOME", str(fakehome))
    monkeypatch.setenv("USERPROFILE", str(fakehome))
    monkeypatch.delenv("APM_HOME", raising=False)
    result = _invoke(["install", "-g"], subdir)
    assert result.exit_code == 1, result.output
    assert "Using apm.yml at" not in result.output
    assert _snapshot_dir(subdir) == before


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


# ---------------------------------------------------------------------------
# S26, S27: configurable default install target via `apm config target`
# ---------------------------------------------------------------------------


def test_s26_config_default_fills_greenfield_no_harness(tmp_path, monkeypatch):
    """S26: `apm config target` default makes bare install succeed in greenfield.

    Without --target and without apm.yml target, a configured default target
    must fill the gap that would otherwise error with 'no harness detected'.
    Provenance must report 'apm config target', not '--target flag'.
    """
    import apm_cli.config as config_mod

    project = _setup(tmp_path, "s04_greenfield_explicit")

    # Redirect config to tmp to avoid touching ~/.apm
    config_dir = tmp_path / ".apm"
    config_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(config_mod, "CONFIG_DIR", str(config_dir))
    monkeypatch.setattr(config_mod, "CONFIG_FILE", str(config_dir / "config.json"))
    monkeypatch.setattr(config_mod, "_config_cache", None)

    config_mod.set_install_target("claude")

    result = _invoke(["install"], project)
    assert result.exit_code == 0, result.output
    assert_provenance(result.output, targets=["claude"], source="apm config target")
    assert (project / ".claude").is_dir()


def test_s27_cli_target_overrides_config_default(tmp_path, monkeypatch):
    """S27: --target flag takes precedence over `apm config target` default.

    Even when a config default is set, the explicit CLI --target must win.
    Provenance must report '--target flag', not 'apm config target'.
    This is the regression trap for the precedence chain:
    CLI --target > apm.yml > apm config target > auto-detect.
    """
    import apm_cli.config as config_mod

    project = _setup(tmp_path, "s04_greenfield_explicit")

    # Set config default to copilot (deliberately different from CLI flag)
    config_dir = tmp_path / ".apm"
    config_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(config_mod, "CONFIG_DIR", str(config_dir))
    monkeypatch.setattr(config_mod, "CONFIG_FILE", str(config_dir / "config.json"))
    monkeypatch.setattr(config_mod, "_config_cache", None)

    config_mod.set_install_target("copilot")

    result = _invoke(["install", "--target", "claude"], project)
    assert result.exit_code == 0, result.output
    assert_provenance(result.output, targets=["claude"], source="--target flag")
    assert (project / ".claude").is_dir()
