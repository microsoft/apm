"""No-op install fast path: replay a verified no-op instead of re-running it.

``apm install`` on an unchanged project runs the whole pipeline and then
reports ``No changes``. This module memoizes that answer the way a build
system memoizes a step: it records every input one full no-op run observed,
and a later run replays the recorded output only when each input is still
bit-identical. Anything unknown or different falls through to the full
pipeline; the fast path never skips a check, it proves the check would see
the same bytes.

Recording (a full run, on every eligible invocation that is not a hit):

- every file opened for reading (``open`` audit event) -> SHA-256 of content;
- every directory listed (``os.listdir`` / ``os.scandir``) -> its entries,
  each with type, size and mtime;
- every path probed with ``os.stat`` / ``os.lstat`` / ``os.readlink`` ->
  type, size, mtime and link target;
- every environment variable read -> folded into one digest;
- the effective org policy -> fingerprint (re-discovered on every run);
- the output the command printed.

A stamp is written only when the recorded run was itself a no-op: it wrote
nothing, made no network call, ran no subprocess other than the policy
discovery ``git remote``, never prompted, exited 0 and printed the
``No changes`` summary. Because that run wrote nothing, its inputs are also
its outputs; a run whose inputs are byte-identical repeats it exactly.

Replay (a hit) additionally re-runs, every time: org policy discovery with
its unchanged cache-freshness rules, and the lockfile's deployed-file
content-hash verification (``verify_deployed_file_hashes``). It writes
nothing. On by default: ``APM_INSTALL_FAST_PATH=0`` turns it off.

Trust boundary: stamps live in the user's cache root, like the cached
package contents themselves; a same-user process that can write the cache
can already alter what an install deploys.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from apm_cli.install.noop_recorder import (
    DISABLE_ENV as _DISABLE_ENV,
)
from apm_cli.install.noop_recorder import (
    _env_digest,
    _file_digest,
    _listing,
    _probe,
    _Recording,
    _resolution,
    _under,
)

_SCHEMA = 1
_STAMP_BUCKET = "noop_v1"
_SUMMARY = re.compile(r"No changes -- install state already up to date in \d+\.\ds\.")
_SUMMARY_PLACEHOLDER = "\x00APM_ELAPSED\x00"
_SUMMARY_TEMPLATE = "No changes -- install state already up to date in {elapsed:.1f}s."
# ---------------------------------------------------------------------------
# Eligibility and keys
# ---------------------------------------------------------------------------


def _disabled() -> bool:
    if sys.platform == "win32":
        # os.path.exists and friends are C functions on Windows: the probe
        # recorder cannot observe them, so the fast path never engages.
        return True
    return os.environ.get(_DISABLE_ENV, "").strip().lower() in _DISABLED


# On by default (#4770). ``APM_INSTALL_FAST_PATH=0`` turns it off; ``=trace``
# keeps it on and reports each decision on stderr (tests, benchmarks).
_DISABLED = frozenset({"0", "false", "no", "off"})


def eligible(options: dict[str, Any], argv: list[str] | tuple[str, ...] = ()) -> bool:
    """Only a plain re-install of the manifest (optionally ``--frozen``) qualifies."""
    if _disabled():
        return False
    if options.get("packages") or options.get("mcp_name") is not None:
        return False
    if options.get("update") or options.get("refresh") or options.get("dry_run"):
        return False
    if options.get("no_policy") or _explicit_policy_in(list(argv)):
        # --no-policy must not trigger discovery; an explicit --policy source composes
        # with discovery in ways the org fingerprint does not capture.
        return False
    return not options.get("root") and not options.get("global_")


def _explicit_policy_in(argv: list[str]) -> bool:
    return any(arg == "--policy" or arg.startswith("--policy=") for arg in argv)


def _code_identity() -> dict[str, Any]:
    from apm_cli import __version__

    package_dir = Path(__file__).resolve().parents[1]
    hasher = hashlib.sha256()
    if getattr(sys, "frozen", False):
        with open(sys.executable, "rb") as handle:
            hasher.update(hashlib.file_digest(handle, "sha256").digest())
    else:
        for dirpath, dirnames, filenames in os.walk(package_dir):
            dirnames.sort()
            for name in sorted(filenames):
                if name.endswith(".py"):
                    path = os.path.join(dirpath, name)
                    hasher.update(os.path.relpath(path, package_dir).encode() + b"\x00")
                    with open(path, "rb") as handle:
                        hasher.update(handle.read())
                    hasher.update(b"\x00")
    return {
        "version": __version__,
        "package": str(package_dir),
        "code_sha256": hasher.hexdigest(),
        "python": sys.version,
        "executable": sys.executable,
    }


def _loaded_modules() -> dict[str, str | None]:
    """Content digests of every loaded module file outside the apm_cli tree.

    Reads under interpreter roots are not recorded, so runtime dependencies
    (PyYAML's loader, click, ...) and the stdlib are bound here instead.
    """
    package_dir = str(Path(__file__).resolve().parents[1])
    frozen = getattr(sys, "frozen", False)
    paths = set()
    for module in list(sys.modules.values()):
        path = getattr(module, "__file__", None)
        if not isinstance(path, str):
            continue
        path = os.path.abspath(path)
        if path.startswith(package_dir + os.sep) or (frozen and not os.path.isfile(path)):
            continue  # apm_cli is hashed whole; a frozen bundle is the executable's hash
        paths.add(path)
    return {path: _file_digest(path) for path in sorted(paths)}


def _invocation_key(argv: list[str]) -> dict[str, Any]:
    tty = []
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            tty.append(bool(stream.isatty()))
        except (AttributeError, ValueError):
            tty.append(False)
    try:
        columns = os.get_terminal_size().columns if tty[1] else None
    except OSError:
        columns = None
    return {
        "schema": _SCHEMA,
        "argv": list(argv),
        "cwd": os.getcwd(),
        "tty": tty,
        "columns": columns,
        "uid": os.getuid() if hasattr(os, "getuid") else None,
    }


def _stamp_path(key: dict[str, Any]) -> Path:
    from apm_cli.cache.paths import get_cache_root

    digest = hashlib.sha256(json.dumps(key, sort_keys=True).encode()).hexdigest()
    return get_cache_root(ensure=False) / _STAMP_BUCKET / f"{digest}.json"


# ---------------------------------------------------------------------------
# Fingerprints of the observed inputs
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _quiet() -> Iterator[None]:
    """Silence optimization-only discovery: the full pipeline reports its own."""
    import io
    import logging

    previous = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            yield
    finally:
        logging.disable(previous)


def policy_fingerprint(project_root: Path) -> str | None:
    """Re-discover and route the org policy live; ``None`` refuses the fast path.

    Discovery keeps its unchanged cache-freshness rules, and the routed
    decision comes from the canonical owner (``policy/outcome_routing.py``).
    A blocking or unresolved route, a stale cache, a refresh failure or a
    warning always takes the full pipeline, which reports it. Discovery runs
    silenced: its diagnostics belong to the full pipeline, not the optimization.
    """
    with _quiet():
        return _policy_fingerprint(project_root)


def _policy_fingerprint(project_root: Path) -> str | None:
    from apm_cli.policy.discovery import policy_disabled_by_env
    from apm_cli.policy.outcome_routing import classify_project_policy, discover_trust_policy

    if policy_disabled_by_env():
        return "disabled-by-env"
    try:
        result = discover_trust_policy(project_root)
        route = classify_project_policy(project_root, fetch_result=result)
    except Exception:
        return None
    if route.blocks or route.unresolved:
        return None
    if result.cache_stale or result.fetch_error or result.warnings:
        return None
    policy_bytes = b""
    if route.policy is not None:
        try:
            from dataclasses import asdict, is_dataclass

            data = asdict(route.policy) if is_dataclass(route.policy) else repr(route.policy)
            policy_bytes = json.dumps(data, sort_keys=True, default=repr).encode()
        except Exception:
            return None
    digest = hashlib.sha256(policy_bytes).hexdigest()
    return f"{route.outcome}|{route.kind}|{result.source}|{digest}"


def deployed_hashes_intact(project_root: Path) -> bool:
    """Every content hash the lockfile records still matches the deployed bytes."""
    from apm_cli.deps.lockfile import LockFile, get_lockfile_path
    from apm_cli.policy.ci_checks import verify_deployed_file_hashes

    lock = LockFile.read(get_lockfile_path(project_root))
    if lock is None:
        return False
    mismatches, unresolved = verify_deployed_file_hashes(project_root, lock)
    return not mismatches and not unresolved


def _unrecordable(self: _Recording, project_root: Path) -> str | None:
    """Why this run's no-op must not be stamped, or ``None`` when it may be."""
    text = "".join(chunk for _, chunk in self.output)
    if len(_SUMMARY.findall(text)) != 1:
        return f"summary seen {len(_SUMMARY.findall(text))} times"
    if not self.inputs_held():
        return f"input changed during the run: {self.changed_input()}"
    return _hidden_local_source(project_root, self.recorder.excluded_roots)


def _save_stamp(self: _Recording, project_root: Path) -> bool:
    """Persist the stamp when the recorded run qualifies; return whether it did."""
    recorder = self.recorder
    if recorder.violations:
        return False
    refusal = _unrecordable(self, project_root)
    if refusal:
        recorder.violations.append(refusal)
        return False
    if self.policy is None:
        # This run was a no-op but carried no live policy view: mark the
        # invocation pending so the next one records with it.
        return _write_stamp(self.stamp, {"key": self.key, "pending": True})
    if policy_fingerprint(project_root) != self.policy:
        recorder.violations.append("org policy changed during the run")
        return False
    code = getattr(self, "code", None)
    if code is None or _code_identity() != code:
        # The output came from the code loaded before the run; bind exactly that.
        recorder.violations.append("APM code changed during the run")
        return False
    modules = _loaded_modules()
    if None in modules.values():
        recorder.violations.append("a loaded module file cannot be hashed")
        return False
    output = [
        [channel, _SUMMARY.sub(_SUMMARY_PLACEHOLDER, chunk)] for channel, chunk in self.output
    ]
    stamp = {
        "key": self.key,
        "code": self.code,
        "policy": self.policy,
        "env": _env_digest(None if recorder.env_all else sorted(recorder.env), self.environ_start),
        "env_names": None if recorder.env_all else sorted(recorder.env),
        "reads": {path: _file_digest(path) for path in sorted(recorder.reads)},
        "listed": {path: _listing(path) for path in sorted(recorder.listed)},
        "probed": {path: _probe(path) for path in sorted(recorder.probed)},
        "resolved": {key: _resolution(key) for key in sorted(recorder.resolved)},
        "modules": modules,
        "output": output,
    }
    if not self.inputs_held():
        recorder.violations.append(f"input changed while stamping: {self.changed_input()}")
        return False
    return _write_stamp(self.stamp, stamp)


def _hidden_local_source(project_root: Path, excluded_roots: tuple[str, ...]) -> str | None:
    """Why a local dependency's reads may be unrecorded, or ``None``.

    Reads under interpreter roots are not recorded, so a local source there
    could change without invalidating the stamp; any doubt refuses it.
    """
    from apm_cli.deps.lockfile import LockFile
    from apm_cli.deps.path_anchoring import resolve_local_dep_dirs

    try:
        lockfile = LockFile.read(project_root / "apm.lock.yaml")
        if lockfile is None:
            return "lockfile unreadable"
        for source in resolve_local_dep_dirs(lockfile, project_root).values():
            for path in {os.path.abspath(source), os.path.realpath(source)}:
                if _under(path, excluded_roots):
                    return f"local source under an unrecorded root: {path}"[:200]
    except Exception as error:
        return f"local sources unresolved: {type(error).__name__}"
    return None


def _write_stamp(path: Path, stamp: dict[str, Any]) -> bool:
    from apm_cli.utils.atomic_io import atomic_write_text

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, json.dumps(stamp), temp_prefix=".stamp-")
    except (OSError, ValueError):
        return False
    return True


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------


