"""Two-phase scheduling: shared checks overlap across tiers, exclusives alone."""

from __future__ import annotations

import re
import shlex
from pathlib import Path
from types import SimpleNamespace

import preflight
import preflight_policy as policy
from conftest import Sandbox


def _probe(probe_dir: Path, check_id: str, *, sleep: float = 0.6, fail: bool = False) -> str:
    """Shell that records how many probes overlap while it runs (outside the repo)."""
    running = shlex.quote(str(probe_dir / "running"))
    peak = shlex.quote(str(probe_dir / f"peak-{check_id}"))
    done = shlex.quote(str(probe_dir / f"done-{check_id}"))
    return (
        f"mkdir -p {running}; touch {running}/{check_id}; "
        f"n=$(ls {running} | wc -l); sleep {sleep}; "
        f"m=$(ls {running} | wc -l); [ $m -gt $n ] && n=$m; echo $n > {peak}; "
        f"rm -f {running}/{check_id}; touch {done}; " + ("exit 1" if fail else "true")
    )


def _peak(probe_dir: Path, check_id: str) -> int:
    return int((probe_dir / f"peak-{check_id}").read_text(encoding="utf-8").strip())


def _policy(fake_policy: SimpleNamespace, tiers: dict[str, list[policy.Check]]):
    def build(tier: str, _ctx) -> list[policy.Check]:
        return tiers.get(tier, [])

    def select(changed: list[str]) -> dict[str, str]:
        return {t: "test" for t in tiers}

    return SimpleNamespace(**{**vars(fake_policy), "build_checks": build, "select": select})


def test_shared_checks_run_concurrently_within_the_bound(
    sandbox: Sandbox, fake_policy, workdir: Path
) -> None:
    probes = workdir / "probes"
    checks = [policy.Check(f"c{i}", "T0", cmd=_probe(probes, f"c{i}")) for i in range(4)]
    pol = _policy(fake_policy, {"T0": checks})
    assert preflight.run(["--jobs", "2", "--no-fetch"], policy=pol, cwd=sandbox.root) == 0
    peaks = [_peak(probes, f"c{i}") for i in range(4)]
    assert max(peaks) == 2
    receipt = sandbox.receipt()
    assert receipt["parallel"] is True
    assert receipt["jobs"] == 2
    assert set(receipt["phase_wall_s"]) == {"A", "B"}
    total = sum(c["duration_s"] for c in receipt["checks"])
    assert receipt["phase_wall_s"]["A"] < total


def test_exclusive_checks_never_overlap(sandbox: Sandbox, fake_policy, workdir: Path) -> None:
    probes = workdir / "probes"
    checks = [
        policy.Check("s1", "T0", cmd=_probe(probes, "s1")),
        policy.Check("x1", "T0", cmd=_probe(probes, "x1"), exclusive=True),
        policy.Check("s2", "T0", cmd=_probe(probes, "s2")),
        policy.Check("x2", "T0", cmd=_probe(probes, "x2"), exclusive=True),
        policy.Check("s3", "T0", cmd=_probe(probes, "s3")),
    ]
    pol = _policy(fake_policy, {"T0": checks})
    assert preflight.run(["--jobs", "4", "--no-fetch"], policy=pol, cwd=sandbox.root) == 0
    assert _peak(probes, "x1") == 1
    assert _peak(probes, "x2") == 1
    assert max(_peak(probes, s) for s in ("s1", "s2", "s3")) == 3
    assert sandbox.receipt()["ran_tiers"] == ["T0"]


def test_shared_checks_overlap_across_tiers(sandbox: Sandbox, fake_policy, workdir: Path) -> None:
    probes = workdir / "probes"
    tiers = {
        "T0": [policy.Check("t0.lint", "T0", cmd=_probe(probes, "t0"))],
        "T1": [policy.Check("t1.contracts", "T1", cmd=_probe(probes, "t1"))],
    }
    pol = _policy(fake_policy, tiers)
    assert preflight.run(["--jobs", "4", "--no-fetch"], policy=pol, cwd=sandbox.root) == 0
    assert _peak(probes, "t0") == 2
    assert _peak(probes, "t1") == 2
    assert sandbox.receipt()["ran_tiers"] == ["T0", "T1"]


def test_phase_b_runs_after_phase_a_in_tier_order(
    sandbox: Sandbox, fake_policy, workdir: Path, capsys
) -> None:
    probes = workdir / "probes"
    tiers = {
        "T0": [policy.Check("a0", "T0", cmd=_probe(probes, "a0"))],
        "T2": [policy.Check("x2", "T2", cmd=_probe(probes, "x2"), exclusive=True)],
        "T3": [
            policy.Check("x3", "T3", cmd=_probe(probes, "x3"), exclusive=True),
            policy.Check("a3", "T3", cmd=_probe(probes, "a3")),
        ],
    }
    pol = _policy(fake_policy, tiers)
    assert preflight.run(["--jobs", "4", "--no-fetch"], policy=pol, cwd=sandbox.root) == 0
    assert _peak(probes, "a0") == 2
    assert _peak(probes, "x2") == 1
    assert _peak(probes, "x3") == 1
    order = re.findall(r"\[[+x]\] T\d (\S+) \(", capsys.readouterr().err)
    assert order == ["a0", "a3", "x2", "x3"]
    receipt = sandbox.receipt()
    assert [c["id"] for c in receipt["checks"]] == ["a0", "x2", "x3", "a3"]
    assert receipt["ran_tiers"] == ["T0", "T2", "T3"]
    assert receipt["skipped_checks"] == []


