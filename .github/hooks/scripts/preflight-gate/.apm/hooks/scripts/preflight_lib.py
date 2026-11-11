"""Shared helpers for the preflight runner and the preToolUse gate.

Stdlib only, Python >= 3.10, printable ASCII, no network. This module owns
HOW things are computed (git plumbing, tree hashes, receipt I/O, bundle
identity, content guards). WHAT runs lives in preflight_policy.py.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import subprocess
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

SCHEMA = 1
PREFLIGHT_DIRNAME = "apm-preflight"
RECEIPT_NAME = "receipt.json"
BYPASS_LOG_NAME = "bypass.log"
BYPASS_ENV = "APM_PREFLIGHT_BYPASS"
BYPASS_VALUE = "accept-unverified"
MAX_AGE_S = 24 * 3600
MAX_FUTURE_SKEW_S = 300

# Repo-relative locations produced by `apm install` (copilot target).
DEPLOYED_SCRIPTS_REL = ".github/hooks/scripts/preflight-gate/.apm/hooks/scripts"
DEPLOYED_HOOK_JSON_REL = ".github/hooks/preflight-gate-preflight-gate.json"
SOURCE_SCRIPTS_REL = "packages/preflight-gate/.apm/hooks/scripts"
RUNNER_NAME = "preflight.py"

_BUNDLE_SKIP_DIRS = {"__pycache__"}
_BUNDLE_SKIP_SUFFIXES = (".pyc", ".pyo")


# ---------------------------------------------------------------------------
# git plumbing
# ---------------------------------------------------------------------------


def git(
    args: list[str],
    cwd: str | Path,
    env: dict[str, str] | None = None,
    timeout: float | None = 60,
) -> subprocess.CompletedProcess[str]:
    """Run git with captured text output; never raises on non-zero exit."""
    run_env = None
    if env is not None:
        run_env = dict(os.environ)
        run_env.update(env)
    return subprocess.run(  # noqa: S603 - fixed git argv, no shell
        ["git", *args],  # noqa: S607 - git resolved from PATH by design
        cwd=str(cwd),
        env=run_env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=timeout,
    )


def git_out(args: list[str], cwd: str | Path, env: dict[str, str] | None = None) -> str | None:
    """Return stripped stdout of a successful git call, else None."""
    try:
        proc = git(args, cwd, env=env)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def repo_info(cwd: str | Path) -> dict[str, str] | None:
    """Return absolute toplevel, git_dir and common_dir for *cwd*, or None."""
    out = git_out(
        [
            "rev-parse",
            "--path-format=absolute",
            "--show-toplevel",
            "--git-dir",
            "--git-common-dir",
        ],
        cwd,
    )
    if out is None:
        return None
    parts = out.splitlines()
    if len(parts) != 3:
        return None
    return {
        "toplevel": os.path.realpath(parts[0]),
        "git_dir": os.path.realpath(parts[1]),
        "common_dir": os.path.realpath(parts[2]),
    }


def rev_commit(ref: str, cwd: str | Path) -> str | None:
    """Resolve *ref* to a commit SHA, or None."""
    return git_out(["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], cwd)


def head_and_tree(cwd: str | Path) -> tuple[str | None, str | None]:
    """Return (HEAD sha, HEAD^{tree} sha)."""
    out = git_out(["rev-parse", "HEAD", "HEAD^{tree}"], cwd)
    if not out:
        return None, None
    parts = out.splitlines()
    if len(parts) != 2:
        return None, None
    return parts[0], parts[1]


def status_porcelain(cwd: str | Path) -> str | None:
    """Return `git status --porcelain` output (untracked included)."""
    try:
        proc = git(["status", "--porcelain", "--untracked-files=all"], cwd)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def is_clean(cwd: str | Path) -> bool:
    """True iff no staged, unstaged or untracked (non-ignored) changes."""
    out = status_porcelain(cwd)
    return out is not None and out.strip() == ""


def preflight_dir(git_dir: str | Path) -> Path:
    """Per-worktree state directory (inside the worktree's own git dir)."""
    return Path(git_dir) / PREFLIGHT_DIRNAME


def receipt_path(git_dir: str | Path) -> Path:
    return preflight_dir(git_dir) / RECEIPT_NAME


def worktree_tree(root: str | Path, git_dir: str | Path) -> str:
    """Hash the full working tree (tracked + untracked, honoring ignores).

    Copies the real index to a scratch file inside the per-worktree
    preflight dir, stages everything into the copy and writes a tree. The
    real index is never touched. Equals HEAD^{tree} for a clean worktree.
    """
    pdir = preflight_dir(git_dir)
    pdir.mkdir(parents=True, exist_ok=True)
    scratch = pdir / f"index.{os.getpid()}.tmp"
    real_index = Path(git_dir) / "index"
    try:
        if real_index.is_file():
            shutil.copyfile(real_index, scratch)
        env = {"GIT_INDEX_FILE": str(scratch)}
        add = git(["add", "-A", "--", "."], root, env=env, timeout=300)
        if add.returncode != 0:
            raise RuntimeError(f"git add -A (scratch index) failed: {add.stderr.strip()}")
        tree = git(["write-tree"], root, env=env, timeout=300)
        if tree.returncode != 0:
            raise RuntimeError(f"git write-tree failed: {tree.stderr.strip()}")
        return tree.stdout.strip()
    finally:
        with contextlib.suppress(FileNotFoundError):
            scratch.unlink()


def untracked_files(root: str | Path) -> list[str]:
    out = git_out(["ls-files", "-o", "--exclude-standard"], root)
    return [line for line in (out or "").splitlines() if line]


def changed_files(root: str | Path, merge_base: str) -> list[str]:
    """Paths changed in the working tree vs *merge_base*, plus untracked."""
    names: set[str] = set(untracked_files(root))
    diff = git_out(["diff", "--name-only", "--no-renames", merge_base], root)
    if diff:
        names.update(line for line in diff.splitlines() if line)
    return sorted(names)


def remotes(root: str | Path) -> list[str]:
    out = git_out(["remote"], root)
    return [line for line in (out or "").splitlines() if line]


def base_branch_name(base_ref: str, remote_names: list[str]) -> str:
    """`origin/main` -> `main`; `refs/remotes/origin/x` -> `x`; else as-is."""
    ref = base_ref
    if ref.startswith("refs/heads/"):
        return ref[len("refs/heads/") :]
    if ref.startswith("refs/remotes/"):
        ref = ref[len("refs/remotes/") :]
        return ref.split("/", 1)[1] if "/" in ref else ref
    for remote in remote_names:
        if ref.startswith(remote + "/"):
            return ref[len(remote) + 1 :]
    return ref


# ---------------------------------------------------------------------------
# receipt I/O and time
# ---------------------------------------------------------------------------


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value: str) -> datetime:
    """Parse the `YYYY-MM-DDTHH:MM:SSZ` form written by now_iso()."""
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def write_json_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="ascii")
    os.replace(tmp, path)


