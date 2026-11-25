"""Machine-local evidence of the MCP entries install wrote, per installation (#4737).

The lockfile records which MCP servers APM manages. This module keeps a second
record on this machine: for each entry an install writes to a runtime config,
the runtime, the config file, the native key and the fingerprint of the entry
as written (the same :func:`~apm_cli.install.mcp.ownership.mcp_native_hash` of
``client.managed_server_config`` that the lockfile's deployment rows hold). It
sits next to the deployment receipts and carries their binding (project root
and its identity, scope, installation identity), checked by
:meth:`~apm_cli.core.deployment_ledger.DeploymentMaterializationSession.binds`.

Only actual writes are recorded; an entry that already matched is never
recorded, so an identical entry the user wrote never becomes APM's.

Records are keyed by (runtime, config file, native key), so each runtime
profile (for example each ``COPILOT_HOME``) keeps its own record.

When ``apm.lock.yaml`` is absent, install retires an entry this evidence
proves APM wrote and no longer deploys (:func:`retire_undeclared_mcp_writes`).
An entry it cannot prove (evidence unusable, or the entry changed since APM
wrote it) is kept, reported, and marked kept; every later install announces it
and ``apm audit --ci`` reports it (:func:`kept_mcp_writes`) until the entry is
removed or declared again. Unusable evidence met there is noted next to the
evidence file, so audit keeps reporting it once the lockfile is rewritten.
An entry of a server still declared that no runtime the manifest's ``targets:``
select deploys to any more (no admitted server's config and native key) is
never removed: it is kept and reported the same way until it is removed or a
targeted runtime deploys there again (#4784).
Only the selected config is ever examined: a record of another profile's
config waits until that profile is selected.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from apm_cli.utils.paths import portable_relpath

_log = logging.getLogger(__name__)

_CANONICAL_HASH = re.compile(r"sha256:[0-9a-f]{64}")
_VERSION = 1

# (runtime, server name, fingerprint, requested info) of each entry the current install wrote.
_Write = tuple[str, str, str | None, dict | None]
_journal: ContextVar[list[_Write] | None] = ContextVar("apm_mcp_write_journal", default=None)


def _ident(write: McpWrite) -> tuple[str, str, str]:
    """Record identity: (runtime, config file, native key); the name when no key was located."""
    return (write.runtime, write.config or "", write.key or write.name)


class McpWriteEvidenceError(ValueError):
    """The MCP write evidence file is unreadable or bound to another installation."""


@dataclass(frozen=True)
class McpWrite:
    """One MCP entry APM wrote: where, under which key, and its fingerprint."""

    runtime: str
    name: str
    config: str | None
    key: str | None
    content_hash: str | None
    kept: bool = False
    # Kept because its server is still declared but no targeted runtime deploys there.
    untargeted: bool = False

    @property
    def usable(self) -> bool:
        """Whether this record can prove an entry unchanged."""
        return (
            self.config is not None
            and self.key is not None
            and self.content_hash is not None
            and _CANONICAL_HASH.fullmatch(self.content_hash) is not None
        )

    def row(self) -> dict[str, Any]:
        row = {
            "runtime": self.runtime,
            "name": self.name,
            "config": self.config,
            "key": self.key,
            "hash": self.content_hash,
            "kept": self.kept,
        }
        if self.untargeted:
            row["untargeted"] = True
        return row

    @classmethod
    def from_row(cls, row: Any) -> McpWrite:
        """Parse one record; fields other than runtime and name may be unusable."""
        if (
            not isinstance(row, dict)
            or not isinstance(row.get("runtime"), str)
            or not isinstance(row.get("name"), str)
        ):
            raise ValueError("invalid MCP write record")

        def text(field: str) -> str | None:
            value = row.get(field)
            return value if isinstance(value, str) else None

        return cls(
            row["runtime"],
            row["name"],
            text("config"),
            text("key"),
            text("hash"),
            row.get("kept") is True,
            row.get("untargeted") is True,
        )


def note_mcp_write(
    runtime: str, name: str, content_hash: str | None, requested_info: dict | None = None
) -> None:
    """Record that the current install wrote *name* for *runtime* (no-op outside a journal).

    *requested_info* is the server info the adapter wrote from; it selects the
    exact native key the adapter used.
    """
    journal = _journal.get()
    if journal is not None:
        journal.append((runtime, name, content_hash, requested_info))


@contextmanager
def journal_mcp_writes(project_root: Path, *, user_scope: bool, logger: Any) -> Iterator[None]:
    """Collect the MCP entries written inside the block and record them on exit.

    Writes made before a failure are recorded too: they are APM's entries.
    """
    token = _journal.set([])
    try:
        yield
    finally:
        writes = _journal.get() or []
        _journal.reset(token)
        try:
            store = McpWriteReceipts.open_for_install(
                project_root, user_scope=user_scope, logger=logger
            )
            store.record(writes)
            store.forget_absent()
            store.save()
        except (OSError, ValueError) as exc:
            if not writes:
                _log.debug("MCP write evidence left unchanged", exc_info=True)
            else:
                logger.warning(f"Could not record the MCP entries this install wrote: {exc}")


class McpWriteReceipts:
    """The MCP write evidence of one installation (project root, scope)."""

    def __init__(self, session: Any, project_root: Path, *, user_scope: bool) -> None:
        self._session = session
        self.project_root = project_root
        self.user_scope = user_scope
        self.path: Path = session.mcp_state_path
        self.writes: dict[tuple[str, str, str], McpWrite] = {}
        self._existed = self.path.exists()
        self._dirty = False
        self._clients: dict[str, Any] = {}
        self._entries: dict[str, dict[str, Any] | None] = {}

    @classmethod
    def open(cls, project_root: Path, *, user_scope: bool) -> McpWriteReceipts:
        """Load the evidence bound to this installation.

        Raises ``ValueError`` (or ``OSError``) when the evidence file is not
        bound to this root, scope and installation identity, or is unreadable.
        """
        from apm_cli.core.deployment_ledger import DeploymentMaterializationSession
        from apm_cli.core.scope import InstallScope

        scope = InstallScope.USER if user_scope else InstallScope.PROJECT
        session = DeploymentMaterializationSession(Path(project_root), scope, None)
        store = cls(session, Path(project_root), user_scope=user_scope)
        if store._existed:
            try:
                data = json.loads(store.path.read_text(encoding="utf-8"))
                if not session.binds(data):
                    raise ValueError("it belongs to another checkout or installation")
                for write in _parse_writes(data):
                    store.writes[_ident(write)] = write
            except (KeyError, TypeError, UnicodeError, ValueError) as exc:
                raise McpWriteEvidenceError(
                    f"Invalid MCP write evidence at {store.path}: {exc}. The entries it lists "
                    "are kept; remove that file to start a new record."
                ) from exc
        return store

    @classmethod
    def open_for_install(
        cls, project_root: Path, *, user_scope: bool, logger: Any
    ) -> McpWriteReceipts:
        """:meth:`open` for an install, which may start this installation's evidence.

        Evidence another installation of this same root wrote (a re-clone or a
        recreated checkout) can never bind here again, so it is set aside once,
        renamed and never deleted, and this installation records its own writes
        (#5396). The entries it lists are left as they are: nothing proves APM
        wrote them for this installation, so none is ever removed. Any other
        unusable evidence still refuses, exactly as :meth:`open`.
        """
        try:
            return cls.open(project_root, user_scope=user_scope)
        except McpWriteEvidenceError:
            aside = _supersede_other_installation(project_root, user_scope=user_scope)
            if aside is None:
                raise
        logger.warning(
            f"Set aside MCP write evidence another checkout of this project wrote: {aside}. "
            "APM no longer manages the MCP entries it lists; "
            "remove any you no longer want from your MCP config."
        )
        _clear_unusable_note(project_root, user_scope=user_scope)
        return cls.open(project_root, user_scope=user_scope)

    # -- runtime configs ---------------------------------------------------------

    def _client(self, runtime: str) -> Any:
        if runtime not in self._clients:
            from apm_cli.factory import ClientFactory

            try:
                self._clients[runtime] = ClientFactory.create_client(
                    runtime, project_root=self.project_root, user_scope=self.user_scope
                )
            except ValueError:
                self._clients[runtime] = None
        return self._clients[runtime]

    def _config(self, runtime: str) -> str | None:
        client = self._client(runtime)
        path = client.get_config_path() if client is not None else None
        return Path(path).absolute().as_posix() if path else None

    def _existing(self, runtime: str) -> dict[str, Any] | None:
        """The runtime's native MCP entries, or None when they cannot be read."""
        if runtime not in self._entries:
            from apm_cli.core.conflict_detector import MCPConflictDetector

            client = self._client(runtime)
            try:
                entries = (
                    dict(MCPConflictDetector(client).get_existing_server_configs())
                    if client is not None
                    else None
                )
            except Exception:
                _log.debug("Unreadable MCP config for %s", runtime, exc_info=True)
                entries = None
            # Adapter readers return no entries for a config they cannot parse:
            # that is not proof the entries are gone.
            if entries == {} and not _parses(self._config(runtime)):
                entries = None
            self._entries[runtime] = entries
        return self._entries[runtime]

    def _fingerprint(self, runtime: str, key: str) -> str | None:
        from apm_cli.install.mcp.ownership import mcp_native_hash

        native = (self._existing(runtime) or {}).get(key)
        if not isinstance(native, dict):
            return None
        return mcp_native_hash(self._client(runtime).managed_server_config(native))

    def _selected(self, write: McpWrite) -> bool:
        """Whether *write* names the config this install selects (not another profile's)."""
        return write.config is None or write.config == self._config(write.runtime)

    def _presence(self, write: McpWrite) -> bool | None:
        """Whether *write*'s entry is in its config; None when that cannot be established."""
        existing = self._existing(write.runtime)
        if existing is None or write.key is None or not self._selected(write):
            return None
        return write.key in existing

    def display(self, write: McpWrite) -> str:
        """The config file holding *write*'s entry now, relative to the project when inside it."""
        config = self._config(write.runtime) or write.config
        if config is None:
            return f"the {write.runtime} MCP config"
        return portable_relpath(Path(config), self.project_root)

    # -- evidence ----------------------------------------------------------------

    def record(self, writes: Iterable[_Write]) -> None:
        """Record entries this install wrote under the exact key the adapter used."""
        from apm_cli.install.mcp.ownership import _native_config_key

        self._entries.clear()
        for runtime, name, content_hash, requested_info in writes:
            client = self._client(runtime)
            if client is None:
                continue
            key: str | None = _native_config_key(client, name, requested_info=requested_info)
            if key not in (self._existing(runtime) or {}) or (
                content_hash is not None and self._fingerprint(runtime, key) != content_hash
            ):
                key = None
            write = McpWrite(runtime, name, self._config(runtime), key, content_hash)
            if key is not None:
                # A located rewrite supersedes the key-less record of the same
                # runtime, config and name (#4861); other profiles' records stay.
                stale = self.writes.get((runtime, write.config or "", name))
                if stale is not None and stale.key is None:
                    del self.writes[(runtime, write.config or "", name)]
            self.writes[_ident(write)] = write
            self._dirty = True

    def forget_absent(self) -> None:
        """Drop records whose entry is no longer in its config: nothing remains to prove."""
        for ident, write in list(self.writes.items()):
            if self._presence(write) is False:
                del self.writes[ident]
                self._dirty = True

    def proven(self, write: McpWrite) -> bool:
        """Whether the entry *write* names is still exactly what APM wrote there."""
        return (
            write.usable
            and write.config == self._config(write.runtime)
            and self._fingerprint(write.runtime, write.key or "") == write.content_hash
        )

    def mark_kept(self, write: McpWrite, *, untargeted: bool = False) -> McpWrite:
        kept = replace(write, kept=True, untargeted=untargeted)
        if kept != write:
            self.writes[_ident(write)] = kept
            self._dirty = True
        return kept

    def active(
        self, admitted: dict[str, set[str]], configs: dict[str, Any]
    ) -> set[tuple[str, str]]:
        """The (config, native key) destinations the *admitted* servers deploy to now.

        *admitted* maps each targeted runtime to the declared servers it receives.
        """
        return {
            (self._config(runtime) or "", key)
            for runtime, names in admitted.items()
            for key in self._declared_keys(runtime, names, configs)
        }

    def retarget(self, active: set[tuple[str, str]]) -> None:
        """Clear the keep of each untargeted record a targeted runtime deploys to again."""
        for ident, write in list(self.writes.items()):
            if write.untargeted and (write.config or "", write.key) in active:
                self.writes[ident] = replace(write, kept=False, untargeted=False)
                self._dirty = True

    def forget(self, write: McpWrite) -> None:
        if self.writes.pop(_ident(write), None) is not None:
            self._dirty = True

    def included_runtimes(self, exclude: str | None) -> set[str]:
        from apm_cli.integration.mcp_user_scope_runtimes import filter_excluded_mcp_runtimes

        runtimes = sorted({write.runtime for write in self.writes.values()})
        return set(filter_excluded_mcp_runtimes(runtimes, exclude))

    def warn_kept(self, write: McpWrite, logger: Any) -> None:
        if write.untargeted:
            logger.warning(
                f"Kept MCP server '{write.name}' in {self.display(write)}: APM no longer "
                f"deploys it for {write.runtime}. Remove the entry if you no longer use it, "
                "or check targets: in apm.yml and in the server's declaration."
            )
            return
        logger.warning(
            f"Kept MCP server '{write.name}' in {self.display(write)}: APM no longer "
            "deploys it and cannot confirm the entry is the one it wrote. "
            "Remove the entry if you no longer use it."
        )

    def retire_undeclared(
        self,
        declared: set[str],
        *,
        exclude: str | None,
        logger: Any,
        declared_configs: dict[str, Any] | None = None,
        admitted: dict[str, set[str]] | None = None,
    ) -> set[str]:
        """Remove proven entries of undeclared servers; keep and report the rest.

        A declared server's present entry that no runtime in *admitted* (the
        manifest's targeted runtimes, each with the declared servers it
        receives; None when the manifest selects none or ``--runtime`` narrows
        this run) deploys to is kept and reported, never removed (#4784).

        Returns the names of the servers whose entries were removed.
        """
        from apm_cli.install.errors import RequiredIntegrationError
        from apm_cli.integration.mcp_integrator_install import _remove_native_entry

        included = self.included_runtimes(exclude)
        removed: set[str] = set()
        active = None if admitted is None else self.active(admitted, declared_configs or {})
        if active is not None:
            self.retarget(active)
        for _, write in sorted(self.writes.items()):
            if write.runtime not in included or not self._selected(write):
                # Another profile's config: decided only when that profile is selected.
                continue
            if write.name in declared:
                if (write.untargeted and self._presence(write) is not False) or self._untargeted(
                    write, active
                ):
                    self.warn_kept(self.mark_kept(write, untargeted=True), logger)
                continue
            if write.key is not None and write.key in self._declared_keys(
                write.runtime, declared, declared_configs or {}
            ):
                # A declared server now deploys to this exact native entry.
                continue
            presence = self._presence(write)
            if presence is False:
                self.forget(write)
                continue
            if presence and self.proven(write):
                try:
                    _remove_native_entry(self._client(write.runtime), write.key, logger)
                except RequiredIntegrationError:
                    # A config the exact-key remover cannot rewrite keeps the entry.
                    _log.debug("Could not remove MCP entry %s", write.key, exc_info=True)
                self._entries.clear()
                if self._presence(write) is False:
                    self.forget(write)
                    removed.add(write.name)
                    continue
            self.mark_kept(write)
            self.warn_kept(write, logger)
        self.save()
        return removed

    def _untargeted(self, write: McpWrite, active: set[tuple[str, str]] | None) -> bool:
        """Whether *write* is a present, located entry no targeted runtime deploys to."""
        return (
            active is not None
            and write.key is not None
            and (write.config or "", write.key) not in active
            and self._presence(write) is not False
        )

    def _declared_keys(self, runtime: str, declared: set[str], configs: dict[str, Any]) -> set[str]:
        """The native keys the declared servers deploy to on *runtime*."""
        from apm_cli.install.mcp.ownership import _native_config_key

        client = self._client(runtime)
        if client is None:
            return set()
        return {
            _native_config_key(client, name, stored_config=configs.get(name)) for name in declared
        }

    def kept(
        self, managed: set[str], configs: dict[str, Any] | None = None
    ) -> list[tuple[McpWrite, str]]:
        """Kept records in the selected configs whose entry is still present.

        A record of an unmanaged server is kept unless its native entry a
        managed server now deploys to (#4861); a record kept because no
        targeted runtime deploys there stays kept until one does again
        (#4784). Install and audit share this one filter.
        """
        return [
            (write, self.display(write))
            for _, write in sorted(self.writes.items())
            if write.kept
            and self._selected(write)
            and self._presence(write) is not False
            and (
                write.untargeted
                or (
                    write.name not in managed
                    and not (
                        write.key is not None
                        and write.key in self._declared_keys(write.runtime, managed, configs or {})
                    )
                )
            )
        ]

    def save(self) -> None:
        """Persist the evidence when it changed; an empty record leaves no file behind."""
        from apm_cli.integration.write_seam import atomic_write_text, mkdir

        if not self._dirty:
            return
        self._dirty = False
        if not self.writes:
            if self._existed:
                self.path.unlink(missing_ok=True)
                self._existed = False
            return
        # binding() validates every evidence path (containment, no symlinks),
        # before and after the directory is created, and creates the identity once.
        self._session.binding()
        mkdir(self.path.parent, mode=0o700, parents=True, exist_ok=True)
        data = self._session.binding()
        data["writes"] = [self.writes[ident].row() for ident in sorted(self.writes)]
        # Machine-local evidence, not a user-editable MCP config.
        text = json.dumps(data, sort_keys=True, indent=2)  # architecture-authority-exempt: evidence
        atomic_write_text(
            self.path, text + "\n", new_file_mode=0o600, create_only=not self._existed
        )
        self._existed = True


def retire_undeclared_mcp_writes(
    declared: Iterable[str],
    *,
    declared_configs: dict[str, Any] | None = None,
    project_root: Path,
    user_scope: bool,
    exclude: str | None,
    logger: Any,
    retire: bool = True,
    admitted: dict[str, set[str]] | None = None,
    manifest: Any = None,
    dependencies: Iterable[Any] = (),
    runtime: str | None = None,
    target_decision: Any = None,
) -> None:
    """Without a lockfile, retire the entries APM wrote for servers it no longer deploys.

    An entry is removed only when this installation's evidence proves it is
    still exactly what APM wrote. Any other recorded entry is kept, reported
    and marked kept for ``apm audit --ci``; an entry with no record (the
    user's own, even when identical to a declaration) is never considered.
    Unusable evidence met here is noted for audit (:func:`unusable_mcp_evidence`).

    With ``retire=False`` (a lockfile governs cleanup) nothing is removed:
    kept entries and noted unusable evidence are announced again.

    *admitted* maps each runtime the manifest's ``targets:`` select to the
    declared servers it receives; a declared server's entry no admitted
    destination covers is kept and reported (#4784). Without *admitted*, it is
    derived from *manifest* and its MCP *dependencies*, unless an explicit
    *runtime* narrows this run (not the project); runtimes a ``--target`` flag
    selects for this run (*target_decision*) are admitted too.
    """
    if admitted is None and manifest is not None and runtime is None:
        admitted = _admitted(
            manifest,
            list(dependencies),
            set(declared),
            user_scope=user_scope,
            flagged=_flag_runtimes(target_decision, user_scope=user_scope),
        )
    try:
        store = McpWriteReceipts.open_for_install(
            project_root, user_scope=user_scope, logger=logger
        )
    except (OSError, ValueError):
        reason = unusable_mcp_evidence(project_root, user_scope=user_scope)
        if reason is None:
            _log.debug("No usable installation binding; every MCP entry is kept", exc_info=True)
        elif retire:
            _note_unusable(project_root, user_scope=user_scope, reason=reason)
            logger.warning(f"Kept every MCP entry APM no longer deploys: {reason}")
        elif _unusable_noted(project_root, user_scope=user_scope):
            logger.warning(f"Kept every MCP entry APM no longer deploys: {reason}")
        return
    _clear_unusable_note(project_root, user_scope=user_scope)
    if not retire:
        if admitted is not None:
            store.retarget(store.active(admitted, declared_configs or {}))
            store.save()
        included = store.included_runtimes(exclude)
        for write, _ in store.kept(set(declared), declared_configs):
            if write.runtime in included:
                store.warn_kept(write, logger)
        return
    removed = store.retire_undeclared(
        set(declared),
        exclude=exclude,
        logger=logger,
        declared_configs=declared_configs,
        admitted=admitted,
    )
    if removed:
        logger.record_mcp_removed(removed)


def _flag_runtimes(target_decision: Any, *, user_scope: bool) -> set[str]:
    """The runtimes a ``--target`` flag selects for this run (empty otherwise)."""
    if target_decision is None or not getattr(target_decision, "from_flag", False):
        return set()
    return set(target_decision.runtime_targets_for_scope(user_scope=user_scope) or ())


def _admitted(
    manifest: Any,
    dependencies: list[Any],
    declared: set[str],
    *,
    user_scope: bool,
    flagged: set[str] = frozenset(),
) -> dict[str, set[str]] | None:
    """Each runtime the manifest's ``targets:`` select, with the declared servers it receives.

    None when the manifest selects no targets (auto-detection never makes an
    entry look abandoned). A server no dependency declares (a local bundle's)
    names no ``targets:`` and reaches every selected runtime. Runtimes in
    *flagged* (selected by ``--target`` for this run) are selected too.
    """
    from apm_cli.install.mcp.integration import _declared_manifest_runtimes
    from apm_cli.integration.mcp_integrator import MCPIntegrator
    from apm_cli.integration.mcp_integrator_install import mcp_servers_for_runtime

    targeted = _declared_manifest_runtimes(manifest, user_scope=user_scope)
    if targeted is None:
        return None
    unscoped = declared - MCPIntegrator.get_server_names(dependencies)
    return {
        runtime: mcp_servers_for_runtime(dependencies, runtime) | unscoped
        for runtime in targeted | flagged
    }


def announce_kept_mcp_writes(
    lock_path: Path, *, project_root: Path, user_scope: bool, exclude: str | None, logger: Any
) -> None:
    """Announce-only: report kept entries against the lockfile's complete managed set.

    For install paths that end before the MCP stage's announcement (local
    bundles, ``--mcp NAME``, ``apm mcp install NAME``; #4861). Nothing is removed.
    """
    from apm_cli.deps.lockfile import LockFile
    from apm_cli.deps.lockfile_write_authority import lockfile_exists

    lock = LockFile.read(lock_path) if lockfile_exists(lock_path) else None
    retire_undeclared_mcp_writes(
        set(lock.mcp_servers) if lock is not None else set(),
        declared_configs=dict(lock.mcp_configs) if lock is not None else {},
        project_root=project_root,
        user_scope=user_scope,
        exclude=exclude,
        logger=logger,
        retire=False,
    )


def forget_removed_mcp_writes(project_root: Path, *, user_scope: bool) -> None:
    """After a cleanup, drop the records of entries no longer present (best effort).

    A record must never outlive its entry: a later identical entry is the user's.
    """
    try:
        store = McpWriteReceipts.open(project_root, user_scope=user_scope)
        store.forget_absent()
        store.save()
    except (OSError, ValueError):
        _log.debug("MCP write evidence left unchanged after cleanup", exc_info=True)


def unusable_mcp_evidence(
    project_root: Path, *, user_scope: bool, noted_only: bool = False
) -> str | None:
    """Read-only: why this installation's MCP write evidence exists but cannot be used.

    With *noted_only*, only evidence an install without a lockfile already met
    (and kept every entry for) is reported.
    """
    if noted_only and not _unusable_noted(project_root, user_scope=user_scope):
        return None
    try:
        McpWriteReceipts.open(project_root, user_scope=user_scope)
    except McpWriteEvidenceError as exc:
        return str(exc)
    except (OSError, ValueError) as exc:
        # Inaccessible or unsafe (symlinked) evidence is reported, never followed.
        path = _evidence_path(project_root, user_scope=user_scope)
        if path is not None and os.path.lexists(path):
            return f"Unusable MCP write evidence at {path}: {exc}"
    return None


def _unusable_note_path(project_root: Path, *, user_scope: bool) -> Path | None:
    path = _evidence_path(project_root, user_scope=user_scope)
    return None if path is None else path.with_name(path.name + ".unusable")


def _unusable_noted(project_root: Path, *, user_scope: bool) -> bool:
    """Whether an install without a lockfile kept entries because this evidence was unusable."""
    path = _unusable_note_path(project_root, user_scope=user_scope)
    return path is not None and os.path.lexists(path)


def _note_unusable(project_root: Path, *, user_scope: bool, reason: str) -> None:
    """Note that unusable evidence kept every entry, so audit reports it with a lockfile too."""
    from apm_cli.integration.write_seam import atomic_write_text
    from apm_cli.utils.path_security import ensure_path_within, has_symlink_component

    path = _unusable_note_path(project_root, user_scope=user_scope)
    if path is None or os.path.lexists(path):
        return
    # The evidence owner's containment anchor (above APM_HOME): no linked component.
    base = path.parent.parent.parent.parent
    try:
        ensure_path_within(path, base)
        if has_symlink_component(base, path, raise_on_error=True) or not path.parent.is_dir():
            return
        atomic_write_text(path, reason + "\n", new_file_mode=0o600, create_only=True)
    except (OSError, ValueError):
        _log.debug("Could not note unusable MCP write evidence", exc_info=True)


def _parse_writes(data: Any) -> list[McpWrite]:
    """The write records of evidence *data*; raises on any malformed record."""
    if not isinstance(data["writes"], list):
        raise TypeError("writes must be a list")
    return [McpWrite.from_row(row) for row in data["writes"]]


def _supersede_other_installation(project_root: Path, *, user_scope: bool) -> Path | None:
    """Rename evidence provably bound to another installation of this root; else None.

    The ledger owner decides provability (DeploymentMaterializationSession.
    another_installation). Anything unreadable, foreign or ambiguous is left in
    place and keeps refusing. The sibling materialization receipt that the same
    other installation wrote is set aside too: fresh MCP evidence creates this
    installation's identity, which that receipt could never bind again. Its
    records are never adopted.
    """
    from apm_cli.core.deployment_ledger import DeploymentMaterializationSession
    from apm_cli.core.scope import InstallScope

    scope = InstallScope.USER if user_scope else InstallScope.PROJECT
    try:
        session = DeploymentMaterializationSession(Path(project_root), scope, None)
        path = session.mcp_state_path
        data = _regular_json(path)
        if data is None:
            return None
        _parse_writes(data)  # malformed write records stay in place and keep refusing
        if not session.another_installation(data):
            return None  # this installation's own evidence, or not provably another's
        aside = _set_aside(path)
    except (OSError, ValueError, KeyError, TypeError, UnicodeError):
        return None
    if aside is not None:
        _set_aside_sibling(session)
    return aside


def _set_aside_sibling(session: Any) -> None:
    """Set aside the other installation's well-formed materialization receipt.

    Anything unreadable, malformed or not provably another installation's stays
    in place, where the ledger keeps reporting it; the MCP evidence already moved.
    """
    from apm_cli.core.deployment_ledger import DeploymentLedgerCodec

    try:
        data = _regular_json(session.state_path)
        if data is None or not session.another_installation(data):
            return
        records = DeploymentLedgerCodec.from_rows(data["records"]).records
        if len(records) != len(data["records"]) or any(
            not isinstance(data[name], dict) or set(data[name]) != set(records)
            for name in ("destinations", "bindings")
        ):
            return
        _set_aside(session.state_path)
    except (OSError, ValueError, KeyError, TypeError, UnicodeError):
        return


def _regular_json(path: Path) -> Any:
    if path.is_symlink() or not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _set_aside(path: Path) -> Path | None:
    """Rename *path* to a free ``.superseded`` name, journaled; never deleted."""
    from apm_cli.integration.write_seam import rename
    from apm_cli.utils.path_security import ensure_path_within, has_symlink_component

    base = path.parent.parent.parent.parent  # the evidence owner's anchor (_note_unusable)
    for n in range(100):
        aside = path.with_name(f"{path.name}.superseded" + (f".{n}" if n else ""))
        if not os.path.lexists(aside):
            break
    else:
        return None
    ensure_path_within(aside, base)
    if has_symlink_component(base, aside, raise_on_error=True):
        return None
    rename(path, aside)  # journaled: a failed install restores it (audit can still read it)
    return aside


def _clear_unusable_note(project_root: Path, *, user_scope: bool) -> None:
    """The evidence is usable (or removed) again: the note has nothing left to report."""
    path = _unusable_note_path(project_root, user_scope=user_scope)
    if path is None or not os.path.lexists(path):
        return
    try:
        path.unlink()
    except OSError:
        _log.debug("Could not clear the unusable MCP write evidence note", exc_info=True)


def _evidence_path(project_root: Path, *, user_scope: bool) -> Path | None:
    from apm_cli.core.deployment_ledger import materialization_evidence_paths
    from apm_cli.core.scope import InstallScope

    scope = InstallScope.USER if user_scope else InstallScope.PROJECT
    try:
        state_path, _ = materialization_evidence_paths(Path(project_root).resolve(), scope)
    except (OSError, ValueError):
        return None
    return state_path.parent / "mcp" / state_path.name


def _parses(config: str | None) -> bool:
    """Whether the config file at *config* is absent, empty or well-formed."""
    if config is None:
        return True
    path = Path(config)
    try:
        if not path.is_file():
            return True
        text = path.read_text(encoding="utf-8")
        if not text.strip():
            return True
        if path.suffix == ".toml":
            import tomllib

            tomllib.loads(text)
        elif path.suffix in {".yaml", ".yml"}:
            from apm_cli.utils.yaml_io import load_yaml_str

            load_yaml_str(text)
        elif path.suffix == ".json":
            json.loads(text)
    except Exception:
        return False
    return True


def kept_mcp_writes(
    project_root: Path,
    *,
    user_scope: bool,
    managed: Iterable[str],
    configs: dict[str, Any] | None = None,
) -> list[tuple[McpWrite, str]]:
    """Read-only: ``(record, config)`` of entries install kept that are still present.

    A server the lockfile manages again (*managed*) is not reported. Unusable
    evidence reports nothing here; install warns about it.
    """
    try:
        store = McpWriteReceipts.open(project_root, user_scope=user_scope)
    except (OSError, ValueError):
        return []
    return store.kept(set(managed), configs)
