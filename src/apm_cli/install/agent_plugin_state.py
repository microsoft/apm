"""Canonical retained roots and state projection for installed Agent Plugins."""

from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ..core.deployment_ledger import (
    DeploymentLedgerCodec,
    DeploymentOwnerReplacement,
)
from ..deps.lockfile import (
    InstalledPluginComponentFact,
    InstalledPluginRecord,
    InstalledPluginRecordCodec,
)
from ..utils.path_security import PathTraversalError, ensure_path_within, safe_rmtree

if TYPE_CHECKING:
    from ..agent_plugins.ir import AgentPluginIdentity
    from ..core.deployment_state import DeploymentRecord
    from ..deps.lockfile import LockFile
    from ..models.validation import ValidationResult


@dataclass(frozen=True, slots=True)
class AgentPluginRootLayout:
    """Canonical retained PLUGIN_ROOT and persistent PLUGIN_DATA paths."""

    storage_key: str
    state_base: Path
    plugin_root: Path
    data_root: Path


@dataclass(slots=True)
class PreparedAgentPluginRoot:
    """A staged PLUGIN_ROOT replacement that can commit, roll back, or finalize."""

    staging_root: Path
    state_base: Path
    plugin_root: Path
    data_root: Path
    backup_root: Path
    had_existing_root: bool
    committed: bool = False
    finalized: bool = False

    def _validate_paths(self) -> None:
        """Reject managed-path symlinks before every filesystem mutation."""
        _managed_path(self.staging_root, self.state_base)
        _managed_path(self.plugin_root, self.state_base)
        _managed_path(self.data_root, self.state_base)
        _managed_path(self.backup_root, self.state_base)

    def commit(self) -> None:
        """Activate the staged root while retaining the prior root for rollback."""
        if self.committed:
            raise RuntimeError("Agent Plugin root transaction is already committed")
        if self.finalized:
            raise RuntimeError("Agent Plugin root transaction is already finalized")
        self._validate_paths()
        parent = self.plugin_root.parent
        backed_up = False
        activated = False
        try:
            if self.had_existing_root:
                os.replace(self.plugin_root, self.backup_root)
                backed_up = True
            os.replace(self.staging_root, self.plugin_root)
            activated = True
            self.data_root.mkdir(parents=True, exist_ok=True)
        except OSError:
            if activated and self.plugin_root.exists():
                safe_rmtree(self.plugin_root, parent)
            if backed_up and self.backup_root.exists():
                os.replace(self.backup_root, self.plugin_root)
            raise
        self.committed = True

    def rollback(self) -> None:
        """Restore the prior PLUGIN_ROOT and leave persistent PLUGIN_DATA intact."""
        if self.finalized:
            raise RuntimeError("Finalized Agent Plugin root transaction cannot be rolled back")
        self._validate_paths()
        parent = self.plugin_root.parent
        if self.staging_root.exists():
            safe_rmtree(self.staging_root, parent)
        if not self.committed:
            return
        if self.plugin_root.exists():
            safe_rmtree(self.plugin_root, parent)
        if self.had_existing_root and self.backup_root.exists():
            os.replace(self.backup_root, self.plugin_root)
        self.committed = False

    def finalize(self) -> None:
        """Discard the rollback root after all related state commits succeed."""
        if self.finalized:
            raise RuntimeError("Agent Plugin root transaction is already finalized")
        if not self.committed:
            raise RuntimeError("Agent Plugin root transaction is not committed")
        self._validate_paths()
        if self.backup_root.exists():
            safe_rmtree(self.backup_root, self.plugin_root.parent)
        self.finalized = True


@dataclass(slots=True)
class PreparedInstalledPluginState:
    """Prepared root, record, and ledger replacement with explicit rollback."""

    lockfile: LockFile
    root: PreparedAgentPluginRoot
    prior_records: dict[str, InstalledPluginRecord]
    replacement_records: dict[str, InstalledPluginRecord]
    ledger: DeploymentOwnerReplacement
    committed: bool = False
    finalized: bool = False

    def commit(self) -> None:
        """Commit root, record, and exact ledger ownership as one helper."""
        if self.committed:
            raise RuntimeError("Installed Agent Plugin state is already committed")
        self.root.commit()
        self.lockfile.replace_installed_plugins(self.replacement_records)
        try:
            DeploymentLedgerCodec.commit_owner_replacement(self.lockfile, self.ledger)
        except (RuntimeError, ValueError):
            self.lockfile.replace_installed_plugins(self.prior_records)
            self.root.rollback()
            raise
        self.committed = True

    def rollback(self) -> None:
        """Restore the prior record, PLUGIN_ROOT, and deployment ledger."""
        if self.finalized:
            raise RuntimeError("Finalized installed Agent Plugin state cannot be rolled back")
        if not self.committed:
            self.root.rollback()
            return
        DeploymentLedgerCodec.validate_owner_rollback(self.lockfile, self.ledger)
        self.root.rollback()
        DeploymentLedgerCodec.rollback_owner_replacement(self.lockfile, self.ledger)
        self.lockfile.replace_installed_plugins(self.prior_records)
        self.committed = False

    def finalize(self) -> None:
        """Discard the prior root once the caller no longer needs rollback."""
        if self.finalized:
            raise RuntimeError("Installed Agent Plugin state is already finalized")
        if not self.committed:
            raise RuntimeError("Installed Agent Plugin state is not committed")
        self.root.finalize()
        self.finalized = True


