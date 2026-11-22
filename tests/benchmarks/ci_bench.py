#!/usr/bin/env python3
"""Interleaved base-vs-new journey benchmarks for CI runners, plus the report.

``abab`` runs the journeys harness once per side per pair, alternating the
order (A B, B A, A B, ...) so runner drift lands on both sides equally. Each
side keeps its own work directory, so neither CLI reads the other's cache.

``report`` merges every shard's JSON and prints a markdown table: CPU p50
(user+sys of apm and its children, from wait4) per side over all samples,
the median of the per-pair new/base CPU ratios, and the deterministic counts
(requests, spawns). Wall time is never reported: runners are shared VMs.

Examples::

    python -m tests.benchmarks.ci_bench abab --base-apm A/.venv/bin/apm \\
        --new-apm B/.venv/bin/apm --pairs 3 --fixtures tree-10 \\
        --journeys install-noop,install-locked-warm --rtt 30 --out build/abab
    python -m tests.benchmarks.ci_bench report build/abab --json result.json
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.benchmarks import journeys  # noqa: E402

SIDES = ("base", "new")


def abab(args: argparse.Namespace) -> int:
    out = Path(args.out).resolve()
    apms = {"base": args.base_apm, "new": args.new_apm}
    failed = 0
    for pair in range(args.pairs):
        order = SIDES if pair % 2 == 0 else tuple(reversed(SIDES))
        for side in order:
            print(f"[>] pair {pair + 1}/{args.pairs} side {side}", flush=True)
            failed |= journeys.main(
                [
                    "run",
                    "--apm",
                    apms[side],
                    "--fixtures",
                    args.fixtures,
                    "--journeys",
                    args.journeys,
                    "--rtt",
                    args.rtt,
                    "--rtt-journeys",
                    args.journeys,
                    "--runs",
                    "1",
                    "--counts",
                    "--max-load",
                    "1000000",
                    "--load-wait-min",
                    "0",
                    "--work",
                    str(out / "work" / side),
                    "--out",
                    str(out / "runs" / f"{side}-{pair:02d}"),
                ]
            )
    return 1 if failed else 0


def _load(root: Path) -> dict:
    """{(fixture, journey, rtt): {side: [(shard_pair, record), ...]}} from every JSON."""
    cells: dict = {}
    for path in sorted(root.rglob("journeys-*.json")):
        run_dir = path.parent.name
        side, _, pair = run_dir.partition("-")
        if side not in SIDES:
            continue
        shard = path.parent.parent.name
        report = json.loads(path.read_text(encoding="utf-8"))
        for record in report["results"]:
            key = (record["fixture"], record["journey"], record["rtt_ms"])
            cells.setdefault(key, {}).setdefault(side, []).append((f"{shard}/{pair}", record))
    return cells


def _total(counts: dict | None) -> int | None:
    return None if counts is None else sum(counts.values())


def _cpu(record: dict) -> float | None:
    return None if record.get("error") else record.get("cpu_p50_s")


def summarize(root: Path) -> list[dict]:
    rows = []
    for (fixture, journey, rtt), sides in sorted(_load(root).items()):
        row: dict = {"fixture": fixture, "journey": journey, "rtt_ms": rtt}
        for side in SIDES:
            records = [r for _, r in sides.get(side, [])]
            cpus = [c for c in map(_cpu, records) if c is not None]
            last = records[-1] if records else {}
            row[side] = {
                "samples": len(cpus),
                "errors": sum(1 for r in records if r.get("error")),
                "cpu_p50_s": round(statistics.median(cpus), 4) if cpus else None,
                "cpu_min_s": round(min(cpus), 4) if cpus else None,
                "cpu_max_s": round(max(cpus), 4) if cpus else None,
                "requests": (last.get("traffic") or {}).get("requests"),
                "spawns": _total(last.get("spawns")),
                "spawns_by_exe": last.get("spawns"),
                "modules": last.get("modules"),
            }
        base_by_pair = {p: _cpu(r) for p, r in sides.get("base", [])}
        ratios = [
            new / base_by_pair[p]
            for p, r in sides.get("new", [])
            if (new := _cpu(r)) is not None and base_by_pair.get(p)
        ]
        row["paired_new_over_base"] = round(statistics.median(ratios), 4) if ratios else None
        rows.append(row)
    return rows


def render(rows: list[dict], meta: dict) -> str:
    lines = [
        "## Perf bench: base vs new (CPU user+sys incl. children; counts)",
        "",
        f"base `{meta.get('base', '?')}` -> new `{meta.get('new', '?')}`; "
        f"{meta.get('shards', '?')} runners x {meta.get('pairs', '?')} interleaved pairs. "
        "Wall time is not reported (shared runners).",
        "",
        "| fixture | journey | rtt ms | base CPU p50 s | new CPU p50 s | speedup x | "
        "CPU removed | paired new/base | requests base->new | spawns base->new | "
        "samples base/new | errors base/new |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        b, n = row["base"], row["new"]
        speedup = removed = "-"
        if b["cpu_p50_s"] and n["cpu_p50_s"]:
            speedup = f"{b['cpu_p50_s'] / n['cpu_p50_s']:.2f}x"
            removed = f"{(1 - n['cpu_p50_s'] / b['cpu_p50_s']) * 100:+.1f}%"
        paired = row["paired_new_over_base"]
        lines.append(
            f"| {row['fixture']} | {row['journey']} | {row['rtt_ms']:g} "
            f"| {journeys._cell(b['cpu_p50_s'])} | {journeys._cell(n['cpu_p50_s'])} "
            f"| {speedup} | {removed} | {'-' if paired is None else f'{paired:.3f}'} "
            f"| {b['requests']}->{n['requests']} | {b['spawns']}->{n['spawns']} "
            f"| {b['samples']}/{n['samples']} | {b['errors']}/{n['errors']} |"
        )
    return "\n".join(lines) + "\n"


def report(args: argparse.Namespace) -> int:
    rows = summarize(Path(args.root))
    meta = {"base": args.base, "new": args.new, "shards": args.shards, "pairs": args.pairs}
    print(render(rows, meta), end="")
    if args.json:
        Path(args.json).write_text(
            json.dumps({"meta": meta, "rows": rows}, indent=2) + "\n", encoding="utf-8"
        )
    return 1 if not rows or any(_incomplete(r, args) for r in rows) else 0


def _incomplete(row: dict, args: argparse.Namespace) -> bool:
    """A failed sample, or fewer samples than the requested runners x pairs."""
    if row["base"]["errors"] or row["new"]["errors"]:
        return True
    if str(args.shards).isdigit() and str(args.pairs).isdigit():
        expected = int(args.shards) * int(args.pairs)
        return any(row[side]["samples"] < expected for side in SIDES)
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tests.benchmarks.ci_bench")
    sub = parser.add_subparsers(dest="command", required=True)
    run_parser = sub.add_parser("abab", help="interleaved base/new journey runs")
    run_parser.add_argument("--base-apm", required=True)
    run_parser.add_argument("--new-apm", required=True)
    run_parser.add_argument("--pairs", type=int, default=3)
    run_parser.add_argument("--fixtures", default="tree-10")
    run_parser.add_argument(
        "--journeys",
        default="install-noop,install-locked-warm,install-warm,install-frozen-warm",
    )
    run_parser.add_argument("--rtt", default="30")
    run_parser.add_argument("--out", default=str(REPO_ROOT / "build" / "abab"))
    rep = sub.add_parser("report", help="merge shard JSON into a markdown table")
    rep.add_argument("root")
    rep.add_argument("--json", default=None)
    rep.add_argument("--base", default="?")
    rep.add_argument("--new", default="?")
    rep.add_argument("--shards", default="?")
    rep.add_argument("--pairs", default="?")
    args = parser.parse_args(argv)
    return abab(args) if args.command == "abab" else report(args)


if __name__ == "__main__":
    raise SystemExit(main())
