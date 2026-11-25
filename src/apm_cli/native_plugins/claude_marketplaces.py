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
clone or worktree, or Claude's own) unchanged with a warning. A write keeps
every other entry's content; the file itself is re-serialized. APM registers
only the name derived from this checkout's own project identity, never one a
committed lock or catalog supplies. APM records each entry it wrote in a receipt under
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
    path = _claude_root() / "plugins" / "known_marketplaces.json"
    _refuse_linked(path, path.parents[1])
    return path


def _receipt_path() -> Path:
    from apm_cli.core.scope import get_apm_home

    return get_apm_home() / _RECEIPT_NAME


def _text(value: Any) -> str:
    return printable_ascii_text(str(value))


def _regular_bytes(path: Path) -> bytes:
    """Read *path* only as a regular file: a FIFO or device never blocks the read."""
    from apm_cli.utils.path_security import open_regular_file

    with open_regular_file(path) as handle:
        return handle.read()


def _read(path: Path) -> tuple[bytes | None, dict[str, Any]]:
    """``(raw bytes, object)``: ``(None, {})`` when absent; ``_Skip`` when unusable."""
    try:
        raw = _regular_bytes(path)
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


def _refuse_linked(path: Path, root: Path) -> None:
    from apm_cli.utils.path_security import user_config_crosses_link

    if user_config_crosses_link(path, root):
        raise _Skip(f"{_text(path)} is, or is reached through, a symbolic link")


def _merge(
    path: Path,
    change: Callable[[dict[str, Any]], bool],
    *,
    root: Path,
    mode: int | None = None,
) -> bool:
    """Apply *change* to the document at *path*; return whether APM published it.

    *change* edits the object in place and returns whether it changed. The
    candidate is staged first and the file compared right before the atomic
    replace; if another writer (Claude Code) changed it meanwhile, the merge is
    redone once on the new content, else skipped and the staged file removed.
    """
    from apm_cli.utils.atomic_io import atomic_write_text, umask_file_mode

    for _attempt in range(2):
        raw, data = _read(path)
        if not change(data):
            return False
        if path.exists() and not os.access(path, os.W_OK):
            raise _Skip(f"{_text(path)} is read-only")
        path.parent.mkdir(parents=True, exist_ok=True)

        def unchanged(raw: bytes | None = raw) -> bool:
            # Checked again after staging: a directory linked meanwhile is never published to.
            _refuse_linked(path, root)
            return _read(path)[0] == raw

        if atomic_write_text(
            path,
            json.dumps(data, indent=2, ensure_ascii=False) + "\n",
            new_file_mode=umask_file_mode(path) if mode is None else mode,
            file_mode=mode,
            normalize_line_endings=False,
            temp_prefix=".apm-",
            temp_suffix=".tmp",
            before_publish=unchanged,
        ):
            return True
    raise _Skip(f"{_text(path)} kept changing while APM updated it")


def _registered_path(entry: Any) -> Any:
    source = entry.get("source") if isinstance(entry, dict) else None
    return source.get("path") if isinstance(source, dict) else None


def _receipt_key(registry: Path, name: str) -> str:
    return f"{name}@{registry.absolute()}"


def _owned() -> dict[str, dict[str, str]]:
    """APM's receipt of the entries it wrote; ``_Skip`` when unreadable (never ``{}``).

    One record per registry file and marketplace name, naming the root, so
    installs under several Claude profiles each keep their own record.
    """
    _raw, data = _read(_receipt_path())
    entries = data.get("entries", {})
    if not isinstance(entries, dict):
        raise _Skip(f"{_text(_receipt_path())} has no entries object")
    return {k: v for k, v in entries.items() if isinstance(v, dict)}


def _record(key: str, record: dict[str, str] | None) -> None:
    """Add (or, with ``None``, drop) one receipt record, leaving every other one."""
    path = _receipt_path()

    def change(data: dict[str, Any]) -> bool:
        entries = data.setdefault("entries", {})
        data["version"] = 3
        if not isinstance(entries, dict):
            raise _Skip(f"{_text(path)} has no entries object")
        if record is None:
            return entries.pop(key, None) is not None
        if entries.get(key) == record:
            return False
        entries[key] = record
        return True

    # The receipt names the user's checkouts and profiles: readable by the owner only.
    _merge(path, change, root=path.parent, mode=0o600)
    if record is None and path.exists() and not _owned():
        path.unlink()


def _claude_trust_file() -> Path:
    """Claude Code's user config: ``$CLAUDE_CONFIG_DIR/.claude.json``, else ``~/.claude.json``."""
    from apm_cli.integration.targets import configured_user_root
    from apm_cli.native_plugins.scopes import CLAUDE

    configured = configured_user_root(CLAUDE)
    return (configured if configured is not None else Path.home()) / ".claude.json"


