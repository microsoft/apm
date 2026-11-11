"""Concurrency inside a tier: bounded pool, exclusive checks alone, order."""

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
    assert set(receipt["tier_wall_s"]) == {"T0"}
    total = sum(c["duration_s"] for c in receipt["checks"])
    assert receipt["tier_wall_s"]["T0"] < total


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


def test_failure_lets_siblings_finish_and_fails_the_tier(
    sandbox: Sandbox, fake_policy, workdir: Path, capsys
) -> None:
    probes = workdir / "probes"
    t0 = [
        policy.Check("a.fast-fail", "T0", cmd=_probe(probes, "a", sleep=0.1, fail=True)),
        policy.Check("b.slow", "T0", cmd=_probe(probes, "b", sleep=0.8)),
        policy.Check("c.slow", "T0", cmd=_probe(probes, "c", sleep=0.8)),
    ]
    t1 = [policy.Check("t1.never", "T1", cmd=_probe(probes, "never"))]
    pol = _policy(fake_policy, {"T0": t0, "T1": t1})
    assert preflight.run(["--jobs", "3", "--no-fetch"], policy=pol, cwd=sandbox.root) == 1
    assert (probes / "done-b").exists()
    assert (probes / "done-c").exists()
    assert not (probes / "done-never").exists()
    receipt = sandbox.receipt()
    assert receipt["status"] == "fail"
    assert receipt["ran_tiers"] == ["T0"]
    assert [(c["id"], c["exit"]) for c in receipt["checks"][:3]] == [
        ("a.fast-fail", 1),
        ("b.slow", 0),
        ("c.slow", 0),
    ]
    err = capsys.readouterr().err
    order = re.findall(r"\[[+x]\] (\S+) \(", err)
    assert order[:3] == ["a.fast-fail", "b.slow", "c.slow"]
    for check in receipt["checks"]:
        assert Path(check["log"]).name == f"{check['id']}.log"


def test_jobs_one_is_serial(sandbox: Sandbox, fake_policy, workdir: Path) -> None:
    probes = workdir / "probes"
    checks = [policy.Check(f"c{i}", "T0", cmd=_probe(probes, f"c{i}", sleep=0.2)) for i in range(3)]
    pol = _policy(fake_policy, {"T0": checks})
    assert preflight.run(["--jobs", "1", "--no-fetch"], policy=pol, cwd=sandbox.root) == 0
    assert max(_peak(probes, f"c{i}") for i in range(3)) == 1
    assert sandbox.receipt()["parallel"] is False


def test_run_tier_preserves_policy_order() -> None:
    checks = [policy.Check(f"c{i}", "T0", cmd="true", exclusive=i % 2 == 0) for i in range(6)]
    seen: list[str] = []

    def execute(check):
        seen.append(check.id)
        return 0, 0.0, Path(f"{check.id}.log")

    outcomes = preflight.run_tier(checks, execute, jobs=3)
    assert [o[2].name for o in outcomes] == [f"c{i}.log" for i in range(6)]
    assert seen[-3:] == ["c0", "c2", "c4"]


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
