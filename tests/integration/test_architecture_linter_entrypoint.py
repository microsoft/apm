"""End-to-end contracts for the architecture linter's CLI entrypoint.

This module owns the only two expensive, full-catalog (all explicitly
registered groups and rules) linter invocations in the architecture-linter test
suite, each computed once via a module-scoped fixture and reused by every
assertion that needs it:

* `clean_shell_run` -- the argument-free shell wrapper plus one direct Python
  CLI invocation with `--metrics-json`, both against the real repository.
  The wrapper has no configuration surface; benchmarks retain the internal
  Python-only metrics option.
* `mutated_report` -- a filesystem copy of the real repository with two
  independent, real-content corruptions, linted via the Python `runner.run`
  API directly (not a subprocess, to avoid a second expensive full
  process). Proves the engine aggregates multiple diagnostics from one run
  and that the owner-guard-executes-exactly-once invariant still holds
  under content corruption.

Every other test here is deliberately cheap: CLI argument-surface checks
fail fast in argparse before any linting happens, the metrics-write-error
test uses a tiny synthetic repo with monkeypatched fake rule groups, and the
selected-rule-API check runs exactly one real rule instead of the full
catalog.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import types
from pathlib import Path
from typing import NamedTuple

import pytest

from scripts import lint_architecture_boundaries as cli
from scripts.architecture_linter import runner
from scripts.architecture_linter.diagnostics import render_violations_and_failures
from scripts.architecture_linter.inventory import EXCLUDED_ROOTS, build_inventory
from scripts.architecture_linter.models import Rule, RunReport
from scripts.architecture_linter.registry import load_registry

pytestmark = [
    # Two module-scoped fixtures below each pay for one expensive, full,
    # all-six-group lint of the real repository. `--dist loadgroup` (the
    # xdist scheduler this repo's sharded integration runs use) is the only
    # scheduler that honors `xdist_group`; without this marker, individual
    # tests here could be split across workers and each fixture would be
    # recomputed per worker, silently doubling this file's real cost.
    pytest.mark.xdist_group(name="architecture_linter_full_run"),
]

ROOT = Path(__file__).resolve().parents[2]
SHELL_SCRIPT = ROOT / "scripts" / "lint-architecture-boundaries.sh"
_DIAGNOSTIC_LINE = re.compile(r"^\S+:\d+:\d+: \S+ .+$")
_METRICS_KEYS = (
    "ast_visits",
    "child_process_count",
    "excluded_root_count",
    "inventory_file_count",
    "max_parses_per_file",
    "max_reads_per_file",
    "parse_attempts",
    "parse_errors",
    "parse_successes",
    "peak_tree_index_nodes",
    "per_group_seconds",
    "read_attempts",
    "read_errors",
    "read_successes",
    "tree_index_builds",
    "tree_index_cache_hits",
    "max_tree_index_builds_per_file",
    "total_seconds",
)


class CleanShellRun(NamedTuple):
    result: subprocess.CompletedProcess[str]
    metrics_result: subprocess.CompletedProcess[str]
    metrics_path: Path
    metrics_text: str
    metrics: dict[str, object]


@pytest.fixture(scope="module")
def clean_shell_run(tmp_path_factory: pytest.TempPathFactory) -> CleanShellRun:
    """Run the stable wrapper and the direct metrics CLI against the real repo.

    The wrapper accepts no arguments and always lints its own repository. The
    Python entrypoint keeps `--metrics-json` as an internal benchmark surface.
    """
    metrics_path = tmp_path_factory.mktemp("clean-shell-metrics") / "metrics.json"
    result = subprocess.run(
        ["bash", str(SHELL_SCRIPT)],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
        timeout=180,
    )
    metrics_result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "lint_architecture_boundaries.py"),
            "--root",
            str(ROOT),
            "--metrics-json",
            str(metrics_path),
        ],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
        timeout=180,
    )
    metrics_text = metrics_path.read_text(encoding="ascii")
    metrics = json.loads(metrics_text)
    return CleanShellRun(result, metrics_result, metrics_path, metrics_text, metrics)


class MutatedRun(NamedTuple):
    report: RunReport
    copy_root: Path
    corrupted_paths: tuple[str, ...]


@pytest.fixture(scope="module")
def mutated_report(tmp_path_factory: pytest.TempPathFactory) -> MutatedRun:
    """Lint a corrupted copy of the real repo exactly once, via the Python API.

    Two independent, real, broadly-referenced source files are corrupted
    (one undecodable, one a syntax error) in a filesystem copy of the real
    repository -- never the actual working tree. This is the one "mutated
    full invocation" this suite budgets: it proves the engine aggregates
    many diagnostics from independent failures in a single run, and that
    doing so does not break the owner-guard-executes-exactly-once
    invariant (file-content corruption surfaces as violations, not as
    rules failing to run).
    """
    copy_root = tmp_path_factory.mktemp("mutated-repo") / "repo"
    shutil.copytree(ROOT, copy_root, ignore=shutil.ignore_patterns(*EXCLUDED_ROOTS))

    console_path = copy_root / "src/apm_cli/utils/console.py"
    console_path.write_bytes(console_path.read_bytes() + b"\xff\xfe\x00corrupted-tail")

    package_path = copy_root / "src/apm_cli/models/apm_package.py"
    package_path.write_text(
        package_path.read_text(encoding="utf-8") + "\ndef _syntactically_broken(:\n    pass\n",
        encoding="utf-8",
    )

    report = runner.run(copy_root)
    corrupted = ("src/apm_cli/utils/console.py", "src/apm_cli/models/apm_package.py")
    return MutatedRun(report, copy_root, corrupted)


# ---------------------------------------------------------------------------
# Clean end-to-end shell wrapper smoke.
# ---------------------------------------------------------------------------


def test_clean_shell_wrapper_run_exits_zero_with_no_diagnostic_output(
    clean_shell_run: CleanShellRun,
) -> None:
    """A clean repository lints to a zero exit code and prints nothing."""
    assert clean_shell_run.result.returncode == 0
    assert clean_shell_run.result.stdout == ""
    assert clean_shell_run.result.stderr == ""


def test_shell_wrapper_rejects_all_arguments_without_running_linter(tmp_path: Path) -> None:
    """No caller can forward options or override the wrapper's canonical root."""
    metrics_path = tmp_path / "must-not-exist.json"

    result = subprocess.run(
        [
            "bash",
            str(SHELL_SCRIPT),
            "--root",
            str(tmp_path),
            "--metrics-json",
            str(metrics_path),
        ],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )

    assert result.returncode == 2
    assert result.stdout == ""
    assert "accepts no arguments" in result.stderr
    assert not metrics_path.exists()


