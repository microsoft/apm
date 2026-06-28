"""Trust gate for project-source lifecycle scripts.

Policy scripts (/etc/apm/policy.d) and user scripts (~/.apm/apm.yml)
originate from sources the developer already controls, so they run
without a gate.  The project apm.yml lifecycle: block, however, is
committed into a repository -- cloning an untrusted repo and running
apm install would otherwise execute attacker-controlled shell
commands with no consent.

To close that supply-chain hole, project-source scripts are skipped
unless the developer has explicitly trusted the exact lifecycle: subtree
of apm.yml (direnv / VS Code Workspace Trust model).  Trust is keyed
by the SHA-256 of the canonical JSON-serialised lifecycle: subtree, so
editing dependencies: or any other apm.yml key does NOT revoke trust,
but editing lifecycle: DOES.

Trust records live in $APM_HOME/scripts-trust.json (default
~/.apm/scripts-trust.json)::

    {"version": 1, "projects": {"<abs apm.yml path>": "<sha256>"}}
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import tempfile
import threading
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX
    fcntl = None  # type: ignore[assignment]

_logger = logging.getLogger(__name__)

TRUST_FILE_VERSION = 1

# Serialises the load -> modify -> write window of the trust store for
# threads in this process; the file lock below covers other processes.
_TRUST_STORE_THREAD_LOCK = threading.Lock()


def _trust_store_path() -> Path:
    """Return the path to the script trust store."""
    apm_home = os.environ.get("APM_HOME")
    base = Path(apm_home) if apm_home else Path.home() / ".apm"
    return base / "scripts-trust.json"


def script_file_fingerprint(path: Path) -> str | None:
    """Return the SHA-256 hex digest of the lifecycle: subtree of apm.yml.

    Returns None if the file is unreadable, has no lifecycle: key, or
    the lifecycle: value is falsy/empty.
    """
    from apm_cli.utils.yaml_io import load_yaml

    try:
        data = load_yaml(path)
    except Exception as e:
        _logger.debug("Cannot fingerprint apm.yml lifecycle %s: %s", path, e)
        return None

    if not isinstance(data, dict):
        return None
    return fingerprint_lifecycle_subtree(data.get("lifecycle"))


def fingerprint_lifecycle_subtree(lifecycle: object) -> str | None:
    """Return the SHA-256 of a canonicalised lifecycle: subtree.

    Single source of truth for the trust fingerprint so the bytes that
    are EXECUTED can be fingerprinted directly (no independent re-read).
    Returns None for a falsy/empty subtree.
    """
    if not lifecycle:
        return None
    canonical = json.dumps(lifecycle, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _load_trust_store() -> dict[str, str]:
    """Load the trusted-projects map (abs path -> sha256)."""
    store = _trust_store_path()
    try:
        data = json.loads(store.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    projects = data.get("projects")
    if not isinstance(projects, dict):
        return {}
    return {str(k): str(v) for k, v in projects.items() if isinstance(v, str)}


def _write_trust_store(projects: dict[str, str]) -> None:
    """Atomically persist the trusted-projects map.

    Writes to a temp file in the same directory then os.replace()s it
    over the target, so a concurrent reader never observes a half-written
    file (replace is atomic on POSIX and Windows). Callers that mutate
    the store must hold _trust_store_lock() to avoid lost updates.
    """
    store = _trust_store_path()
    store.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": TRUST_FILE_VERSION, "projects": projects}
    text = json.dumps(payload, indent=2) + "\n"
    fd, tmp = tempfile.mkstemp(dir=str(store.parent), prefix=".scripts-trust.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, store)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


@contextlib.contextmanager
def _trust_store_lock():
    """Serialise a trust-store read-modify-write across threads and processes.

    Holds an in-process thread lock plus, where available, an exclusive
    advisory file lock so two concurrent trust()/untrust() calls cannot
    clobber each other's entries on a stale snapshot (lost update).
    """
    store = _trust_store_path()
    store.parent.mkdir(parents=True, exist_ok=True)
    lock_path = store.with_name(store.name + ".lock")
    with _TRUST_STORE_THREAD_LOCK:
        lock_handle = None
        try:
            if fcntl is not None:
                lock_handle = open(lock_path, "w", encoding="utf-8")  # noqa: SIM115
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            if lock_handle is not None:
                with contextlib.suppress(OSError):
                    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
                lock_handle.close()


def is_project_scripts_trusted(script_file: Path) -> bool:
    """Return True only when the lifecycle: subtree of script_file is trusted.

    The stored fingerprint must match the current lifecycle: subtree SHA-256,
    so any edit to lifecycle: revokes trust until re-approved.
    Editing other keys (e.g. dependencies:) does not affect trust.
    """
    return is_fingerprint_trusted(script_file, script_file_fingerprint(script_file))


def is_fingerprint_trusted(script_file: Path, fingerprint: str | None) -> bool:
    """Return True when *fingerprint* matches the trusted record for script_file.

    Lets a caller fingerprint the EXACT content it has already parsed and
    will execute, instead of forcing an independent re-read of the file
    (which opens a TOCTOU window between the executed and trusted bytes).
    """
    if fingerprint is None:
        return False
    trusted = _load_trust_store().get(str(script_file.resolve()))
    return trusted == fingerprint


def trust_project_scripts(script_file: Path) -> str | None:
    """Record trust for the current lifecycle: subtree of script_file.

    Returns the recorded fingerprint, or None when the file cannot be
    read or has no lifecycle: key (nothing is recorded in that case).
    """
    fingerprint = script_file_fingerprint(script_file)
    if fingerprint is None:
        return None
    with _trust_store_lock():
        projects = _load_trust_store()
        projects[str(script_file.resolve())] = fingerprint
        _write_trust_store(projects)
    return fingerprint


def untrust_project_scripts(script_file: Path) -> bool:
    """Revoke trust for script_file. Returns True if a record was removed."""
    key = str(script_file.resolve())
    with _trust_store_lock():
        projects = _load_trust_store()
        if key not in projects:
            return False
        del projects[key]
        _write_trust_store(projects)
    return True
