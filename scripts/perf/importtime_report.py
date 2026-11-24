"""Summarize ``-X importtime`` traces from the boot probe (research only).

Usage: python scripts/perf/importtime_report.py <out-dir>
"""

from __future__ import annotations

import statistics
import sys
from collections import defaultdict
from pathlib import Path

STDLIB = set(sys.stdlib_module_names)


def parse(path: Path) -> list[tuple[str, int, int, int]]:
    rows = []
    for line in path.read_text(errors="replace").splitlines():
        if not line.startswith("import time:") or "self [us]" in line:
            continue
        try:
            _, self_us, cum_us, name = _split(line)
        except ValueError:
            continue
        depth = (len(name) - len(name.lstrip(" "))) // 2
        rows.append((name.strip(), int(self_us), int(cum_us), depth))
    return rows


def _split(line: str) -> tuple[str, str, str, str]:
    head, cum, name = line.split("|", 2)
    self_us = head.split(":", 1)[1].strip()
    return "", self_us, cum.strip(), name[1:] if name.startswith(" ") else name


def kind(name: str) -> str:
    top = name.split(".", 1)[0]
    if top == "apm_cli":
        return "apm_cli"
    if top in STDLIB or top.startswith("_"):
        return "stdlib"
    return "third-party"


def summarize(label: str, files: list[Path]) -> None:
    if not files:
        print(f"## {label}: no traces\n")
        return
    totals, by_kind, by_top, mod_self, mod_cum, counts = [], defaultdict(list), {}, {}, {}, []
    per_file_top = []
    for path in files:
        rows = parse(path)
        counts.append(len(rows))
        totals.append(sum(r[1] for r in rows))
        k = defaultdict(int)
        top = defaultdict(int)
        for name, self_us, cum_us, _ in rows:
            k[kind(name)] += self_us
            top[name.split(".", 1)[0]] += self_us
            mod_self.setdefault(name, []).append(self_us)
            mod_cum.setdefault(name, []).append(cum_us)
        for key in ("stdlib", "third-party", "apm_cli"):
            by_kind[key].append(k[key])
        per_file_top.append(top)
    for top in per_file_top:
        for key, value in top.items():
            by_top.setdefault(key, []).append(value)
    n = len(files)
    print(f"## {label} ({n} traces)\n")
    print(
        f"- modules imported: p50 {statistics.median(counts):.0f}; "
        f"total import self-time p50 {statistics.median(totals) / 1e3:.1f} ms"
    )
    for key in ("stdlib", "third-party", "apm_cli"):
        print(f"- {key}: {statistics.median(by_kind[key]) / 1e3:.1f} ms")
    print("\n| top-level package | self ms (p50 per trace) |\n|---|---|")
    ranked = sorted(
        by_top.items(), key=lambda kv: -statistics.median(kv[1] + [0] * (n - len(kv[1])))
    )
    for key, values in ranked[:15]:
        print(f"| {key} | {statistics.median(values + [0] * (n - len(values))) / 1e3:.1f} |")
    print("\n| module | self ms | cumulative ms |\n|---|---|---|")
    for name, values in sorted(mod_self.items(), key=lambda kv: -statistics.median(kv[1]))[:15]:
        print(
            f"| {name} | {statistics.median(values) / 1e3:.1f} | "
            f"{statistics.median(mod_cum[name]) / 1e3:.1f} |"
        )
    print("\n| module (by cumulative) | cumulative ms | self ms |\n|---|---|---|")
    for name, values in sorted(mod_cum.items(), key=lambda kv: -statistics.median(kv[1]))[:15]:
        print(
            f"| {name} | {statistics.median(values) / 1e3:.1f} | "
            f"{statistics.median(mod_self[name]) / 1e3:.1f} |"
        )
    print()


def main() -> int:
    out = Path(sys.argv[1])
    summarize(
        "apm install (cold, tree-50) per apm invocation", sorted((out / "importtime").glob("*.txt"))
    )
    summarize("apm --version", sorted(out.glob("*-importtime-version.txt")))
    summarize(
        "import apm_cli.cli + commands.install", sorted(out.glob("*-importtime-install-module.txt"))
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
