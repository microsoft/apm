"""Contracts for the architecture linter's process-parallel driver.

The driver (`scripts/architecture_linter/parallel.py`) runs one CLI worker
per bucket of rule groups and merges their partial reports. These cells keep
it honest without paying for a full lint of the real repository:

* merge parity -- bucketed partials, round-tripped through the worker JSON
  report, render and exit exactly like one process (clean, violations,
  rule failure), on a tiny synthetic repo with fake rule groups;
* fail closed -- a worker that is killed, exits oddly, times out, or leaves a
  missing or invalid report fails the run and names its bucket;
* failure parity through real worker processes (a missing root);
* the internal `--groups` / `--report-json` / `--workers` CLI surface.

* real-repository parity -- a copy of this repository with one real
  owner-guard mutation per rule group: one process vs 2, 3 and 4 workers give
  identical output, exit code and guard hits, and the CLI at `--workers 4`
  kills every mutation. These are the module's only full runs; they share
  one fixture and one xdist group.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import types
from collections import Counter
from pathlib import Path
from typing import NamedTuple

import pytest

from scripts import lint_architecture_boundaries as cli
from scripts.architecture_linter import parallel, runner
from scripts.architecture_linter.diagnostics import render_violations_and_failures
from scripts.architecture_linter.inventory import EXCLUDED_ROOTS
from scripts.architecture_linter.models import Rule, RunReport, Violation
from tests.integration.test_architecture_owner_rule_mutations import MUTATIONS, MutationCase

pytestmark = [
    pytest.mark.component,
    pytest.mark.promise("P-OWN-architecture-linter-performance"),
]

_FAILURE_LINE = "architecture-linter-failure"
ROOT = Path(__file__).resolve().parents[2]
_FULL_RUN_TIMEOUT = 360


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


def _raise(_provider: object) -> list[Violation]:
    raise RuntimeError("rule exploded")


def _install_fake_groups(
    root: Path, monkeypatch: pytest.MonkeyPatch, *, with_findings: bool
) -> None:
    """One guarded rule per group; optionally a violation each and one crash."""
    guards = [f"guard-{name.replace('_', '-')}" for name in runner.GROUP_MODULE_NAMES]
    _write_synthetic_repo(root, guards)
    modules = []
    for index, name in enumerate(runner.GROUP_MODULE_NAMES):
        rule_id = f"{name}.fixture"
        found = (Violation(rule_id, f"src/guard_{index}.py", 1, 1, f"bad {name}"),)
        check = (lambda _p, found=found: list(found)) if with_findings else (lambda _p: [])
        if with_findings and name == "transport_platform":
            check = _raise
        module = types.ModuleType(f"fake-{name}")
        module.RULES = (Rule(rule_id, name, (guards[index],), "d", check),)
        module.COLLECTORS = ()
        modules.append((name, module, None))
    monkeypatch.setattr(runner, "_GROUP_IMPORTS", tuple(modules))


def _bucket_reports(root: Path, workers: int, out_dir: Path) -> list[Path]:
    """Write each bucket's partial report in-process, as a worker would."""
    paths = []
    for index, bucket in enumerate(parallel.plan_buckets(workers)):
        path = out_dir / f"good-{index}.json"
        parallel.write_partial_report(runner.run_groups(root, bucket), path)
        paths.append(path)
    return paths


# ---------------------------------------------------------------------------
# Merge parity: partials merge to exactly the single-process report.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("with_findings", [True, False], ids=["findings", "clean"])
@pytest.mark.parametrize("workers", [2, 3, 4, 6])
def test_bucketed_partials_merge_to_the_single_process_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, workers: int, with_findings: bool
) -> None:
    repo = tmp_path / "repo"
    _install_fake_groups(repo, monkeypatch, with_findings=with_findings)
    single = runner.run(repo)

    partials = [
        parallel.partial_from_json(json.loads(path.read_text(encoding="ascii")))
        for path in _bucket_reports(repo, workers, tmp_path)
    ]
    merged = runner.merge_partial_runs(partials)

    rendered_single = render_violations_and_failures(single.violations, single.failures)
    assert render_violations_and_failures(merged.violations, merged.failures) == rendered_single
    assert merged.exit_code == single.exit_code == (1 if with_findings else 0)
    assert merged.group_results == tuple(result for result in single.group_results) or [
        r.rules for r in merged.group_results
    ] == [r.rules for r in single.group_results]
    if with_findings:
        assert len(merged.violations) == len(runner.GROUP_MODULE_NAMES) - 1
        assert "rule:transport_platform.fixture" in rendered_single
    else:
        assert rendered_single == ""
    guard_failures = [f.message for f in merged.failures if f.stage == "guard"]
    expected = ["guard-transport-platform (count=0)"] if with_findings else []
    assert [message.rsplit(": ", 1)[1] for message in guard_failures] == expected


