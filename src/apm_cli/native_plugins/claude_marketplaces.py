"""Claude Code's user-level marketplace registry for project-scope plugins (#5176).

A project declares its native plugins under a relative marketplace path
(``./.agents/plugins``) so every clone agrees. Claude Code loads a plugin's
skills and hooks in a session only for marketplaces listed in its user-level
``plugins/known_marketplaces.json``; it adds a project's marketplace there
itself only after a first session, so that first session (any CI run,
``claude -p``, a fresh clone) loads the plugin's MCP server but not its skills
or hooks. ``apm install`` therefore registers the project marketplace there
with its absolute path, per machine.

The write merges one entry and never touches another. APM records each entry
it wrote in a ledger under ``$APM_HOME``, so it removes only an entry it wrote
and that still names the same path; an entry Claude wrote stays.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from apm_cli.utils.atomic_io import atomic_write_text

_LEDGER_NAME = "claude-known-marketplaces.json"


def known_marketplaces_path() -> Path:
    """Claude's marketplace registry, under ``CLAUDE_CONFIG_DIR`` (default ``~/.claude``)."""
    from apm_cli.integration.targets import resolve_user_root
    from apm_cli.native_plugins.scopes import CLAUDE

    return resolve_user_root(CLAUDE) / "plugins" / "known_marketplaces.json"


def _ledger_path() -> Path:
    from apm_cli.core.scope import get_apm_home

    return get_apm_home() / _LEDGER_NAME


def _read_object(path: Path) -> dict[str, Any] | None:
    """The JSON object at *path*: ``{}`` when absent, ``None`` when unreadable or not one."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError):
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _write_object(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(data, indent=2) + "\n")


def _registered_path(entry: Any) -> Any:
    source = entry.get("source") if isinstance(entry, dict) else None
    return source.get("path") if isinstance(source, dict) else None


def _owned() -> dict[str, str]:
    ledger = _read_object(_ledger_path()) or {}
    entries = ledger.get("entries")
    return dict(entries) if isinstance(entries, dict) else {}


def _record(owned: dict[str, str]) -> None:
    path = _ledger_path()
    if owned:
        _write_object(path, {"version": 1, "entries": owned})
    elif path.exists():
        path.unlink()


def _warn(logger: Any, message: str) -> None:
    if logger is not None:
        logger.warning(message)


def register_project_marketplace(name: str, root: Path, logger: Any = None) -> None:
    """List project marketplace *name* in Claude's registry at absolute *root*.

    Idempotent: an entry already naming *root* (APM's or Claude's own) is left
    byte for byte. An unreadable registry is left alone with a warning; the
    committed relative declaration still loads from the second session on.
    """
    target = known_marketplaces_path()
    location = str(Path(root).absolute())
    known = _read_object(target)
    if known is None:
        _warn(
            logger,
            f"{target} is not a readable JSON object, so APM did not register marketplace "
            f"'{name}' there; Claude Code loads the plugins' skills and hooks only from its "
            "second session in this project.",
        )
        return
    if _registered_path(known.get(name)) == location:
        return
    now = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    known[name] = {
        "source": {"source": "directory", "path": location},
        "installLocation": location,
        "lastUpdated": now,
    }
    try:
        _write_object(target, known)
        _record({**_owned(), name: location})
    except OSError as exc:
        _warn(logger, f"Could not register marketplace '{name}' in {target}: {exc}")


def unregister_project_marketplace(name: str, root: Path, logger: Any = None) -> None:
    """Remove APM's registry entry for *name* at *root*, and nothing else."""
    location = str(Path(root).absolute())
    owned = _owned()
    if owned.get(name) != location:
        return
    target = known_marketplaces_path()
    known = _read_object(target)
    try:
        if known is not None and _registered_path(known.get(name)) == location:
            del known[name]
            _write_object(target, known)
        del owned[name]
        _record(owned)
    except OSError as exc:
        _warn(logger, f"Could not remove marketplace '{name}' from {target}: {exc}")


WORKTREE_NOTICE = (
    "This checkout is a git worktree: Claude Code may load the main checkout's copy of "
    "its native plugins (anthropics/claude-code#111812). See "
    "https://microsoft.github.io/apm/consumer/install-plugins/#known-gaps"
)


def _is_linked_worktree(checkout: Path) -> bool:
    """A linked git worktree keeps a ``.git`` file pointing at the main repository."""
    return (Path(checkout) / ".git").is_file()


def sync_project_marketplace(
    name: str, root: Path, *, user_scope: bool, claude_declared: bool, logger: Any = None
) -> None:
    """Register *name* while Claude declares project plugins from it; unregister after."""
    if user_scope:
        return
    if not claude_declared:
        unregister_project_marketplace(name, root, logger)
        return
    register_project_marketplace(name, root, logger)
    if _is_linked_worktree(Path(root).parents[1]):
        _warn(logger, WORKTREE_NOTICE)
