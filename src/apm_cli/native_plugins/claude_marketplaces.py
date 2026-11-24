"""Claude Code's user-level marketplace registry for project-scope plugins (#5176).

A project declares its native plugins under a relative marketplace path
(``./.agents/plugins``) so every clone agrees. Claude Code loads a plugin's
skills and hooks in a session only for marketplaces listed in its user-level
``plugins/known_marketplaces.json``; it adds a project's marketplace there
itself only after a first session, so that first session (any CI run,
``claude -p``, a fresh clone) loads the plugin's MCP server but not its skills
or hooks. ``apm install`` therefore lists the project marketplace there with
the installing checkout's absolute path, per machine.

The registry is user-level state, so APM only writes an entry it owns for this
checkout: it adds the entry when none exists under the name, keeps an entry
that already names this checkout as is, and keeps any other entry (another
clone or worktree, or Claude's own) byte for byte with a warning. Every other
entry is untouched. APM records each entry it wrote in a receipt under
``$APM_HOME``, so it removes only an entry it wrote that still names the same
root. A linked registry location, an unreadable registry or receipt, or an
unusable ``CLAUDE_CONFIG_DIR`` is left alone with a warning, never failing the
install that already committed the project's declaration.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from apm_cli.utils.atomic_io import atomic_write_text
from apm_cli.utils.diagnostics import printable_ascii_text

_RECEIPT_NAME = "claude-known-marketplaces.json"
_PROJECT_MARKETPLACE = re.compile(r"^apm-p-[0-9a-f]{8}$")

WORKTREE_NOTICE = (
    "This checkout is a git worktree: Claude Code may load the main checkout's copy of "
    "its native plugins (anthropics/claude-code#111812). See "
    "https://microsoft.github.io/apm/consumer/install-plugins/#known-gaps"
)


class _Skip(Exception):
    """The registry or receipt cannot be used; the message says why."""


def _claude_root() -> Path:
    from apm_cli.integration.targets import resolve_user_root
    from apm_cli.native_plugins.scopes import CLAUDE

    return resolve_user_root(CLAUDE)


def known_marketplaces_path() -> Path:
    """Claude's marketplace registry, under ``CLAUDE_CONFIG_DIR`` (default ``~/.claude``).

    Raises ``ValueError`` for a non-absolute ``CLAUDE_CONFIG_DIR``.
    """
    return _claude_root() / "plugins" / "known_marketplaces.json"


def _registry() -> Path:
    """The registry path to write, refusing any linked component (the MCP user-config policy)."""
    from apm_cli.utils.path_security import user_config_crosses_link

    root = _claude_root()
    path = root / "plugins" / "known_marketplaces.json"
    if user_config_crosses_link(path, root):
        raise _Skip(f"{_text(path)} is, or is reached through, a symbolic link")
    return path


def _receipt_path() -> Path:
    from apm_cli.core.scope import get_apm_home

    return get_apm_home() / _RECEIPT_NAME


def _text(value: Any) -> str:
    return printable_ascii_text(str(value))


def _read(path: Path) -> tuple[bytes | None, dict[str, Any]]:
    """``(raw bytes, object)``: ``(None, {})`` when absent; ``_Skip`` when unusable."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None, {}
    except OSError as exc:
        raise _Skip(f"{_text(path)} cannot be read ({_text(exc)})") from exc
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _Skip(f"{_text(path)} is not valid JSON") from exc
    if not isinstance(data, dict):
        raise _Skip(f"{_text(path)} does not hold a JSON object")
    return raw, data


def _write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def _merge(path: Path, change: Callable[[dict[str, Any]], bool]) -> None:
    """Apply *change* to the document at *path*, writing only if nothing raced it.

    *change* edits the object in place and returns whether it changed. The file
    is re-read right before the write; if another writer (Claude Code) changed
    it meanwhile, the merge is redone once on the new content, else skipped.
    """
    for _attempt in range(2):
        raw, data = _read(path)
        if not change(data):
            return
        if path.exists() and not os.access(path, os.W_OK):
            raise _Skip(f"{_text(path)} is read-only")
        if _read(path)[0] == raw:
            _write(path, data)
            return
    raise _Skip(f"{_text(path)} kept changing while APM updated it")