def read_receipt(git_dir: str | Path) -> tuple[dict | None, str | None]:
    """Return (receipt, error). (None, None) when no receipt exists."""
    path = receipt_path(git_dir)
    if not path.is_file():
        return None, None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, f"unreadable receipt {path}: {exc}"
    if not isinstance(data, dict):
        return None, f"malformed receipt {path}"
    return data, None


# ---------------------------------------------------------------------------
# bundle identity (R6)
# ---------------------------------------------------------------------------


def bundle_files(scripts_dir: str | Path) -> list[tuple[str, Path]]:
    """Sorted (posix relpath, path) of bundle files, ignoring bytecode."""
    base = Path(scripts_dir)
    found: list[tuple[str, Path]] = []
    if not base.is_dir():
        return found
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(d for d in dirnames if d not in _BUNDLE_SKIP_DIRS)
        for name in filenames:
            if name.endswith(_BUNDLE_SKIP_SUFFIXES):
                continue
            path = Path(dirpath) / name
            found.append((path.relative_to(base).as_posix(), path))
    return sorted(found)


def bundle_hash(scripts_dir: str | Path, hook_json: str | Path | None) -> str:
    """sha256 over sorted (relpath, bytes) of the scripts dir + hook JSON."""
    digest = hashlib.sha256()

    def feed(label: str, payload: bytes) -> None:
        digest.update(label.encode("utf-8") + b"\0")
        digest.update(str(len(payload)).encode("ascii") + b"\0")
        digest.update(payload)

    for rel, path in bundle_files(scripts_dir):
        feed("scripts/" + rel, path.read_bytes())
    if hook_json is not None and Path(hook_json).is_file():
        feed("hook.json", Path(hook_json).read_bytes())
    else:
        feed("hook.json", b"<missing>")
    return digest.hexdigest()