def _managed_path(path: Path, state_base: Path) -> Path:
    """Return a canonical managed path after containment and symlink checks."""
    resolved_base = state_base.resolve()
    lexical_candidate = path if path.is_absolute() else resolved_base / path
    for ancestor in (lexical_candidate, *lexical_candidate.parents):
        if ancestor.is_symlink():
            raise PathTraversalError(
                f"Managed Agent Plugin path '{lexical_candidate}' cannot contain symbolic links"
            )
        if ancestor == resolved_base:
            break
    resolved_candidate = ensure_path_within(lexical_candidate, resolved_base)
    current = resolved_candidate
    while current != resolved_base:
        if current.is_symlink():
            raise PathTraversalError(
                f"Managed Agent Plugin path '{resolved_candidate}' cannot contain symbolic links"
            )
        parent = current.parent
        if parent == current:
            raise PathTraversalError(
                f"Managed Agent Plugin path '{resolved_candidate}' has no contained ancestor"
            )
        current = parent
    return resolved_candidate


def resolve_agent_plugin_roots(
    project_root: Path,
    *,
    global_: bool,
    identity: AgentPluginIdentity,
) -> AgentPluginRootLayout:
    """Resolve stable roots from canonical ``AgentPlugin.identity`` only."""
    storage_key = InstalledPluginRecordCodec.storage_key(identity.name)
    scope = "user" if global_ else "project"
    logical_plugin, logical_data = InstalledPluginRecordCodec.root_values(identity.name, scope)
    state_base = (
        Path(os.environ.get("APM_HOME", str(Path.home() / ".apm"))) if global_ else project_root
    ).resolve()
    plugin_root = _managed_path(state_base / logical_plugin, state_base)
    data_root = _managed_path(state_base / logical_data, state_base)
    return AgentPluginRootLayout(
        storage_key=storage_key,
        state_base=state_base,
        plugin_root=plugin_root,
        data_root=data_root,
    )


def prepare_agent_plugin_root(
    source_dir: Path,
    project_root: Path,
    *,
    global_: bool,
    identity: AgentPluginIdentity,
) -> PreparedAgentPluginRoot:
    """Copy plugin bytes into an owned staging root without activating them."""
    layout = resolve_agent_plugin_roots(
        project_root,
        global_=global_,
        identity=identity,
    )
    parent = layout.plugin_root.parent
    parent.mkdir(parents=True, exist_ok=True)
    staging_root = Path(tempfile.mkdtemp(prefix=f".{layout.storage_key}-", dir=parent))
    backup_root = parent / f".{layout.storage_key}.previous"
    ensure_path_within(staging_root, parent)
    ensure_path_within(backup_root, parent)
    if backup_root.exists():
        if layout.plugin_root.exists():
            safe_rmtree(backup_root, parent)
        else:
            os.replace(backup_root, layout.plugin_root)
    try:
        shutil.copytree(source_dir, staging_root, dirs_exist_ok=True, symlinks=True)
        if staging_root.is_symlink() or any(path.is_symlink() for path in staging_root.rglob("*")):
            raise ValueError("Agent Plugin bundles cannot contain symbolic links")
    except (OSError, shutil.Error, ValueError):
        safe_rmtree(staging_root, parent)
        raise
    return PreparedAgentPluginRoot(
        staging_root=staging_root,
        state_base=layout.state_base,
        plugin_root=layout.plugin_root,
        data_root=layout.data_root,
        backup_root=backup_root,
        had_existing_root=layout.plugin_root.exists(),
    )