def test_shell_wrapper_root_discovery_uses_only_bash_builtins() -> None:
    """Portable root discovery does not shell out before Python starts."""
    source = SHELL_SCRIPT.read_text(encoding="utf-8")

    assert "dirname" not in source
    assert "$(" not in source
    assert '"$@"' not in source


def test_clean_shell_wrapper_run_writes_a_metrics_json_artifact(
    clean_shell_run: CleanShellRun,
) -> None:
    """The direct Python benchmark surface writes metrics on a clean run."""
    assert clean_shell_run.metrics_result.returncode == 0
    assert clean_shell_run.metrics_result.stdout == ""
    assert clean_shell_run.metrics_result.stderr == ""
    assert clean_shell_run.metrics_path.is_file()
    assert clean_shell_run.metrics_text.endswith("\n")
    assert clean_shell_run.metrics_text.isascii()


# ---------------------------------------------------------------------------
# Metrics JSON schema, ordering, counts.
# ---------------------------------------------------------------------------


def test_metrics_json_has_the_exact_documented_schema(clean_shell_run: CleanShellRun) -> None:
    """The metrics document has exactly the documented top-level fields."""
    assert set(clean_shell_run.metrics) == set(_METRICS_KEYS)
    assert set(clean_shell_run.metrics["per_group_seconds"]) == set(runner.GROUP_MODULE_NAMES)


def test_metrics_json_text_is_canonically_sorted_and_indented(
    clean_shell_run: CleanShellRun,
) -> None:
    """The on-disk JSON is byte-identical to its own canonical re-encoding.

    `sort_keys=True` sorts nested mappings too, so `per_group_seconds`'s
    six group-name keys are alphabetized in the file, not in
    `GROUP_MODULE_NAMES` execution order.
    """
    canonical = (
        json.dumps(clean_shell_run.metrics, indent=2, sort_keys=True, ensure_ascii=True) + "\n"
    )
    assert clean_shell_run.metrics_text == canonical

    group_keys_in_file = list(clean_shell_run.metrics["per_group_seconds"])
    assert group_keys_in_file == sorted(group_keys_in_file)
    top_level_keys_in_file = list(clean_shell_run.metrics)
    assert top_level_keys_in_file == sorted(top_level_keys_in_file)