def deployed_bundle_hash(root: str | Path) -> str | None:
    """Bundle hash of the deployed copy under *root*, None if not deployed."""
    scripts = Path(root) / DEPLOYED_SCRIPTS_REL
    if not scripts.is_dir():
        return None
    return bundle_hash(scripts, Path(root) / DEPLOYED_HOOK_JSON_REL)


def bundle_differences(source_dir: str | Path, deployed_dir: str | Path) -> list[str]:
    """Human-readable differences between source and deployed scripts."""
    src = dict(bundle_files(source_dir))
    dep = dict(bundle_files(deployed_dir))
    problems: list[str] = []
    for rel in sorted(set(src) | set(dep)):
        if rel not in dep:
            problems.append(f"missing in deployed copy: {rel}")
        elif rel not in src:
            problems.append(f"extra in deployed copy: {rel}")
        elif src[rel].read_bytes() != dep[rel].read_bytes():
            problems.append(f"content differs: {rel}")
    return problems


# ---------------------------------------------------------------------------
# CI parity fingerprints (R5)
# ---------------------------------------------------------------------------

_JOB_KEY = re.compile(r"^  ([A-Za-z0-9_-]+):\s*$")
_TRAILING_NOISE = re.compile(r"^\s*(#.*)?$")


def ci_job_block(text: str, job: str) -> str | None:
    """Extract the exact text block of a GitHub Actions job.

    Deterministic definition: normalize CRLF to LF; find the first line that
    is exactly `jobs:`; the block starts at the line `  <job>:` (two-space
    indent) and ends before the next two-space job key line, the next
    top-level (column 0, non-comment) line, or EOF. Trailing blank and
    comment-only lines are dropped (they belong to the next job's preamble).
    """
    lines = text.replace("\r\n", "\n").split("\n")
    try:
        start_jobs = lines.index("jobs:")
    except ValueError:
        return None
    start = None
    for idx in range(start_jobs + 1, len(lines)):
        match = _JOB_KEY.match(lines[idx])
        if match and match.group(1) == job:
            start = idx
            break
    if start is None:
        return None
    end = len(lines)
    for idx in range(start + 1, len(lines)):
        line = lines[idx]
        if _JOB_KEY.match(line) or (line[:1] not in ("", " ", "#")):
            end = idx
            break
    block = lines[start:end]
    while block and _TRAILING_NOISE.match(block[-1]):
        block.pop()
    return "\n".join(block) + "\n"


def fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def ci_fingerprints(root: str | Path, mirrored: dict[str, tuple[str, str]]) -> dict[str, str]:
    """Compute current fingerprints for {key: (workflow relpath, job)}."""
    result: dict[str, str] = {}
    for key, (rel, job) in mirrored.items():
        path = Path(root) / rel
        if not path.is_file():
            result[key] = "<missing workflow>"
            continue
        block = ci_job_block(path.read_text(encoding="utf-8"), job)
        result[key] = fingerprint(block) if block is not None else "<missing job>"
    return result


# ---------------------------------------------------------------------------
# content guards
# ---------------------------------------------------------------------------


def _is_printable_ascii(text: str) -> bool:
    return all(ch == "\t" or " " <= ch <= "~" for ch in text)


