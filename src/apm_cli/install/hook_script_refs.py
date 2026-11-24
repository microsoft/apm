"""Whether a hook command may run a bundle's script, however it is spelled (#5106).

``apm uninstall <bundle>`` keeps a bundle's hook scripts while a hook it does
not own still runs one of them. Shell spellings are open-ended (``cd``,
substitutions, globs, assignments, nested shells), so the decision is
default-deny: a command that names a script's file name runs it, unless every
such word is a plain path that certainly resolves to another file.
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Collection
from pathlib import Path

from ..utils.paths import caseless

# Variables a deployed hook command anchors its paths at.
_ANCHOR_VARIABLES = re.compile(
    r"\$\{(CLAUDE_PROJECT_DIR|HOME)\}|\$env:(CLAUDE_PROJECT_DIR|HOME)\b|\$(CLAUDE_PROJECT_DIR|HOME)\b"
)
_ASSIGNED_ANCHOR = re.compile(r"\b(CLAUDE_PROJECT_DIR|HOME)\s*=")
_DRIVE = re.compile(r"^[A-Za-z]:/")
# Characters that make a word more than a plain path: expansion, globbing,
# assignment, quoting or a command separator.
_NOT_PLAIN = re.compile(r"[$`*?\[\]{}~=;&|()<>'\"\\\n]")
# Words that change the working directory relative paths resolve from.
_DIRECTORY_CHANGES = frozenset({"cd", "pushd", "popd"})
_QUOTING = re.compile(r"[\"'\\]")
_HANDS_TO_SHELL = re.compile(r"^(?:-.*|/[A-Za-z]|eval|exec|source|\.|<+)$")


def script_refs(project_root: Path, scripts: Collection[str]) -> tuple[frozenset, frozenset]:
    """The caseless keys of a bundle's script names and absolute paths (#5106).

    Every spelling compares by the one shared caseless key on every OS: a
    case-insensitive volume (default macOS, Windows) runs ``Run.sh`` or its
    NFD form as ``run.sh``. On a case-sensitive volume that over-refuses,
    which fails closed.
    """
    paths = {
        Path(value) if Path(value).is_absolute() else project_root / value for value in scripts
    }
    return (
        frozenset(caseless(path.name) for path in paths),
        frozenset(_normalized(path.as_posix()) for path in paths),
    )


def _normalized(path: str) -> str:
    return caseless(posixpath.normpath(path))


def _words(command: str) -> list[str] | None:
    """Shell words of *command*, quoted words kept whole; ``None`` when unparsable."""
    import shlex

    lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|()<>")
    lexer.whitespace_split = True
    try:
        return list(lexer)
    except ValueError:
        return None


def _name_pattern(names: Collection[str]) -> re.Pattern[str]:
    """A caseless file name standing alone, not inside a longer one (``max.sh`` is not ``x.sh``)."""
    alternatives = "|".join(re.escape(name) for name in sorted(names))
    return re.compile(rf"(?<![\w.-])(?:{alternatives})(?![\w.-])")


def _mentions(command: str, pattern: re.Pattern[str]) -> bool:
    """Whether *command* names a script, as written or as the shell decodes its quoting.

    ``./'x'.sh`` and ``x\\.sh`` run ``x.sh``; ANSI-C quoting (``$'...'``) can
    spell any name, so it counts as naming one (fail closed).
    """
    folded = caseless(command)
    return bool(
        "$'" in command
        or pattern.search(folded)
        or pattern.search(_QUOTING.sub("", folded))
        or any(pattern.search(caseless(word)) for word in _words(command) or ())
    )


def runs_script(
    command: str, refs: tuple[frozenset, frozenset], bases: Collection[Path], project_root: Path
) -> bool:
    """Whether *command* may run a bundle script, in any target and spelling (#5106).

    Default-deny: a command naming a script's file name runs it, unless every
    word naming it is a plain path (no expansion, glob, assignment, quote or
    separator) that certainly resolves elsewhere. An absolute path resolves
    as written; a relative one from every base in *bases*, and only when the
    command never changes directory. ``${CLAUDE_PROJECT_DIR}`` and ``$HOME``
    count as plain anchors unless the command assigns them.
    """
    names, paths = refs
    pattern = _name_pattern(names)
    if not _mentions(command, pattern):
        return False
    text = command
    if not _ASSIGNED_ANCHOR.search(command):
        home = Path.home().as_posix()

        def anchor(match: re.Match) -> str:
            name = next(group for group in match.groups() if group)
            return home if name == "HOME" else project_root.as_posix()

        text = _ANCHOR_VARIABLES.sub(anchor, command)
    return _runs(text, pattern, paths, tuple(bases), moved=False)


def _runs(
    text: str, pattern: re.Pattern[str], paths: frozenset, bases: tuple, *, moved: bool
) -> bool:
    if "$(" in text or "`" in text:
        return True  # a command substitution may build the path around the name
    words = _words(text)
    if words is None:
        return True
    moved = moved or any(word in _DIRECTORY_CHANGES for word in words)
    # An option, ``eval`` or a redirection may hand a quoted word to a shell
    # (``sh -c``, ``cmd /c``, ``pwsh -Command``, ``bash <<<``): rescan it.
    nested = any(_HANDS_TO_SHELL.match(word) for word in words)
    named = [word for word in words if pattern.search(caseless(word))]
    if not named:
        return True  # the name sits in syntax the words lost: fail closed
    for word in named:
        if nested and any(space in word for space in " \t"):
            if _runs(word, pattern, paths, bases, moved=moved):
                return True
        elif not _certainly_elsewhere(word, paths, bases, moved):
            return True
    return False


def _certainly_elsewhere(word: str, paths: frozenset, bases: tuple, moved: bool) -> bool:
    """Whether *word* is a plain path that resolves, with certainty, to no script."""
    if _NOT_PLAIN.search(word.replace(" ", "")):
        return False
    if word.startswith("/") or _DRIVE.match(word):
        return _normalized(word) not in paths
    relative = posixpath.normpath(word)
    if moved or "/" not in relative or relative.startswith(".."):
        return False  # a bare name, or a directory that is not certain
    return all(_normalized(f"{base.as_posix()}/{relative}") not in paths for base in bases)