def _first_changed(recorded: dict[str, Any], current: Callable[[str], Any]) -> str | None:
    for path, value in recorded.items():
        if current(path) != value:
            return path
    return None


def _stamp_mismatch(
    stamp: dict[str, Any],
    key: dict[str, Any],
    policy: Callable[[], str | None],
    project_root: Path,
) -> str | None:
    """Return why *stamp* does not prove this run a no-op, or ``None`` when it does.

    Checks run cheapest first; each is a callable so the first failure stops.
    """
    checks: list[tuple[str, Callable[[], object]]] = [
        ("invocation", lambda: stamp.get("key") != key),
        ("environment", lambda: stamp.get("env") != _env_digest(stamp.get("env_names"))),
        ("content", lambda: _first_changed(stamp.get("reads", {}), _file_digest)),
        ("listing", lambda: _first_changed(stamp.get("listed", {}), _listing)),
        ("path", lambda: _first_changed(stamp.get("probed", {}), _probe)),
        ("resolution", lambda: _first_changed(stamp.get("resolved", {}), _resolution)),
        ("apm version", lambda: stamp.get("code") != _code_identity()),
        ("dependency code", lambda: _first_changed(stamp.get("modules", {}), _file_digest)),
        ("org policy", lambda: (lambda now: now is None or now != stamp.get("policy"))(policy())),
        ("deployed file hash", lambda: not deployed_hashes_intact(project_root)),
        # Second pass: nothing recorded changed while the live checks ran.
        ("content (recheck)", lambda: _first_changed(stamp.get("reads", {}), _file_digest)),
        ("listing (recheck)", lambda: _first_changed(stamp.get("listed", {}), _listing)),
        ("path (recheck)", lambda: _first_changed(stamp.get("probed", {}), _probe)),
    ]
    for reason, changed in checks:
        found = changed()
        if found:
            return reason if found is True else f"{reason} {found}"
    return None