def test_a_missing_bucket_is_incomplete_and_never_exits_clean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_groups(tmp_path, monkeypatch, with_findings=False)
    buckets = parallel.plan_buckets(2)
    partial = runner.run_groups(tmp_path, buckets[0])

    report = runner.merge_partial_runs([partial])

    assert report.exit_code == 2


# ---------------------------------------------------------------------------
# Fail closed: any broken worker fails the run and names its bucket.
# ---------------------------------------------------------------------------


_FAULTS = {
    "signal": (
        "import os, signal; os.kill(os.getpid(), signal.SIGKILL)",
        "killed by signal",
    ),
    "exit-code": ("import sys; sys.exit(3)", "exited with code 3 (expected 2)"),
    "no-report": ("import sys; sys.exit(2)", "report missing or unreadable"),
    "bad-json": (
        "import pathlib, sys; pathlib.Path(sys.argv[1]).write_text('{'); sys.exit(2)",
        "report invalid",
    ),
    "wrong-schema": (
        "import pathlib, sys; pathlib.Path(sys.argv[1]).write_text('{\"version\": 1}');"
        " sys.exit(2)",
        "report invalid: malformed report",
    ),
    "timeout": ("import time; time.sleep(60)", "timed out after 1 s"),
}


@pytest.mark.parametrize("fault", [None, *_FAULTS])
def test_driver_fails_closed_and_names_the_broken_bucket(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fault: str | None,
) -> None:
    if fault == "signal" and sys.platform == "win32":
        pytest.skip("POSIX signal delivery")
    repo = tmp_path / "repo"
    _install_fake_groups(repo, monkeypatch, with_findings=True)
    single_exit = cli.main(["--root", str(repo), "--workers", "1"])
    single_out = capsys.readouterr().out
    good = dict(zip(parallel.plan_buckets(3), _bucket_reports(repo, 3, tmp_path), strict=True))
    faulty_bucket = parallel.plan_buckets(3)[0]

    def scripted_argv(root: Path, bucket: tuple[str, ...], report_path: Path) -> list[str]:
        if fault is not None and tuple(bucket) == faulty_bucket:
            return [sys.executable, "-c", _FAULTS[fault][0], str(report_path)]
        copy = "import shutil, sys; shutil.copyfile(sys.argv[1], sys.argv[2]); sys.exit(2)"
        return [sys.executable, "-c", copy, str(good[tuple(bucket)]), str(report_path)]

    monkeypatch.setattr(parallel, "worker_argv", scripted_argv)
    monkeypatch.setenv(parallel.TIMEOUT_ENV, "1" if fault == "timeout" else "60")

    exit_code = cli.main(["--root", str(repo), "--workers", "3"])

    out = capsys.readouterr().out
    if fault is None:
        assert exit_code == single_exit == 1
        assert out == single_out
        return
    label = "+".join(faulty_bucket)
    assert exit_code == 1
    assert out.splitlines() == [
        line for line in out.splitlines() if line.startswith(f"parallel:{label}:1:1: ")
    ]
    assert len(out.splitlines()) == 1
    assert _FAULTS[fault][1] in out
    assert _FAILURE_LINE in out


def _guard_of(group: str) -> str:
    return f"guard-{group.replace('_', '-')}"


def _drop_first_guard(doc: dict, buckets: tuple) -> None:
    doc["registered_guard_ids"] = doc["registered_guard_ids"][1:]


def _steal_other_bucket_guard(doc: dict, buckets: tuple) -> None:
    doc["guard_hits"].append([_guard_of(buckets[0][0]), 1])


