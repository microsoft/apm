"""Whether a local-bundle hook runs a bundle script through its ``cwd`` field (#5281).

``install/local_bundle_hook_trust.py`` consumes :func:`runs_through_cwd` and
:func:`commands_without_cwd` in its bundle-script-in-use check.
"""

from __future__ import annotations

import os
from collections import Counter
from collections.abc import Collection, Iterator
from pathlib import Path
from typing import Any


def _grammar_handlers(document: Any) -> list[dict] | None:
    """The handlers the hook grammar reads, or ``None`` for a shape it cannot read."""
    from apm_cli.hook_contract import HookContractError, hook_handlers

    try:
        return [dict(handler.value) for handler in hook_handlers(document)]
    except HookContractError:
        return None


def _handler_cwds(document: Any, handlers: list[dict] | None = None) -> list[tuple[str, Any]]:
    """``(command, cwd)`` for every handler that declares a working directory (#5281)."""
    from apm_cli.hook_contract import HOOK_COMMAND_KEYS

    if handlers is None:
        handlers = _grammar_handlers(document)
    if handlers is None:
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


def commands_without_cwd(document: Any, commands: Collection[str]) -> list[str]:
    """*commands* less those a grammar handler runs in a declared ``cwd`` (#5332).

    :func:`runs_through_cwd` decides those from their cwd. A shape the hook
    grammar cannot read keeps every command, so the default bases still judge it.
    """
    handlers = _grammar_handlers(document)
    if handlers is None:
        return list(commands)
    in_cwd = Counter(command for command, _cwd in _handler_cwds(document, handlers))
    return list((Counter(commands) - in_cwd).elements())


def runs_through_cwd(document: Any, project_root: Path, scripts: Collection[str]) -> bool:
    """Whether a handler runs a bundle script relative to its ``cwd`` field (#5281).

    The cwd resolves against the project root. One that cannot be resolved
    with certainty (not a plain path, a variable, ``~`` or outside the
    project) may run anything, so it counts as running the scripts. Otherwise
    the command is judged by the default-deny matcher from the cwd (#5332),
    and from any script directory the cwd is by file identity, so a symlink,
    a symlinked ancestor or a case variant naming a script's directory counts.
    """
    from apm_cli.install.hook_script_refs import runs_script, script_refs

    root = Path(os.path.abspath(project_root))
    located = [Path(os.path.normpath(root / value)) for value in scripts]
    refs = script_refs(root, scripts)
    index: dict[tuple[int, int], set[Path]] | None = None
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
        if _cwd_through_a_link(spelled, root):
            return True  # a linked cwd may alias the script directory: not certain
        bases = {base}
        try:
            identity = os.stat(spelled)
        except FileNotFoundError:
            identity = None
        except OSError:
            return True  # a directory that cannot be read cannot be cleared
        if identity is not None:
            if index is None:
                index = _script_directories(root, located)
            bases |= index.get((identity.st_dev, identity.st_ino), set())
        if runs_script(command, refs, sorted(bases), root, from_cwd=True):
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


def _script_directories(root: Path, scripts: Collection[Path]) -> dict[tuple[int, int], set[Path]]:
    """``(st_dev, st_ino)`` of each directory above a script, up to the root -> that directory."""
    index: dict[tuple[int, int], set[Path]] = {}
    for script in scripts:
        for directory in script.parents:
            if not directory.is_relative_to(root):
                break
            try:
                found = os.stat(directory)
            except OSError:
                continue
            index.setdefault((found.st_dev, found.st_ino), set()).add(directory)
    return index


def _cwd_through_a_link(spelled: Path, root: Path) -> bool:
    """Whether the declared cwd goes through a link or junction (or cannot be proven not to)."""
    from apm_cli.utils.path_security import has_symlink_component

    return has_symlink_component(root, spelled)
