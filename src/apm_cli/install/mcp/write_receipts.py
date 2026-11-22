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

When ``apm.lock.yaml`` is absent, install retires an entry this evidence
proves APM wrote and no longer deploys (:func:`retire_undeclared_mcp_writes`).
An entry it cannot prove (evidence unusable, or the entry changed since APM
wrote it) is kept, reported, and marked kept; ``apm audit --ci`` reports it
(:func:`kept_mcp_writes`) until the entry is removed or declared again.
"""

from __future__ import annotations

import json
import logging
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
        return {
            "runtime": self.runtime,
            "name": self.name,
            "config": self.config,
            "key": self.key,
            "hash": self.content_hash,
            "kept": self.kept,
        }

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
            store = McpWriteReceipts.open(project_root, user_scope=user_scope)
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
        self.writes: dict[tuple[str, str], McpWrite] = {}
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
                for row in data["writes"]:
                    write = McpWrite.from_row(row)
                    store.writes[(write.runtime, write.name)] = write
            except (KeyError, TypeError, UnicodeError, ValueError) as exc:
                raise McpWriteEvidenceError(
                    f"Invalid MCP write evidence at {store.path}: {exc}. The entries it lists "
                    "are kept; remove that file to start a new record."
                ) from exc
        return store

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

    def _present(self, write: McpWrite) -> bool:
        return (write.key or write.name) in (self._existing(write.runtime) or {})

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
            self.writes[(runtime, name)] = McpWrite(
                runtime, name, self._config(runtime), key, content_hash
            )
            self._dirty = True

    def forget_absent(self) -> None:
        """Drop records whose entry is no longer in its config: nothing remains to prove."""
        for ident, write in list(self.writes.items()):
            if self._existing(write.runtime) is not None and not self._present(write):
                del self.writes[ident]
                self._dirty = True

    def proven(self, write: McpWrite) -> bool:
        """Whether the entry *write* names is still exactly what APM wrote there."""
        return (
            write.usable
            and write.config == self._config(write.runtime)
            and self._fingerprint(write.runtime, write.key or "") == write.content_hash
        )

    def mark_kept(self, write: McpWrite) -> None:
        if not write.kept:
            self.writes[(write.runtime, write.name)] = replace(write, kept=True)
            self._dirty = True

    def forget(self, write: McpWrite) -> None:
        if self.writes.pop((write.runtime, write.name), None) is not None:
            self._dirty = True

    def retire_undeclared(
        self, declared: set[str], *, exclude: str | None, logger: Any
    ) -> set[str]:
        """Remove proven entries of undeclared servers; keep and report the rest.

        Returns the names of the servers whose entries were removed.
        """
        from apm_cli.integration.mcp_integrator_install import _remove_native_entry

        removed: set[str] = set()
        for _, write in sorted(self.writes.items()):
            if write.name in declared or write.runtime == exclude:
                continue
            if self._existing(write.runtime) is None:
                logger.warning(
                    f"Kept MCP server '{write.name}' in {self.display(write)}: APM cannot "
                    "read that config to confirm the entry, so its record is kept."
                )
                continue
            if not self._present(write):
                self.forget(write)
            elif self.proven(write):
                _remove_native_entry(self._client(write.runtime), write.key, logger)
                self._entries.clear()
                if self._existing(write.runtime) is not None and not self._present(write):
                    self.forget(write)
                    removed.add(write.name)
                else:
                    self.mark_kept(write)
                    logger.warning(
                        f"Kept MCP server '{write.name}' in {self.display(write)}: APM could "
                        "not confirm its removal. Remove the entry if you no longer use it."
                    )
            else:
                self.mark_kept(write)
                logger.warning(
                    f"Kept MCP server '{write.name}' in {self.display(write)}: APM no longer "
                    "deploys it and cannot confirm the entry is the one it wrote. "
                    "Remove the entry if you no longer use it."
                )
        self.save()
        return removed

    def kept(self, managed: set[str]) -> list[tuple[McpWrite, str]]:
        """Kept records of unmanaged servers whose entry is still present."""
        return [
            (write, self.display(write))
            for _, write in sorted(self.writes.items())
            if write.kept and write.name not in managed and self._present(write)
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
    project_root: Path,
    user_scope: bool,
    exclude: str | None,
    logger: Any,
) -> None:
    """Without a lockfile, retire the entries APM wrote for servers it no longer deploys.

    An entry is removed only when this installation's evidence proves it is
    still exactly what APM wrote. Any other recorded entry is kept, reported
    and marked kept for ``apm audit --ci``; an entry with no record (the
    user's own, even when identical to a declaration) is never considered.
    """
    try:
        store = McpWriteReceipts.open(project_root, user_scope=user_scope)
    except McpWriteEvidenceError as exc:
        logger.warning(f"Kept every MCP entry APM no longer deploys: {exc}")
        return
    except (OSError, ValueError):
        _log.debug("No usable installation binding; every MCP entry is kept", exc_info=True)
        return
    removed = store.retire_undeclared(set(declared), exclude=exclude, logger=logger)
    if removed:
        logger.record_mcp_removed(removed)


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


def unusable_mcp_evidence(project_root: Path, *, user_scope: bool) -> str | None:
    """Read-only: why this installation's MCP write evidence exists but cannot be used."""
    try:
        McpWriteReceipts.open(project_root, user_scope=user_scope)
    except McpWriteEvidenceError as exc:
        return str(exc)
    except (OSError, ValueError):
        return None
    return None


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
            import yaml

            yaml.safe_load(text)
        elif path.suffix == ".json":
            json.loads(text)
    except Exception:
        return False
    return True


def kept_mcp_writes(
    project_root: Path, *, user_scope: bool, managed: Iterable[str]
) -> list[tuple[McpWrite, str]]:
    """Read-only: ``(record, config)`` of entries install kept that are still present.

    A server the lockfile manages again (*managed*) is not reported. Unusable
    evidence reports nothing here; install warns about it.
    """
    try:
        store = McpWriteReceipts.open(project_root, user_scope=user_scope)
    except (OSError, ValueError):
        return []
    return store.kept(set(managed))
