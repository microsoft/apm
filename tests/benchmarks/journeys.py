#!/usr/bin/env python3
"""End-to-end journey benchmarks of the real ``apm`` CLI on the perf fixtures.

Runs the installed CLI as a subprocess (``--apm``, else ``APM_BINARY_PATH``,
else ``.venv/bin/apm`` or ``.venv/Scripts/apm.exe``, else ``apm`` on PATH)
through critical user journeys on the seeded fixtures of ``tests/fixtures/perf``
and writes JSON plus
markdown: wall p50/p90, requests, round trips and connects per command, the
1-min load of every sample, the git SHA and the CLI version.

Fixtures: ``tree-10``, ``tree-50``, ``tree-200`` (scaling curve),
``monorepo-4`` (several ``apm.yml`` roots) and ``real`` (curated public
packages on github.com, real network). Enterprise shapes: ``tree-N-multi``
(packages spread over GitHub, GHE Cloud, GitLab and Azure DevOps),
``tree-N-ghe`` / ``tree-N-gitlab`` (every package on one non-github.com host,
so installs take the git path), ``tree-N-mcp`` (a stdio MCP server in every package) and the ``-policy``
suffix on any offline fixture (the project's git remote points at an org
whose blocking policy is served, so discovery and enforcement run). On Windows,
offline fixtures model the default Git for Windows TLS config (schannel, the
mirror CA in the machine Root store, a served CRL); an ``-openssl`` suffix
runs the secondary OpenSSL cell. Offline fixtures are served by a local
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
import base64
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
import tempfile
import time
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.benchmarks import output_oracle  # noqa: E402
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
from tests.fixtures.perf.counters import read_spawn_log  # noqa: E402
from tests.fixtures.perf.generator import consumer_manifest, remove_tree, write_lf  # noqa: E402
from tests.fixtures.perf.offline import POLICY_ORG  # noqa: E402

LOCAL_JOURNEYS = ("compile", "audit", "audit-ci", "pack", "install-bundle")
NETWORK_JOURNEYS = (
    "install-cold",
    "install-warm",
    "install-noop",
    "install-frozen",
    "install-locked-warm",
    "install-frozen-warm",
    "install-global-cold",
    "install-global-warm",
    "install-cold-auth",
    "install-warm-auth",
    "update",
    "uninstall",
    "outdated",
    "prune",
)
BOOT_JOURNEYS = ("boot-version", "boot-help")
ALL_JOURNEYS = BOOT_JOURNEYS + NETWORK_JOURNEYS + LOCAL_JOURNEYS
_GLOBAL = ("install-global-cold", "install-global-warm")
_PACKS = ("pack", "install-bundle")
DEFAULT_RTT_JOURNEYS = ("install-cold", "install-warm", "install-frozen", "update")
_COMMAND_TIMEOUT_S = 1800
_SPAWN_COUNTER_DIR = Path(__file__).resolve().parent / "spawn_counter"

# The North Star scoreboard: tree-10 at 30 ms RTT, 5 runs, CPU and counts on.
# Mode -> journey: no-op (all installed), warm (lockfile, no apm_modules, warm
# cache), fresh (no lockfile, warm cache), frozen (--frozen, warm cache),
# empty-cache (no lockfile, empty cache: network-bound, reported separately).
SCOREBOARD_MODES = (
    ("no-op", "install-noop"),
    ("warm", "install-locked-warm"),
    ("fresh", "install-warm"),
    ("frozen", "install-frozen-warm"),
    ("empty-cache", "install-cold"),
    ("boot", "boot-version"),
)
SCOREBOARD_PRESET = {
    "fixtures": "tree-10",
    "journeys": ",".join(journey for _, journey in SCOREBOARD_MODES),
    "rtt": [30.0],
    "runs": 5,
}


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
    policy: bool = False
    # Windows git TLS for offline fixtures: "system" (default Git for Windows,
    # schannel) or "openssl" (an ``-openssl`` suffix; a secondary cell).
    tls_mode: str = "system"


_POLICY_SUFFIX = "-policy"
_OPENSSL_SUFFIX = "-openssl"


def prepare_fixture(name: str, work: Path) -> Fixture:
    """Fixture *name*; a ``-policy`` suffix puts the project under the served org policy.

    An ``-openssl`` suffix (last) runs Windows git over OpenSSL with the mirror CA
    file instead of the default Git for Windows TLS config.
    """
    if name.endswith(_OPENSSL_SUFFIX) and name != _OPENSSL_SUFFIX:
        fixture = prepare_fixture(name[: -len(_OPENSSL_SUFFIX)], work)
        if fixture.real:
            raise SystemExit(f"{name!r}: the openssl variant needs an offline fixture")
        fixture.name, fixture.tls_mode = name, "openssl"
        return fixture
    if name.endswith(_POLICY_SUFFIX) and name != _POLICY_SUFFIX:
        fixture = _prepare_fixture(name[: -len(_POLICY_SUFFIX)], work)
        if fixture.real:
            raise SystemExit(f"{name!r}: the policy variant needs an offline fixture")
        fixture.name, fixture.policy = name, True
        fixture.stats["policy"] = POLICY_ORG
        return fixture
    return _prepare_fixture(name, work)


def _prepare_fixture(name: str, work: Path) -> Fixture:
    """Generate (or reuse, when the recipe is unchanged) fixture *name* under *work*."""
    directory = work / "fixtures" / name
    if name == "real":
        template = directory / "project"
        remove_tree(directory)
        template.mkdir(parents=True)
        text = consumer_manifest("real-consumer", list(REAL_PACKAGES), ("copilot",))
        write_lf(template / "apm.yml", text)
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
            "hosts": ",".join(recipe.hosts),
            "mcp_every": recipe.primitives.mcp_every,
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
    remove_tree(directory)
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
        remove_tree(self.cache_dir)
        self.cache_dir.mkdir(parents=True)


@contextlib.contextmanager
def open_world(fixture: Fixture, directory: Path) -> Iterator[object]:
    remove_tree(directory / "env")
    if fixture.real:
        isolated, env = isolated_environment(directory / "env")
        with counting_tunnel() as tunnel:
            yield _RealWorld(isolated, env, tunnel)
        return
    with serve_offline(
        fixture.trees, directory, policy=fixture.policy, tls_mode=fixture.tls_mode
    ) as world:
        if world.tls:
            fixture.stats["git_tls"] = world.tls
        yield world


def _traffic(world) -> dict:
    traffic = world.traffic()
    return traffic if isinstance(traffic, dict) else traffic.as_dict()


def _observations(world) -> tuple:
    """Requests the offline mirror saw (empty for the real-network tunnel)."""
    services = getattr(world, "services", None)
    return services.proxy.observations if services is not None else ()


def _route(observation) -> str:
    """``METHOD host /coarse/path``: owner, repo and ref segments collapsed to ``*``."""
    parts = [p for p in observation.path.split("/") if p]
    if parts[:1] == ["repos"] and len(parts) > 3:
        parts = ["repos", "*", "*", parts[3]] + (["*"] if len(parts) > 4 else [])
    elif parts and parts[-1] in ("refs", "git-upload-pack", "git-receive-pack"):
        parts = ["*", *parts[-2:]] if parts[-1] == "refs" else ["*", parts[-1]]
    return f"{observation.method} {observation.host} /{'/'.join(parts)}"


def _routes(observations) -> dict[str, int]:
    routes: dict[str, int] = {}
    for observation in observations:
        if observation.method != "CONNECT":
            route = _route(observation)
            routes[route] = routes.get(route, 0) + 1
    return dict(sorted(routes.items(), key=lambda kv: -kv[1]))


# -- journeys ------------------------------------------------------------------


def _wait4_with_deadline(proc: subprocess.Popen, timeout_s: float) -> tuple[int, object]:
    """``wait4`` the child (CPU of it and its waited-for descendants), bounded.

    A child still running at the deadline is killed and reaped, and the
    command fails like ``subprocess.run(timeout=...)`` would.
    """
    deadline = time.monotonic() + timeout_s
    delay = 0.005
    while True:
        pid, status, usage = os.wait4(proc.pid, os.WNOHANG)
        if pid:
            return status, usage
        if time.monotonic() >= deadline:
            proc.kill()
            _, killed, _ = os.wait4(proc.pid, 0)
            proc.returncode = os.waitstatus_to_exitcode(killed)
            raise subprocess.TimeoutExpired(proc.args, timeout_s)
        time.sleep(delay)
        delay = min(delay * 2, 0.1)


_CREATE_SUSPENDED = 0x00000004
_JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION = 1
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000


def _job_popen(argv: list[str], **kwargs) -> tuple[subprocess.Popen, int]:
    """Windows: start *argv* inside a fresh Job Object; return the process and the job.

    The process starts suspended and resumes only once it is in the job, so
    every descendant (the venv launcher's python.exe, git and its helpers)
    lands in the job too. Were it started running, children spawned before
    ``AssignProcessToJobObject`` would be missed. The job kills its processes
    when its last handle closes, so a crashed harness orphans nothing.
    """
    import ctypes
    from ctypes import wintypes

    class _BasicLimits(ctypes.Structure):
        _fields_ = (
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        )

    class _ExtendedLimits(ctypes.Structure):
        _fields_ = (
            ("BasicLimitInformation", _BasicLimits),
            ("IoInfo", ctypes.c_uint64 * 6),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        )

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.CreateJobObjectW.argtypes = (wintypes.LPVOID, wintypes.LPCWSTR)
    kernel32.SetInformationJobObject.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    )
    kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    kernel32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
    ntdll = ctypes.WinDLL("ntdll")
    ntdll.NtResumeProcess.argtypes = (wintypes.HANDLE,)
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    proc = None
    try:
        limits = _ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(
            job, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, ctypes.byref(limits), ctypes.sizeof(limits)
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        proc = subprocess.Popen(argv, creationflags=_CREATE_SUSPENDED, **kwargs)
        # Popen keeps the process handle in the private ``_handle``.
        handle = int(proc._handle)
        if not kernel32.AssignProcessToJobObject(job, handle):
            raise ctypes.WinError(ctypes.get_last_error())
        status = ntdll.NtResumeProcess(handle)
        if status:
            raise OSError(f"NtResumeProcess failed: NTSTATUS {status & 0xFFFFFFFF:#010x}")
    except BaseException:
        if proc is not None:
            kernel32.TerminateJobObject(job, 1)
            proc.kill()
            proc.wait()
        _close_job(job)
        raise
    return proc, job


def _close_job(job: int) -> None:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle(job)


def _job_wait_with_deadline(proc: subprocess.Popen, job: int, timeout_s: float) -> float:
    """Windows: wait for *proc*, bounded; return the job's user + kernel CPU seconds.

    ``JobObjectBasicAccountingInformation`` totals the CPU of every process
    ever in the job (the child and all its descendants), in 100 ns units. A
    child still running at the deadline has its whole job terminated, and the
    command fails like ``subprocess.run(timeout=...)`` would.
    """
    import ctypes
    from ctypes import wintypes

    class _Accounting(ctypes.Structure):
        _fields_ = (
            ("TotalUserTime", ctypes.c_int64),
            ("TotalKernelTime", ctypes.c_int64),
            ("ThisPeriodTotalUserTime", ctypes.c_int64),
            ("ThisPeriodTotalKernelTime", ctypes.c_int64),
            ("TotalPageFaultCount", wintypes.DWORD),
            ("TotalProcesses", wintypes.DWORD),
            ("ActiveProcesses", wintypes.DWORD),
            ("TotalTerminatedProcesses", wintypes.DWORD),
        )

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
    kernel32.QueryInformationJobObject.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.LPDWORD,
    )
    try:
        try:
            proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            kernel32.TerminateJobObject(job, 1)
            proc.wait()
            raise
        info = _Accounting()
        if not kernel32.QueryInformationJobObject(
            job,
            _JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION,
            ctypes.byref(info),
            ctypes.sizeof(info),
            None,
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        return _job_cpu_seconds(info.TotalUserTime, info.TotalKernelTime)
    finally:
        _close_job(job)


def _job_cpu_seconds(user_100ns: int, kernel_100ns: int) -> float:
    """Job Object user + kernel times (100 ns units) as CPU seconds."""
    return (user_100ns + kernel_100ns) / 10_000_000


def _run_measured(
    argv: list[str], timeout_s: float, *, platform: str = sys.platform, **popen_kwargs
) -> tuple[int, float]:
    """Run *argv* to completion, bounded; return its exit code and CPU seconds.

    CPU is user + sys of the child and its descendants: ``wait4`` on POSIX
    (descendants it waited for), a Job Object on Windows (every process ever
    in the job).
    """
    if platform == "win32":
        proc, job = _job_popen(argv, **popen_kwargs)
        cpu = _job_wait_with_deadline(proc, job, timeout_s)
        return proc.returncode, cpu
    proc = subprocess.Popen(argv, **popen_kwargs)
    status, usage = _wait4_with_deadline(proc, timeout_s)
    proc.returncode = os.waitstatus_to_exitcode(status)
    return proc.returncode, usage.ru_utime + usage.ru_stime


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

    spawn_log: Path | None = None
    count_fs: bool = False
    # Setup reuse: the installed state survives a no-op sample, and the
    # committed lockfiles are captured once per fixture (generation is seeded).
    is_installed: bool = False
    locks: dict[str, bytes] | None = None

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

    def timed_run(self, argv: list[str], cwd: Path) -> tuple[int, str, float]:
        """Run one command; return its exit code, output tail and CPU seconds.

        CPU is user + sys of the child and every descendant it waited for
        (``wait4``; on Windows, every process of its Job Object), so
        server-side ``git http-backend`` work is excluded.
        """
        env = dict(self.world.env)
        if self.spawn_log is not None:
            env["APM_PERF_SPAWN_LOG"] = str(self.spawn_log)
            env["PYTHONPATH"] = str(_SPAWN_COUNTER_DIR)
            if self.count_fs:
                env["APM_PERF_COUNT_FS"] = "1"
        with (
            tempfile.TemporaryFile() as out,
            tempfile.TemporaryFile() as err,
        ):
            returncode, cpu = _run_measured(
                [*self.apm, *argv], _COMMAND_TIMEOUT_S, cwd=cwd, env=env, stdout=out, stderr=err
            )
            out.seek(0)
            err.seek(0)
            # Redact before slicing: a token cut at the boundary leaves no fragment.
            tail = _redact(out.read().decode(errors="replace"))[-3000:] + "\n"
            tail += _redact(err.read().decode(errors="replace"))[-3000:]
        return returncode, tail, cpu

    def checked(self, argv: list[str], cwd: Path) -> None:
        result = self.run(argv, cwd)
        if result.returncode != 0:
            raise RuntimeError(
                f"setup `apm {' '.join(argv)}` failed ({result.returncode}) in {cwd}:\n"
                f"{_redact(result.stdout)[-3000:]}\n{_redact(result.stderr)[-3000:]}"
            )

    # state helpers (untimed)

    def fresh_project(self) -> None:
        remove_tree(self.project)
        shutil.copytree(self.fixture.template, self.project)
        self.elsewhere.mkdir(parents=True, exist_ok=True)
        self.is_installed = False
        if self.fixture.policy:
            self._govern()

    def _govern(self) -> None:
        """A git checkout whose ``origin`` is in ``POLICY_ORG``: its policy governs."""
        remote = f"https://github.com/{POLICY_ORG}/consumer.git"
        for argv in (
            ["init", "-q", "--initial-branch=main"],
            ["remote", "add", "origin", remote],
        ):
            subprocess.run(
                ["git", "-C", str(self.project), *argv],
                env=self.world.env,
                capture_output=True,
                check=True,
                timeout=60,
            )

    def installed(self) -> None:
        """Every root installed from the current manifests (warm cache as a side effect)."""
        if self.is_installed:
            return
        self.fresh_project()
        for root in self.roots():
            self.checked(["install"], root)
        self.is_installed = True

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
        remove_tree(Path(self.world.env["HOME"]) / ".copilot")
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


def _locked_project(ctx: Context, warm: bool = True) -> None:
    """A fresh checkout of an installed project: manifests plus committed lockfiles."""
    if ctx.locks is None:
        ctx.installed()
        ctx.locks = {
            root: (ctx.project / root / "apm.lock.yaml").read_bytes() for root in ctx.fixture.roots
        }
    if warm:
        ctx.warm_cache()
    ctx.fresh_project()
    for root, data in ctx.locks.items():
        (ctx.project / root / "apm.lock.yaml").write_bytes(data)


def _setup_frozen(ctx: Context) -> None:
    """CI replay: committed lockfiles, nothing else, cold cache."""
    _locked_project(ctx, warm=False)
    ctx.world.clear_cache()


def _setup_global_cold(ctx: Context) -> None:
    ctx.world.clear_cache()
    ctx.fresh_project()
    ctx.fresh_global()


def _setup_global_warm(ctx: Context) -> None:
    ctx.warm_cache()
    ctx.fresh_global()


def _setup_install_bundle(ctx: Context) -> None:
    """A plugin bundle packed from the installed project; a fresh consumer installs it."""
    ctx.installed()
    dist = ctx.work / "dist-bundle"
    remove_tree(dist)
    ctx.checked(["pack", "--archive", "-o", str(dist)], ctx.roots()[0])
    consumer = ctx.work / "bundle-consumer"
    remove_tree(consumer)
    consumer.mkdir()
    text = consumer_manifest("bundle-consumer", [], ("copilot",))
    write_lf(consumer / "apm.yml", text)


def _install_bundle_steps(ctx: Context) -> list[Step]:
    (bundle,) = sorted((ctx.work / "dist-bundle").iterdir())
    return [(["install", str(bundle)], ctx.work / "bundle-consumer")]


def _uninstall_steps(ctx: Context) -> list[Step]:
    """Uninstall the first remote direct dependency of the first root."""
    root = ctx.roots()[0]
    manifest = yaml.safe_load((root / "apm.yml").read_text(encoding="utf-8"))
    remote = [d for d in manifest["dependencies"]["apm"] if not d.startswith(".")]
    return [(["uninstall", remote[0].split("#", 1)[0]], root)]


def _setup_prune(ctx: Context) -> None:
    """An installed project whose first remote direct dependency left ``apm.yml``.

    The edit a user makes by hand before ``apm prune``: that package (and any
    subtree only it needed) is now absent from the resolved graph.
    """
    ctx.installed()
    root = ctx.roots()[0]
    manifest = yaml.safe_load((root / "apm.yml").read_text(encoding="utf-8"))
    deps = manifest["dependencies"]["apm"]
    deps.remove(next(d for d in deps if not d.startswith(".")))
    write_lf(root / "apm.yml", yaml.safe_dump(manifest, sort_keys=False))
    ctx.is_installed = False


def _setup_pack(ctx: Context) -> None:
    ctx.installed()
    remove_tree(ctx.work / "dist")


@dataclass(frozen=True)
class Journey:
    name: str
    setup: Callable[[Context], None]
    steps: Callable[[Context], list[Step]]
    # Extra child environment for this journey only (setup, timed steps, profile).
    env: tuple[tuple[str, str], ...] = ()


def _noop(_ctx: Context) -> None:
    return None


_AUTH = (("GITHUB_APM_PAT", "ghp_" + "0" * 36), ("APM_GITHUB_AUTH_FIRST", "1"))
# The CI job's read-only token for real-network authenticated cells. Taken out of
# the environment at import, so no world (and no anonymous cell) inherits it.
_BENCH_REAL_TOKEN = os.environ.pop("APM_BENCH_REAL_TOKEN", "")


JOURNEYS: dict[str, Journey] = {
    j.name: j
    for j in (
        Journey("boot-version", _noop, lambda c: [(["--version"], c.elsewhere)]),
        Journey("boot-help", _noop, lambda c: [(["--help"], c.elsewhere)]),
        Journey("install-cold", _setup_cold, lambda c: _per_root(c, ["install"])),
        Journey("install-warm", _setup_warm, lambda c: _per_root(c, ["install"])),
        # Authenticated users (private repos, GHE, ADO, GitLab, or a token to
        # avoid the anonymous rate limit) take the token-first git CLI path: a
        # fixed placeholder token the offline mirror accepts, auth first.
        Journey("install-cold-auth", _setup_cold, lambda c: _per_root(c, ["install"]), _AUTH),
        Journey("install-warm-auth", _setup_warm, lambda c: _per_root(c, ["install"]), _AUTH),
        Journey("install-noop", Context.installed, lambda c: _per_root(c, ["install"])),
        Journey("install-frozen", _setup_frozen, lambda c: _per_root(c, ["install", "--frozen"])),
        Journey("install-locked-warm", _locked_project, lambda c: _per_root(c, ["install"])),
        Journey(
            "install-frozen-warm",
            _locked_project,
            lambda c: _per_root(c, ["install", "--frozen"]),
        ),
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
        Journey("audit-ci", Context.installed, lambda c: _per_root(c, ["audit", "--ci"])),
        Journey("outdated", Context.installed, lambda c: _per_root(c, ["outdated"])),
        Journey("prune", _setup_prune, lambda c: [(["prune"], c.roots()[0])]),
        Journey(
            "pack",
            _setup_pack,
            lambda c: [(["pack", "--archive", "-o", str(c.work / "dist")], c.roots()[0])],
        ),
        Journey("install-bundle", _setup_install_bundle, _install_bundle_steps),
    )
}


# -- measurement ---------------------------------------------------------------


def load1() -> float:
    """The 1-min load average; 0.0 on Windows, which has none."""
    if not hasattr(os, "getloadavg"):
        return 0.0
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
    cpu_s: float = 0.0
    spawns: dict = field(default_factory=dict)
    modules: int = 0
    routes: dict = field(default_factory=dict)
    git_verbs: dict = field(default_factory=dict)
    fs: dict = field(default_factory=dict)
    output: dict = field(default_factory=dict)


def _read_spawn_log(path: Path | None) -> tuple[dict, int]:
    """Spawns by executable summed over every Python process, plus the max module count."""
    counters = read_spawn_log(path)
    return counters["spawns"], counters["modules"]


def _timed(ctx: Context, journey: Journey) -> tuple[Sample, str]:
    ctx.world.reset_traffic()
    seen = len(_observations(ctx.world))
    if ctx.spawn_log is not None:
        ctx.spawn_log.unlink(missing_ok=True)
    output = ""
    returncode = 0
    cpu = 0.0
    start = time.perf_counter()
    for argv, cwd in journey.steps(ctx):
        code, tail, step_cpu = ctx.timed_run(argv, cwd)
        cpu += step_cpu
        if code != 0:
            returncode = code
            output = f"$ apm {' '.join(argv)}\n{tail}"
            break
    wall = time.perf_counter() - start
    counters = read_spawn_log(ctx.spawn_log)
    spawns, modules = counters["spawns"], counters["modules"]
    sample = Sample(round(wall, 4), 0.0, True, _traffic(ctx.world), returncode)
    sample.cpu_s, sample.spawns, sample.modules = round(cpu, 4), spawns, modules
    sample.git_verbs, sample.fs = counters["git_verbs"], counters["fs"]
    sample.routes = _routes(_observations(ctx.world)[seen:])
    if returncode == 0:
        sample.output = _deployed_output(ctx, journey)
    return sample, output


_USER_TARGETS = (".github", ".claude", ".cursor", ".windsurf", ".kiro", ".gemini", ".codex")
_USER_TARGETS += (".opencode", ".copilot")


def _deployed_output(ctx: Context, journey: Journey) -> dict:
    """What the journey left behind (untimed): see ``output_oracle``."""
    work = ctx.work.parents[1]
    if journey.name in _GLOBAL:
        home = Path(ctx.world.env["HOME"])
        return output_oracle.capture(
            [home / name for name in _USER_TARGETS], work, lock_dirs=[ctx.world.apm_home]
        )
    return output_oracle.capture(ctx.roots(), work)


def _sample_record(sample: Sample) -> dict:
    record = asdict(sample)
    record["output"] = {k: v for k, v in sample.output.items() if k != "manifest"}
    return record


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
        result = subprocess.run(
            [python, "-m", "cProfile", "-o", str(target), str(apm), *argv],
            cwd=cwd,
            env=ctx.world.env,
            capture_output=True,
            timeout=_COMMAND_TIMEOUT_S,
            check=False,
        )
        if result.returncode != 0 or not target.is_file():
            stderr = _redact(result.stderr.decode(errors="replace"))[-500:]
            print(f"[!] cProfile run failed for {journey.name}: {stderr}")
            return None
    if prof.exists():
        with prof.with_suffix(".txt").open("w", encoding="utf-8") as handle:
            stats = pstats.Stats(str(prof), stream=handle)
            stats.sort_stats("cumulative").print_stats(60)
            stats.sort_stats("tottime").print_stats(40)
    return str(prof)


@contextlib.contextmanager
def _journey_env(ctx: Context, journey: Journey):
    """Overlay *journey*'s extra environment on the world's child env, then restore it."""
    env = ctx.world.env
    saved = {key: env.get(key) for key, _value in journey.env}
    env.update(journey.env)
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value


def _refuse_real_auth(_ctx: Context) -> None:
    raise RuntimeError(
        "authenticated journeys on the real fixture need the caller's GITHUB_APM_PAT"
    )


def _for_world(ctx: Context, journey: Journey) -> Journey:
    """*journey* as run against this world.

    The placeholder token only works against the offline mirror. On real
    github.com the authenticated journeys use ``APM_BENCH_REAL_TOKEN`` (the CI
    job's read-only ``GITHUB_TOKEN``) or the caller's own ``GITHUB_APM_PAT``
    (copied by ``_RealWorld``); without one the journey fails instead of
    measuring a rejected token. The anonymous journeys never get the bench token.
    """
    if not ctx.fixture.real or all(key != "GITHUB_APM_PAT" for key, _ in journey.env):
        return journey
    token = _real_token(ctx)
    if not token:
        return replace(journey, setup=_refuse_real_auth)
    env = tuple((key, token if key == "GITHUB_APM_PAT" else value) for key, value in journey.env)
    return replace(journey, env=env)


def _real_token(ctx: Context) -> str:
    return _BENCH_REAL_TOKEN or ctx.world.env.get("GITHUB_APM_PAT", "")


def _redact(text: str) -> str:
    """*text* without the bench token (error tails land in uploaded artifacts).

    Covers the raw token and the Basic credential git sends for it
    (``x-access-token:<token>``, plain and base64).
    """
    if not _BENCH_REAL_TOKEN or not text:
        return text
    basic = f"x-access-token:{_BENCH_REAL_TOKEN}"
    for secret in (base64.b64encode(basic.encode()).decode(), basic, _BENCH_REAL_TOKEN):
        text = text.replace(secret, "***")
    return text


def measure(ctx: Context, journey: Journey, args: argparse.Namespace) -> dict:
    journey = _for_world(ctx, journey)
    with _journey_env(ctx, journey):
        return _measure(ctx, journey, args)


def _measure(ctx: Context, journey: Journey, args: argparse.Namespace) -> dict:
    samples: list[Sample] = []
    error = ""
    for _ in range(args.runs):
        try:
            journey.setup(ctx)
        except RuntimeError as exc:
            error = _redact(str(exc))
            break
        load = wait_for_load(args.max_load, args.load_wait_min * 60)
        sample, output = _timed(ctx, journey)
        if journey.name != "install-noop":
            ctx.is_installed = False
        sample.load1 = load
        sample.load_ok = load < args.max_load
        samples.append(sample)
        if sample.returncode != 0:
            error = _redact(output)
            break
    valid = [s for s in samples if s.load_ok and s.returncode == 0]
    if not valid and not error:
        error = (
            f"no valid sample: 1-min load stayed >= --max-load {args.max_load} "
            f"in all {len(samples)} run(s)"
        )
    walls = [s.wall_s for s in valid]
    # CPU and counts do not depend on load, so every successful sample counts.
    ok = [s for s in samples if s.returncode == 0]
    cpus = [s.cpu_s for s in ok]
    record = {
        "fixture": ctx.fixture.name,
        "journey": journey.name,
        "rtt_ms": ctx.world.rtt_ms,
        "runs": len(samples),
        "valid_runs": len(valid),
        "p50_s": round(statistics.median(walls), 4) if walls else None,
        "p90_s": round(percentile(walls, 0.9), 4) if walls else None,
        "cpu_p50_s": round(statistics.median(cpus), 4) if cpus else None,
        "traffic": samples[-1].traffic if samples else {},
        "spawns": ok[-1].spawns if ok else {},
        "modules": ok[-1].modules if ok else 0,
        "routes": ok[-1].routes if ok else {},
        "git_verbs": ok[-1].git_verbs if ok else {},
        "fs": ok[-1].fs if ok else {},
        "output": ok[-1].output if ok else {},
        "samples": [_sample_record(s) for s in samples],
        "error": error,
    }
    if args.profile and not error:
        record["profile"] = _profile(ctx, journey, Path(args.out).resolve())
    return record


# -- orchestration -------------------------------------------------------------


def _git_sha() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, capture_output=True, text=True, check=False
    )
    return result.stdout.strip() or "unknown"


def resolve_apm(explicit: str | None) -> list[str]:
    venv_apm = ".venv/Scripts/apm.exe" if sys.platform == "win32" else ".venv/bin/apm"
    candidates = [explicit, os.environ.get("APM_BINARY_PATH"), str(REPO_ROOT / venv_apm)]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return [str(Path(candidate).resolve())]
    found = shutil.which("apm")
    if found:
        return [found]
    raise SystemExit("no apm CLI found: pass --apm or set APM_BINARY_PATH")


def _journeys_for(
    fixture: Fixture, rtt: float, first: bool, wanted: list[str], rtt_journeys: list[str]
) -> list[str]:
    """Boot runs once (first fixture, first RTT); other non-RTT journeys only at RTT 0."""
    names = []
    for name in wanted:
        if name in BOOT_JOURNEYS:
            if first:
                names.append(name)
            continue
        if rtt != 0 and name not in rtt_journeys:
            continue
        # A monorepo has several roots and local-path deps, which pack refuses by design.
        if name in (*_GLOBAL, *_PACKS) and len(fixture.roots) > 1:
            continue
        # The served policy governs the project checkout only: global installs
        # and the bundle consumer have no governed remote, so they would not
        # exercise it.
        if name in (*_GLOBAL, "install-bundle") and fixture.policy:
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
            "git": subprocess.run(
                ["git", "--version"], capture_output=True, text=True, check=False
            ).stdout.strip(),
            "platform": platform.platform(),
            "cpus": os.cpu_count(),
            "started": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "runs": args.runs,
            "rtt_ms": args.rtt,
            "rtt_journeys": args.rtt_journeys,
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
            if args.counts:
                ctx.spawn_log = ctx.work / "spawns.jsonl"
                ctx.count_fs = args.count_fs
            ctx.fresh_project()
            for rtt_index, rtt in enumerate(rtts):
                world.rtt_ms = rtt
                first = index == 0 and rtt_index == 0
                names = _journeys_for(fixture, rtt, first, wanted, args.rtt_journeys)
                for journey in names:
                    record = measure(ctx, JOURNEYS[journey], args)
                    report["results"].append(record)
                    _progress(record)
                    _write(report, out)
    report["meta"]["uptime_end"] = _uptime()
    _write(report, out)
    return report


def _uptime() -> str:
    try:
        result = subprocess.run(["uptime"], capture_output=True, text=True, check=False)
    except OSError:  # no ``uptime`` (Windows)
        return "-"
    return result.stdout.strip()


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


def _cli_sha(report: dict) -> str:
    """The measured CLI's commit as ``apm --version`` prints it, or ``unknown``.

    A wheel-installed CLI prints no commit; the harness SHA is recorded separately
    and never stands in for it.
    """
    version = report["meta"]["apm_version"]
    if version.endswith(")") and "(" in version:
        sha = version.rsplit("(", 1)[1][:-1].strip()
        if sha and all(c in "0123456789abcdef" for c in sha.lower()):
            return sha
    return "unknown"


def _write(report: dict, out: Path) -> None:
    cli = _cli_sha(report)
    stem = (
        f"journeys-{cli}"
        if cli != "unknown"
        else f"journeys-cli-unknown-{report['meta']['sha'][:12]}"
    )
    (out / f"{stem}.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    (out / f"{stem}.md").write_text(render_markdown(report), encoding="utf-8")


def _cell(value) -> str:
    return "-" if value is None else f"{value:.3f}" if isinstance(value, float) else str(value)


def _spawn_cell(spawns: dict | None) -> str:
    """Total spawns plus the per-executable split, e.g. ``42 (git 40, uv 2)``."""
    if not spawns:
        return "0" if spawns == {} else "-"
    split = ", ".join(f"{name} {count}" for name, count in spawns.items())
    return f"{sum(spawns.values())} ({split})"


_NORTH_STAR = ("no-op", "warm", "fresh", "frozen")
SCOREBOARD_HEADER = (
    "| main SHA | measured | "
    + " | ".join(f"{mode} wall / CPU s" for mode, _ in SCOREBOARD_MODES)
    + " | requests (no-op/warm/fresh/frozen/empty) | spawns (no-op/warm/fresh/frozen/empty) "
    "| modules no-op | load1 | worst gap to 1 s (wall / CPU) "
    "| CPU vs previous row (no-op/warm/fresh/frozen) |"
)


def _previous_cpu(text: str) -> tuple[str, dict[str, float]]:
    """The SHA and per-mode CPU p50 of the last scoreboard row in *text*."""
    rows = [
        line
        for line in text.splitlines()
        if line.startswith("| ") and " / " in line and line != SCOREBOARD_HEADER
    ]
    if not rows:
        return "", {}
    cells = [cell.strip() for cell in rows[-1].strip("|").split("|")]
    cpu = {}
    for (mode, _), cell in zip(SCOREBOARD_MODES, cells[2:], strict=False):
        value = cell.rsplit("/", 1)[-1].strip()
        if value not in ("-", ""):
            cpu[mode] = float(value)
    return cells[0], cpu


def _delta(before: float | None, after: float | None) -> str:
    """Speedup factor and change of the whole journey, e.g. ``2.00x -50%``."""
    if not before or after is None:
        return "-"
    return f"{before / after:.2f}x {100 * (after - before) / before:+.0f}%"


def scoreboard_row(report: dict, previous: tuple[str, dict[str, float]] = ("", {})) -> str:
    """One markdown row for the North Star scoreboard (wall and CPU p50 per mode)."""
    by_journey = {r["journey"]: r for r in report["results"] if r["fixture"] == "tree-10"}
    cells, requests, spawns, loads = [], [], [], []
    wall_gaps, cpu_gaps, deltas = [], [], []
    for mode, journey in SCOREBOARD_MODES:
        r = by_journey.get(journey)
        if r is None:
            cells.append("-")
            continue
        loads += [s["load1"] for s in r["samples"]]
        cpu = r.get("cpu_p50_s")
        cells.append(f"{_cell(r['p50_s'])} / {_cell(cpu)}")
        if mode != "boot":
            requests.append(str(r["traffic"].get("requests", "-")))
            spawns.append(str(sum((r.get("spawns") or {}).values())))
        if mode in _NORTH_STAR:
            if r["p50_s"] is not None:
                wall_gaps.append(r["p50_s"] - 1.0)
            if cpu is not None:
                cpu_gaps.append(cpu - 1.0)
            deltas.append(_delta(previous[1].get(mode), cpu))
    noop = by_journey.get("install-noop") or {}
    load = f"{min(loads)}-{max(loads)}" if loads else "-"
    wall_gap = f"{max(wall_gaps):+.2f}" if wall_gaps else "-"
    cpu_gap = f"{max(cpu_gaps):+.2f}" if cpu_gaps else "-"
    vs = f"vs {previous[0]}: " + " / ".join(deltas) if previous[0] else "first row"
    return (
        f"| {_cli_sha(report)} | {report['meta']['started']} | "
        + " | ".join(cells)
        + f" | {'/'.join(requests)} | {'/'.join(spawns)} | {noop.get('modules') or '-'} "
        f"| {load} | {wall_gap} / {cpu_gap} s | {vs} |"
    )


def append_scoreboard(report: dict, path: Path) -> None:
    """Append this run's row to *path*, writing the header on first use."""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    previous = (
        _previous_cpu(text[text.find(SCOREBOARD_HEADER) :])
        if SCOREBOARD_HEADER in text
        else ("", {})
    )
    if SCOREBOARD_HEADER not in text:
        separator = "|" + "---|" * SCOREBOARD_HEADER.count(" | ") + "---|"
        text += f"\n{SCOREBOARD_HEADER}\n{separator}\n"
    path.write_text(text + scoreboard_row(report, previous) + "\n", encoding="utf-8")


def render_markdown(report: dict) -> str:
    meta = report["meta"]
    lines = [
        f"# APM journey benchmarks: CLI @ {_cli_sha(report)} (harness @ {meta['sha'][:12]})",
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
        "| fixture | rtt ms | journey | p50 s | p90 s | CPU p50 s | valid/runs | requests "
        "| round trips | connects | spawns | modules | load1 range | error |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in report["results"]:
        loads = [s["load1"] for s in r["samples"]]
        load_range = f"{min(loads)}-{max(loads)}" if loads else "-"
        t = r["traffic"]
        lines.append(
            f"| {r['fixture']} | {r['rtt_ms']:g} | {r['journey']} | {_cell(r['p50_s'])} "
            f"| {_cell(r['p90_s'])} | {_cell(r.get('cpu_p50_s'))} | {r['valid_runs']}/{r['runs']} "
            f"| {t.get('requests', '-')} | {t.get('round_trips', '-')} | {t.get('connects', '-')} "
            f"| {_spawn_cell(r.get('spawns'))} | {r.get('modules') or '-'} | {load_range} "
            f"| {'yes' if r['error'] else ''} |"
        )
    routed = [r for r in report["results"] if r.get("routes")]
    if routed:
        lines += ["", "## Requests by route (last valid sample)", ""]
        lines += ["| fixture | rtt ms | journey | route | count |", "|---|---|---|---|---|"]
        for r in routed:
            for route, count in r["routes"].items():
                lines.append(
                    f"| {r['fixture']} | {r['rtt_ms']:g} | {r['journey']} | `{route}` | {count} |"
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
    remove_tree(work)
    failures = 0
    isolated, env = isolated_environment(work / "env")
    with counting_tunnel() as tunnel:
        world = _RealWorld(isolated, env, tunnel)
        for index, spec in enumerate(REAL_PACKAGES):
            project = work / f"p{index}"
            project.mkdir(parents=True)
            text = consumer_manifest("verify", [spec], ("copilot",))
            write_lf(project / "apm.yml", text)
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


def _explicit_options(argv: list[str]) -> set[str]:
    """Destination names of the ``--options`` the caller actually passed."""
    return {arg[2:].split("=", 1)[0].replace("-", "_") for arg in argv if arg.startswith("--")}


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
    run_parser.add_argument(
        "--rtt-journeys",
        default=",".join(DEFAULT_RTT_JOURNEYS),
        type=lambda v: v.split(","),
        help="journeys also measured at non-zero RTT (all journeys run at RTT 0)",
    )
    run_parser.add_argument("--runs", type=int, default=5)
    run_parser.add_argument("--max-load", type=float, default=30.0)
    run_parser.add_argument("--load-wait-min", type=float, default=30.0)
    run_parser.add_argument(
        "--profile", action="store_true", help="one extra cProfile run per cell"
    )
    run_parser.add_argument(
        "--counts",
        action="store_true",
        help="count subprocess spawns and imported modules of each apm child",
    )
    run_parser.add_argument(
        "--count-fs",
        action="store_true",
        help="with --counts, also count files written, lockfile reads, realpath/stat "
        "calls and hashing (adds CPU: counts only, never compare its timings)",
    )
    run_parser.add_argument(
        "--preset",
        choices=("scoreboard",),
        default=None,
        help="scoreboard: tree-10, rtt 30, 5 runs, the North Star modes + boot, counts on",
    )
    run_parser.add_argument(
        "--scoreboard", default=None, help="append the North Star row to this markdown file"
    )
    for p in (run_parser, sub.add_parser("verify-real", help="check REAL_PACKAGES resolve")):
        p.add_argument("--apm", default=None)
        p.add_argument("--work", default=str(REPO_ROOT / "build" / "perf-work"))
    run_parser.add_argument("--out", default=str(REPO_ROOT / "build" / "perf"))
    board = sub.add_parser("scoreboard", help="append a North Star row from a journeys JSON")
    board.add_argument("file", help="scoreboard markdown file to append to")
    board.add_argument(
        "json", nargs="+", help="journeys-<sha>.json files of one CLI SHA (chunks are merged)"
    )
    args = parser.parse_args(argv)
    if args.command == "scoreboard":
        reports = [json.loads(Path(j).read_text(encoding="utf-8")) for j in args.json]
        report = reports[0]
        for extra in reports[1:]:
            report["results"] += extra["results"]
        append_scoreboard(report, Path(args.file))
        return 0
    if args.command == "verify-real":
        return verify_real(args)
    if args.preset == "scoreboard":
        # The preset replaces defaults only; explicit flags (e.g. --runs 1) still win.
        given = _explicit_options(argv if argv is not None else sys.argv[1:])
        for key, value in SCOREBOARD_PRESET.items():
            if key not in given:
                setattr(args, key, value)
        if "rtt_journeys" not in given:
            args.rtt_journeys = args.journeys.split(",")
        args.counts = True
    report = run(args)
    if args.scoreboard:
        append_scoreboard(report, Path(args.scoreboard))
    return 1 if any(r["error"] for r in report["results"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
