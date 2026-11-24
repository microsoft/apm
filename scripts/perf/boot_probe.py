"""Boot-floor probe (research only): interpreter start, imports, CLI plumbing.

Usage: python scripts/perf/boot_probe.py <side-python> <side-apm> <out.json> [runs]

CPU is user + sys of the child and its descendants (wait4 / Job Object),
via the bench's own ``_run_measured``.
"""

from __future__ import annotations

import compileall
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tests.benchmarks.journeys import _run_measured


def measure(argv: list[str], runs: int, env: dict | None = None) -> dict:
    cpus, walls = [], []
    for _ in range(runs):
        start = time.perf_counter()
        code, cpu = _run_measured(
            argv,
            120,
            env=env or dict(os.environ),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        walls.append(time.perf_counter() - start)
        if code != 0:
            raise SystemExit(f"{argv} exited {code}")
        cpus.append(cpu)
    return {
        "cpu_p50": statistics.median(cpus),
        "wall_p50": statistics.median(walls),
        "cpu_min": min(cpus),
        "runs": runs,
    }


def main() -> int:
    py, apm, out = sys.argv[1], sys.argv[2], Path(sys.argv[3])
    runs = int(sys.argv[4]) if len(sys.argv) > 4 else 21
    base = dict(os.environ)
    base.pop("PYTHONPATH", None)
    pkg = Path(
        subprocess.run(
            [py, "-c", "import apm_cli,os;print(os.path.dirname(apm_cli.__file__))"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    )
    first = {}
    has_pyc = any(pkg.rglob("__pycache__/*.pyc"))
    first["pyc_present_after_install"] = has_pyc
    start = time.perf_counter()
    _, cpu = _run_measured([apm, "--version"], 300, env=base, stdout=subprocess.DEVNULL)
    first["first_apm_version_cpu"] = cpu
    first["first_apm_version_wall"] = time.perf_counter() - start
    _, cpu = _run_measured(
        [py, "-c", "import apm_cli.cli, apm_cli.commands.install"],
        300,
        env=base,
        stdout=subprocess.DEVNULL,
    )
    first["first_import_install_cpu"] = cpu

    imp_install = "import apm_cli.cli, apm_cli.commands.install"
    rows = {
        "python -S -c pass": [py, "-S", "-c", "pass"],
        "python -I -c pass": [py, "-I", "-c", "pass"],
        "python -c pass": [py, "-c", "pass"],
        "python -X frozen_modules=off -c pass": [py, "-X", "frozen_modules=off", "-c", "pass"],
        "import click": [py, "-c", "import click"],
        "import apm_cli": [py, "-c", "import apm_cli"],
        "import apm_cli.cli": [py, "-c", "import apm_cli.cli"],
        "import apm_cli.cli+commands.install": [py, "-c", imp_install],
        "apm --version": [apm, "--version"],
        "apm --help": [apm, "--help"],
        "apm install --help": [apm, "install", "--help"],
    }
    results = {name: measure(argv, runs, base) for name, argv in rows.items()}

    work = Path(tempfile.mkdtemp(prefix="boot-probe-"))
    try:
        nopyc = work / "nopyc"
        shutil.copytree(pkg, nopyc / "apm_cli", ignore=shutil.ignore_patterns("__pycache__"))
        env = dict(base, PYTHONPATH=str(nopyc))
        results["apm_cli from source, no pyc (-B)"] = measure(
            [py, "-B", "-c", imp_install], max(5, runs // 3), env
        )
        compileall.compile_dir(str(nopyc), quiet=1)
        results["apm_cli from source dir, pyc"] = measure([py, "-c", imp_install], runs, env)
        legacy = work / "legacy"
        shutil.copytree(pkg, legacy / "apm_cli", ignore=shutil.ignore_patterns("__pycache__"))
        compileall.compile_dir(str(legacy), quiet=1, legacy=True)
        archive = work / "apm_cli.zip"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_STORED) as zf:
            for path in sorted(legacy.rglob("*")):
                if path.is_file() and path.suffix != ".py":
                    zf.write(path, path.relative_to(legacy).as_posix())
        env = dict(base, PYTHONPATH=str(archive))
        check = subprocess.run(
            [py, "-c", "import apm_cli;print(apm_cli.__file__)"],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        first["zip_import_file"] = check.stdout.strip() or check.stderr[-500:]
        results["apm_cli from zip (pyc only)"] = measure([py, "-c", imp_install], runs, env)
        trace = subprocess.run(
            [py, "-X", "importtime", "-c", imp_install],
            env=base,
            capture_output=True,
            text=True,
            check=True,
        )
        (out.parent / f"{out.stem}-importtime-install-module.txt").write_text(trace.stderr)
        trace = subprocess.run(
            [apm, "--version"],
            env=dict(base, PYTHONPROFILEIMPORTTIME="1"),
            capture_output=True,
            text=True,
            check=True,
        )
        (out.parent / f"{out.stem}-importtime-version.txt").write_text(trace.stderr)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    report = {
        "python": sys.version,
        "platform": sys.platform,
        "side_python": py,
        "first": first,
        "results": results,
    }
    out.write_text(json.dumps(report, indent=2))
    for name, row in results.items():
        print(
            f"{name:45s} cpu {row['cpu_p50'] * 1000:8.1f} ms  wall {row['wall_p50'] * 1000:8.1f} ms"
        )
    print(json.dumps(first, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
