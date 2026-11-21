"""Retire Codex registrations made by the removed app-server route (#3920).

Earlier APM versions registered a Codex compatibility bundle by running
``codex app-server`` and journaled what they owned in
``<APM home>/.codex-native/ownership.json``. Codex now receives every plugin
as a split install and APM never runs ``codex``. This module removes the
registrations that route still owns, once, through the Codex config owner,
and leaves every registration the user changed or created in place.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from apm_cli.core.scope import InstallScope
from apm_cli.deps.plugin_parser import _bounded_read_json
from apm_cli.utils.path_security import PathTraversalError, safe_rmtree


@dataclass(frozen=True)
class LegacyRetirement:
    """Plugin names retired or left to the user, or why nothing changed."""

    retired: tuple[str, ...] = ()
    kept: tuple[str, ...] = ()
    problem: str | None = None


def legacy_ledger_path(modules_dir: Path) -> Path:
    """The ownership journal the app-server route kept beside ``apm_modules``."""
    return modules_dir.parent / ".codex-native" / "ownership.json"


def _marketplace_name(modules_dir: Path, dependency_key: str) -> str:
    """Reproduce the route's marketplace name for an APM home and dependency."""
    identity = f"{modules_dir.resolve().as_posix()}\0{dependency_key}".encode()
    return "apm-" + hashlib.sha256(identity).hexdigest()[:24]


def _snapshot(value: Any) -> tuple[Path, dict[str, str]] | None:
    """Return a recorded snapshot path and the marketplace source APM wrote for it."""
    if not isinstance(value, dict):
        return None
    path, commit = value.get("path"), value.get("catalog_commit")
    if not isinstance(path, str) or not isinstance(commit, str) or not Path(path).is_absolute():
        return None
    snapshot = Path(path)
    return snapshot, {"source_type": "git", "source": snapshot.as_uri(), "ref": commit}


def retire_legacy_codex_registrations(modules_dir: Path, scope: Any) -> LegacyRetirement | None:
    """Remove the registrations the journal owns, then the journal itself.

    Returns ``None`` when there is nothing to migrate. Never runs ``codex`` and
    never touches Codex's plugin cache; only the documented ``config.toml``
    entries APM wrote, APM's own snapshots, and its journal are removed. A
    failure is reported, never raised: install and uninstall still complete,
    and the journal stays for the next run.
    """
    if scope is not InstallScope.USER:
        return None
    ledger = legacy_ledger_path(modules_dir)
    if not ledger.is_file():
        return None
    try:
        return _retire(modules_dir, ledger)
    except (OSError, ValueError, TypeError, AttributeError, PathTraversalError) as exc:
        return LegacyRetirement(problem=f"cannot retire using {ledger} ({exc})")


def _retire(modules_dir: Path, ledger: Path) -> LegacyRetirement:
    try:
        data = _bounded_read_json(ledger)
        records = data.get("plugins") if isinstance(data, dict) else None
        if not isinstance(records, dict) or data.get("version") != 1:
            raise ValueError("unrecognized format")
    except (OSError, ValueError) as exc:
        return LegacyRetirement(problem=f"cannot read {ledger} ({exc})")

    from apm_cli.adapters.client.codex import CodexClientAdapter

    adapter = CodexClientAdapter(user_scope=True)
    config_path = Path(adapter.get_config_path()).resolve()
    registrations: dict[str, tuple[str, list[dict[str, str]]]] = {}
    snapshots: dict[str, list[Path]] = {}
    kept: list[str] = []
    for key, record in records.items():
        name = record.get("name") if isinstance(record, dict) else None
        if not isinstance(name, str) or not name:
            return LegacyRetirement(problem=f"cannot read {ledger} (invalid record {key!r})")
        recorded = [
            item
            for item in (_snapshot(record.get("snapshot")), _snapshot(record.get("previous")))
            if item is not None
        ]
        recorded_config = record.get("config_path")
        if (
            not recorded
            or not isinstance(recorded_config, str)
            or Path(recorded_config).resolve() != config_path
        ):
            kept.append(name)
            continue
        marketplace = _marketplace_name(modules_dir, key)
        registrations[marketplace] = (name, [source for _path, source in recorded])
        snapshots[marketplace] = [path for path, _source in recorded]

    outcome = adapter.retire_plugin_registrations(registrations)
    if outcome is None:
        return LegacyRetirement(problem=f"cannot parse {config_path}")
    retired_names, kept_names = outcome
    snapshot_root = ledger.parent / "snapshots"
    for marketplace in sorted(retired_names):
        for path in snapshots[marketplace]:
            if path.exists():
                safe_rmtree(path, snapshot_root)
    kept.extend(registrations[marketplace][0] for marketplace in kept_names)
    ledger.unlink()
    for directory in (snapshot_root, ledger.parent):
        try:
            directory.rmdir()
        except OSError:
            break
    return LegacyRetirement(
        retired=tuple(sorted(registrations[name][0] for name in retired_names)),
        kept=tuple(sorted(kept)),
    )


def report_legacy_retirement(outcome: LegacyRetirement | None, logger: Any) -> None:
    """Tell the user what the one-time migration did; never silent when it acted."""
    if outcome is None or logger is None:
        return
    if outcome.problem:
        logger.warning(
            "Could not retire Codex plugin registrations from an earlier APM version: "
            f"{outcome.problem}. Fix the file and rerun 'apm install -g'; "
            "nothing was changed."
        )
        return
    if outcome.retired:
        logger.info(
            f"Retired {len(outcome.retired)} Codex plugin registration(s) made by an "
            f"earlier APM version: {', '.join(outcome.retired)}. "
            "APM now installs Codex plugins as separate primitives and never runs codex."
        )
    if outcome.kept:
        logger.warning(
            f"Left {len(outcome.kept)} changed Codex plugin registration(s) in place: "
            f"{', '.join(outcome.kept)}. APM no longer manages them, and an enabled one "
            "loads alongside the split install; remove them in Codex if not needed."
        )