def claude_trusts(checkout: Path) -> bool:
    """Whether Claude Code has recorded folder trust for exactly *checkout*.

    Read only and never through a link: ``projects["<abs checkout>"]
    .hasTrustDialogAccepted`` must be ``true``. A missing, unreadable or linked
    file, or one in any other shape, counts as untrusted.
    """
    from apm_cli.utils.path_security import user_config_crosses_link

    path = _claude_trust_file()
    if user_config_crosses_link(path, path.parent):
        return False
    try:
        data = json.loads(_regular_bytes(path).decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return False
    projects = data.get("projects") if isinstance(data, dict) else None
    if not isinstance(projects, dict):
        return False
    # Claude Code keys projects by the checkout path; on Windows with forward slashes.
    for key in dict.fromkeys((str(checkout), checkout.as_posix())):
        entry = projects.get(key)
        if isinstance(entry, dict) and entry.get("hasTrustDialogAccepted") is True:
            return True
    return False


def register_project_marketplace(name: str, root: Path, logger: Any = None) -> None:
    """List project marketplace *name* in Claude's registry at absolute *root*.

    Only adds an entry: one already under *name* (this checkout's, another
    clone's or worktree's, or Claude's own) is kept unchanged, with a warning
    when it names another root. *name* must be the one derived from this
    checkout's own project identity. The receipt is recorded before the
    entry is published and undone if publishing fails, so APM never leaves an
    entry it wrote without a record of it.
    """
    location = str(Path(root).absolute())
    if not _PROJECT_MARKETPLACE.match(name):
        raise _Skip(f"'{_text(name)}' is not an APM project marketplace name")
    checkout = Path(location).parents[1]
    # The committed lock or catalog chooses the declared name; register only our own.
    from apm_cli.native_plugins.scopes import project_marketplace_name

    derived = project_marketplace_name(checkout, None, None)
    if name != derived:
        _warn(
            logger,
            f"APM did not list marketplace '{_text(name)}' for Claude Code: this "
            f"checkout's own marketplace name is '{_text(derived)}'. Claude Code lists "
            "the project's marketplace itself after its first session here.",
        )
        return
    # The entry makes Claude Code load the plugins, so only a trusted folder gets one.
    if not claude_trusts(checkout):
        from apm_cli.core.remediation import CLAUDE_MARKETPLACE_TRUST

        _warn(
            logger,
            f"APM did not list marketplace '{_text(name)}' for Claude Code: Claude Code "
            f"does not trust {_text(checkout)} yet. {CLAUDE_MARKETPLACE_TRUST.sentence()}",
        )
        return
    _owned()  # an unreadable receipt refuses before anything is written
    target = _registry()

    def warn_if_elsewhere() -> None:
        existing = _registered_path(_read(target)[1].get(name))
        if existing is not None and existing != location:
            _warn(
                logger,
                f"{_text(target)} already lists marketplace '{_text(name)}' at "
                f"{_text(existing)}; APM only overwrites an entry it owns for this checkout, "
                "so Claude Code may load this project's plugins from there.",
            )

    if name in _read(target)[1]:
        warn_if_elsewhere()
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

    key = _receipt_key(target, name)
    _record(key, {"name": name, "root": location, "registry": str(target.absolute())})
    try:
        published = _merge(target, add, root=target.parents[1])
    except BaseException:
        _record(key, None)
        raise
    if not published:
        _record(key, None)  # an entry appeared meanwhile: it is not APM's
        warn_if_elsewhere()


def unregister_project_marketplace(name: str, root: Path, logger: Any = None) -> None:
    """Remove APM's registry entry for *name* at *root*, and nothing else.

    Only the registry the receipt names counts: with another
    ``CLAUDE_CONFIG_DIR``, another profile's identical entry is left alone.
    """
    del logger
    location = str(Path(root).absolute())
    owned = _owned()
    target = _registry()
    key = _receipt_key(target, name)
    record = owned.get(key, {})
    if record.get("root") != location or record.get("registry") != str(target.absolute()):
        return

    def remove(known: dict[str, Any]) -> bool:
        if _registered_path(known.get(name)) != location:
            return False
        del known[name]
        return True

    _merge(target, remove, root=target.parents[1])
    _record(key, None)


def _receipt_names_for(root: Path) -> list[str]:
    """Names APM's receipt records for this checkout's *root* in the current registry."""
    if not _receipt_path().exists():
        return []
    location = str(Path(root).absolute())
    try:
        registry = str(_registry().absolute())
        return [
            key.split("@", 1)[0]
            for key, record in _owned().items()
            if record.get("root") == location and record.get("registry") == registry
        ]
    except (_Skip, OSError, ValueError):
        return []  # a declaring command names an unusable registry or receipt


def project_release_pending(project_root: Path) -> bool:
    """Whether APM still records a registry entry for *project_root*'s plugins (#5258).

    A fast install path declines while one does: only the full run releases it.
    """
    from apm_cli.native_plugins.layout import plugins_root

    return bool(_receipt_names_for(plugins_root(Path(project_root))))


def release_project_marketplaces(root: Path, *, user_scope: bool, logger: Any = None) -> None:
    """Remove every entry APM's receipt records for this checkout's *root* (#5258).

    Called when the scope declares no native plugin, so a registry entry an
    earlier uninstall could not remove (its receipt kept) converges on the
    next command. Only receipt-owned entries for this root and the current
    registry are touched; without a readable receipt nothing is.
    """
    if user_scope:
        return
    for name in _receipt_names_for(root):
        sync_project_marketplace(name, root, user_scope=False, claude_declared=False, logger=logger)


def release_undeclared_marketplaces(deploy_root: Path, scope: Any, logger: Any = None) -> None:
    """:func:`release_project_marketplaces` for a scope that declares no dependency (#5258)."""
    from apm_cli.core.scope import is_user_scope
    from apm_cli.native_plugins.layout import plugins_root

    release_project_marketplaces(
        plugins_root(Path(deploy_root).absolute()),
        user_scope=is_user_scope(scope),
        logger=logger,
    )


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
            f"{_text(exc)}. Resolve that, then re-run the command.",
        )
    if claude_declared and _is_linked_worktree(Path(root).parents[1]):
        _warn(logger, WORKTREE_NOTICE)
