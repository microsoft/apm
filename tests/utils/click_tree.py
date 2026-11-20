"""Walk the apm Click tree and resolve ``apm <cmd> [flags]`` command lines.

Standalone test helper (no other test dependencies). A command line that
APM prints as advice is *true* when every leading word names a real
subcommand and every ``-x`` / ``--flag`` it passes is an option of the
command it reaches.

Public API:

* :func:`iter_commands` -- every ``(path, command)`` in the tree.
* :func:`resolve` -- resolve one argv to a :class:`Resolution`.
* :func:`resolve_line` -- the same for a shell-quoted command line.
"""

from __future__ import annotations

import shlex
from collections.abc import Iterator, Sequence
from dataclasses import dataclass

import click


def root_group() -> click.Group:
    """Return the ``apm`` root Click group."""
    from apm_cli.cli import cli

    return cli


def _subcommand(group: click.Group, name: str) -> click.Command | None:
    with click.Context(group, info_name=group.name) as ctx:
        return group.get_command(ctx, name)


def iter_commands(
    group: click.Group | None = None, path: tuple[str, ...] = ()
) -> Iterator[tuple[tuple[str, ...], click.Command]]:
    """Yield ``(path, command)`` for every command below *group*, depth first."""
    group = group or root_group()
    with click.Context(group, info_name=group.name) as ctx:
        names = group.list_commands(ctx)
    for name in names:
        command = _subcommand(group, name)
        if command is None:
            continue
        yield (*path, name), command
        if isinstance(command, click.Group):
            yield from iter_commands(command, (*path, name))


def _option_names(command: click.Command) -> frozenset[str]:
    names = {"--help", "-h"}
    for param in command.params:
        if isinstance(param, click.Option):
            names.update(param.opts)
            names.update(param.secondary_opts)
    return frozenset(names)


def _option(command: click.Command, token: str) -> click.Option | None:
    name = token.split("=", 1)[0]
    for param in command.params:
        if isinstance(param, click.Option) and name in (*param.opts, *param.secondary_opts):
            return param
    return None


@dataclass(frozen=True)
class Resolution:
    """Where an argv lands in the Click tree, and what it got wrong."""

    path: tuple[str, ...]
    command: click.Command | None
    unknown_command: str | None
    unknown_options: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return self.command is not None and not self.unknown_command and not self.unknown_options


def resolve(argv: Sequence[str], group: click.Group | None = None) -> Resolution:
    """Resolve *argv* (without the leading ``apm``) against the Click tree.

    Leading words select subcommands while the current command is a group;
    a group's own options (and their values) may precede its subcommand.
    Every later token that starts with ``-`` must be an option of the
    command reached (``--name=value`` is checked by its name). Positional
    values, including placeholders such as ``<name>`` or ``{}``, are not
    checked.
    """
    command: click.Command = group or root_group()
    path: list[str] = []
    tokens = list(argv)
    while tokens and isinstance(command, click.Group):
        if tokens[0].startswith("-"):
            option = _option(command, tokens[0])
            if option is None:
                break
            token = tokens.pop(0)
            if "=" not in token and not option.is_flag and not option.count and tokens:
                tokens.pop(0)
            continue
        child = _subcommand(command, tokens[0])
        if child is None:
            return Resolution(tuple(path), None, tokens[0], ())
        path.append(tokens.pop(0))
        command = child
    if isinstance(command, click.Group) and not path and not tokens:
        return Resolution((), command, None, ())
    known = _option_names(command)
    unknown = tuple(
        token
        for token in tokens
        if token.startswith("-") and token != "--" and token.split("=", 1)[0] not in known
    )
    return Resolution(tuple(path), command, None, unknown)


def resolve_line(line: str, group: click.Group | None = None) -> Resolution:
    """Resolve a shell-quoted ``apm ...`` command line."""
    argv = shlex.split(line)
    if not argv or argv[0] != "apm":
        raise ValueError(f"not an apm command line: {line!r}")
    if "--" in argv:
        argv = argv[: argv.index("--")]
    return resolve(argv[1:], group)


__all__ = ["Resolution", "iter_commands", "resolve", "resolve_line", "root_group"]