def _drop_own_guards(doc: dict, buckets: tuple) -> None:
    doc["guard_hits"] = []


def _other_inventory(doc: dict, buckets: tuple) -> None:
    doc["inventory_digest"] = "0" * 64


def _other_bucket_groups(doc: dict, buckets: tuple) -> None:
    doc["groups"] = list(buckets[0])


def _drop_group_results(doc: dict, buckets: tuple) -> None:
    doc["group_results"] = []


def _null_registered_guards(doc: dict, buckets: tuple) -> None:
    doc["registered_guard_ids"] = None


# (tamper applied to the SECOND bucket's report, expected stage, expected text)
_REPORT_TAMPERS = {
    "groups-not-bucket": (_other_bucket_groups, "parallel:{b1}", "report covers groups"),
    "inventory-digest": (
        _other_inventory,
        "parallel:{b1}",
        "workers saw different repository inventories",
    ),
    "guard-set": (_drop_first_guard, "parallel:{b1}", "disagree on the registered owner guards"),
    "guard-in-two-workers": (_steal_other_bucket_guard, "parallel:{b0},{b1}", "ran in 2 workers"),
    "guard-in-no-worker": (_drop_own_guards, "guard", "did not execute exactly once"),
    "no-group-results": (_drop_group_results, "parallel:{b1}", "do not match the bucket"),
    "no-guards-no-registry-failure": (
        _null_registered_guards,
        "parallel:{b1}",
        "no registered guards and no registry failure",
    ),
}


@pytest.mark.parametrize("tamper", list(_REPORT_TAMPERS))
def test_inconsistent_worker_reports_fail_the_run_and_name_the_bucket(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tamper: str,
) -> None:
    repo = tmp_path / "repo"
    _install_fake_groups(repo, monkeypatch, with_findings=False)
    buckets = parallel.plan_buckets(2)
    reports = dict(zip(buckets, _bucket_reports(repo, 2, tmp_path), strict=True))
    edit, stage, text = _REPORT_TAMPERS[tamper]
    doc = json.loads(reports[buckets[1]].read_text(encoding="ascii"))
    edit(doc, buckets)
    reports[buckets[1]].write_text(json.dumps(doc), encoding="ascii")

    def scripted_argv(root: Path, bucket: tuple[str, ...], report_path: Path) -> list[str]:
        copy = "import shutil, sys; shutil.copyfile(sys.argv[1], sys.argv[2]); sys.exit(2)"
        return [sys.executable, "-c", copy, str(reports[tuple(bucket)]), str(report_path)]

    monkeypatch.setattr(parallel, "worker_argv", scripted_argv)

    exit_code = cli.main(["--root", str(repo), "--workers", "2"])

    out = capsys.readouterr().out
    expected_stage = stage.format(b0="+".join(buckets[0]), b1="+".join(buckets[1]))
    assert exit_code == 1
    assert any(
        line.startswith(f"{expected_stage}:1:1: {_FAILURE_LINE}") and text in line
        for line in out.splitlines()
    ), out


@pytest.mark.parametrize("plan", ["missing-group", "duplicate-group"])
def test_a_bucket_plan_that_misses_or_repeats_a_group_starts_no_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, plan: str
) -> None:
    groups = runner.GROUP_MODULE_NAMES
    if plan == "missing-group":
        buckets = [groups[:2], groups[2:-1]]
        expected = (f"parallel:{groups[-1]}", "not assigned to any worker")
    else:
        buckets = [groups[:3], groups[2:]]
        expected = ("parallel:" + "+".join(groups[2:]), "already assigned")
    started: list[object] = []
    monkeypatch.setattr(parallel, "_start", lambda *args: started.append(args))

    report = parallel.run_parallel(tmp_path, buckets)

    assert report.exit_code == 1
    assert started == []
    assert [(f.stage, expected[1] in f.message) for f in report.failures] == [(expected[0], True)]


def test_merging_two_partials_for_the_same_groups_is_never_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_groups(tmp_path, monkeypatch, with_findings=False)
    everything = runner.run_groups(tmp_path, runner.GROUP_MODULE_NAMES)

    report = runner.merge_partial_runs([everything, everything])

    assert report.exit_code == 2
    label = "parallel:" + "+".join(runner.GROUP_MODULE_NAMES)
    assert any(
        f.stage == label and "already run by another worker" in f.message for f in report.failures
    )


