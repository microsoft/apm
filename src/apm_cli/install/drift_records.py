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
from shutil import rmtree
from typing import Any

# ``APM_AUDIT_RECORDS=0`` replays every package live.
RECORDS_SWITCH = "APM_AUDIT_RECORDS"
# Source directories whose content is executable or gate-decided: always live.
_LIVE_DIRS = frozenset({"hooks", "bin", "scripts", "extensions"})
# Plugin-format MCP/LSP server declarations and plugin manifests: always live.
_LIVE_FILES = frozenset({".mcp.json", ".lsp.json", "plugin.json"})
# Target primitive kinds this decision can map; hooks and canvas extensions are
# live by _LIVE_DIRS, and any other kind (e.g. rendered commands) is never mapped.
_MAPPED_KINDS = frozenset({"instructions", "prompts", "agents", "skills"})
_LIVE_KINDS = frozenset({"hooks", "canvas"})
_EXEC_BITS = stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH

# ``(install_path, context) -> {dest_rel: source_path}`` built by the integrators'
# own pure destination mapping (context: targets, scope, legacy skill paths), or
# ``None`` when any deployed kind renders or two sources would share a dest.
VerbatimMapping = Callable[[Path, Any], Mapping[str, Path] | None]


def records_enabled() -> bool:
    return os.environ.get(RECORDS_SWITCH, "").strip() != "0"


def _integrator_mapping(_install_path: Path, context: Any) -> Mapping[str, Path] | None:
    """The union of the integrators' own verbatim destinations, relative to the deploy root.

    *context* carries ``package_info``, ``targets``, ``integrators`` and
    ``project_root`` (the root the replay writes under). ``None`` when any
    target deploys a kind no integrator maps, any integrator renders, or two
    sources would land on one destination.
    """
    targets = list(context.targets)
    if any(set(t.primitives) - _MAPPED_KINDS - _LIVE_KINDS for t in targets):
        return None
    integrators = context.integrators
    root = Path(context.project_root)
    parts = [
        integrators[name].verbatim_destinations(context.package_info, root, target)
        for name in ("instruction", "prompt", "agent")
        for target in targets
    ]
    parts.append(integrators["skill"].verbatim_destinations(context.package_info, root, targets))
    planned: dict[str, Path] = {}
    for part in parts:
        if part is None:
            return None
        for destination, source in part.items():
            rel = destination.relative_to(root).as_posix()
            if planned.setdefault(rel, source) != source:
                return None
    return planned


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
    if _source_is_live(root, getattr(getattr(context, "package_info", None), "package", None)):
        return None
    return _verbatim(record, mapping(root, context), root)


def _bound_record(
    lock_dep: Any, targets: Any, store: Any | None, policy_fingerprint: str
) -> dict | None:
    """The package's record when it binds this lock entry and this run's policy exactly."""
    from apm_cli.cache.deploy_store import record_matches

    deployed = set(getattr(lock_dep, "deployed_files", None) or ())
    hashes = dict(getattr(lock_dep, "deployed_file_hashes", None) or {})
    if not hashes or not set(hashes) <= deployed or _names_live_kind(deployed):
        return None
    record = _record(lock_dep, targets, store)
    if record is None or record.get("policy") != policy_fingerprint:
        return None
    if set(record["files"]) != set(hashes) or not record_matches(record, hashes):
        return None
    # Entries without a hash are directories the package owns (a skill dir):
    # each must hold at least one recorded file.
    holders = _recorded_parents(hashes)
    for directory in deployed - set(hashes):
        if directory.rstrip("/") not in holders:
            return None
    return record


def _recorded_parents(files: Iterable[str]) -> set[str]:
    """Every directory that holds a recorded file, at any depth."""
    parents: set[str] = set()
    for rel in files:
        parts = rel.split("/")
        parents.update("/".join(parts[:depth]) for depth in range(1, len(parts)))
    return parents


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
    from apm_cli.utils.content_hash import canonical_digest

    served: dict[str, bytes] = {}
    for rel, source in planned.items():
        data = _plain_bytes(Path(source), root)
        if data is None or not _plain_text(data) or _may_rewrite_links(data):
            return None
        canonical, raw = record["files"][rel][:2]
        # Raw bytes the install recorded, and the lock-bound canonical digest.
        if hashlib.sha256(data).hexdigest() != raw or canonical_digest(data) != canonical:
            return None
        served[rel] = data
    return served


def _plain_text(data: bytes) -> bool:
    """UTF-8 without carriage returns: a live text read returns exactly these bytes.

    A live render reads its source as strict UTF-8 text with universal
    newlines, so a CR or undecodable byte would read differently (or fail).
    """
    if b"\r" in data:
        return False
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _may_rewrite_links(data: bytes) -> bool:
    """Whether a live render could rewrite a link in *data*: any non-URL markdown link.

    Link rewriting depends on files outside the package bytes (context files,
    sibling assets), so such a file replays live.
    """
    from apm_cli.compilation.link_resolver import UnifiedLinkResolver, is_external_url

    text = data.decode("utf-8")
    return any(
        not is_external_url(match.group(2))
        for match in UnifiedLinkResolver.LINK_PATTERN.finditer(text)
    )


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


