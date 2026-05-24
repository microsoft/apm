"""Perf harness for #1433 sparse-cone consumer materialization.

Measures cold + warm cache `apm install` against a deep-monorepo
subdir dep (default: dotnet/skills/plugins/dotnet-diag/skills/
analyzing-dotnet-performance#main).

Usage:
    uv run python scripts/perf/measure_dotnet_skills.py

Prints a table comparing:
  - wall_time (seconds)
  - bare cache bytes (db_v1/<shard>/)
  - checkouts_v1 shard bytes (.../<shard>/<sha>/<variant>/)
  - delivered apm_modules dep bytes

ASCII-only output (matches encoding-rules.instructions.md).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from apm_cli.cache.paths import (  # noqa: E402
    get_cache_root,
    get_git_checkouts_path,
    get_git_db_path,
)

DEFAULT_DEP = (
    "github.com/dotnet/skills/plugins/dotnet-diag/"
    "skills/analyzing-dotnet-performance#main"
)
APM_YML_TEMPLATE = """\
name: perf-harness
version: 0.0.0
targets:
  - copilot
dependencies:
  apm:
    - {dep}
"""


def _dir_size_bytes(path: Path | None) -> int:
    if path is None or not path.exists():
        return 0
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += (Path(root) / f).stat().st_size
            except OSError:
                pass
    return total


def _fmt_bytes(n: int) -> str:
    if n == 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB"]
    v = float(n)
    i = 0
    while v >= 1024 and i < len(units) - 1:
        v /= 1024
        i += 1
    return f"{v:.1f} {units[i]}"


def _wipe_cache_root() -> None:
    root = get_cache_root()
    if not root.exists():
        return

    def _chmod_writable(_func, path, _exc):
        try:
            os.chmod(path, 0o700)
        except OSError:
            pass

    # First pass: re-grant write to any dirs the cache chmod'd to 0o700
    # (or chmod 0o500 on parent dirs after some failure mode).
    for r, dirs, files in os.walk(root):
        for d in dirs:
            try:
                os.chmod(Path(r) / d, 0o700)
            except OSError:
                pass
        for f in files:
            try:
                os.chmod(Path(r) / f, 0o600)
            except OSError:
                pass
    shutil.rmtree(root, onerror=_chmod_writable)
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)


def _largest_subdir(parent: Path | None) -> Path | None:
    if parent is None or not parent.exists():
        return None
    children = [c for c in parent.iterdir() if c.is_dir()]
    if not children:
        return None
    return max(children, key=lambda c: _dir_size_bytes(c))


def _run_install(project_dir: Path) -> float:
    env = os.environ.copy()
    # Always use `uv run apm` from this repo's root so we exercise the
    # current worktree's apm, NOT a stale system-installed apm.
    cmd = ["uv", "run", "--project", str(ROOT), "apm", "install", "--verbose"]

    start = time.monotonic()
    result = subprocess.run(
        cmd,
        cwd=str(project_dir),
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
    )
    elapsed = time.monotonic() - start

    if result.returncode != 0:
        sys.stderr.write("[x] apm install failed:\n")
        sys.stderr.write(result.stdout)
        sys.stderr.write(result.stderr)
        raise SystemExit(2)
    sys.stdout.write(result.stdout)
    sys.stdout.write(result.stderr)
    return elapsed


def _measure(project_dir: Path) -> dict:
    elapsed = _run_install(project_dir)
    cache_root = get_cache_root()
    db_root = get_git_db_path(cache_root)
    co_root = get_git_checkouts_path(cache_root)

    db_shard = _largest_subdir(db_root)
    co_shard = _largest_subdir(co_root)
    co_sha = _largest_subdir(co_shard)
    co_variant = _largest_subdir(co_sha)

    # On baseline layout, <sha>/ IS the checkout (no variant subdir).
    # On the new layout, <sha>/<variant>/ holds the working tree.
    # Report the deepest non-empty directory whose total size is the
    # actual on-disk consumer materialization cost.
    sha_total = _dir_size_bytes(co_sha)
    variant_total = _dir_size_bytes(co_variant)

    return {
        "wall_time": elapsed,
        "bare_bytes": _dir_size_bytes(db_shard),
        "checkout_sha_bytes": sha_total,
        "checkout_variant_bytes": variant_total,
        "checkouts_variant": co_variant.name if co_variant else "(none)",
        "apm_modules_bytes": _dir_size_bytes(project_dir / "apm_modules"),
    }


def main() -> int:
    dep = os.environ.get("PERF_DEP", DEFAULT_DEP)
    with tempfile.TemporaryDirectory(prefix="apm-perf-") as td:
        project = Path(td) / "project"
        project.mkdir()
        (project / "apm.yml").write_text(
            APM_YML_TEMPLATE.format(dep=dep), encoding="utf-8"
        )

        print(f"[i] Harness project: {project}")
        print(f"[i] Dep: {dep}")
        print(f"[i] Cache root: {get_cache_root()}")
        print()

        print("[>] Cold run (wiping caches)...")
        _wipe_cache_root()
        cold = _measure(project)

        shutil.rmtree(project / "apm_modules", ignore_errors=True)
        lock = project / "apm.lock.yaml"
        if lock.exists():
            lock.unlink()

        print()
        print("[>] Warm run (caches retained)...")
        warm = _measure(project)

        print()
        print("=" * 64)
        print(f"{'metric':<28} {'cold':>15} {'warm':>15}")
        print("-" * 64)
        print(
            f"{'wall_time (s)':<28} "
            f"{cold['wall_time']:>15.2f} {warm['wall_time']:>15.2f}"
        )
        print(
            f"{'bare cache':<28} "
            f"{_fmt_bytes(cold['bare_bytes']):>15} "
            f"{_fmt_bytes(warm['bare_bytes']):>15}"
        )
        print(
            f"{'checkouts sha-dir total':<28} "
            f"{_fmt_bytes(cold['checkout_sha_bytes']):>15} "
            f"{_fmt_bytes(warm['checkout_sha_bytes']):>15}"
        )
        print(
            f"{'  variant subdir (if any)':<28} "
            f"{_fmt_bytes(cold['checkout_variant_bytes']):>15} "
            f"{_fmt_bytes(warm['checkout_variant_bytes']):>15}"
        )
        print(f"  variant name (cold): {cold['checkouts_variant']}")
        print(f"  variant name (warm): {warm['checkouts_variant']}")
        print(
            f"{'apm_modules delivered':<28} "
            f"{_fmt_bytes(cold['apm_modules_bytes']):>15} "
            f"{_fmt_bytes(warm['apm_modules_bytes']):>15}"
        )
        print("=" * 64)
        speedup = cold["wall_time"] / max(warm["wall_time"], 0.001)
        print(f"[i] cold/warm speedup: {speedup:.2f}x")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