def project_installed_plugin_record(
    validation: ValidationResult,
    project_root: Path,
    *,
    global_: bool,
    source_kind: str,
    source_locator: str,
    resolved_ref: str | None = None,
    source_digest: str | None = None,
    prior_record: InstalledPluginRecord | None = None,
) -> InstalledPluginRecord:
    """Project canonical validation IR into the sole installed-state record."""
    plugin = validation.agent_plugin
    package = validation.package
    if not validation.is_valid or plugin is None or package is None:
        raise ValueError("Installed Agent Plugin state requires a valid canonical ValidationResult")
    if package.agent_plugin is not plugin:
        raise ValueError("ValidationResult package discarded its canonical Agent Plugin IR")
    if package.name != plugin.identity.name:
        raise ValueError("Projected package identity differs from AgentPlugin.identity")

    components: list[InstalledPluginComponentFact] = []
    for skill in plugin.components.skills:
        relative_path = skill.root.relative_to(plugin.root).as_posix()
        components.append(
            InstalledPluginComponentFact(
                kind="skill",
                name=skill.name,
                relative_path=relative_path,
            )
        )
    for server in plugin.components.mcp_servers:
        components.append(
            InstalledPluginComponentFact(
                kind="mcp",
                name=server.name,
                metadata=(("transport", server.server_type.value),),
            )
        )
    if plugin.apm_extension is not None:
        components.append(
            InstalledPluginComponentFact(
                kind="apm-extension",
                name="com.microsoft.apm",
                metadata=(("schema_version", plugin.apm_extension.schema_version),),
            )
        )

    record = InstalledPluginRecordCodec.build(
        identity=plugin.identity.name,
        version=plugin.identity.version,
        source_kind=source_kind,
        source_locator=source_locator,
        resolved_ref=resolved_ref,
        source_digest=source_digest,
        scope="user" if global_ else "project",
        components=tuple(components),
        prior_record=prior_record,
    )
    resolve_installed_plugin_record_roots(record, project_root)
    return record


def prepare_installed_plugin_state(
    validation: ValidationResult,
    source_dir: Path,
    project_root: Path,
    lockfile: LockFile,
    *,
    global_: bool,
    source_kind: str,
    source_locator: str,
    resolved_ref: str | None = None,
    source_digest: str | None = None,
    owned_records: tuple[DeploymentRecord, ...] = (),
) -> PreparedInstalledPluginState:
    """Prepare an unwired installed-state transition for a validated plugin."""
    plugin = validation.agent_plugin
    if plugin is None:
        raise ValueError("Installed Agent Plugin state requires canonical plugin IR")
    try:
        source_root = source_dir.resolve(strict=True)
        validated_root = plugin.root.resolve(strict=True)
    except OSError as exc:
        raise ValueError("Installed Agent Plugin source root must exist") from exc
    if source_root != validated_root:
        raise ValueError("Installed Agent Plugin source root differs from validated plugin root")
    prior_records = dict(lockfile.installed_plugins)
    folded_matches = [
        record
        for identity, record in prior_records.items()
        if identity.casefold() == plugin.identity.name.casefold()
    ]
    if len(folded_matches) > 1 or (
        folded_matches and folded_matches[0].identity != plugin.identity.name
    ):
        raise ValueError("Installed Agent Plugin identity is case-fold ambiguous")
    prior_record = folded_matches[0] if folded_matches else None
    record = project_installed_plugin_record(
        validation,
        project_root,
        global_=global_,
        source_kind=source_kind,
        source_locator=source_locator,
        resolved_ref=resolved_ref,
        source_digest=source_digest,
        prior_record=prior_record,
    )
    replacement_records = dict(prior_records)
    replacement_records[record.identity] = record
    root = prepare_agent_plugin_root(
        plugin.root,
        project_root,
        global_=global_,
        identity=plugin.identity,
    )
    ledger = DeploymentLedgerCodec.prepare_owner_replacement(
        DeploymentLedgerCodec.from_lockfile(lockfile),
        record.owner_key,
        owned_records,
    )
    return PreparedInstalledPluginState(
        lockfile=lockfile,
        root=root,
        prior_records=prior_records,
        replacement_records=replacement_records,
        ledger=ledger,
    )


def resolve_installed_plugin_record_roots(
    record: InstalledPluginRecord,
    project_root: Path,
) -> AgentPluginRootLayout:
    """Resolve persisted logical roots against a trusted project or APM_HOME."""
    state_base = (
        Path(os.environ.get("APM_HOME", str(Path.home() / ".apm")))
        if record.scope == "user"
        else project_root
    ).resolve()
    plugin_root = _managed_path(state_base / record.plugin_root, state_base)
    data_root = _managed_path(state_base / record.data_root, state_base)
    return AgentPluginRootLayout(
        storage_key=InstalledPluginRecordCodec.storage_key(record.identity),
        state_base=state_base,
        plugin_root=plugin_root,
        data_root=data_root,
    )


def remove_installed_plugin_root(
    record: InstalledPluginRecord,
    project_root: Path,
) -> None:
    """Remove APM-owned PLUGIN_ROOT while retaining stable PLUGIN_DATA."""
    layout = resolve_installed_plugin_record_roots(record, project_root)
    if layout.plugin_root.exists():
        safe_rmtree(layout.plugin_root, layout.plugin_root.parent)
