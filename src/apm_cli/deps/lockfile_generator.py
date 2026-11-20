"""Refuse lockfiles written by a newer APM than the running CLI (#2334).

``apm.lock.yaml`` records the CLI that generated it in top-level
``apm_version``. An older CLI cannot know fields or rules a newer one added, so
letting it rewrite, replay, audit or pack that lockfile silently drops or
misreads them. This module is the single owner of that compatibility decision:

* Commands that write, replay or certify the lockfile call
  :func:`require_current_cli` before any mutation; :meth:`LockFile.write`
  calls :func:`assert_writable` as the last-line backstop.
* Read-only reads call :func:`warn_if_newer` once per lockfile.

There is deliberately no override: the fix is upgrading the CLI.
"""

from __future__ import annotations

import threading
from pathlib import Path

from .lockfile import LockfileRefusal

LOCKFILE_UNKNOWN_VERSION = "unknown"

_UPGRADE_HINT = (
    "Upgrade, then retry:\n"
    "  apm self-update                            (installer script)\n"
    "  brew upgrade apm                           (Homebrew)\n"
    "  python3 -m pip install --upgrade apm-cli   (pip)"
)

_warned: set[Path] = set()
_warned_lock = threading.Lock()


class LockfileGeneratorError(RuntimeError, LockfileRefusal):
    """The lockfile's generator metadata forbids this CLI from using it."""


def running_cli_version() -> str:
    """Return the version this CLI stamps into lockfiles and reports."""
    from .lockfile import installed_apm_version

    return installed_apm_version()


def _precedence(version: object) -> tuple[int, int, int, int, int] | None:
    from ..utils.version_checker import release_precedence

    return release_precedence(version) if isinstance(version, str) else None


_RESTORE_HINT = (
    "Restore the lockfile from version control, or regenerate it with the APM "
    "version your project uses."
)


def _blocking_reason(recorded: object, declarations: int, running: str) -> tuple[str, str] | None:
    """Return ``(reason, remedy)`` when *running* must not use the lockfile."""
    if declarations > 1:
        return "it declares apm_version more than once, so its generator cannot be verified", (
            _RESTORE_HINT
        )
    if recorded is None or recorded in (LOCKFILE_UNKNOWN_VERSION, running):
        return None
    if _precedence(recorded) is None:
        return f"its apm_version {recorded!r} is not a valid APM version", _RESTORE_HINT
    if _precedence(running) is None:
        return (
            f"it was written by APM {recorded}, and this APM reports version {running!r}, "
            "which cannot be compared",
            f"Reinstall APM {recorded} or newer. {_UPGRADE_HINT}",
        )
    if _precedence(running) < _precedence(recorded):
        return (
            f"it was written by APM {recorded}, newer than this APM {running}. An older "
            "APM can drop or misread what a newer one recorded",
            f"Install APM {recorded} or newer. {_UPGRADE_HINT}",
        )
    return None


_UNPARSEABLE = object()


def _inspect(path: Path) -> tuple[object, int] | None:
    """Return ``(apm_version, declaration count)`` or ``None`` without a lockfile.

    Reads the composed top-level mapping, so flow style, indentation and
    duplicate keys (which a loader silently collapses) are all seen. An
    explicitly declared ``null`` is reported as the invalid value ``"null"``.
    Text whose generator cannot be read is reported as ``_UNPARSEABLE``.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError):
        return _UNPARSEABLE, 0
    from ..utils.yaml_io import top_level_scalar_values

    values = top_level_scalar_values(text, "apm_version")
    if values is None:
        return _UNPARSEABLE, 0
    if not values:
        return None, 0
    return ("null" if values[0] is None else values[0]), len(values)


def assert_writable(path: Path, *, action: str = "use", before_mutation: bool = True) -> None:
    """Raise :class:`LockfileGeneratorError` when this CLI must not *action* *path*.

    *before_mutation* is true for command entry gates. The ``LockFile.write``
    backstop passes false: it refuses even a lockfile whose generator cannot
    be read, and does not claim the command changed nothing else. Entry gates
    leave an unreadable lockfile to the lockfile parser's own diagnostics.
    """
    inspected = _inspect(Path(path))
    if inspected is None:
        return
    recorded, declarations = inspected
    if recorded is _UNPARSEABLE:
        if before_mutation:
            return
        blocked: tuple[str, str] | None = (
            "its generator cannot be verified because it is not readable YAML",
            _RESTORE_HINT,
        )
    else:
        blocked = _blocking_reason(recorded, declarations, running_cli_version())
    if blocked is None:
        return
    reason, remedy = blocked
    outcome = (
        "Nothing was changed."
        if before_mutation
        else "The lockfile was not rewritten; review other files this command changed."
    )
    raise LockfileGeneratorError(
        f"Refusing to {action} {_display_name(Path(path))}: {reason}. {outcome}\n{remedy}"
    )


def _display_name(path: Path) -> str:
    """Name a project lockfile by file name, any other (user scope) by its path (#3967)."""
    try:
        if path.absolute().parent == Path.cwd().absolute():
            return path.name
    except OSError:
        pass
    return str(path)


def check_current_cli(project_root: Path, *, command: str) -> Path:
    """Raise :class:`LockfileGeneratorError` before *command* touches *project_root*.

    Returns *project_root* so callers can bind the checked root inline.
    """
    from .lockfile import resolve_lockfile_path_for_read

    # Read-only resolution also finds a legacy ``apm.lock`` without renaming it.
    lockfile = resolve_lockfile_path_for_read(Path(project_root), read_only=True)
    assert_writable(lockfile, action=f"run '{command}' on")
    return project_root


def require_current_cli(project_root: Path, *, command: str) -> Path:
    """Fail a writing or certifying *command* before it touches *project_root*.

    Raises :class:`click.ClickException` so the command exits 1 with the
    message as its only output.
    """
    try:
        return check_current_cli(project_root, command=command)
    except LockfileGeneratorError as exc:
        raise refusal_exit(exc) from exc


def refusal_exit(exc: LockfileGeneratorError) -> Exception:
    """Return the exit-1 exception every command renders a refusal with.

    One renderer keeps the ``[x]`` prefix ``apm install`` prints, on stderr
    where click reports errors, so no command says ``Error: Refusing`` (#3967).
    """
    import click

    class _LockfileRefusalExit(click.ClickException):
        def show(self, file=None) -> None:
            from ..utils.console import _rich_echo

            _rich_echo(self.message, color="red", symbol="error", err=True)

    return _LockfileRefusalExit(str(exc))


def warn_if_newer(path: Path, recorded: object, *, bundle: bool = False) -> None:
    """Warn once when a read-only command reads a lockfile it cannot vouch for.

    *bundle* marks a local bundle's embedded lockfile: no command rewrites it,
    so its warning carries the remedy rather than the refusal notice (#3963).
    """
    # Reads stay cheap: duplicate detection is left to the refusing gates.
    blocked = _blocking_reason(recorded, int(recorded is not None), running_cli_version())
    if blocked is None:
        return
    reason, remedy = blocked
    resolved = Path(path).absolute()
    with _warned_lock:
        if resolved in _warned:
            return
        _warned.add(resolved)
    from ..utils.console import _rich_warning

    if bundle:
        message = f"Bundle {Path(path).name}: {reason}; continuing anyway.\n{remedy}"
    else:
        message = (
            f"{Path(path).name}: {reason}; results may be incomplete. "
            "Commands that change or verify it will refuse until this is fixed."
        )
    _rich_warning(message, symbol="warning")
