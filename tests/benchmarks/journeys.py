#!/usr/bin/env python3
"""End-to-end journey benchmarks of the real ``apm`` CLI on the perf fixtures.

Runs the installed CLI as a subprocess (``--apm``, else ``APM_BINARY_PATH``,
else ``.venv/bin/apm``, else ``apm`` on PATH) through critical user journeys
on the seeded fixtures of ``tests/fixtures/perf`` and writes JSON plus
markdown: wall p50/p90, requests, round trips and connects per command, the
1-min load of every sample, the git SHA and the CLI version.

Fixtures: ``tree-10``, ``tree-50``, ``tree-200`` (scaling curve),
``monorepo-4`` (several ``apm.yml`` roots) and ``real`` (curated public
packages on github.com, real network). Offline fixtures are served by a local
mirror whose RTT is injected per round trip (``--rtt 0,30,100``).

Examples::

    python -m tests.benchmarks.journeys run --fixtures tree-10 --runs 1 --rtt 0
    python -m tests.benchmarks.journeys run --fixtures tree-10,tree-50,tree-200 \\
        --rtt 0,30,100 --out build/perf --profile
    python -m tests.benchmarks.journeys verify-real

A sample counts only when the 1-min load average is under ``--max-load``
(default 30); the harness waits for the load to drop before each timed run.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import math
import os
import platform
import pstats
import shutil
import statistics
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.fixtures.perf import (  # noqa: E402
    MONOREPO_RECIPES,
    REAL_PACKAGES,
    TREE_RECIPES,
    build_monorepo,
    build_tree,
    counting_tunnel,
    isolated_environment,
    serve_offline,
)
from tests.fixtures.perf.generator import consumer_manifest  # noqa: E402

LOCAL_JOURNEYS = ("compile", "audit", "pack", "unpack")
NETWORK_JOURNEYS = (
    "install-cold",
    "install-warm",
    "install-noop",
    "install-frozen",
    "install-global-cold",
    "install-global-warm",
    "update",
    "uninstall",
)
BOOT_JOURNEYS = ("boot-version", "boot-help")
ALL_JOURNEYS = BOOT_JOURNEYS + NETWORK_JOURNEYS + LOCAL_JOURNEYS
_GLOBAL = ("install-global-cold", "install-global-warm")
_COMMAND_TIMEOUT_S = 1800


# -- fixtures ------------------------------------------------------------------


@dataclass
class Fixture:
    """A project template (one or more ``apm.yml`` roots) plus what serves its deps."""

    name: str
    template: Path
    roots: tuple[str, ...]
    trees: tuple = ()
    real: bool = False
    stats: dict = field(default_factory=dict)


def prepare_fixture(name: str, work: Path) -> Fixture:
    """Generate (or reuse, when the recipe is unchanged) fixture *name* under *work*."""
    directory = work / "fixtures" / name
    if name == "real":
        template = directory / "project"
        shutil.rmtree(directory, ignore_errors=True)
        template.mkdir(parents=True)
        text = consumer_manifest("real-consumer", list(REAL_PACKAGES), ("copilot",))
        (template / "apm.yml").write_text(text, encoding="utf-8")
        return Fixture(name, template, (".",), real=True, stats={"packages": len(REAL_PACKAGES)})
    if name in TREE_RECIPES:
        recipe = TREE_RECIPES[name]
        tree = _rebuild(directory, recipe, lambda root: build_tree(recipe, root))
        stats = {
            "packages": len(tree.packages),
            "depth": tree.depth,
            "diamonds": tree.diamonds,
            "edges": sum(len(c) for c in tree.edges.values()),
            "primitives_per_package": recipe.primitives.per_package(),
        }
        return Fixture(name, tree.project, (".",), trees=(tree,), stats=stats)
    if name in MONOREPO_RECIPES:
        recipe = MONOREPO_RECIPES[name]
        mono = _rebuild(directory, recipe, lambda root: build_monorepo(recipe, root))
        roots = tuple(
            "." if root == mono.root else root.relative_to(mono.root).as_posix()
            for root in mono.project_roots
        )
        stats = {
            "roots": len(roots),
            "local_packages": len(mono.local_packages),
            "remote_packages": len(mono.remote.packages),
        }
        return Fixture(name, mono.root, roots, trees=(mono.remote,), stats=stats)
    known = ["real", *TREE_RECIPES, *MONOREPO_RECIPES]
    raise SystemExit(f"unknown fixture {name!r}; known: {known}")


def _rebuild(directory: Path, recipe: object, build: Callable[[Path], object]):
    """Regenerate the fixture; generation is seeded, so this is cheap to trust."""
    shutil.rmtree(directory, ignore_errors=True)
    directory.mkdir(parents=True)
    return build(directory / "src")


# -- worlds --------------------------------------------------------------------


class _RealWorld:
    """Real github.com through a counting CONNECT tunnel (no RTT injection)."""

    def __init__(self, isolated, env, tunnel) -> None:
        self.isolated = isolated
        self.tunnel = tunnel
        self.env = tunnel.child_environment(env)
        for name in ("GITHUB_APM_PAT", "GITHUB_TOKEN", "GH_TOKEN"):
            if os.environ.get(name):
                self.env[name] = os.environ[name]
        self.rtt_ms = 0.0

    @property
    def cache_dir(self) -> Path:
        return self.isolated.cache_root

    @property
    def apm_home(self) -> Path:
        return self.isolated.config_root

    def traffic(self) -> dict:
        return self.tunnel.traffic().as_dict()

    def reset_traffic(self) -> None:
        self.tunnel.reset_traffic()

    def clear_cache(self) -> None:
        shutil.rmtree(self.cache_dir, ignore_errors=True)
        self.cache_dir.mkdir(parents=True)


@contextlib.contextmanager
def open_world(fixture: Fixture, directory: Path) -> Iterator[object]:
    shutil.rmtree(directory / "env", ignore_errors=True)
    if fixture.real:
        isolated, env = isolated_environment(directory / "env")
        with counting_tunnel() as tunnel:
            yield _RealWorld(isolated, env, tunnel)
        return
    with serve_offline(fixture.trees, directory) as world:
        yield world


def _traffic(world) -> dict:
    traffic = world.traffic()
    return traffic if isinstance(traffic, dict) else traffic.as_dict()


# -- journeys ------------------------------------------------------------------


@dataclass
class Context:
    fixture: Fixture
    world: object
    apm: list[str]
    work: Path

    @property
    def project(self) -> Path:
        return self.work / "project"

    @property
    def elsewhere(self) -> Path:
        return self.work / "elsewhere"

    def roots(self) -> list[Path]:
        return [self.project / root for root in self.fixture.roots]

    def run(self, argv: list[str], cwd: Path) -> subprocess.CompletedProcess:
        return subprocess.run(
            [*self.apm, *argv],
            cwd=cwd,
            env=self.world.env,
            capture_output=True,
            text=True,
            timeout=_COMMAND_TIMEOUT_S,
            check=False,
        )

    def checked(self, argv: list[str], cwd: Path) -> None:
        result = self.run(argv, cwd)
        if result.returncode != 0:
            raise RuntimeError(
                f"setup `apm {' '.join(argv)}` failed ({result.returncode}) in {cwd}:\n"
                f"{result.stdout[-3000:]}\n{result.stderr[-3000:]}"
            )

    # state helpers (untimed)

    def fresh_project(self) -> None:
        shutil.rmtree(self.project, ignore_errors=True)
        shutil.copytree(self.fixture.template, self.project)
        self.elsewhere.mkdir(parents=True, exist_ok=True)

    def installed(self) -> None:
        """Every root installed from the current manifests (warm cache as a side effect)."""
        self.fresh_project()
        for root in self.roots():
            self.checked(["install"], root)

    def warm_cache(self) -> None:
        if not any(self.world.cache_dir.iterdir()):
            self.installed()

    def fresh_global(self) -> None:
        home = self.world.apm_home
        for name in ("apm_modules", "apm.lock.yaml"):
            path = home / name
            if path.is_dir():
                shutil.rmtree(path)
            elif path.exists():
                path.unlink()
        shutil.rmtree(Path(self.world.env["HOME"]) / ".copilot", ignore_errors=True)
        shutil.copyfile(self.fixture.template / "apm.yml", home / "apm.yml")


Step = tuple[list[str], Path]


def _per_root(ctx: Context, argv: list[str]) -> list[Step]:
    return [(argv, root) for root in ctx.roots()]


def _setup_cold(ctx: Context) -> None:
    ctx.world.clear_cache()
    ctx.fresh_project()


def _setup_warm(ctx: Context) -> None:
    ctx.warm_cache()
    ctx.fresh_project()


def _setup_frozen(ctx: Context) -> None:
    """CI replay: committed lockfiles, nothing else, cold cache."""
    ctx.installed()
    locks = {root: (root / "apm.lock.yaml").read_bytes() for root in ctx.roots()}
    ctx.fresh_project()
    for root, data in locks.items():
        (root / "apm.lock.yaml").write_bytes(data)
    ctx.world.clear_cache()


def _setup_global_cold(ctx: Context) -> None:
    ctx.world.clear_cache()
    ctx.fresh_project()
    ctx.fresh_global()


def _setup_global_warm(ctx: Context) -> None:
    ctx.warm_cache()
    ctx.fresh_global()


def _setup_unpack(ctx: Context) -> None:
    ctx.installed()
    root = ctx.roots()[0]
    ctx.checked(["pack", "--format", "apm", "--archive", "-o", str(ctx.work / "dist-apm")], root)
    target = ctx.work / "unpacked"
    shutil.rmtree(target, ignore_errors=True)
    target.mkdir()


def _unpack_steps(ctx: Context) -> list[Step]:
    (bundle,) = sorted((ctx.work / "dist-apm").iterdir())
    return [(["unpack", str(bundle), "-o", str(ctx.work / "unpacked")], ctx.work / "unpacked")]


def _uninstall_steps(ctx: Context) -> list[Step]:
    tree = ctx.fixture.trees[0] if ctx.fixture.trees else None
    slug = tree.slug(tree.roots[0]) if tree else REAL_PACKAGES[0].split("#")[0]
    return [(["uninstall", slug], ctx.roots()[0])]


def _setup_pack(ctx: Context) -> None:
    ctx.installed()
    shutil.rmtree(ctx.work / "dist", ignore_errors=True)


@dataclass(frozen=True)
class Journey:
    name: str
    setup: Callable[[Context], None]
    steps: Callable[[Context], list[Step]]


def _noop(_ctx: Context) -> None:
    return None


JOURNEYS: dict[str, Journey] = {
    j.name: j
    for j in (
        Journey("boot-version", _noop, lambda c: [(["--version"], c.elsewhere)]),
        Journey("boot-help", _noop, lambda c: [(["--help"], c.elsewhere)]),
        Journey("install-cold", _setup_cold, lambda c: _per_root(c, ["install"])),
        Journey("install-warm", _setup_warm, lambda c: _per_root(c, ["install"])),
        Journey("install-noop", Context.installed, lambda c: _per_root(c, ["install"])),
        Journey("install-frozen", _setup_frozen, lambda c: _per_root(c, ["install", "--frozen"])),
        Journey(
            "install-global-cold", _setup_global_cold, lambda c: [(["install", "-g"], c.elsewhere)]
        ),
        Journey(
            "install-global-warm", _setup_global_warm, lambda c: [(["install", "-g"], c.elsewhere)]
        ),
        Journey("update", Context.installed, lambda c: _per_root(c, ["update", "-y"])),
        Journey("uninstall", Context.installed, _uninstall_steps),
        Journey("compile", Context.installed, lambda c: _per_root(c, ["compile"])),
        Journey("audit", Context.installed, lambda c: _per_root(c, ["audit"])),
        Journey(
            "pack",
            _setup_pack,
            lambda c: [(["pack", "--archive", "-o", str(c.work / "dist")], c.roots()[0])],
        ),
        Journey("unpack", _setup_unpack, _unpack_steps),
    )
}


# -- measurement ---------------------------------------------------------------


def load1() -> float:
    return round(os.getloadavg()[0], 2)


def wait_for_load(max_load: float, max_wait_s: float) -> float:
    deadline = time.monotonic() + max_wait_s
    load = load1()
    while load >= max_load and time.monotonic() < deadline:
        print(f"[i] 1-min load {load} >= {max_load}; waiting", flush=True)
        time.sleep(30)
        load = load1()
    return load


def percentile(values: list[float], fraction: float) -> float:
    """Nearest-rank percentile (p90 of 5 samples is the maximum)."""
    ordered = sorted(values)
    return ordered[max(0, math.ceil(fraction * len(ordered)) - 1)]


@dataclass
class Sample:
    wall_s: float
    load1: float
    load_ok: bool
    traffic: dict
    returncode: int


def _timed(ctx: Context, journey: Journey) -> tuple[Sample, str]:
    ctx.world.reset_traffic()
    output = ""
    returncode = 0
    start = time.perf_counter()
    for argv, cwd in journey.steps(ctx):
        result = ctx.run(argv, cwd)
        if result.returncode != 0:
            returncode = result.returncode
            output = f"$ apm {' '.join(argv)}\n{result.stdout[-3000:]}\n{result.stderr[-3000:]}"
            break
    wall = time.perf_counter() - start
    return Sample(round(wall, 4), 0.0, True, _traffic(ctx.world), returncode), output


def _profile(ctx: Context, journey: Journey, out: Path) -> str | None:
    """One extra run under cProfile; only for a Python console-script ``apm``."""
    apm = Path(ctx.apm[-1])
    first = apm.read_bytes()[:200].split(b"\n", 1)[0]
    if not first.startswith(b"#!") or b"python" not in first:
        return None
    python = first[2:].decode().strip()
    journey.setup(ctx)
    prof = out / "profiles" / f"{ctx.fixture.name}-{journey.name}-rtt{int(ctx.world.rtt_ms)}.prof"
    prof.parent.mkdir(parents=True, exist_ok=True)
    for index, (argv, cwd) in enumerate(journey.steps(ctx)):
        target = prof if index == 0 else prof.with_suffix(f".{index}.prof")
        subprocess.run(
            [python, "-m", "cProfile", "-o", str(target), str(apm), *argv],
            cwd=cwd,
            env=ctx.world.env,
            capture_output=True,
            timeout=_COMMAND_TIMEOUT_S,
            check=False,
        )
    if prof.exists():
        with prof.with_suffix(".txt").open("w", encoding="utf-8") as handle:
            pstats.Stats(str(prof), stream=handle).sort_stats("cumulative").print_stats(40)
    return str(prof)


def measure(ctx: Context, journey: Journey, args: argparse.Namespace) -> dict:
    samples: list[Sample] = []
    error = ""
    for _ in range(args.runs):
        try:
            journey.setup(ctx)
        except RuntimeError as exc:
            error = str(exc)
            break
        load = wait_for_load(args.max_load, args.load_wait_min * 60)
        sample, output = _timed(ctx, journey)
        sample.load1 = load
        sample.load_ok = load < args.max_load
        samples.append(sample)
        if sample.returncode != 0:
            error = output
            break
    valid = [s for s in samples if s.load_ok and s.returncode == 0]
    walls = [s.wall_s for s in valid]
    record = {
        "fixture": ctx.fixture.name,
        "journey": journey.name,
        "rtt_ms": ctx.world.rtt_ms,
        "runs": len(samples),
        "valid_runs": len(valid),
        "p50_s": round(statistics.median(walls), 4) if walls else None,
        "p90_s": round(percentile(walls, 0.9), 4) if walls else None,
        "traffic": samples[-1].traffic if samples else {},
        "samples": [asdict(s) for s in samples],
        "error": error,
    }
    if args.profile and not error:
        record["profile"] = _profile(ctx, journey, Path(args.out))
    return record


# -- orchestration -------------------------------------------------------------


def _git_sha() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, capture_output=True, text=True, check=False
    )
    return result.stdout.strip() or "unknown"


def resolve_apm(explicit: str | None) -> list[str]:
    candidates = [explicit, os.environ.get("APM_BINARY_PATH"), str(REPO_ROOT / ".venv/bin/apm")]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return [str(Path(candidate).resolve())]
    found = shutil.which("apm")
    if found:
        return [found]
    raise SystemExit("no apm CLI found: pass --apm or set APM_BINARY_PATH")


def _journeys_for(fixture: Fixture, rtt: float, first: bool, wanted: list[str]) -> list[str]:
    names = []
    for name in wanted:
        if name in BOOT_JOURNEYS and not (first and rtt == 0):
            continue
        if name in LOCAL_JOURNEYS and rtt != 0:
            continue
        if name in _GLOBAL and len(fixture.roots) > 1:
            continue
        names.append(name)
    return names


def run(args: argparse.Namespace) -> dict:
    apm = resolve_apm(args.apm)
    work = Path(args.work).resolve()
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    version = subprocess.run([*apm, "--version"], capture_output=True, text=True, check=False)
    report: dict = {
        "meta": {
            "sha": _git_sha(),
            "apm": apm[0],
            "apm_version": version.stdout.strip(),
            "python": platform.python_version(),
            "platform": platform.platform(),
            "cpus": os.cpu_count(),
            "started": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "runs": args.runs,
            "rtt_ms": args.rtt,
            "max_load": args.max_load,
            "uptime_start": _uptime(),
        },
        "fixtures": {},
        "results": [],
    }
    wanted = args.journeys.split(",") if args.journeys else list(ALL_JOURNEYS)
    for index, name in enumerate(args.fixtures.split(",")):
        fixture = prepare_fixture(name, work)
        report["fixtures"][name] = fixture.stats
        rtts = [0.0] if fixture.real else args.rtt
        with open_world(fixture, work / "worlds" / name) as world:
            ctx = Context(fixture, world, apm, work / "runs" / name)
            ctx.work.mkdir(parents=True, exist_ok=True)
            ctx.fresh_project()
            for rtt in rtts:
                world.rtt_ms = rtt
                for journey in _journeys_for(fixture, rtt, index == 0, wanted):
                    record = measure(ctx, JOURNEYS[journey], args)
                    report["results"].append(record)
                    _progress(record)
                    _write(report, out)
    report["meta"]["uptime_end"] = _uptime()
    _write(report, out)
    return report


def _uptime() -> str:
    return subprocess.run(["uptime"], capture_output=True, text=True, check=False).stdout.strip()


def _progress(record: dict) -> None:
    status = "[x]" if record["error"] else "[+]"
    print(
        f"{status} {record['fixture']} rtt={record['rtt_ms']:g} {record['journey']}: "
        f"p50={record['p50_s']} p90={record['p90_s']} valid={record['valid_runs']}/"
        f"{record['runs']} traffic={record['traffic']}",
        flush=True,
    )
    if record["error"]:
        print(record["error"][-1500:], flush=True)


def _write(report: dict, out: Path) -> None:
    stem = f"journeys-{report['meta']['sha'][:12]}"
    (out / f"{stem}.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    (out / f"{stem}.md").write_text(render_markdown(report), encoding="utf-8")


def _cell(value) -> str:
    return "-" if value is None else f"{value:.3f}" if isinstance(value, float) else str(value)


def render_markdown(report: dict) -> str:
    meta = report["meta"]
    lines = [
        f"# APM journey benchmarks @ {meta['sha'][:12]}",
        "",
        f"- CLI: `{meta['apm_version']}` ({meta['apm']})",
        f"- Host: {meta['platform']}, {meta['cpus']} CPUs, Python {meta['python']}",
        f"- Started: {meta['started']}; runs per cell: {meta['runs']}; "
        f"samples count only at 1-min load < {meta['max_load']}",
        f"- uptime at start: `{meta['uptime_start']}`",
        f"- uptime at end: `{meta.get('uptime_end', 'running')}`",
        "",
        "## Fixtures",
        "",
    ]
    for name, stats in report["fixtures"].items():
        lines.append(f"- `{name}`: " + ", ".join(f"{k}={v}" for k, v in stats.items()))
    lines += [
        "",
        "## Results",
        "",
        "| fixture | rtt ms | journey | p50 s | p90 s | valid/runs | requests | round trips "
        "| connects | load1 range | error |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in report["results"]:
        loads = [s["load1"] for s in r["samples"]]
        load_range = f"{min(loads)}-{max(loads)}" if loads else "-"
        t = r["traffic"]
        lines.append(
            f"| {r['fixture']} | {r['rtt_ms']:g} | {r['journey']} | {_cell(r['p50_s'])} "
            f"| {_cell(r['p90_s'])} | {r['valid_runs']}/{r['runs']} | {t.get('requests', '-')} "
            f"| {t.get('round_trips', '-')} | {t.get('connects', '-')} | {load_range} "
            f"| {'yes' if r['error'] else ''} |"
        )
    lines += _scaling(report)
    return "\n".join(lines) + "\n"


def _scaling(report: dict) -> list[str]:
    trees = [name for name in TREE_RECIPES if name in report["fixtures"]]
    if len(trees) < 2:
        return []
    cells: dict[tuple[str, float], dict[str, str]] = {}
    for r in report["results"]:
        if r["fixture"] in trees and r["journey"] not in BOOT_JOURNEYS:
            cells.setdefault((r["journey"], r["rtt_ms"]), {})[r["fixture"]] = _cell(r["p50_s"])
    lines = ["", "## Scaling (p50 s by tree size)", ""]
    lines.append("| journey | rtt ms | " + " | ".join(trees) + " |")
    lines.append("|---|---|" + "---|" * len(trees))
    for (journey, rtt), row in sorted(cells.items(), key=lambda kv: (kv[0][1], kv[0][0])):
        lines.append(
            f"| {journey} | {rtt:g} | " + " | ".join(row.get(t, "-") for t in trees) + " |"
        )
    return lines


def verify_real(args: argparse.Namespace) -> int:
    """Install each REAL_PACKAGES entry alone; exit 1 when any fails to resolve."""
    apm = resolve_apm(args.apm)
    work = Path(args.work).resolve() / "verify-real"
    shutil.rmtree(work, ignore_errors=True)
    failures = 0
    isolated, env = isolated_environment(work / "env")
    with counting_tunnel() as tunnel:
        world = _RealWorld(isolated, env, tunnel)
        for index, spec in enumerate(REAL_PACKAGES):
            project = work / f"p{index}"
            project.mkdir(parents=True)
            text = consumer_manifest("verify", [spec], ("copilot",))
            (project / "apm.yml").write_text(text, encoding="utf-8")
            result = subprocess.run(
                [*apm, "install"],
                cwd=project,
                env=world.env,
                capture_output=True,
                text=True,
                timeout=600,
                check=False,
            )
            ok = result.returncode == 0
            failures += 0 if ok else 1
            print(f"{'[+]' if ok else '[x]'} {spec}", flush=True)
            if not ok:
                print((result.stdout + result.stderr)[-1500:], flush=True)
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tests.benchmarks.journeys")
    sub = parser.add_subparsers(dest="command", required=True)
    run_parser = sub.add_parser("run", help="run journeys and write JSON + markdown")
    run_parser.add_argument("--fixtures", default="tree-10,tree-50,tree-200,monorepo-4")
    run_parser.add_argument(
        "--journeys", default="", help=f"comma list; default all: {ALL_JOURNEYS}"
    )
    run_parser.add_argument(
        "--rtt", default="0,30,100", type=lambda v: [float(x) for x in v.split(",")]
    )
    run_parser.add_argument("--runs", type=int, default=5)
    run_parser.add_argument("--max-load", type=float, default=30.0)
    run_parser.add_argument("--load-wait-min", type=float, default=30.0)
    run_parser.add_argument(
        "--profile", action="store_true", help="one extra cProfile run per cell"
    )
    for p in (run_parser, sub.add_parser("verify-real", help="check REAL_PACKAGES resolve")):
        p.add_argument("--apm", default=None)
        p.add_argument("--work", default=str(REPO_ROOT / "build" / "perf-work"))
    run_parser.add_argument("--out", default=str(REPO_ROOT / "build" / "perf"))
    args = parser.parse_args(argv)
    if args.command == "verify-real":
        return verify_real(args)
    report = run(args)
    return 1 if any(r["error"] for r in report["results"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
