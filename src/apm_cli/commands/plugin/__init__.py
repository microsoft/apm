"""``apm plugin`` command group.

Hosts plugin-author subcommands. Currently ships with ``init``
(scaffold plugin.json + apm.yml). Sibling verbs (``validate``,
``publish``, ...) may be added later -- the group is shaped to
mirror the existing ``apm marketplace`` noun namespace.
"""

from __future__ import annotations

import click

from .init import init as _init


@click.group(help="Scaffold and manage plugins (plugin-author workflows)")
def plugin() -> None:
    """``apm plugin`` -- plugin-author noun namespace."""


plugin.add_command(_init, name="init")
