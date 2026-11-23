"""Drift replay served from package render records (#4940).

``served_outputs`` is the one owner of the decision. It returns, for one
locked package, the scratch output a live replay would write as
``{dest_rel: raw_bytes}``, or ``None`` to replay that package live exactly as
before. A record can only ever send a package live; it never changes what the
drift diff sees: a served package's scratch files are the exact raw bytes of
its live sources, read once, hashed and served from that same buffer, and only
when each hashes to the raw digest the verifying install recorded (lockfile
digests are line-ending normalized, so they cannot vouch for raw bytes).
"""

from __future__ import annotations

import hashlib
import os
import stat
import unicodedata
from collections.abc import Callable, Collection, Iterable, Mapping
from pathlib import Path
from typing import Any

# ``APM_AUDIT_RECORDS=0`` replays every package live.
RECORDS_SWITCH = "APM_AUDIT_RECORDS"
# Source directories whose content is executable or gate-decided: always live.
_LIVE_DIRS = frozenset({"hooks", "bin", "scripts"})
_EXEC_BITS = stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH

# ``(install_path, context) -> {dest_rel: source_path}`` built by the integrators'
# own pure destination mapping (context: targets, scope, legacy skill paths), or
# ``None`` when any deployed kind renders or two sources would share a dest.
VerbatimMapping = Callable[[Path, Any], Mapping[str, Path] | None]


def records_enabled() -> bool:
    return os.environ.get(RECORDS_SWITCH, "").strip() != "0"


def _integrator_mapping(_install_path: Path, _context: Any) -> Mapping[str, Path] | None:
    """The integrators' verbatim destinations (not wired yet: every package replays live)."""
    return None


def served_outputs(
    lock_dep: Any,
    install_path: Path,
    *,
    context: Any,
    selection: Any,
    policy_fingerprint: str | None,
    other_dests: Collection[str] = (),
    store: Any | None = None,
    mapping: VerbatimMapping = _integrator_mapping,
) -> dict[str, bytes] | None:
    """The scratch output of *lock_dep* proven by its render record, else ``None`` (live).

    *context* carries the integrators' deploy context (``targets``, ``scope``,
    legacy skill paths). *policy_fingerprint* is the effective-policy
    fingerprint of this run from the owner the install gate shares (``None``
    when it cannot be computed). *other_dests* are the destinations every
    other locked package claims: a shared destination replays live.
    """
    try:
        return _served(
            lock_dep,
            install_path,
            context,
            selection,
            policy_fingerprint,
            other_dests,
            store,
            mapping,
        )
    except Exception:
        return None


def _served(
    lock_dep: Any,
    install_path: Path,
    context: Any,
    selection: Any,
    policy_fingerprint: str | None,
    other_dests: Collection[str],
    store: Any | None,
    mapping: VerbatimMapping,
) -> dict[str, bytes] | None:
    if not records_enabled() or policy_fingerprint is None:
        return None
    if not _plain_shape(lock_dep, selection):
        return None
    record = _bound_record(lock_dep, getattr(context, "targets", None), store, policy_fingerprint)
    root = Path(install_path)
    if record is None or not _distinct_dests(record["files"], other_dests):
        return None
    if _source_is_live(root):
        return None
    return _verbatim(record, mapping(root, context), root)


def _bound_record(
    lock_dep: Any, targets: Any, store: Any | None, policy_fingerprint: str
) -> dict | None:
    """The package's record when it binds this lock entry and this run's policy exactly."""
    from apm_cli.cache.deploy_store import record_matches

    deployed = set(getattr(lock_dep, "deployed_files", None) or ())
    hashes = dict(getattr(lock_dep, "deployed_file_hashes", None) or {})
    if not deployed or deployed != set(hashes) or _names_live_kind(deployed):
        return None
    record = _record(lock_dep, targets, store)
    if record is None or record.get("policy") != policy_fingerprint:
        return None
    if set(record["files"]) != deployed or not record_matches(record, hashes):
        return None
    return record


def _dest_key(rel: str) -> str:
    """How a case-insensitive or normalizing file system names *rel*."""
    return unicodedata.normalize("NFC", rel).casefold()


def _distinct_dests(files: Mapping[str, Any], other_dests: Collection[str]) -> bool:
    """No two of this package's dests, nor any other package's dest, name the same file."""
    keys = {_dest_key(rel) for rel in files}
    if len(keys) != len(files):
        return False
    return keys.isdisjoint(_dest_key(rel) for rel in other_dests)


def _verbatim(
    record: dict, planned: Mapping[str, Path] | None, root: Path
) -> dict[str, bytes] | None:
    """The raw source bytes, read once, when they are exactly the record's files."""
    if planned is None or set(planned) != set(record["files"]):
        return None
    if len({Path(source) for source in planned.values()}) != len(planned):
        return None
    served: dict[str, bytes] = {}
    for rel, source in planned.items():
        data = _plain_bytes(Path(source), root)
        if data is None or hashlib.sha256(data).hexdigest() != record["files"][rel][1]:
            return None
        served[rel] = data
    return served


def _plain_shape(lock_dep: Any, selection: Any) -> bool:
    """Not a native plugin, not a local source, no skill or target narrowing."""
    if getattr(lock_dep, "native_plugin", None) is not None:
        return False
    if getattr(lock_dep, "is_local_source", False) or getattr(lock_dep, "local_path", None):
        return False
    return all(
        getattr(selection, name, None) is None
        for name in ("skill_subset", "target_subset", "skill_pairing")
    )


def _names_live_kind(paths: Iterable[str]) -> bool:
    return any(_LIVE_DIRS.intersection(rel.split("/")) for rel in paths)


def _record(lock_dep: Any, targets: Any, store: Any | None) -> dict | None:
    from apm_cli.cache.deploy_store import DeployStore
    from apm_cli.core.scope import InstallScope
    from apm_cli.version import get_version

    store = store if store is not None else DeployStore.open_default()
    if store is None:
        return None
    return store.package_record(
        lock_dep.repo_url,
        lock_dep.resolved_commit,
        lock_dep.content_hash,
        targets,
        get_version(),
        InstallScope.PROJECT,
    )


def _source_is_live(root: Path) -> bool:
    """A package with executables, executable-gated or MCP/LSP content replays live."""
    from apm_cli.core.scope import manifest_file
    from apm_cli.models.apm_package import APMPackage

    manifest = manifest_file(root)
    if manifest.exists():
        package = APMPackage.from_apm_yml(manifest)
        if package.get_mcp_dependencies() or package.get_lsp_dependencies():
            return True
    for directory, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs if d != ".git"]
        if _LIVE_DIRS.intersection(dirs):
            return True
        for name in names:
            info = os.lstat(Path(directory) / name)
            if not stat.S_ISREG(info.st_mode) or info.st_mode & _EXEC_BITS:
                return True
    return False


def _plain_bytes(source: Path, root: Path) -> bytes | None:
    """Bytes of a regular, non-executable file inside *root*, else ``None``."""
    from apm_cli.utils.path_security import has_symlink_component

    try:
        source.relative_to(root)
    except ValueError:
        return None
    if has_symlink_component(root, source):
        return None
    info = os.lstat(source)
    if not stat.S_ISREG(info.st_mode) or info.st_mode & _EXEC_BITS:
        return None
    return source.read_bytes()