def test_phase_a_failure_skips_phase_b(sandbox: Sandbox, fake_policy, workdir: Path) -> None:
    probes = workdir / "probes"
    tiers = {
        "T0": [
            policy.Check("a.fail", "T0", cmd=_probe(probes, "a", sleep=0.1, fail=True)),
            policy.Check("b.slow", "T0", cmd=_probe(probes, "b", sleep=0.8)),
        ],
        "T1": [policy.Check("t1.shared", "T1", cmd=_probe(probes, "t1", sleep=0.8))],
        "T2": [policy.Check("t2.xdist", "T2", cmd=_probe(probes, "t2"), exclusive=True)],
    }
    pol = _policy(fake_policy, tiers)
    assert preflight.run(["--jobs", "4", "--no-fetch"], policy=pol, cwd=sandbox.root) == 1
    assert (probes / "done-b").exists()
    assert (probes / "done-t1").exists()
    assert not (probes / "done-t2").exists()
    receipt = sandbox.receipt()
    assert receipt["status"] == "fail"
    assert receipt["ran_tiers"] == ["T0", "T1"]
    assert receipt["skipped_checks"] == ["t2.xdist"]
    assert [(c["id"], c["exit"]) for c in receipt["checks"]] == [
        ("a.fail", 1),
        ("b.slow", 0),
        ("t1.shared", 0),
    ]
    assert (
        preflight.run(["--jobs", "4", "--no-fetch", "--keep-going"], policy=pol, cwd=sandbox.root)
        == 1
    )
    assert (probes / "done-t2").exists()
    assert sandbox.receipt()["ran_tiers"] == ["T0", "T1", "T2"]


def test_exclusive_failure_skips_remaining_exclusives(
    sandbox: Sandbox, fake_policy, workdir: Path
) -> None:
    probes = workdir / "probes"
    tiers = {
        "T0": [policy.Check("a0", "T0", cmd=_probe(probes, "a0", sleep=0.1))],
        "T2": [policy.Check("x2", "T2", cmd=_probe(probes, "x2", fail=True), exclusive=True)],
        "T3": [
            policy.Check("x3a", "T3", cmd=_probe(probes, "x3a"), exclusive=True),
            policy.Check("a3", "T3", cmd=_probe(probes, "a3", sleep=0.1)),
        ],
    }
    pol = _policy(fake_policy, tiers)
    assert preflight.run(["--jobs", "4", "--no-fetch"], policy=pol, cwd=sandbox.root) == 1
    assert not (probes / "done-x3a").exists()
    assert (probes / "done-a3").exists()
    receipt = sandbox.receipt()
    assert receipt["ran_tiers"] == ["T0", "T2"]
    assert receipt["skipped_checks"] == ["x3a"]
    assert receipt["status"] == "fail"


def test_phase_wall_clock_is_recorded(sandbox: Sandbox, fake_policy, workdir: Path) -> None:
    probes = workdir / "probes"
    tiers = {"T0": [policy.Check("a0", "T0", cmd=_probe(probes, "a0", sleep=0.1))]}
    pol = _policy(fake_policy, tiers)
    assert preflight.run(["--jobs", "2", "--no-fetch"], policy=pol, cwd=sandbox.root) == 0
    receipt = sandbox.receipt()
    assert receipt["status"] == "pass"
    assert receipt["phase_wall_s"]["B"] < receipt["phase_wall_s"]["A"]


def test_jobs_one_is_serial(sandbox: Sandbox, fake_policy, workdir: Path) -> None:
    probes = workdir / "probes"
    checks = [policy.Check(f"c{i}", "T0", cmd=_probe(probes, f"c{i}", sleep=0.2)) for i in range(3)]
    pol = _policy(fake_policy, {"T0": checks})
    assert preflight.run(["--jobs", "1", "--no-fetch"], policy=pol, cwd=sandbox.root) == 0
    assert max(_peak(probes, f"c{i}") for i in range(3)) == 1
    assert sandbox.receipt()["parallel"] is False


def test_schedule_and_run_concurrent_preserve_policy_order() -> None:
    plan = {
        "T0": [policy.Check(f"t0.{i}", "T0", cmd="true", exclusive=i == 1) for i in range(3)],
        "T1": [policy.Check(f"t1.{i}", "T1", cmd="true", exclusive=i == 0) for i in range(2)],
    }
    phase_a, phase_b = preflight.schedule(plan, ["T0", "T1"])
    assert [c.id for c in phase_a] == ["t0.0", "t0.2", "t1.1"]
    assert [c.id for c in phase_b] == ["t0.1", "t1.0"]

    def execute(check):
        return 0, 0.0, Path(f"{check.id}.log")

    outcomes = preflight.run_concurrent(phase_a, execute, jobs=3)
    assert [o[2].name for o in outcomes] == ["t0.0.log", "t0.2.log", "t1.1.log"]


def test_heavy_policy_checks_are_exclusive() -> None:
    ctx = policy.Context(
        root=Path(__file__).resolve().parents[3],
        base_ref="origin/main",
        base_sha="0" * 40,
        merge_base="0" * 40,
        changed=["src/apm_cli/core/deployment_ledger.py", "tests/integration/test_x.py"],
        selected={"T0": "", "T1": "", "T2": "", "T3": ""},
    )
    by_id = {c.id: c for t in policy.TIERS for c in policy.build_checks(t, ctx)}
    for check in by_id.values():
        if check.cmd and "pytest" in check.cmd and " -n " in check.cmd:
            assert check.exclusive, check.id
    assert by_id["unit.full"].exclusive
    assert by_id["lifecycle.smoke-1"].exclusive
    assert by_id["lifecycle.smoke-2"].exclusive
    assert not any(c.exclusive for c in policy.build_checks("T0", ctx))