def added_lines(root: str | Path, merge_base: str, paths: list[str]) -> list[tuple[str, str]]:
    """(path, line) for lines added vs merge_base, incl. untracked files."""
    result: list[tuple[str, str]] = []
    proc = git(
        ["diff", "-U0", "--no-color", "--no-ext-diff", "--no-renames", merge_base, "--", *paths],
        root,
        timeout=300,
    )
    current = None
    for raw in proc.stdout.splitlines():
        if raw.startswith("+++ "):
            target = raw[4:]
            current = target[2:] if target.startswith("b/") else None
            continue
        if raw.startswith("+") and current is not None:
            result.append((current, raw[1:]))
    prefixes = tuple(p.rstrip("/") for p in paths)
    for rel in untracked_files(root):
        if not any(rel == p or rel.startswith(p + "/") for p in prefixes):
            continue
        try:
            data = (Path(root) / rel).read_bytes()
        except OSError:
            continue
        if b"\0" in data:
            continue
        for line in data.decode("utf-8", errors="replace").splitlines():
            result.append((rel, line))
    return result


def ascii_guard(root: str | Path, merge_base: str, paths: list[str]) -> tuple[bool, list[str]]:
    """Fail on added lines containing characters outside printable ASCII."""
    problems = []
    for rel, line in added_lines(root, merge_base, paths):
        if not _is_printable_ascii(line):
            bad = sorted({f"U+{ord(ch):04X}" for ch in line if not _is_printable_ascii(ch)})
            problems.append(f"{rel}: non-ASCII {', '.join(bad)}: {line.strip()[:80]!a}")
    return (not problems), problems


_URL = re.compile(r"https?://[^\s\"'<>]+")
_REVISION = re.compile(r"^revision\s*=\s*(\d+)\s*$", re.MULTILINE)


def uvlock_guard(
    root: str | Path, merge_base: str, allowed_hosts: tuple[str, ...]
) -> tuple[bool, list[str]]:
    """uv.lock: added URLs must be on allowed hosts; revision must not drop."""
    problems = []
    for _rel, line in added_lines(root, merge_base, ["uv.lock"]):
        for url in _URL.findall(line):
            host = urlparse(url).hostname or ""
            if host not in allowed_hosts:
                problems.append(f"uv.lock: non-allowlisted registry host {host!r}: {url}")
    old = git_out(["show", f"{merge_base}:uv.lock"], root) or ""
    new_path = Path(root) / "uv.lock"
    new = new_path.read_text(encoding="utf-8") if new_path.is_file() else ""
    old_rev = _REVISION.search(old)
    new_rev = _REVISION.search(new)
    if old_rev and new_rev and int(new_rev.group(1)) < int(old_rev.group(1)):
        problems.append(
            f"uv.lock: revision decreased {old_rev.group(1)} -> {new_rev.group(1)} "
            "(lock written by an older uv)"
        )
    return (not problems), problems