def test_metrics_json_counts_reflect_a_read_once_parse_once_clean_run(
    clean_shell_run: CleanShellRun,
) -> None:
    """Every count is sane, and no file was ever read or parsed more than once."""
    metrics = clean_shell_run.metrics
    real_inventory = build_inventory(ROOT)

    assert metrics["inventory_file_count"] == len(real_inventory.files)
    assert metrics["excluded_root_count"] == len(EXCLUDED_ROOTS) == 17
    assert metrics["read_errors"] == 0
    assert metrics["parse_errors"] == 0
    assert metrics["max_reads_per_file"] <= 1
    assert metrics["max_parses_per_file"] <= 1
    assert metrics["max_tree_index_builds_per_file"] <= 1
    assert metrics["read_attempts"] == metrics["read_successes"]
    assert metrics["parse_attempts"] == metrics["parse_successes"]
    assert metrics["child_process_count"] == 0
    assert metrics["total_seconds"] > 0
    assert metrics["ast_visits"] > 0
    assert metrics["tree_index_builds"] > 0
    assert metrics["peak_tree_index_nodes"] > 0


def test_metrics_write_error_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A metrics destination that cannot be written flips the exit code to 1.

    Uses a tiny synthetic repo and monkeypatched fake rule groups (not the
    real 103-rule catalog) so this stays fast: only the metrics-write step
    is under test here.
    """
    _write_synthetic_repo(tmp_path, ["only-guard"])
    ok_rule = Rule(
        id="only-rule",
        group="registry_delegation",
        guard_ids=("only-guard",),
        description="d",
        check=lambda provider: [],
    )
    monkeypatch.setattr(
        runner, "_GROUP_IMPORTS", _fake_group_imports({"registry_delegation": [ok_rule]})
    )
    metrics_target = tmp_path / "metrics_is_a_directory"
    metrics_target.mkdir()

    exit_code = cli.main(["--root", str(tmp_path), "--metrics-json", str(metrics_target)])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    assert "architecture-linter-failure" in captured.err
    assert "cannot write metrics JSON" in captured.err
    assert captured.err.isascii()


def test_missing_root_is_a_structured_failure_and_still_writes_metrics(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Inventory startup failures render without a traceback and keep metrics."""
    missing_root = tmp_path / "missing"
    metrics_path = tmp_path / "missing-root-metrics.json"

    exit_code = cli.main(["--root", str(missing_root), "--metrics-json", str(metrics_path)])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.err == ""
    assert captured.out.startswith(
        "inventory:1:1: architecture-linter-failure root is not a directory:"
    )
    assert "Traceback" not in captured.out
    metrics = json.loads(metrics_path.read_text(encoding="ascii"))
    assert metrics["inventory_file_count"] == 0
    assert metrics["child_process_count"] == 0


# ---------------------------------------------------------------------------
# End-to-end aggregation mutation: >=2 diagnostics in one run.
# ---------------------------------------------------------------------------


def test_mutated_run_aggregates_at_least_two_diagnostics_from_independent_rules(
    mutated_report: MutatedRun,
) -> None:
    """Two independent real-content corruptions surface as >=2 diagnostics
    from more than one distinct rule, in a single run -- not just the first
    problem found."""
    report = mutated_report.report

    total_diagnostics = len(report.violations) + len(report.failures)
    assert total_diagnostics >= 2
    assert report.exit_code == 1

    distinct_rule_ids = {violation.rule_id for violation in report.violations}
    assert len(distinct_rule_ids) >= 2

    all_paths = {violation.path for violation in report.violations}
    all_failure_text = " ".join(failure.message for failure in report.failures)
    for corrupted_path in mutated_report.corrupted_paths:
        assert corrupted_path in all_paths or corrupted_path in all_failure_text


def test_mutated_run_still_executes_every_registered_owner_guard_exactly_once(
    mutated_report: MutatedRun,
) -> None:
    """Content corruption surfaces as violations, not as skipped rules: the
    guard invariant inspected from an actual full `RunReport` still holds."""
    report = mutated_report.report

    guard_hits: dict[str, int] = {}
    for group in report.group_results:
        for rule_result in group.rules:
            if rule_result.error is None:
                for guard_id in rule_result.guard_ids:
                    guard_hits[guard_id] = guard_hits.get(guard_id, 0) + 1

    inventory = build_inventory(mutated_report.copy_root)
    registry = load_registry(mutated_report.copy_root / ".apm/architecture/owners", inventory.files)
    registered_guard_ids = {guard for owner in registry.owners for guard in owner.guards}

    assert not any(failure.stage == "guard" for failure in report.failures)
    for guard_id in registered_guard_ids:
        assert guard_hits.get(guard_id) == 1


