"""Whether a local-bundle hook runs a bundle script through its ``cwd`` field (#5281).

Moved unchanged from ``install/local_bundle_hook_trust.py``, which consumes
:func:`runs_through_cwd` in its bundle-script-in-use check.
"""

from __future__ import annotations

import os
from collections.abc import Collection, Iterator
from pathlib import Path
from typing import Any


def _handler_cwds(document: Any) -> list[tuple[str, Any]]:
    """``(command, cwd)`` for every handler that declares a working directory (#5281)."""
    from apm_cli.hook_contract import HOOK_COMMAND_KEYS, HookContractError, hook_handlers

    try:
        handlers = [dict(handler.value) for handler in hook_handlers(document)]
    except HookContractError:
        handlers = list(_every_mapping(document))
    return [
        (handler[key], handler["cwd"])
        for handler in handlers
        if "cwd" in handler
        for key in HOOK_COMMAND_KEYS
        if isinstance(handler.get(key), str)
    ]


def _every_mapping(node: Any) -> Iterator[dict]:
    """Every object anywhere in a document the hook grammar cannot read."""
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _every_mapping(value)
    elif isinstance(node, list):
        for item in node:
            yield from _every_mapping(item)


def runs_through_cwd(document: Any, project_root: Path, scripts: Collection[str]) -> bool:
    """Whether a handler runs a bundle script relative to its ``cwd`` field (#5281).

    The cwd resolves against the project root. One that cannot be resolved
    with certainty (not a plain path, a variable, ``~`` or outside the
    project) may run anything, so it counts as running the scripts. Besides
    the lexical match, the cwd is matched by file identity, so a symlink, a
    symlinked ancestor or a case variant naming a script's directory counts.
    """
    root = Path(os.path.abspath(project_root))
    located = [Path(os.path.normpath(root / value)) for value in scripts]
    index: dict[tuple[int, int], set[str]] | None = None
    for command, cwd in _handler_cwds(document):
        if not isinstance(cwd, str) or not cwd.strip() or cwd.startswith("~"):
            return True
        if "$" in cwd or "%" in cwd or "\x00" in cwd:
            return True
        spelled = root / cwd.replace("\\", "/")
        base = Path(os.path.normpath(spelled))
        # Links are followed before "..", so identity and containment use the
        # spelled path; the lexical form only refuses an obvious escape.
        if not base.is_relative_to(root) or _escapes_through_a_link(spelled, root):
            return True
        text = command.replace("\\", "/")
        if any(
            script.is_relative_to(base) and script.relative_to(base).as_posix() in text
            for script in located
        ):
            return True
        try:
            identity = os.stat(spelled)
        except FileNotFoundError:
            continue
        except OSError:
            return True  # a directory that cannot be read cannot be cleared
        if index is None:
            index = _script_directories(root, located)
        if any(rel in text for rel in index.get((identity.st_dev, identity.st_ino), ())):
            return True
    return False


def _escapes_through_a_link(spelled: Path, root: Path) -> bool:
    """Whether *spelled* resolves outside the project, or cannot be resolved (refuse)."""
    from apm_cli.utils.path_security import PathTraversalError, ensure_path_within

    try:
        ensure_path_within(spelled, root)
    except (PathTraversalError, OSError, ValueError, RuntimeError):
        return True
    return False


def _script_directories(root: Path, scripts: Collection[Path]) -> dict[tuple[int, int], set[str]]:
    """``(st_dev, st_ino)`` of each directory above a script, up to the root -> its relative path."""
    index: dict[tuple[int, int], set[str]] = {}
    for script in scripts:
        for directory in script.parents:
            if not directory.is_relative_to(root):
                break
            try:
                found = os.stat(directory)
            except OSError:
                continue
            index.setdefault((found.st_dev, found.st_ino), set()).add(
                script.relative_to(directory).as_posix()
            )
    return index
