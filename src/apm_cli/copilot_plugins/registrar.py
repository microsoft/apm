"""GitHub Copilot's side of native plugin activation (#2703, #3921).

Copilot loads natively activated plugins from the shared profile root,
declared by the one shared owner,
:func:`apm_cli.claude_plugins.registrar.reconcile_native_plugins`, through
the shared settings writer. This module keeps two things:

* :func:`candidates_from_lockfile`, the executable trust gate over locked
  state that other native routes share;
* :func:`plan_legacy_copilot_retirement`, which finds the registration
  earlier releases made (a marketplace named ``apm`` pointing at
  ``apm_modules``, its generated catalog and the ``apm-registration.json``
  ledger beside it) so the shared owner can retire it inside its own
  transaction. Ownership now comes from ``apm.lock.yaml`` alone.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..native_plugins.settings import (
    ENABLED_KEY,
    MARKETPLACES_KEY,
    NativeSettingsError,
    read_settings,
    write_settings_document,
)
from ..utils.path_security import has_symlink_component
from .constants import (
    APM_MARKETPLACE_NAME,
    MARKETPLACE_MANIFEST_RELATIVE,
    PROJECT_SETTINGS_LOCAL_RELATIVE,
    PROJECT_SETTINGS_SHARED_RELATIVE,
    REGISTRATION_LEDGER_RELATIVE,
    USER_SETTINGS_FILENAME,
)
from .settings import copilot_home, read_ledger


@dataclass(frozen=True, slots=True)
class ResolvedPluginCandidate:
    """One resolved dependency whose bytes may hold an Agent Plugin."""

    dependency_key: str
    install_path: Path
    direct: bool = False
    target_subset: tuple[str, ...] | None = None
    exec_status: str | None = None


def candidates_from_lockfile(lockfile: Any, modules_dir: Path) -> list[ResolvedPluginCandidate]:
    """Return registration candidates from canonical locked state.

    The synthesized self-entry (dependency key ``.``) is never a registration
    candidate; every other consumer of the dependency map filters it too.
    Direct dependencies (no declaring parent, not resolved by a parent repo)
    and any per-dependency target subset flow through so precedence and target
    narrowing decisions have the same inputs as the install path.

    This is the ONE executable trust gate shared by install and every lifecycle
    command (uninstall, prune, restore): a locked entry whose executables the
    scanner denied or left pending approval (``exec_status`` of
    :data:`~apm_cli.security.executables.TRUST_DENIED` /
    :data:`~apm_cli.security.executables.TRUST_GATED`) is dropped here, so a
    resync can never silently enable an MCP server the install refused. The
    in-flight ``deps_to_install`` overlay is gated separately by the install
    phase, since its exec status is not yet persisted to the lockfile.
    """
    from ..deps.lockfile import _SELF_KEY
    from ..security.executables import REGISTRABLE_EXEC_STATUSES

    candidates: list[ResolvedPluginCandidate] = []
    for dep_key, locked in sorted(getattr(lockfile, "dependencies", {}).items()):
        if dep_key == _SELF_KEY:
            continue
        exec_status = getattr(locked, "exec_status", None)
        # Allowlist, not denylist: a status APM does not recognise is never
        # registrable, matching the severity ladder that ranks unknown above
        # `denied`.
        if exec_status not in REGISTRABLE_EXEC_STATUSES:
            continue
        to_reference = getattr(locked, "to_dependency_ref", None)
        if to_reference is None:
            continue
        try:
            install_path = to_reference().get_install_path(modules_dir)
        except (ValueError, AttributeError):
            continue
        target_subset = getattr(locked, "target_subset", None)
        direct = (
            getattr(locked, "declaring_parent", None) is None
            and getattr(locked, "resolved_by", None) is None
        )
        candidates.append(
            ResolvedPluginCandidate(
                dependency_key=dep_key,
                install_path=Path(install_path),
                direct=direct,
                target_subset=tuple(target_subset) if target_subset else None,
                exec_status=exec_status,
            )
        )
    return candidates


def _legacy_settings_paths(ledger: Any, project_root: Path, user_scope: bool) -> list[Path]:
    """Every settings file an earlier release may have registered ``apm`` in."""
    if user_scope:
        paths = [copilot_home() / USER_SETTINGS_FILENAME]
    else:
        paths = [
            project_root / PROJECT_SETTINGS_LOCAL_RELATIVE,
            project_root / PROJECT_SETTINGS_SHARED_RELATIVE,
        ]
    recorded = ledger.settings_path
    if recorded is not None:
        recorded = recorded if recorded.is_absolute() else project_root / recorded
        if recorded.name in {path.name for path in paths} and recorded not in paths:
            paths.append(recorded)
    return paths


def _points_at(value: Any, modules_dir: Path, project_root: Path) -> bool:
    """Whether a marketplace entry is a directory source for *modules_dir*."""
    source = value.get("source") if isinstance(value, dict) else None
    where = source.get("path") if isinstance(source, dict) else None
    if not isinstance(where, str) or not where:
        return False
    candidate = Path(where)
    if not candidate.is_absolute():
        candidate = project_root / candidate
    try:
        return candidate.resolve() == modules_dir.resolve()
    except OSError:
        return False


def _retired_document(
    settings: Path, ledger_keys: frozenset[str], modules_dir: Path, project_root: Path
) -> tuple[dict[str, Any], frozenset[str]] | None:
    """The document without the legacy ``apm`` marketplace and the ``true`` keys it enabled.

    Returns ``None`` unless the file's ``apm`` marketplace provably points at
    *modules_dir*; also returns the plugin names whose keys it drops.
    """
    if not settings.is_file() or settings.is_symlink():
        return None
    try:
        document = read_settings(settings)
    except NativeSettingsError:
        # Not provably ours; the shared writer fails closed on the files it writes.
        return None
    marketplaces = document.get(MARKETPLACES_KEY)
    enabled = document.get(ENABLED_KEY)
    if not isinstance(marketplaces, dict) or not _points_at(
        marketplaces.get(APM_MARKETPLACE_NAME), modules_dir, project_root
    ):
        return None
    del marketplaces[APM_MARKETPLACE_NAME]
    if not marketplaces:
        document.pop(MARKETPLACES_KEY)
    dropped: set[str] = set()
    if isinstance(enabled, dict):
        suffix = f"@{APM_MARKETPLACE_NAME}"
        for key in sorted(enabled):
            # An explicit false stays: it is the user's decision.
            if (key.endswith(suffix) or key in ledger_keys) and enabled[key] is True:
                del enabled[key]
                dropped.add(key.split("@", 1)[0])
        if not enabled:
            document.pop(ENABLED_KEY)
    return document, frozenset(dropped)


@dataclass
class LegacyRegistration:
    """What an earlier release registered with Copilot from ``apm_modules``.

    Planned before any write, applied inside the shared owner's transaction
    (settings through the shared writer, restored on rollback), and cleaned
    up (ledger and catalog) only after that transaction commits.
    """

    documents: dict[Path, dict[str, Any]]
    plugins: frozenset[str]
    generated: tuple[Path, ...]
    modules_dir: Path
    unreadable_ledger: Path | None = None

    @property
    def settings_files(self) -> tuple[Path, ...]:
        return tuple(self.documents)

    def apply(self) -> None:
        """Write the legacy-free documents; the caller snapshotted them first."""
        for path, document in self.documents.items():
            write_settings_document(path, document)

    def finish(self, logger: Any = None) -> None:
        """After the commit: delete the ledger and the generated catalog."""
        for generated in self.generated:
            generated.unlink(missing_ok=True)
        catalog = self.modules_dir / MARKETPLACE_MANIFEST_RELATIVE
        for directory in (catalog.parent, catalog.parent.parent):
            if directory.is_dir() and not any(directory.iterdir()):
                directory.rmdir()
        if logger is not None:
            logger.tree_item(
                "Moved the GitHub Copilot plugin registration from apm_modules to .agents/plugins."
            )

    def keep_message(self, reason: str) -> str:
        names = ", ".join(sorted(self.plugins)) or "its plugins"
        from apm_cli.core.remediation import COPILOT_PLUGIN_REGISTRATION_STALE

        return (
            f"GitHub Copilot still loads {names} through the registration an earlier APM "
            f"wrote in {self.modules_dir}; APM left it unchanged because {reason}. "
            + COPILOT_PLUGIN_REGISTRATION_STALE.sentence()
        )


def plan_legacy_copilot_retirement(
    *, modules_dir: Path, project_root: Path, user_scope: bool
) -> LegacyRegistration | None:
    """Find an ``apm-registration.json`` registration to migrate, without writing.

    The legacy marketplace counts only where it provably points at this
    *modules_dir*. A ledger that exists but cannot be read is reported, never
    deleted, so its registration is left for the user to review.
    """
    modules_dir = Path(modules_dir)
    project_root = Path(project_root)
    ledger_path = modules_dir / REGISTRATION_LEDGER_RELATIVE
    catalog_path = modules_dir / MARKETPLACE_MANIFEST_RELATIVE
    if has_symlink_component(modules_dir, ledger_path):
        return None
    ledger = read_ledger(ledger_path)
    documents: dict[Path, dict[str, Any]] = {}
    plugins: set[str] = set()
    for settings in _legacy_settings_paths(ledger, project_root, user_scope):
        retired = _retired_document(
            settings, frozenset(ledger.enabled_plugins), modules_dir, project_root
        )
        if retired is not None:
            documents[settings], dropped = retired
            plugins |= dropped
    generated = tuple(path for path in (ledger_path, catalog_path) if path.is_file())
    if not documents and not generated:
        return None
    return LegacyRegistration(
        documents,
        frozenset(plugins),
        generated,
        modules_dir,
        unreadable_ledger=ledger_path if ledger.unreadable else None,
    )