def test_mutated_run_still_reads_and_parses_each_file_at_most_once(
    mutated_report: MutatedRun,
) -> None:
    """Even with dozens of rules re-reading the same two corrupted files,
    the shared cache still reads/parses each file at most once."""
    metrics = mutated_report.report.metrics
    assert metrics.max_reads_per_file <= 1
    assert metrics.max_parses_per_file <= 1
    assert metrics.child_process_count == 0


def test_mutated_run_renders_as_deterministic_strict_ascii_diagnostics(
    mutated_report: MutatedRun,
) -> None:
    """The real aggregated findings still render as sorted, ASCII, Ruff-style
    `path:line:column: rule-id message` lines, regardless of input order."""
    report = mutated_report.report

    rendered = render_violations_and_failures(report.violations, report.failures)
    rendered_from_reversed = render_violations_and_failures(
        tuple(reversed(report.violations)), tuple(reversed(report.failures))
    )

    assert rendered != ""
    assert rendered == rendered_from_reversed
    assert rendered.isascii()
    lines = rendered.splitlines()
    assert all(_DIAGNOSTIC_LINE.match(line) for line in lines)
    violation_lines = lines[: len(report.violations)]
    assert violation_lines == sorted(violation_lines)


# ---------------------------------------------------------------------------
# No CLI rule-selection surface; the Python selected-rule API works instead.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "flag", ["--select", "--rule", "--rule-id", "--filter", "--only", "--rules"]
)
def test_cli_has_no_rule_selection_flag(flag: str) -> None:
    """Rule selection is a Python test API only, never a CLI surface."""
    with pytest.raises(SystemExit) as exc_info:
        cli._parse_args(["--root", ".", flag, "x"])
    assert exc_info.value.code == 2


def test_cli_accepts_only_root_and_metrics_json() -> None:
    """The full, exact CLI argument surface is `--root` and `--metrics-json`."""
    minimal = cli._parse_args(["--root", "."])
    assert minimal.root == Path(".")
    assert minimal.metrics_json is None

    with_metrics = cli._parse_args(["--root", ".", "--metrics-json", "out.json"])
    assert with_metrics.metrics_json == Path("out.json")

    with pytest.raises(SystemExit) as exc_info:
        cli._parse_args([])
    assert exc_info.value.code == 2


def test_python_selected_rule_api_selects_one_real_rule_with_no_cli_equivalent() -> None:
    """The Python-only selection capability that the CLI deliberately lacks
    still works, selecting exactly one registered semantic rule."""
    picked = "registry_delegation.diagnostic_ascii_owner"
    assert picked in {rule.id for rule in runner.registered_rules()}

    report = runner.run_selected_rules(ROOT, [picked])

    executed_ids = [
        rule_result.rule_id for group in report.group_results for rule_result in group.rules
    ]
    assert executed_ids == [picked]
    assert report.exit_code == 2


# ---------------------------------------------------------------------------
# Shared synthetic-repo helpers for the metrics-write-error test above.
# `run()`'s six group-module imports are monkeypatched with fake, trivial
# modules so this test never touches the real 103-rule catalog.
# ---------------------------------------------------------------------------


def _write_synthetic_repo(root: Path, guard_ids: list[str]) -> None:
    for name in ("src", "scripts", "tests", ".apm"):
        (root / name).mkdir(parents=True, exist_ok=True)
    (root / "pyproject.toml").write_text("", encoding="utf-8")
    owners_dir = root / ".apm" / "architecture" / "owners"
    owners_dir.mkdir(parents=True, exist_ok=True)
    owners = []
    for index, guard in enumerate(guard_ids):
        (root / "src" / f"guard_{index}.py").write_text("x = 1\n", encoding="utf-8")
        owners.append(
            {
                "id": f"fixture-owner-{index}",
                "decision": f"Fixture decision {index}",
                "owner": f"core/guard_{index}.py (FixtureOwner{index})",
                "selectors": [f"src/guard_{index}.py"],
                "guards": [guard],
            }
        )
    (owners_dir / "index.json").write_text(
        json.dumps({"version": 1, "shards": ["fixture.json"]}), encoding="utf-8"
    )
    (owners_dir / "fixture.json").write_text(
        json.dumps({"version": 1, "owners": owners}), encoding="utf-8"
    )


def _fake_group_imports(
    rules_by_group: dict[str, list[Rule]],
) -> tuple[tuple[str, types.ModuleType, None], ...]:
    entries = []
    for name in runner.GROUP_MODULE_NAMES:
        module = types.ModuleType("fake-group")
        module.RULES = tuple(rules_by_group.get(name, []))
        module.COLLECTORS = ()
        entries.append((name, module, None))
    return tuple(entries)