def host_has_pcre_grep() -> bool:
    """True when the default `grep` supports -P (GNU grep)."""
    try:
        proc = subprocess.run(
            ["grep", "-P", "x"],  # noqa: S607
            input="x\n",
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


# ---------------------------------------------------------------------------
# CI-faithful environment (D3)
# ---------------------------------------------------------------------------

ENV_PASSTHROUGH = (
    "PATH",
    "USER",
    "LOGNAME",
    "SHELL",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TZ",
    "TERM",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE",
    "NODE_EXTRA_CA_CERTS",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
    # Tool location, not user config: bundled gits (e.g. the Copilot CLI's)
    # cannot find their http helpers without it.
    "GIT_EXEC_PATH",
)
ENV_PASSTHROUGH_PREFIXES = ("UV_", "PREFLIGHT_")


def _real_dirs(env: dict[str, str]) -> dict[str, Path]:
    home = Path(env.get("HOME") or os.path.expanduser("~"))
    config = Path(env.get("XDG_CONFIG_HOME") or home / ".config")
    cache = Path(env.get("XDG_CACHE_HOME") or home / ".cache")
    data = Path(env.get("XDG_DATA_HOME") or home / ".local" / "share")
    return {"home": home, "config": config, "cache": cache, "data": data}


def short_tmpdir(env: dict[str, str]) -> Path:
    """A short, stable temp root: deep macOS TMPDIRs make CLI output wrap."""
    path = _real_dirs(env)["cache"] / "apm-pf"
    path.mkdir(parents=True, exist_ok=True)
    return path


def ci_like_env(env: dict[str, str], home: Path, tmpdir: Path) -> dict[str, str]:
    """Environment for command checks that mirrors a fresh CI runner.

    Only an allowlist of host variables passes through, so user-specific
    APM / Claude / Copilot / token variables never leak into test lanes.
    HOME and XDG dirs are isolated; uv keeps the real cache, config and
    managed pythons so it never needs the network.
    """
    real = _real_dirs(env)
    out = {k: v for k, v in env.items() if k in ENV_PASSTHROUGH}
    out.update({k: v for k, v in env.items() if k.startswith(ENV_PASSTHROUGH_PREFIXES)})
    out.update(
        {
            # GitHub-hosted runners run as `runner`; pytest embeds the user
            # name in tmp paths, and some CLI-output assertions in the suite
            # are sensitive to that path length.
            "USER": "runner",
            "LOGNAME": "runner",
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "XDG_CACHE_HOME": str(home / ".cache"),
            "XDG_DATA_HOME": str(home / ".local" / "share"),
            "XDG_STATE_HOME": str(home / ".local" / "state"),
            "TMPDIR": str(tmpdir),
        }
    )
    out.setdefault("UV_CACHE_DIR", str(real["cache"] / "uv"))
    py_dir = real["data"] / "uv" / "python"
    if py_dir.is_dir():
        out.setdefault("UV_PYTHON_INSTALL_DIR", str(py_dir))
    uv_config = real["config"] / "uv" / "uv.toml"
    if uv_config.is_file():
        out.setdefault("UV_CONFIG_FILE", str(uv_config))
    for sub in ("HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME"):
        Path(out[sub]).mkdir(parents=True, exist_ok=True)
    # The apm CLI checks GitHub for a newer release at most once a day and
    # prints an update banner before command output; mark it as just done
    # for commands that use this HOME (tests that override HOME are handled
    # per check in the policy).
    stamp = home / ".cache" / "apm" / "last_version_check"
    stamp.parent.mkdir(parents=True, exist_ok=True)
    stamp.touch()
    return out


def force_rmtree(path: str | Path) -> None:
    """Remove a tree even when tests left unreadable/unwritable dirs in it."""
    base = Path(path)
    if not base.exists():
        return
    with contextlib.suppress(OSError):
        os.chmod(base, 0o700)
    # topdown: children are made traversable before os.walk descends.
    for dirpath, dirnames, _files in os.walk(base, topdown=True):
        for name in dirnames:
            with contextlib.suppress(OSError):
                os.chmod(os.path.join(dirpath, name), 0o700)
    shutil.rmtree(base)


TOLERATE_ENV = "APM_PREFLIGHT_LOCK_TOLERATE"
TOLERATE_GIT_KEY = "apm.preflight.lockTolerate"
UvRunner = Callable[[list[str], Path, dict[str, str]], tuple[int, str]]
_DRIFT_LINE = re.compile(r"^ ([-+~]) ([A-Za-z0-9_.\-]+)==(\S+)")
_REQ_LINE = re.compile(r"^([A-Za-z0-9_.\-]+)==([^\s;]+)(.*)$")


def normalize_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name.strip()).lower()


def tolerated_names(env: dict[str, str], root: str | Path | None = None) -> set[str]:
    """Machine-local packages allowed to differ from uv.lock (mirror gaps).

    APM_PREFLIGHT_LOCK_TOLERATE wins when set; otherwise git config
    `apm.preflight.lockTolerate` (set it --global; never commit it).
    """
    if TOLERATE_ENV in env:
        raw = env[TOLERATE_ENV]
    elif root is not None:
        raw = git_out(["config", "--get", TOLERATE_GIT_KEY], root) or ""
    else:
        raw = ""
    return {normalize_name(n) for n in raw.split(",") if n.strip()}