def test_a_worker_that_cannot_start_fails_the_run_and_reaps_started_workers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    buckets = parallel.plan_buckets(2)
    pid_file = tmp_path / "first.pid"
    sleeper = (
        "import os, pathlib, sys, time;"
        " pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(120)"
    )

    def scripted_argv(root: Path, bucket: tuple[str, ...], report_path: Path) -> list[str]:
        if tuple(bucket) == buckets[0]:
            return [sys.executable, "-c", sleeper, str(pid_file)]
        return [str(tmp_path / "no-such-interpreter")]

    started_sleepers: list[parallel._Worker] = []
    real_start = parallel._start

    def recording_start(*args: object) -> parallel._Worker:
        worker = real_start(*args)
        started_sleepers.append(worker)
        return worker

    monkeypatch.setattr(parallel, "worker_argv", scripted_argv)
    monkeypatch.setattr(parallel, "_start", recording_start)

    exit_code = cli.main(["--root", str(tmp_path), "--workers", "2"])

    out = capsys.readouterr().out
    assert exit_code == 1
    assert out.splitlines() == [
        f"parallel:{'+'.join(buckets[1])}:1:1: {_FAILURE_LINE} could not start: FileNotFoundError"
    ]
    assert len(started_sleepers) == 1
    assert started_sleepers[0].process.returncode is not None


def test_startup_failure_renders_identically_through_real_workers(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing_root = tmp_path / "missing"

    single_exit = cli.main(["--root", str(missing_root), "--workers", "1"])
    single = capsys.readouterr()
    parallel_exit = cli.main(["--root", str(missing_root), "--workers", "2"])
    parallel_run = capsys.readouterr()

    assert single_exit == parallel_exit == 1
    assert parallel_run.out == single.out
    assert single.out.startswith("inventory:1:1: architecture-linter-failure")
    assert parallel_run.err == ""


# ---------------------------------------------------------------------------
# Internal CLI surface and worker-count resolution.
# ---------------------------------------------------------------------------


def test_worker_protocol_writes_a_partial_report_and_exits_incomplete(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    report_path = tmp_path / "report.json"

    exit_code = cli.main(
        [
            "--root",
            str(tmp_path / "missing"),
            "--groups",
            "contracts_tests,mutation_writes",
            "--report-json",
            str(report_path),
        ]
    )

    assert exit_code == 2
    assert capsys.readouterr().out == ""
    partial = parallel.partial_from_json(json.loads(report_path.read_text(encoding="ascii")))
    assert partial.groups == ("mutation_writes", "contracts_tests")
    assert partial.inventory_ok is False
    assert [failure.stage for failure in partial.startup_failures] == ["inventory"]


@pytest.mark.parametrize(
    "argv",
    [
        ["--groups", "contracts_tests"],
        ["--report-json", "r.json"],
        ["--groups", "no_such_group", "--report-json", "r.json"],
        ["--groups", "contracts_tests,contracts_tests", "--report-json", "r.json"],
        ["--groups", "contracts_tests", "--report-json", "r.json", "--workers", "2"],
        ["--groups", "contracts_tests", "--report-json", "r.json", "--metrics-json", "m.json"],
    ],
)
def test_internal_worker_flags_reject_incomplete_or_mixed_use(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli._parse_args(["--root", ".", *argv])
    assert exc_info.value.code == 2


@pytest.mark.parametrize(
    ("argv", "env_value"), [(["--workers", "0"], None), ([], "0"), ([], "many")]
)
def test_invalid_worker_count_is_a_usage_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    env_value: str | None,
) -> None:
    if env_value is not None:
        monkeypatch.setenv(parallel.WORKERS_ENV, env_value)

    exit_code = cli.main(["--root", str(tmp_path), *argv])

    captured = capsys.readouterr()
    assert exit_code == 2
    assert captured.out == ""
    assert captured.err.startswith("workers:1:1: architecture-linter-failure")


# ---------------------------------------------------------------------------
# Real-repository parity: the gate CI actually runs is the parallel path.
# ---------------------------------------------------------------------------


class ParityRuns(NamedTuple):
    injected: dict[str, MutationCase]
    single: RunReport
    parallel: dict[int, RunReport]
    cli: subprocess.CompletedProcess[str]


def _one_mutation_per_group(copy_root: Path) -> dict[str, MutationCase]:
    """Pick one applicable owner-guard mutation per rule group, on distinct files."""
    group_by_guard = {
        guard: rule.group for rule in runner.registered_rules() for guard in rule.guard_ids
    }
    picked: dict[str, MutationCase] = {}
    used: set[str] = set()
    for case in MUTATIONS:
        group = group_by_guard[case.guard_id]
        path = copy_root / case.path
        if group in picked or case.path in used or not path.is_file():
            continue
        if case.old not in path.read_text(encoding="utf-8"):
            continue
        picked[group] = case
        used.add(case.path)
    return picked


def _guard_hits(report: RunReport) -> Counter[str]:
    return Counter(
        guard
        for group in report.group_results
        for rule in group.rules
        if rule.error is None
        for guard in rule.guard_ids
    )


@pytest.fixture(scope="module")
def parity_runs(tmp_path_factory: pytest.TempPathFactory) -> ParityRuns:
    """Lint one mutated copy of the repository with 1, 2, 3 and 4 workers.

    A copy, not the live tree: other xdist workers create files under the
    checkout, and the driver fails closed when workers see different trees.
    """
    copy_root = tmp_path_factory.mktemp("parallel-parity") / "repo"
    shutil.copytree(ROOT, copy_root, ignore=shutil.ignore_patterns(*EXCLUDED_ROOTS))
    injected = _one_mutation_per_group(copy_root)
    for case in injected.values():
        path = copy_root / case.path
        text = path.read_text(encoding="utf-8")
        path.write_text(
            text.replace(case.old, case.new, -1 if case.replace_all else 1), encoding="utf-8"
        )

    single = runner.run(copy_root)
    reports = {
        workers: parallel.run_parallel(copy_root, parallel.plan_buckets(workers))
        for workers in (2, 3, 4)
    }
    cli_run = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "lint_architecture_boundaries.py"),
            "--root",
            str(copy_root),
            "--workers",
            "4",
        ],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
        timeout=_FULL_RUN_TIMEOUT,
    )
    return ParityRuns(injected, single, reports, cli_run)