def _replay(stamp: dict[str, Any], started: float) -> None:
    elapsed = time.perf_counter() - started
    summary = _SUMMARY_TEMPLATE.format(elapsed=elapsed)
    for channel, chunk in stamp["output"]:
        stream = sys.stdout if channel == 1 else sys.stderr
        stream.write(chunk.replace(_SUMMARY_PLACEHOLDER, summary))
    sys.stdout.flush()
    sys.stderr.flush()


_STAMP_MAPS = ("reads", "listed", "probed", "resolved", "modules")


def _load(path: Path) -> dict[str, Any] | None:
    """Read a stamp; anything malformed is no stamp (the full pipeline runs)."""
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("key"), dict):
        return None
    if data.get("pending") is True:
        return data
    if not all(isinstance(data.get(name), dict) for name in _STAMP_MAPS):
        return None
    if not isinstance(data.get("policy"), str) or not isinstance(data.get("env"), str):
        return None
    output = data.get("output")
    if not isinstance(output, list) or not all(
        isinstance(row, list) and len(row) == 2 and row[0] in (1, 2) and isinstance(row[1], str)
        for row in output
    ):
        return None
    if sum(chunk.count(_SUMMARY_PLACEHOLDER) for _, chunk in output) != 1:
        return None  # a replay must print the no-op summary exactly once
    return data


