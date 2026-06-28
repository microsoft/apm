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

# Bounds on the lifecycle: subtree the trust fingerprint will canonicalise.
# The fingerprint is SHA-256(json.dumps(subtree)); json.dumps follows every
# container edge (shared YAML aliases are re-serialised once per reference)
# and recurses one Python frame per nesting level. A hostile project apm.yml
# (attacker-controlled, committed) can therefore weaponise the canonicaliser
# THREE ways, all of which must fail closed BEFORE json.dumps runs:
#   - a high-fan-out recursive alias (``&a [*a,*a,...]``) -> exponential /
#     unbounded WALK (the in-memory DAG is tiny but the tree is not);
#   - a fat scalar aliased many times (``&s "..."; [*s,*s,...]``) -> the node
#     COUNT is small but the serialised BYTE size is enormous;
#   - a deep linear nest -> blows json.dumps' C-recursion stack (RecursionError).
# A legitimate lifecycle block has NO shared/aliased references, nests only a
# handful deep, and canonicalises to a few KB. So the guard rejects any subtree
# that (a) reuses a container reference (alias/cycle), (b) nests deeper than the
# depth cap, (c) exceeds the node cap, or (d) would serialise past the byte cap.
# Any of these -> the manifest is treated as untrusted (fail-closed) rather than
# allowed to exhaust memory or crash inside json.dumps.
_MAX_FINGERPRINT_NODES = 100_000
_MAX_FINGERPRINT_DEPTH = 64
_MAX_FINGERPRINT_BYTES = 1_000_000

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


def _leaf_byte_cost(node: object) -> int:
    """Approximate the canonical-JSON byte contribution of a scalar leaf.

    Used to bound the serialised size BEFORE json.dumps allocates it, so a
    fat scalar that is aliased many times (small node count, huge output)
    cannot OOM the canonicaliser. An over-estimate is fine -- the goal is
    only to bail before the cumulative size becomes dangerous.
    """
    if isinstance(node, str):
        return len(node) + 2  # surrounding quotes
    if isinstance(node, (bytes, bytearray)):
        return len(node) + 2
    if isinstance(node, bool):
        return 5  # "false"
    if isinstance(node, int):
        # json.dumps emits the FULL decimal expansion of an int, so a fat
        # integer scalar aliased many times stays a small node count but
        # serialises to enormous bytes. PyYAML decodes unbounded integer
        # scalars to Python int, so this must be magnitude-aware or the byte
        # cap is blind to it. Estimate the decimal digit count from the bit
        # length (log10(2) ~ 0.30 digits/bit; // 3 over-estimates slightly,
        # which is safe) without materialising the string. +3 covers the
        # sign and a couple of guard chars.
        return (node.bit_length() // 3) + 3
    # float / None / other short-repr scalars -> small constant upper bound.
    return 8


def _is_fingerprint_safe(obj: object) -> bool:
    """True if *obj* can be canonicalised by json.dumps without abuse.

    Bounded structural walk that fails closed on any property a legitimate
    lifecycle block never has, while still ACCEPTING the benign YAML anchor /
    alias sharing an author may use to de-duplicate a manifest:

    * a CYCLE (a container reachable from itself -- direct self-reference or
      a back-edge) is rejected via PATH-scoped id tracking: only ancestors on
      the current descent path are checked, so a shared acyclic reference (a
      benign anchor reused by two sibling keys) passes, but a self-alias bomb
      (``&a [*a, ...]``) is rejected on its first child;
    * the TRUE tree-expansion node count (no dedup -- json.dumps re-emits each
      shared reference) is capped, catching a classic billion-laughs DAG;
    * nesting DEPTH is capped before our own recursion can exceed it, so a deep
      linear chain is rejected before json.dumps' C-recursion would crash;
    * the cumulative serialised BYTE size is capped, catching a fat scalar
      aliased many times (small node count, huge output).

    Our recursion never exceeds _MAX_FINGERPRINT_DEPTH frames because the depth
    cap is checked at entry, before descending further.
    """
    counters = {"nodes": 0, "bytes": 0}

    def _walk(node: object, depth: int, path_ids: frozenset[int]) -> bool:
        counters["nodes"] += 1
        if counters["nodes"] > _MAX_FINGERPRINT_NODES or depth > _MAX_FINGERPRINT_DEPTH:
            return False
        if isinstance(node, (dict, list, tuple)):
            node_id = id(node)
            if node_id in path_ids:
                # Container reachable from itself -> cycle / self-alias bomb.
                return False
            child_path = path_ids | {node_id}
            child_depth = depth + 1
            if isinstance(node, dict):
                for key, value in node.items():
                    if not _walk(key, child_depth, child_path):
                        return False
                    if not _walk(value, child_depth, child_path):
                        return False
            else:
                for child in node:
                    if not _walk(child, child_depth, child_path):
                        return False
            return True
        counters["bytes"] += _leaf_byte_cost(node)
        return counters["bytes"] <= _MAX_FINGERPRINT_BYTES

    return _walk(obj, 0, frozenset())


def fingerprint_lifecycle_subtree(lifecycle: object) -> str | None:
    """Return the SHA-256 of a canonicalised lifecycle: subtree.

    Single source of truth for the trust fingerprint so the bytes that
    are EXECUTED can be fingerprinted directly (no independent re-read).
    Returns None for a falsy/empty subtree, for a subtree that cannot
    be canonicalised (e.g. a YAML scalar that safe_load decoded into a
    non-JSON-serializable Python object such as datetime.date or set),
    or for a subtree that fails the structural safety check (a YAML alias
    bomb, a fat aliased scalar, or pathological nesting depth) -- an
    un-fingerprintable manifest is treated as untrusted (fail-closed).
    """
    if not lifecycle:
        return None
    if not _is_fingerprint_safe(lifecycle):
        _logger.debug(
            "lifecycle subtree failed the fingerprint safety check "
            "(alias bomb / fat scalar / over-deep) -- treating as untrusted",
        )
        return None
    try:
        canonical = json.dumps(lifecycle, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError, RecursionError, MemoryError) as e:
        _logger.debug("Cannot canonicalise lifecycle subtree for fingerprint: %s", e)
        return None
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
    try:
        key = str(script_file.resolve())
    except OSError:
        # A concurrent symlink swap (or any FS resolution error) on the
        # project apm.yml must fail CLOSED: treat the tier as untrusted and
        # let install/update/uninstall proceed. is_fingerprint_trusted sits
        # on the firing boundary (install/service.py is not wrapped like the
        # update/uninstall callers), so a propagated OSError would abort the
        # primary install path -- a fail-not-closed DoS, not a trust bypass.
        return False
    trusted = _load_trust_store().get(key)
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
