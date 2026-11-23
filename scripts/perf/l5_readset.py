"""Read-set audit of the verified-state record (#4977): every path a recording no-op touches.

Usage: l5_readset.py NEW_APM WORK FIXTURE [FIXTURE ...]

Per fixture: a fresh project, a full install, then the first no-op (the run that
writes the record) under ``strace -f -e trace=%file,%process,chdir`` (Linux).
Every path that run or any child (git) opens, stats, probes or lists is classed:

  keyed     a file the record's MAC tags name (git:, surface:, project:, user:,
            home:, policy:, governed:, lifecycle:, lock, notice), or a path
            inside a tree the record walks (listing and bytes) or proves
            (deployed files and package copies against the MAC'd lock)
  allowed   on the justified allowlist below
  self      written by this run first (its own files), or the record machinery
  GAP       anything else: a verdict input the record does not key

Exit 1 when any fixture has a GAP. The allowlist is printed with its reasons.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tests.benchmarks import journeys  # noqa: E402
from tests.fixtures.perf import offline  # noqa: E402

# The served bench policy blocks tree-10's transitive MCP server, so its no-op is
# never verified; trusting transitive servers keeps discovery and enforcement on.
offline.POLICY_DOCUMENT["mcp"]["trust_transitive"] = True

new_apm, work = sys.argv[1], Path(sys.argv[2]).resolve()
fixture_names = sys.argv[3:]

ALLOW = [
    ("code", "interpreter, stdlib, the venv and its packages: the program, keyed by build"),
    ("kernel", "/dev, /proc, /sys: process plumbing, not state"),
    ("git install", "the git binary, exec path and templates: keyed by git-binary identity"),
    ("system libs", "shared libraries, locale, zoneinfo, nsswitch/passwd: runtime, not verdict"),
    ("tls store", "the OS CA store: a no-op makes no TLS connection; env-selected bundles are keyed"),
    ("path lookup", "executable lookup along PATH (PATH is keyed); reported by name below"),
    ("record", "the record, its key and the lifecycle lock: the record machinery itself"),
    ("tmp", "the run's own temp files"),
    ("path resolution", "ancestors of the project, HOME, APM_HOME, the cache and the program"),
    ("deploy cache", "deploy_v1/records: a missing record only declines the fast path to the"
     " full pipeline, whose output equals it (engine-parity scenarios)"),
]

_LINE = re.compile(r"^(\d+)\s+(\w+)\((.*)\)\s+=\s+(-?\d+|\?)(.*)$")
_STR = re.compile(r'"((?:[^"\\]|\\.)*)"')
_DIRFD = re.compile(r"^(\d+)<([^>]*)>, ")
_WRITE_FLAGS = ("O_WRONLY", "O_RDWR", "O_CREAT", "O_TRUNC")
_WRITE_CALLS = {"mkdir", "mkdirat", "unlink", "unlinkat", "rmdir", "rename", "renameat",
                "renameat2", "symlink", "symlinkat", "link", "linkat", "utimensat",
                "chmod", "fchmodat", "truncate"}


def _unescape(text: str) -> str:
    return text.encode("latin-1", "backslashreplace").decode("unicode_escape", "replace")


def parse(log: Path, start_cwd: str) -> tuple[dict[str, set[str]], set[str], list[str]]:
    """{path: {syscalls}} read or probed, paths written, executed argv0s."""
    cwd: dict[str, str] = {}
    reads: dict[str, set[str]] = defaultdict(set)
    writes: set[str] = set()
    execs: list[str] = []
    for raw in log.read_text(encoding="utf-8", errors="replace").splitlines():
        m = _LINE.match(raw)
        if not m:
            continue
        pid, call, args, ret = m.group(1), m.group(2), m.group(3), m.group(4)
        here = cwd.setdefault(pid, cwd.get("root", start_cwd))
        if call in ("clone", "clone3", "vfork", "fork") and ret.isdigit():
            cwd[ret] = here
            continue
        if call == "chdir" and ret == "0":
            s = _STR.search(args)
            if s:
                cwd[pid] = os.path.normpath(os.path.join(here, _unescape(s.group(1))))
            continue
        s = _STR.search(args)
        if not s:
            continue
        name = _unescape(s.group(1))
        base = here
        d = _DIRFD.match(args)
        if d:
            base = d.group(2)
        path = os.path.normpath(os.path.join(base, name)) if name else base
        if call == "execve":
            execs.append(path)
        is_write = call in _WRITE_CALLS or (
            call in ("open", "openat", "openat2", "creat") and any(f in args for f in _WRITE_FLAGS)
        )
        if is_write:
            if ret != "-1" and not ret.startswith("-"):
                writes.add(path)
            continue
        reads[path].add(call)
    return reads, writes, execs


def covered_from_record(record: dict, root: Path, env: dict) -> tuple[set[str], list[str]]:
    """Files the MAC keys, and the trees the record walks or proves."""
    trees_listed: set[str] = set()
    home = Path(env["HOME"])
    apm_home = Path(env["APM_HOME"])
    cache = Path(env.get("APM_CACHE_DIR") or "")
    files: set[str] = set()
    trees: list[str] = []
    for tag, _h in record.get("tags", []):
        kind, _, rest = tag.partition(":")
        if kind in ("git", "surface") and rest.startswith("/"):
            files.add(os.path.normpath(rest))
        elif kind == "project":
            files.add(str(root / rest))
        elif kind == "user":
            files.add(str(home / rest))
        elif kind == "home":
            files.add(str(apm_home / rest))
        elif kind == "governed":
            files.add(str(root / rest))
        elif kind == "listing":
            trees.append(str(root / rest))
        elif kind == "target":
            trees.append(str(root / rest))  # existence keyed; contents keyed by listing
        elif kind in ("parent", "git-head") and rest.startswith("/"):
            files.add(os.path.normpath(rest if kind == "parent" else os.path.join(rest, "HEAD")))
        elif kind == "modules":
            trees_listed.add(str(root / rest))
            LISTED.add(str(root / rest))
        elif kind == "listing":
            LISTED.add(str(root / rest))
    files.add(str(root / "apm.lock.yaml"))
    if record.get("notice"):
        files.add(record["notice"])
    trees.append(str(apm_home))  # walked, minus the record and cache below
    trees.append("/etc/apm/policy.d")  # the lifecycle policy-script tree (listing and bytes)
    files |= trees_listed
    if str(cache):
        trees.append(str(cache / "policy_v1"))
    return files, trees


def lock_proven(root: Path) -> tuple[set[str], list[str]]:
    import yaml

    lock = yaml.safe_load((root / "apm.lock.yaml").read_text(encoding="utf-8")) or {}
    files, trees = set(), []
    for row in lock.get("dependencies") or []:
        for rel in row.get("deployed_file_hashes") or {}:
            files.add(str(root / rel))
        repo = str(row.get("repo_url", ""))
        if repo:
            trees.append(str(root / "apm_modules" / Path(*repo.split("/"))))
    return files, trees


def under(path: str, tree: str) -> bool:
    return path == tree or path.startswith(tree.rstrip("/") + "/")


def classify(path, calls, *, files, trees, writes, env, venv, prefixes, git_dirs, path_dirs):
    apm_home = env["APM_HOME"]
    if any(under(path, w) for w in (apm_home + "/verdicts",)) or path.endswith(
        ("/.apm-lifecycle.lock", "/.apm-lifecycle.lock.lock")
    ) or os.path.basename(path) in ("verdict-key", "verdict.key"):
        return "record"
    if path in writes:
        return "self"
    if path in files or os.path.dirname(path) in env["_LISTED"]:
        return "keyed"  # a keyed file, or an entry of a keyed listing (its name and kind)
    if path == os.path.join(env["_ROOT"], ".apm"):
        return "keyed"  # its existence makes the run ineligible (never recorded, never a hit)
    if os.path.basename(path) == ".git" and any(under(env["_ROOT"], os.path.dirname(path)) for _ in [0]):
        return "keyed"  # git discovery: verdict_git repeats the walk (a new .git changes the MAC)
    if any(under(env["_ROOT"], path) or under(k, path) for k in env["_ANCHORS"]):
        return "path resolution"
    if path in env["_FILE_ANCESTORS"]:
        return "keyed"
    if "/deploy_v1" in path and under(path, env.get("APM_CACHE_DIR", "/nonexistent")):
        return "deploy cache"
    if under(path, apm_home) and not under(path, apm_home + "/cache"):
        return "keyed"
    if any(under(path, t) for t in trees if t != apm_home):
        return "keyed"
    if path.startswith(("/dev", "/proc", "/sys")) or path in ("/", ""):
        return "kernel"
    if any(under(path, p) for p in prefixes) or path.endswith((".py", ".pyc", ".so", ".pth", ".zip")):
        return "code"
    if any(under(path, g) for g in git_dirs):
        return "git install"
    if path.startswith(("/etc/ssl", "/usr/lib/ssl", "/usr/share/ca-certificates", "/etc/pki")):
        return "tls store"
    if path.startswith(("/lib", "/usr/lib", "/usr/lib64", "/lib64", "/etc/ld.so", "/usr/share/locale",
                        "/usr/lib/locale", "/usr/share/zoneinfo", "/etc/localtime", "/etc/nsswitch.conf",
                        "/etc/passwd", "/etc/group", "/usr/share/i18n", "/etc/host.conf",
                        "/etc/resolv.conf", "/etc/hosts", "/etc/gai.conf", "/usr/local/lib")):
        return "system libs"
    if os.path.dirname(path) in path_dirs or any(under(d, path) for d in path_dirs):
        return "path lookup"
    tmp = os.environ.get("RUNNER_TEMP", "/tmp")
    if any(under(path, w) for w in writes if os.path.isdir(w) is False) or path.startswith("/tmp/"):
        return "tmp"
    return "GAP"


def interpreter_prefixes(apm: str) -> list[str]:
    venv = Path(apm).resolve().parents[1]
    py = venv / "bin" / "python"
    out = subprocess.run(
        [str(py), "-c", "import sys,sysconfig;print(sys.base_prefix);print(sys.prefix);"
         "print(sysconfig.get_paths()['stdlib'])"],
        capture_output=True, text=True, check=True,
    ).stdout.split()
    return sorted({str(venv), *out, str(Path(out[0]).resolve())})


def git_dirs(env: dict | None = None) -> list[str]:
    git = shutil.which("git") or "/usr/bin/git"
    exec_path = subprocess.run([git, "--exec-path"], capture_output=True, text=True).stdout.strip()
    extra = [env["GIT_EXEC_PATH"]] if env and env.get("GIT_EXEC_PATH") else []
    return [git, exec_path, "/usr/share/git-core", "/usr/share/gitweb", *extra]


LISTED: set[str] = set()
gaps_total = 0
prefixes = interpreter_prefixes(new_apm)
gdirs = git_dirs()
print("ALLOWLIST:")
for name, why in ALLOW:
    print(f"  {name}: {why}")
for name in fixture_names:
    fixture = journeys.prepare_fixture(name, work)
    with journeys.open_world(fixture, work / f"world-{name}") as world:
        world.rtt_ms = 30
        ctx = journeys.Context(fixture, world, [new_apm], work / "runs" / name)
        ctx.work.mkdir(parents=True, exist_ok=True)
        env = dict(world.env)
        home = Path(env["APM_HOME"])
        ctx.fresh_project()
        for root in ctx.roots():
            ctx.checked(["install"], root)
        shutil.rmtree(home / "verdicts", ignore_errors=True)
        LISTED.clear()
        log = work / f"strace-{name}.log"
        run_env = dict(env)
        run_env["APM_INSTALL_FAST_PATH"] = "verdict"
        proc = subprocess.run(
            ["strace", "-f", "-y", "-s", "4096", "-qq", "-e",
             "trace=%file,%process,chdir", "-o", str(log), new_apm, "install"],
            cwd=ctx.project, env=run_env, capture_output=True, text=True, timeout=600,
        )
        if proc.returncode != 0 or "verdict record: recorded" not in proc.stderr:
            print(f"== {name}: no record written (exit {proc.returncode})")
            print(proc.stderr[-2000:])
            gaps_total += 1
            continue
        record = json.loads(next((home / "verdicts").rglob("*.json")).read_text(encoding="utf-8"))
        root = ctx.project.resolve()
        files, trees = covered_from_record(record, root, env)
        lfiles, ltrees = lock_proven(root)
        files |= lfiles
        trees += ltrees
        reads, writes, execs = parse(log, str(ctx.project))
        path_dirs = {os.path.normpath(p) for p in env.get("PATH", "").split(os.pathsep) if p}
        env["_ROOT"] = str(root)
        anchors = [env["HOME"], env["APM_HOME"], env.get("APM_CACHE_DIR", ""), *prefixes,
                   str(Path(new_apm).resolve().parent), os.path.dirname(new_apm)]
        env["_ANCHORS"] = [a for a in anchors if a]
        ancestors = set()
        stops = {str(root), env["HOME"], "/"}
        for f in files:
            if os.path.lexists(f):
                parent = os.path.dirname(f)
                while parent not in stops and parent not in ancestors and parent != "/":
                    ancestors.add(parent)
                    parent = os.path.dirname(parent)
        env["_FILE_ANCESTORS"] = ancestors
        env["_LISTED"] = set(LISTED)  # only real listings (modules:, listing:), never parent kinds
        gdirs_here = gdirs + git_dirs(env)[4:]
        classes: Counter[str] = Counter()
        gaps: list[str] = []
        lookups: Counter[str] = Counter()
        for path, calls in sorted(reads.items()):
            c = classify(path, calls, files=files, trees=trees, writes=writes, env=env,
                         venv=None, prefixes=prefixes, git_dirs=gdirs_here, path_dirs=path_dirs)
            classes[c] += 1
            if c == "GAP":
                gaps.append(f"{path}  [{','.join(sorted(calls))}]")
            if c == "path lookup":
                lookups[os.path.basename(path)] += 1
        execs_named = Counter(os.path.basename(e) for e in execs)
        print(f"== {name}: {sum(classes.values())} paths; " + ", ".join(f"{k}={v}" for k, v in sorted(classes.items())))
        print(f"   executed: {dict(execs_named)}")
        print(f"   PATH lookups by name: {dict(lookups)}")
        for gap in gaps:
            print(f"   GAP {gap.replace(str(root), 'ROOT').replace(env['HOME'], 'HOME')}")
        gaps_total += len(gaps)
print(f"TOTAL GAPS: {gaps_total}")
sys.exit(1 if gaps_total else 0)
