"""Cost map of the no-op / locked-warm / frozen-warm install (CI only).

For each fixture x journey: N timed runs (CPU user+sys of the apm process,
p50), then one cProfile run whose stage shares are scaled to that p50, plus
syscall-ish counts (lstat, stat, realpath, Path.resolve) and Path.resolve's
top callers. Usage: l5_costmap.py PYTHON WORK FIXTURES JOURNEYS N
"""

from __future__ import annotations

import json
import os
import pstats
import statistics
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tests.benchmarks import journeys  # noqa: E402

python, work = sys.argv[1], Path(sys.argv[2]).resolve()
fixtures, names, repeats = sys.argv[3].split(","), sys.argv[4].split(","), int(sys.argv[5])
RUNNER = Path(__file__).with_name("l5_entry.py")

# (label, filename suffix, function name). Cumulative and overlapping by design.
STAGES = [
    ("TOTAL (incl. imports)", "l5_entry.py", "run"),
    ("apm main", "apm_cli/entrypoint.py", "main"),
    ("imports (all)", "<frozen importlib._bootstrap>", "_find_and_load"),
    ("cli group (pre-command)", "apm_cli/cli.py", "cli"),
    ("fast_entry.run", "apm_cli/install/fast_entry.py", "run"),
    ("try_apply", "apm_cli/install/diff_apply.py", "try_apply"),
    ("  lockfile read", "apm_cli/deps/lockfile.py", "read"),
    ("  yaml lockfile parse", "apm_cli/deps/lockfile_container.py", "parse_lockfile_text"),
    ("  apm.yml load", "apm_cli/models/manifest_io.py", "load_manifest"),
    ("  policy admit", "apm_cli/install/noop_diff.py", "_policy_admits"),
    ("  sources scan", "apm_cli/install/noop_diff.py", "sources_scan_clean"),
    ("  plan packages", "apm_cli/install/diff_apply.py", "_plan_packages"),
    ("  ledger session", "apm_cli/core/deployment_ledger.py", "__init__"),
    ("  package records", "apm_cli/install/diff_apply.py", "_package_records"),
    ("  plan files", "apm_cli/install/diff_apply.py", "_plan_files"),
    ("  audit gate", "apm_cli/install/diff_apply.py", "_audit_gate"),
    ("    run_baseline_checks", "apm_cli/policy/ci_checks.py", "run_baseline_checks"),
    ("    hook approval pins", "apm_cli/policy/ci_checks.py", "_check_hook_approval_pins"),
    ("    audit scope", "apm_cli/policy/ci_checks.py", "resolve_audit_scope"),
    ("    deployed files present", "apm_cli/policy/ci_checks.py", "_check_deployed_files_present"),
    ("    skill subset", "apm_cli/policy/ci_checks.py", "_check_skill_subset_consistency"),
    ("    exec status", "apm_cli/policy/ci_checks.py", "_check_exec_status_current"),
    ("  policy checks", "apm_cli/install/noop_diff.py", "_policy_checks_pass"),
    ("  stage all", "apm_cli/install/diff_apply.py", "_stage_all"),
    ("  verify staged", "apm_cli/install/diff_apply.py", "_verify_staged"),
    ("  unproven edges", "apm_cli/install/diff_apply.py", "_refuse_unproven_edges"),
    ("  commit", "apm_cli/install/diff_apply.py", "_commit"),
    ("  render", "apm_cli/install/fast_output.py", "render"),
    ("    policy gate", "apm_cli/install/fast_output.py", "run_policy_gate"),
    ("    services", "apm_cli/install/fast_output.py", "run_services"),
    ("      merged MCP check", "apm_cli/install/fast_output.py", "check_merged_mcp"),
    ("      run_service_integrations", "apm_cli/install/service_integration.py", "run_service_integrations"),
    ("    summary", "apm_cli/install/summary.py", "render_post_install_summary"),
    ("update notice", "apm_cli/update_notice.py", "check_and_notify_updates"),
    ("MCP view derive", "apm_cli/integration/mcp_config_view.py", "derive"),
    ("ContentScanner.scan_file", "apm_cli/security/content_scanner.py", "scan_file"),
    ("Path.resolve", "pathlib.py", "resolve"),
    ("os.path.realpath", "posixpath", "realpath"),
    ("os.path.realpath (nt)", "ntpath", "realpath"),
    ("deepcopy", "copy.py", "deepcopy"),
]
COUNTS = ["lstat", "stat", "realpath", "resolve", "scandir", "open", "read_bytes", "read_text"]


