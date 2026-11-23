"""Whether a hook command may run a bundle's script, however it is spelled (#5106).

``apm uninstall <bundle>`` keeps a bundle's hook scripts while a hook it does
not own still runs one of them; this module decides "runs one of them" over
shell words, ``cd`` prefixes, nested shells and path spellings, and fails
closed where the command's working directory or expansion is not certain.
"""

from __future__ import annotations

import os
import posixpath
import re
from collections.abc import Collection
from pathlib import Path

# Shell separators and quotes a substitution's path word may sit next to.
_SEPARATORS = re.compile(r"[\s;&|()<>'\"`]+")
# Variables a deployed hook command anchors its paths at, and their values.
_ANCHOR_VARIABLES = re.compile(
    r"\$\{(CLAUDE_PROJECT_DIR|HOME)\}|\$env:(CLAUDE_PROJECT_DIR|HOME)\b|\$(CLAUDE_PROJECT_DIR|HOME)\b"
)
_DRIVE = re.compile(r"^[A-Za-z]:/")
# A working directory is tracked through at most this many distinct roots.
_MAX_ROOTS = 16


def script_refs(project_root: Path, scripts: Collection[str]) -> tuple[frozenset, frozenset]:
    """The file names and normalized absolute paths of a bundle's script rows (#5106)."""
    paths = {
        Path(value) if Path(value).is_absolute() else project_root / value for value in scripts
    }
    return (
        frozenset(path.name for path in paths),
        frozenset(_normalized(path.as_posix()) for path in paths),
    )


def _folds(path: str) -> bool:
    """Whether *path* compares case-insensitively (Windows, or a drive path)."""
    return os.name == "nt" or bool(_DRIVE.match(path))


def _normalized(path: str) -> str:
    normal = posixpath.normpath(path)
    return normal.casefold() if _folds(normal) else normal


def _words(command: str) -> list[str] | None:
    """Shell words of *command*, quoted words kept whole; ``None`` when unparsable."""
    import shlex

    lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|()<>")
    lexer.whitespace_split = True
    try:
        return list(lexer)
    except ValueError:
        return None


def runs_script(
    command: str, refs: tuple[frozenset, frozenset], bases: Collection[Path], project_root: Path
) -> bool:
    """Whether *command* may run a bundle script under any spelling, in any target (#5106).

    ``${CLAUDE_PROJECT_DIR}`` and ``$HOME`` anchors are resolved first. A
    path word runs the script when it normalizes to it (``/./``, ``..``): an
    absolute one (``/``, ``C:/``) as written, a relative one from any of
    *bases* (the project root, the hook's target root) or a ``cd <dir>``
    before it, nested shells included. A bare file name (``./x.sh``), a word
    with another expansion, a command substitution, or a relative word under
    a working directory that cannot be determined counts when its name
    matches (fail closed); so does any command that cannot be parsed.
    """
    home = Path.home().as_posix()

    def anchor(match: re.Match) -> str:
        name = next(group for group in match.groups() if group)
        return home if name == "HOME" else project_root.as_posix()

    text = _ANCHOR_VARIABLES.sub(anchor, command)
    roots = frozenset(_normalized(base.as_posix()) for base in bases)
    return _scan_words(text, refs, roots, True)


def _names_word(word: str, names: frozenset) -> bool:
    name = word.rstrip("/").rsplit("/", 1)[-1]
    if _folds(word):
        return name.casefold() in {candidate.casefold() for candidate in names}
    return name in names


def _scan_words(
    text: str, refs: tuple[frozenset, frozenset], roots: frozenset, certain: bool
) -> bool:
    """:func:`runs_script` over one shell string, from *roots* (#5106)."""
    names, paths = refs
    pieces = _SEPARATORS.split(text)
    if ("$(" in text or "`" in text) and any(_names_word(piece, names) for piece in pieces):
        return True  # a command substitution may build the script path
    words = _words(text)
    if words is None:
        return any(name.casefold() in text.casefold() for name in names)
    tracked = set(roots)
    for index, word in enumerate(words):
        if word == "cd":
            rest = [w for w in words[index + 1 :] if w != "--" and not w.startswith("-")]
            target = rest[0] if rest else None
            if target is None or target in ";&|()<>" or "$" in target or target.startswith("~"):
                certain = False
            elif certain:
                # A cd in a subshell need not hold afterwards: roots only grow.
                if target.startswith("/") or _DRIVE.match(target):
                    tracked.add(_normalized(target))
                else:
                    tracked |= {_normalized(posixpath.join(root, target)) for root in tracked}
                if len(tracked) > _MAX_ROOTS:
                    certain = False
            continue
        if any(space in word for space in " \t\n") and _scan_words(
            word, refs, frozenset(tracked), certain
        ):
            return True
        if not _names_word(word, names):
            continue
        if word.startswith("/") or _DRIVE.match(word):
            if "$" in word or _normalized(word) in paths:
                return True
            continue
        if not certain or "$" in word or word.startswith("~"):
            return True
        if "/" not in word.removeprefix("./"):
            return True
        if any(_normalized(f"{root}/{word}") in paths for root in tracked):
            return True
    return False