def _registered_path(entry: Any) -> Any:
    source = entry.get("source") if isinstance(entry, dict) else None
    return source.get("path") if isinstance(source, dict) else None


def _owned() -> dict[str, dict[str, str]]:
    """APM's receipt of the entries it wrote; ``_Skip`` when unreadable (never ``{}``).

    Each record names the root and the absolute registry file APM wrote it in,
    so only that registry's entry counts as APM's.
    """
    _raw, data = _read(_receipt_path())
    entries = data.get("entries", {})
    if not isinstance(entries, dict):
        raise _Skip(f"{_text(_receipt_path())} has no entries object")
    return {k: v for k, v in entries.items() if isinstance(v, dict)}


def _record(owned: dict[str, dict[str, str]]) -> None:
    path = _receipt_path()
    if owned:
        _write(path, {"version": 2, "entries": owned})
    elif path.exists():
        path.unlink()


def register_project_marketplace(name: str, root: Path, logger: Any = None) -> None:
    """List project marketplace *name* in Claude's registry at absolute *root*.

    Only adds an entry: one already under *name* (this checkout's, another
    clone's or worktree's, or Claude's own) is kept byte for byte, with a
    warning when it names another root. The receipt is recorded before the
    entry is published and undone if publishing fails, so APM never leaves an
    entry it wrote without a record of it.
    """
    location = str(Path(root).absolute())
    if not _PROJECT_MARKETPLACE.match(name):
        raise _Skip(f"'{_text(name)}' is not an APM project marketplace name")
    owned = _owned()
    target = _registry()
    known = _read(target)[1]
    if name in known:
        existing = _registered_path(known[name])
        if existing != location:
            _warn(
                logger,
                f"{_text(target)} already lists marketplace '{_text(name)}' at "
                f"{_text(existing)}; APM only overwrites an entry it owns for this checkout, "
                "so Claude Code may load this project's plugins from there.",
            )
        return

    def add(known: dict[str, Any]) -> bool:
        if name in known:
            return False
        now = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        known[name] = {
            "source": {"source": "directory", "path": location},
            "installLocation": location,
            "lastUpdated": now,
        }
        return True

    _record({**owned, name: {"root": location, "registry": str(target.absolute())}})
    try:
        _merge(target, add)
    except BaseException:
        _record(owned)
        raise
    if _registered_path(_read(target)[1].get(name)) != location:
        _record(owned)


def unregister_project_marketplace(name: str, root: Path, logger: Any = None) -> None:
    """Remove APM's registry entry for *name* at *root*, and nothing else.

    Only the registry the receipt names counts: with another
    ``CLAUDE_CONFIG_DIR``, another profile's identical entry is left alone.
    """
    del logger
    location = str(Path(root).absolute())
    owned = _owned()
    record = owned.get(name, {})
    if record.get("root") != location:
        return
    target = _registry()
    if record.get("registry") != str(target.absolute()):
        return

    def remove(known: dict[str, Any]) -> bool:
        if _registered_path(known.get(name)) != location:
            return False
        del known[name]
        return True

    _merge(target, remove)
    del owned[name]
    _record(owned)


def _warn(logger: Any, message: str) -> None:
    if logger is not None:
        logger.warning(message)


def _is_linked_worktree(checkout: Path) -> bool:
    """A linked git worktree keeps a ``.git`` file pointing at the main repository."""
    return (Path(checkout) / ".git").is_file()


def sync_project_marketplace(
    name: str, root: Path, *, user_scope: bool, claude_declared: bool, logger: Any = None
) -> None:
    """Register *name* while Claude declares project plugins from it; unregister after.

    Runs after the project's declaration is committed, so nothing here fails
    the command: an unusable registry, receipt or ``CLAUDE_CONFIG_DIR`` is
    reported as a warning and left alone.
    """
    if user_scope:
        return
    try:
        if claude_declared:
            register_project_marketplace(name, root, logger)
        else:
            unregister_project_marketplace(name, root, logger)
    except (_Skip, OSError, ValueError) as exc:
        _warn(
            logger,
            f"APM did not update Claude Code's marketplace registry for '{_text(name)}': "
            f"{_text(exc)}. Claude Code loads the plugins' skills and hooks from its second "
            "session in this project.",
        )
    if claude_declared and _is_linked_worktree(Path(root).parents[1]):
        _warn(logger, WORKTREE_NOTICE)
