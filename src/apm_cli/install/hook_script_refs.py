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
from collections.abc import Collection, Iterator
from pathlib import Path
from typing import Any

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
# Left after anchor substitution, these may spell a name the text never shows:
# expansion, command substitution, globbing, brace lists or ranges, escapes,
# and cmd.exe ``%NAME%`` or delayed ``!NAME!`` expansion (``+%Y`` is no name).
_HIDES_A_NAME = re.compile(r"[$`*?\[\\]|\{[^{}]*(?:,|\.\.)[^{}]*\}|%[A-Za-z_]\w*%|![A-Za-z_]\w*!")
_QUOTING = re.compile(r"[\"'\\]")
_HANDS_TO_SHELL = re.compile(r"^(?:-.*|/[A-Za-z]|eval|exec|source|\.|<+)$")
# A POSIX shell run on a script file with only options that take no command
# string (#5210): ``sh -e``, ``bash -eu``, ``sh -o errexit -x``. ``-c``, ``-s``,
# long options and any other option keep the rescan (fail closed).
_SHELLS = frozenset({"sh", "bash", "dash", "ksh", "zsh"})
_SHELL_FLAGS = re.compile(r"^[-+][abCefhmnuvxBEHPT]+$")
_SHELL_OPTION_NAME = re.compile(r"^[a-z][a-z-]*$")
_SEPARATORS = frozenset({";", "&", "&&", "|", "||", "(", ")"})


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

    ``./'x'.sh`` and ``"x"".sh"`` run ``x.sh``.
    """
    folded = caseless(command)
    return bool(
        pattern.search(folded)
        or pattern.search(_QUOTING.sub("", folded))
        or any(pattern.search(caseless(word)) for word in _words(command) or ())
    )


def runs_script(
    command: str,
    refs: tuple[frozenset, frozenset],
    bases: Collection[Path],
    project_root: Path,
    *,
    from_cwd: bool = False,
) -> bool:
    """Whether *command* may run a bundle script, in any target and spelling (#5106).

    Default-deny: a command naming a script's file name runs it, unless every
    word naming it is a plain path (no expansion, glob, assignment, quote or
    separator) that certainly resolves elsewhere. An absolute path resolves
    as written; a relative one from every base in *bases*, and only when the
    command never changes directory. ``${CLAUDE_PROJECT_DIR}`` and ``$HOME``
    count as plain anchors unless the command assigns them. With *from_cwd*
    the bases are the handler's declared working directory, so ``./name``
    resolves from it (#5332); otherwise a bare name may be any directory's.
    """
    names, paths = refs
    pattern = _name_pattern(names)
    text = command
    if not _ASSIGNED_ANCHOR.search(command):
        home = Path.home().as_posix()

        def anchor(match: re.Match) -> str:
            name = next(group for group in match.groups() if group)
            return home if name == "HOME" else project_root.as_posix()

        text = _ANCHOR_VARIABLES.sub(anchor, command)
    # An expansion left after the anchors may spell a name anywhere: fail closed,
    # even beside a literal path to another file of that name.
    if _HIDES_A_NAME.search(text):
        return True
    if not _mentions(command, pattern):
        return False
    return _runs(text, pattern, paths, tuple(bases), moved=False, from_cwd=from_cwd)


def _runs(
    text: str,
    pattern: re.Pattern[str],
    paths: frozenset,
    bases: tuple,
    *,
    moved: bool,
    from_cwd: bool = False,
) -> bool:
    if "$(" in text or "`" in text:
        return True  # a command substitution may build the path around the name
    words = _words(text)
    if words is None:
        return True
    moved = moved or any(word in _DIRECTORY_CHANGES for word in words)
    operands, options = _script_operands(words)
    # An option, ``eval`` or a redirection may hand a quoted word to a shell
    # (``sh -c``, ``cmd /c``, ``pwsh -Command``, ``bash <<<``): rescan it.
    nested = any(_HANDS_TO_SHELL.match(w) for i, w in enumerate(words) if i not in options)
    named = [(i, word) for i, word in enumerate(words) if pattern.search(caseless(word))]
    if not named:
        return True  # the name sits in syntax the words lost: fail closed
    for index, word in named:
        if index in operands:
            # A shell's script operand is one path, judged whole (#5210), from
            # the handler's cwd when it declares one (#5332).
            if not _certainly_elsewhere(word, paths, bases, moved, from_cwd):
                return True
        elif nested and any(space in word for space in " \t"):
            # The intact word may itself be a script path with a space in it.
            if _names_a_script(word, paths, bases, moved) or _runs(
                word, pattern, paths, bases, moved=moved, from_cwd=from_cwd
            ):
                return True
        elif not _certainly_elsewhere(word, paths, bases, moved, from_cwd):
            return True
    return False


def _script_operands(words: list[str]) -> tuple[set[int], set[int]]:
    """Indices of shell script operands and of the known options before them (#5210).

    Only a shell word that starts a command, followed by at least one known option
    and then a word that is no option or separator, counts; anything else is left
    to the rescan.
    """
    operands: set[int] = set()
    options: set[int] = set()
    for start, word in enumerate(words):
        if start and words[start - 1] not in _SEPARATORS:
            continue
        if posixpath.basename(word) not in _SHELLS:
            continue
        seen: list[int] = []
        at = start + 1
        while at < len(words):
            if words[at] in ("-o", "+o") and at + 1 < len(words):
                if not _SHELL_OPTION_NAME.match(words[at + 1]):
                    break
                seen += [at, at + 1]
                at += 2
            elif _SHELL_FLAGS.match(words[at]):
                seen.append(at)
                at += 1
            else:
                break
        if seen and at < len(words) and not words[at].startswith(("-", "+")):
            if words[at] not in _SEPARATORS:
                operands.add(at)
                options.update(seen)
    return operands, options


def _names_a_script(word: str, paths: frozenset, bases: tuple, moved: bool) -> bool:
    """Whether *word*, read whole as a path, resolves to a recorded script."""
    if word.startswith("/") or _DRIVE.match(word):
        return _normalized(word) in paths
    relative = posixpath.normpath(word)
    return not moved and any(
        _normalized(f"{base.as_posix()}/{relative}") in paths for base in bases
    )


def _certainly_elsewhere(
    word: str, paths: frozenset, bases: tuple, moved: bool, from_cwd: bool = False
) -> bool:
    """Whether *word* is a plain path that resolves, with certainty, to no script."""
    if _NOT_PLAIN.search(word.replace(" ", "")):
        return False
    if word.startswith("/") or _DRIVE.match(word):
        if _linked_below(Path(Path(word).anchor or "/"), word):
            return False  # a link may alias the bundle script: identity is not certain
        return _normalized(word) not in paths
    relative = posixpath.normpath(word)
    qualified = "/" in relative or (from_cwd and word.startswith("./"))
    if moved or not qualified or relative.startswith(".."):
        return False  # a bare name, or a directory that is not certain
    if any(_linked_below(base, relative) for base in bases):
        return False  # a link may alias the bundle script: identity is not certain
    return all(_normalized(f"{base.as_posix()}/{relative}") not in paths for base in bases)


def _linked_below(base: Path, relative: str) -> bool:
    """Whether an existing *relative* below *base* goes through a link (or cannot be proven not to).

    A link or junction anywhere on the path may alias a bundle script, so a
    lexical mismatch proves nothing there (fail closed).
    """
    from ..utils.path_security import has_symlink_component

    candidate = Path(base) / relative
    if not (candidate.is_symlink() or candidate.exists()):
        return False
    return has_symlink_component(Path(base), candidate)


def hook_commands(document: Any) -> list[str]:
    """Commands a per-file hook document runs, read from handler positions only (#5136).

    The shared hook grammar locates the handlers, so metadata that merely names
    a script (``env.command``) runs nothing. A shape the grammar does not
    recognize (a Kiro action list, a malformed entry) is scanned whole, as
    before, so it still fails closed.
    """
    from apm_cli.hook_contract import HookContractError, parse_hook_source

    try:
        commands = [declared.command for declared in parse_hook_source(document).commands]
    except HookContractError:
        commands = list(_every_command_field(document))
    # Raw text: the matcher reads backslash escapes (#5106).
    return commands


def _every_command_field(node: Any) -> Iterator[str]:
    """Every hook command field anywhere in a document the grammar cannot read."""
    from apm_cli.hook_contract import HOOK_COMMAND_KEYS

    if isinstance(node, dict):
        for key, value in node.items():
            if key in HOOK_COMMAND_KEYS and isinstance(value, str):
                yield value
            else:
                yield from _every_command_field(value)
    elif isinstance(node, list):
        for item in node:
            yield from _every_command_field(item)