def _source_is_live(root: Path, package: Any) -> bool:
    """A package with executables, executable-gated or MCP/LSP content replays live.

    *package* is the manifest the replay already parsed for this package (no
    second parse, so manifest warnings print exactly as a live replay prints
    them); a package without one replays live.
    """
    from apm_cli.core.scope import manifest_file
    from apm_cli.utils.helpers import find_plugin_json

    if find_plugin_json(root) is not None:
        return True
    if manifest_file(root).exists():
        if package is None or any(
            getattr(package, getter)()
            for getter in (
                "get_mcp_dependencies",
                "get_dev_mcp_dependencies",
                "get_lsp_dependencies",
                "get_dev_lsp_dependencies",
            )
        ):
            return True
    for directory, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs if d != ".git"]
        if _LIVE_DIRS.intersection(dirs) or _LIVE_FILES.intersection(names):
            return True
        # A linked directory can change what a live render resolves.
        for name in [*dirs, *names]:
            path = Path(directory) / name
            info = os.lstat(path)
            if stat.S_ISDIR(info.st_mode):
                continue
            if not stat.S_ISREG(info.st_mode) or info.st_mode & _EXEC_BITS:
                return True
            # Live primitive discovery parses every markdown file, deployed or
            # not, and warns about one a text read cannot take as-is.
            if name.lower().endswith(".md") and not _plain_text(path.read_bytes()):
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


class ReplayRecords:
    """The record-served side of one drift replay run (``install/drift.py::run_replay``).

    Serves a package only at its own position in the replay loop, so the scratch
    tree is written in the live order. ``intact()`` reports whether every served
    file still holds its served bytes once the loop ends: a live package that
    rendered onto a served destination makes the caller replay everything live.
    """

    @classmethod
    def for_replay(
        cls,
        config: Any,
        project_root: Path,
        lock: Any,
        targets: Any,
        integrators: Any,
        scratch_root: Path,
        *,
        enabled: bool,
    ) -> ReplayRecords:
        """The record side of one replay.

        ``config`` is the replay config the executables gate reads its policy
        from: when records need a discovery and the caller gave none, it routes
        the gate's policy from that same single discovery.
        """
        from dataclasses import replace

        policy_fetch, exec_policy = record_policy(config, project_root, enabled=enabled)
        records = cls(
            enabled=enabled and not config.user_scope,
            lock=lock,
            targets=targets,
            integrators=integrators,
            scratch_root=scratch_root,
            policy_fetch=policy_fetch,
        )
        records.config = (
            config
            if exec_policy is config.policy_provider
            else replace(config, policy_provider=exec_policy, policy_fetch=policy_fetch)
        )
        return records

    def __init__(
        self,
        *,
        enabled: bool,
        lock: Any,
        targets: Any,
        integrators: Any,
        scratch_root: Path,
        policy_fetch: Callable[[], Any] | None,
    ) -> None:
        self.config: Any = None
        global _LAST_SUMMARY
        _LAST_SUMMARY = None
        self._enabled = enabled and records_enabled()
        self._targets = targets
        self._integrators = integrators
        self._scratch_root = Path(scratch_root)
        self._policy_fetch = policy_fetch
        self._fingerprint: str | None = None
        self._fingerprinted = False
        # Each served destination's sha256, not its bytes: memory stays flat.
        self._served: dict[str, str] = {}
        self.served_packages = 0
        self._claims: dict[str, int] = {}
        for dep in lock.get_all_dependencies() if self._enabled else ():
            for key in {_dest_key(rel) for rel in getattr(dep, "deployed_files", None) or ()}:
                self._claims[key] = self._claims.get(key, 0) + 1

    def _policy_fingerprint(self) -> str | None:
        if not self._fingerprinted:
            self._fingerprinted = True
            from apm_cli.policy.outcome_routing import effective_policy_fingerprint

            try:
                fetch = self._policy_fetch() if self._policy_fetch is not None else None
                self._fingerprint = effective_policy_fingerprint(fetch)
            except Exception:
                self._fingerprint = None
        return self._fingerprint

    def serve(self, lock_dep: Any, install_path: Path, package_info: Any, selection: Any) -> bool:
        """Write *lock_dep*'s proven output into scratch; ``False`` means replay it live."""
        from apm_cli.deps.lockfile import _SELF_KEY

        if not self._enabled or getattr(lock_dep, "local_path", None) == _SELF_KEY:
            return False
        fingerprint = self._policy_fingerprint()
        if fingerprint is None:
            return False
        own = getattr(lock_dep, "deployed_files", None) or ()
        context = _MappingContext(
            package_info, self._targets, self._integrators, self._scratch_root
        )
        served = served_outputs(
            lock_dep,
            install_path,
            context=context,
            selection=selection,
            policy_fingerprint=fingerprint,
            other_dests=[rel for rel in own if self._claims.get(_dest_key(rel), 0) > 1],
        )
        if served is None:
            return False
        for rel, data in served.items():
            path = self._scratch_root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            self._served[rel] = hashlib.sha256(data).hexdigest()
        self.served_packages += 1
        return True

    def finish(self, logger: Any, live: int) -> bool:
        """Report the replay when every served file is intact; ``False`` means replay live."""
        global _LAST_SUMMARY
        if not self.intact():
            return False
        _LAST_SUMMARY = (self.served_packages, live)
        logger.replay_complete(live + self.served_packages)
        return True

    def intact(self) -> bool:
        """Every served destination is still a regular file holding the bytes served to it."""
        return all(
            _regular_file_digest(self._scratch_root / rel) == digest
            for rel, digest in self._served.items()
        )