def uv_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k not in ("UV_NO_SYNC", "UV_FROZEN")}


def run_uv(args: list[str], cwd: Path, env: dict[str, str]) -> tuple[int, str]:
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv
            ["uv", *args],  # noqa: S607
            cwd=str(cwd),
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=1800,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return 127, f"cannot run uv: {exc}"
    return proc.returncode, proc.stdout + proc.stderr


def parse_drift(output: str) -> tuple[bool, dict[str, dict[str, str | None]]]:
    """Parse `uv sync --dry-run`: (no changes?, {name: {locked, installed}}).

    `~ <project> (from file://...)` re-install lines of the editable project
    are not drift.
    """
    clean = "Would make no changes" in output
    drift: dict[str, dict[str, str | None]] = {}
    for line in output.splitlines():
        match = _DRIFT_LINE.match(line)
        if not match or (match.group(1) == "~" and "(from file:" in line):
            continue
        sign, name, version = match.groups()
        entry = drift.setdefault(normalize_name(name), {"locked": None, "installed": None})
        entry["installed" if sign == "-" else "locked"] = version
    return clean, drift


def check_lock(
    root: str | Path, tolerate: set[str], runner: UvRunner = run_uv
) -> dict[str, object]:
    """Compare the uv project env with uv.lock (CI: `uv sync --frozen --extra dev`)."""
    args = ["sync", "--frozen", "--extra", "dev", "--dry-run", "--offline"]
    code, output = runner(args, Path(root), uv_env())
    clean, drift = parse_drift(output)
    records = [{"name": n, **v} for n, v in sorted(drift.items())]
    tolerated = [r for r in records if r["name"] in tolerate]
    untolerated = [r for r in records if r["name"] not in tolerate]
    plan_seen = clean or "Would use project environment" in output
    plan_seen = plan_seen or "Would create project environment" in output
    ok = code == 0 and plan_seen and not untolerated
    return {
        "ok": ok,
        "tolerated": tolerated,
        "untolerated": untolerated,
        "output": output,
        "exit": code,
    }


def project_env(root: str | Path, env: dict[str, str]) -> Path:
    custom = env.get("UV_PROJECT_ENVIRONMENT")
    if custom:
        path = Path(custom)
        return path if path.is_absolute() else Path(root) / path
    return Path(root) / ".venv"


def installed_version(venv: Path, name: str) -> str | None:
    wanted = normalize_name(name)
    for info in venv.glob("lib/python*/site-packages/*.dist-info"):
        stem = info.name[: -len(".dist-info")]
        dist, _, version = stem.rpartition("-")
        if normalize_name(dist) == wanted:
            return version
    return None


def relax_requirements(lines: list[str], tolerate: set[str], venv: Path, keep: bool) -> list[str]:
    """Rewrite tolerated pins: same major.minor (`==X.Y.*`), or (keep=True) the
    currently installed version; untolerated pins are untouched."""
    out = []
    for line in lines:
        match = _REQ_LINE.match(line)
        if not match or normalize_name(match.group(1)) not in tolerate:
            out.append(line)
            continue
        name, version, rest = match.groups()
        if keep:
            current = installed_version(venv, name)
            if current is None:
                continue
            out.append(f"{name}=={current}{rest}")
        else:
            parts = version.split(".")
            out.append(f"{name}=={'.'.join(parts[:2])}.*{rest}" if len(parts) >= 2 else line)
    return out


def _upgrade_tolerated(
    lines: list[str],
    tolerate: set[str],
    work_dir: Path,
    root: Path,
    python: str,
    env: dict[str, str],
    log: Callable[[str], None],
    runner: UvRunner,
) -> None:
    """`uv pip sync` keeps an installed version that satisfies `==X.Y.*`;
    move tolerated packages to the highest same-major.minor release."""
    wanted = [
        line
        for line in lines
        if (match := _REQ_LINE.match(line)) and normalize_name(match.group(1)) in tolerate
    ]
    if not wanted:
        return
    target = work_dir / "lock-requirements.tolerated.txt"
    target.write_text("\n".join(wanted) + "\n", encoding="utf-8")
    args = ["pip", "install", "--python", python, "--no-deps"]
    for name in sorted(tolerate):
        args += ["--upgrade-package", name]
    args += ["-r", str(target)]
    code, output = runner(args, root, env)
    log(f"$ uv {' '.join(args)} -> exit {code}\n{output[-1000:]}")