def cpu_of(argv: list[str], cwd: Path, env: dict) -> tuple[int, float]:
    with tempfile.TemporaryFile() as out:
        proc = subprocess.Popen(argv, cwd=cwd, env=env, stdout=out, stderr=out)
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            proc.wait()
            times = [wintypes.FILETIME() for _ in range(4)]
            ctypes.windll.kernel32.GetProcessTimes(
                wintypes.HANDLE(int(proc._handle)), *[ctypes.byref(t) for t in times]
            )

            def ft(t):
                return ((t.dwHighDateTime << 32) | t.dwLowDateTime) / 1e7

            return proc.returncode, ft(times[2]) + ft(times[3])
        _, status, usage = os.wait4(proc.pid, 0)
        return os.waitstatus_to_exitcode(status), usage.ru_utime + usage.ru_stime


def find(stats: dict, suffix: str, func: str) -> tuple[float, int]:
    best = (0.0, 0)
    for (fname, _line, name), (cc, nc, _tt, ct, _callers) in stats.items():
        if name == func and fname.replace("\\", "/").endswith(suffix):
            if ct > best[0]:
                best = (ct, nc)
    return best


report = {"platform": sys.platform, "rows": []}
for fixture_name in fixtures:
    fixture = journeys.prepare_fixture(fixture_name, work)
    with journeys.open_world(fixture, work / "worlds" / fixture_name) as world:
        world.rtt_ms = 30
        ctx = journeys.Context(fixture, world, [python, str(RUNNER)], work / "runs" / fixture_name)
        ctx.work.mkdir(parents=True, exist_ok=True)
        for name in names:
            journey = journeys.JOURNEYS[name]
            samples = []
            for _ in range(repeats):
                journey.setup(ctx)
                cpu = 0.0
                for argv, cwd in journey.steps(ctx):
                    code, step = cpu_of([python, str(RUNNER), *argv], cwd, world.env)
                    assert code == 0, (name, argv, code)
                    cpu += step
                samples.append(cpu)
            p50 = statistics.median(samples)
            journey.setup(ctx)
            steps = journey.steps(ctx)
            traces = []
            merged = None
            for index, (argv, cwd) in enumerate(steps):
                prof = work / f"{fixture_name}-{name}-{index}.prof"
                env = dict(world.env, L5_PROFILE=str(prof), APM_INSTALL_FAST_PATH="trace")
                done = subprocess.run(
                    [python, str(RUNNER), *argv], cwd=cwd, env=env, capture_output=True, text=True
                )
                traces.append(
                    next((l for l in done.stderr.splitlines() if "fast path:" in l), "(no trace)")
                )
                merged = pstats.Stats(str(prof)) if merged is None else merged.add(str(prof))
            raw = merged.stats  # type: ignore[union-attr]
            total = find(raw, "l5_entry.py", "run")[0] or 1.0
            rows = []
            for label, suffix, func in STAGES:
                ct, nc = find(raw, suffix, func)
                rows.append((label, round(ct / total * p50, 4), round(100 * ct / total, 1), nc))
            counts = {}
            for (fname, _l, fn), (cc, nc, tt, ct, _c) in raw.items():
                if fn in COUNTS or any(fn == f"<built-in method posix.{c}>" or fn == f"<built-in method nt.{c}>" for c in COUNTS):
                    counts[fn] = counts.get(fn, 0) + nc
            callers = []
            for (fname, line, fn), (cc, nc, tt, ct, cl) in raw.items():
                if fn == "resolve" and fname.endswith("pathlib.py"):
                    callers = sorted(
                        ((v[1], f"{Path(k[0]).name}:{k[1]}({k[2]})") for k, v in cl.items()),
                        reverse=True,
                    )[:12]
            row = {
                "fixture": fixture_name,
                "journey": name,
                "cpu_p50_s": round(p50, 4),
                "cpu_samples": [round(s, 3) for s in samples],
                "traces": sorted(set(traces)),
                "stages": rows,
                "counts": counts,
                "resolve_callers": callers,
            }
            report["rows"].append(row)
            print(f"\n=== {sys.platform} {fixture_name} {name}: CPU p50 {p50:.3f}s {row['cpu_samples']}")
            print("    trace:", row["traces"])
            for label, secs, pct, nc in rows:
                print(f"    {label:<34} {secs:>7.3f}s {pct:>5.1f}%  calls {nc}")
            print("    counts:", counts)
            print("    Path.resolve callers:", callers)
            stats_txt = work / f"{fixture_name}-{name}-top.txt"
            with stats_txt.open("w") as handle:
                pstats.Stats(*[str(work / f"{fixture_name}-{name}-{i}.prof") for i in range(len(steps))], stream=handle).sort_stats("tottime").print_stats(40)
            print(stats_txt.read_text()[-6000:])
out = Path(os.environ.get("L5_COSTMAP_OUT", work / "costmap.json"))
out.write_text(json.dumps(report, indent=1))