@pytest.mark.xdist_group(name="architecture_linter_parallel_parity")
def test_real_repo_mutations_reach_every_group(parity_runs: ParityRuns) -> None:
    single = parity_runs.single
    assert set(parity_runs.injected) == set(runner.GROUP_MODULE_NAMES)
    with_findings = {
        group.group for group in single.group_results if any(r.violations for r in group.rules)
    }
    assert with_findings == set(runner.GROUP_MODULE_NAMES)
    reported = {violation.rule_id for violation in single.violations}
    assert {case.rule_id for case in parity_runs.injected.values()} <= reported
    assert single.exit_code == 1


@pytest.mark.xdist_group(name="architecture_linter_parallel_parity")
@pytest.mark.parametrize("workers", [2, 3, 4])
def test_real_repo_parallel_run_equals_one_process(parity_runs: ParityRuns, workers: int) -> None:
    single = parity_runs.single
    merged = parity_runs.parallel[workers]

    assert render_violations_and_failures(
        merged.violations, merged.failures
    ) == render_violations_and_failures(single.violations, single.failures)
    assert merged.exit_code == single.exit_code
    assert _guard_hits(merged) == _guard_hits(single)
    assert set(_guard_hits(merged).values()) == {1}
    assert [r.rules for r in merged.group_results] == [r.rules for r in single.group_results]


@pytest.mark.xdist_group(name="architecture_linter_parallel_parity")
def test_cli_with_four_workers_kills_every_injected_mutation(parity_runs: ParityRuns) -> None:
    single = parity_runs.single
    result = parity_runs.cli

    assert result.returncode == single.exit_code == 1
    assert result.stderr == ""
    assert (
        result.stdout == render_violations_and_failures(single.violations, single.failures) + "\n"
    )
    for case in parity_runs.injected.values():
        assert f" {case.rule_id} " in result.stdout
