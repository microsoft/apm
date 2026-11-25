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

The two expensive real-repository parity runs (clean at the default worker
count, injected violations in every group at four workers) live in
`test_architecture_linter_entrypoint.py`, which already owns full runs.
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

from scripts import lint_architecture_boundaries as cli
from scripts.architecture_linter import parallel, runner
from scripts.architecture_linter.diagnostics import render_violations_and_failures
from scripts.architecture_linter.models import Rule, Violation

pytestmark = [
    pytest.mark.component,
    pytest.mark.promise("P-OWN-architecture-linter-performance"),
]

_FAILURE_LINE = "architecture-linter-failure"


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


@pytest.mark.parametrize(
    ("cpus", "expected"), [(None, 1), (1, 1), (2, 1), (3, 1), (4, 2), (8, 4), (64, 4)]
)
def test_default_workers_use_half_the_cpus_capped_at_four(
    monkeypatch: pytest.MonkeyPatch, cpus: int | None, expected: int
) -> None:
    monkeypatch.setattr(parallel.os, "cpu_count", lambda: cpus)

    assert parallel.resolve_workers(None, {}) == expected


def test_worker_count_precedence_is_flag_env_then_pytest() -> None:
    pytest_env = {"PYTEST_CURRENT_TEST": "t", "PYTEST_XDIST_WORKER": "gw0"}

    assert parallel.resolve_workers(None, pytest_env) == 1
    assert parallel.resolve_workers(None, {**pytest_env, parallel.WORKERS_ENV: "3"}) == 3
    assert parallel.resolve_workers(5, {parallel.WORKERS_ENV: "3"}) == 5
    assert parallel.resolve_workers(99, {}) == len(runner.GROUP_MODULE_NAMES)


@pytest.mark.parametrize("workers", range(1, 9))
def test_buckets_cover_every_group_exactly_once(workers: int) -> None:
    buckets = parallel.plan_buckets(workers)

    flat = [group for bucket in buckets for group in bucket]
    assert sorted(flat) == sorted(runner.GROUP_MODULE_NAMES)
    assert len(buckets) == min(workers, len(runner.GROUP_MODULE_NAMES))