# A FIFO at a served destination must read as not intact, not block the replay.
_SCRATCH_OPEN = (
    os.O_RDONLY
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_NONBLOCK", 0)
    | getattr(os, "O_BINARY", 0)
)


def _regular_file_digest(path: Path) -> str | None:
    """The sha256 of *path* if it is a regular file that is not a link, else ``None``.

    Opens without following a link and without blocking, then checks the open
    descriptor's type, as the deploy store reads its entries (#5103).
    """
    try:
        if stat.S_ISLNK(os.lstat(path).st_mode):
            return None
        fd = os.open(path, _SCRATCH_OPEN)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        with os.fdopen(fd, "rb", closefd=False) as handle:
            return hashlib.file_digest(handle, "sha256").hexdigest()
    except OSError:
        return None
    finally:
        os.close(fd)


_LAST_SUMMARY: tuple[int, int] | None = None


def last_replay_summary() -> tuple[int, int] | None:
    """``(served, live)`` package counts of the last drift replay in this process."""
    return _LAST_SUMMARY


def record_policy(
    config: Any, project_root: Path, *, enabled: bool
) -> tuple[Callable[[], Any] | None, Callable[[], Any] | None]:
    """``(discovery, policy provider)`` for one replay, sharing ONE discovery.

    With records off, or when the caller supplies the provider (and maybe the
    discovery it routes), nothing changes. Otherwise the replay discovers once
    and the executables gate resolves its policy from that same discovery,
    exactly as it would have resolved it on its own.
    """
    if not enabled or config.policy_provider is not None:
        return config.policy_fetch, config.policy_provider
    from apm_cli.core.scope import manifest_root
    from apm_cli.policy.outcome_routing import (
        discover_trust_policy,
        keep_policy_answer,
        resolve_trust_policy,
    )

    root = manifest_root(project_root, user_scope=config.user_scope)
    kept: list[Any] = []
    # An answer a retry may change, which the records' probe discovered: the
    # gate evaluates it first, as a live replay's first discovery.
    loaned: list[Any] = []

    def fetch() -> Any:
        if kept:
            return kept[0]
        if loaned:
            return loaned[0]
        result = discover_trust_policy(root)
        (kept if keep_policy_answer(result) else loaned).append(result)
        return result

    def gate_fetch() -> Any:
        if kept:
            return kept[0]
        if loaned:
            return loaned.pop()
        result = discover_trust_policy(root)
        if keep_policy_answer(result):
            kept.append(result)
        return result

    return fetch, lambda: resolve_trust_policy(root, announce=False, fetch_result=gate_fetch())


class _MappingContext:
    """The integrators' deploy context for one package (targets, root, integrators)."""

    def __init__(self, package_info: Any, targets: Any, integrators: Any, root: Path) -> None:
        self.package_info = package_info
        self.targets = targets
        self.integrators = integrators
        self.project_root = root


def replay_all_live(
    config: Any, logger: Any, scratch_root: Path, run_replay: Callable[[Any, Any], Path]
) -> Path:
    """Rerun *config*'s replay all live in *scratch_root* (F3b restart, #4940)."""
    keep = config.modules_root.resolve() if config.modules_root is not None else None
    # Without a caller's scratch root the restart takes a fresh one; the first
    # is removed at exit like any replay scratch.
    if config.scratch_root is not None:
        for child in scratch_root.iterdir():
            if child.resolve() != keep:
                if child.is_dir() and not child.is_symlink():
                    rmtree(child, ignore_errors=True)
                else:
                    child.unlink(missing_ok=True)
    return run_replay(config, _Restarted(logger))


class _Restarted:
    """The replay's logger for an all-live restart: the start lines already printed."""

    def __init__(self, logger: Any) -> None:
        self._logger = logger

    def replay_start(self) -> None:
        return None

    def scratch_root(self, _path: Path) -> None:
        return None

    def __getattr__(self, name: str) -> Any:
        return getattr(self._logger, name)