def _invocation_argv() -> list[str] | None:
    """The arguments this CLI invocation actually parsed (``cli.py`` records them).

    ``sys.argv`` is not trustworthy in-process (``CliRunner``, ``cli.main(args=)``).
    """
    import click

    ctx = click.get_current_context(silent=True)
    raw = ctx.meta.get("apm_raw_args") if ctx is not None else None
    return list(raw) if isinstance(raw, (list, tuple)) else None


def _trace(text: str) -> None:
    from apm_cli.utils.console import ascii_safe

    with contextlib.suppress(Exception):  # a diagnostic never changes the outcome
        sys.stderr.write(ascii_safe(text) + "\n")


def run_with_fast_path(
    options: dict[str, Any],
    run: Callable[[], Any],
    project_root: Path | None = None,
    argv: list[str] | None = None,
) -> Any:
    """Replay a verified no-op, or run *run* fully while recording its inputs."""
    started = time.perf_counter()
    argv = _invocation_argv() if argv is None else list(argv)
    if argv is None or not eligible(options, argv):
        return run()
    root = project_root or Path.cwd()
    try:
        key = _invocation_key(argv)
        stamp_path = _stamp_path(key)
    except Exception:
        return run()
    if not (root / "apm.lock.yaml").is_file():
        return run()  # a first install is never a no-op
    trace = os.environ.get(_DISABLE_ENV, "").strip().lower() == "trace"
    stamp = _load(stamp_path)
    policy: str | None = None
    code: dict[str, Any] | None = None
    if stamp is None:
        reason = "no stamp"
    elif stamp.get("pending"):
        # The previous run was a no-op: record this one with the live policy view.
        reason = "pending"
        try:
            policy = policy_fingerprint(root)
            code = _code_identity()
        except Exception:
            return run()  # the optimization never fails an install
    else:
        try:
            reason = _stamp_mismatch(stamp, key, lambda: policy_fingerprint(root), root)
        except Exception as error:
            reason = f"error {type(error).__name__}"
        if reason is None:
            _replay(stamp, started)
            if trace:
                _trace("[i] install fast path: replayed a verified no-op")
            return None
    # A miss runs no live discovery of its own (only a pending invocation does):
    # cold, warm and changed installs pay only the recorder.
    if trace:
        _trace(f"[i] install fast path: full run ({reason})")
    recording = _Recording(key, stamp_path, policy, started, root)
    recording.code = code
    return _run_recorded(run, recording, root, trace)


def _run_recorded(run: Callable[[], Any], recording: _Recording, root: Path, trace: bool) -> Any:
    try:
        with recording:
            result = run()
    except BaseException as error:
        # ``apm install`` ends with ``ctx.exit(code)``: only a clean exit 0 records.
        if _exit_code(error) == 0:
            _finish(recording, root, trace)
        raise
    _finish(recording, root, trace)
    return result


def _exit_code(error: BaseException) -> int | None:
    from click.exceptions import Exit

    if isinstance(error, Exit):
        return error.exit_code
    if isinstance(error, SystemExit):
        return 0 if error.code in (None, 0) else 1
    return None


def _finish(recording: _Recording, root: Path, trace: bool) -> None:
    """Save the stamp; a recording failure never changes the install's own outcome."""
    try:
        saved = _save_stamp(recording, root)
    except Exception as error:
        recording.recorder.violations.append(f"save failed: {type(error).__name__}")
        saved = False
    if trace:
        detail = "recorded" if saved else f"not recorded {recording.recorder.violations[:8]}"
        _trace(f"[i] install fast path: {detail}")