def provision_env(
    root: str | Path,
    work_dir: Path,
    tolerate: set[str],
    log: Callable[[str], None],
    runner: UvRunner = run_uv,
    python: str | None = None,
) -> bool:
    """Make the uv project env match uv.lock in place (never touches uv.lock).

    1. `uv sync --frozen --extra dev` (the CI command).
    2. Fallback for mirrors/proxies: `uv export` the lock and `uv pip sync` it
       through the configured index; tolerated packages may resolve to the
       highest same-major.minor version, else stay as installed. Then
       re-install the project editable.
    """
    root = Path(root)
    env = uv_env()
    pin = ["--python", python] if python else []
    code, output = runner(["sync", "--frozen", "--extra", "dev", *pin], root, env)
    log(f"$ uv sync --frozen --extra dev {' '.join(pin)} -> exit {code}\n{output[-2000:]}")
    if code == 0:
        return True
    work_dir.mkdir(parents=True, exist_ok=True)
    req = work_dir / "lock-requirements.txt"
    export = ["export", "--frozen", "--extra", "dev", "--no-hashes", "--no-emit-project"]
    export += ["--format", "requirements-txt", "--no-header", "-o", str(req)]
    code, output = runner(export, root, env)
    log(f"$ uv {' '.join(export)} -> exit {code}\n{output[-1000:]}")
    if code != 0 or not req.is_file():
        return False
    venv = project_env(root, env)
    if not (venv / "bin" / "python").exists():
        code, output = runner(["venv", *pin, str(venv)], root, env)
        log(f"$ uv venv {venv} -> exit {code}\n{output[-500:]}")
        if code != 0:
            return False
    penv = dict(env, VIRTUAL_ENV=str(venv))
    python = str(venv / "bin" / "python")
    lines = req.read_text(encoding="utf-8").splitlines()
    attempts = (
        [lines]
        if not tolerate
        else [
            relax_requirements(lines, tolerate, venv, keep=False),
            relax_requirements(lines, tolerate, venv, keep=True),
        ]
    )
    synced = None
    for index, attempt in enumerate(attempts):
        target = work_dir / "lock-requirements.sync.txt"
        target.write_text("\n".join(attempt) + "\n", encoding="utf-8")
        code, output = runner(["pip", "sync", "--python", python, str(target)], root, penv)
        log(f"$ uv pip sync {target} -> exit {code}\n{output[-2000:]}")
        if code == 0:
            synced = index
            break
    if synced is None:
        return False
    if synced == 0 and tolerate:
        _upgrade_tolerated(attempts[0], tolerate, work_dir, root, python, penv, log, runner)
    code, output = runner(
        ["pip", "install", "--python", python, "--no-deps", "-e", "."], root, penv
    )
    log(f"$ uv pip install --no-deps -e . -> exit {code}\n{output[-1000:]}")
    return code == 0


def snapshot_outputs(root: str | Path, paths: tuple[str, ...]) -> dict[str, bytes | None]:
    snap: dict[str, bytes | None] = {}
    for rel in paths:
        path = Path(root) / rel
        snap[rel] = path.read_bytes() if path.is_file() else None
    return snap


def restore_outputs(root: str | Path, snap: dict[str, bytes | None]) -> list[str]:
    """Put volatile outputs back to their pre-run state; return changed paths."""
    restored = []
    for rel, before in snap.items():
        path = Path(root) / rel
        now = path.read_bytes() if path.is_file() else None
        if now == before:
            continue
        if before is None:
            path.unlink()
        else:
            path.write_bytes(before)
        restored.append(rel)
    return restored
