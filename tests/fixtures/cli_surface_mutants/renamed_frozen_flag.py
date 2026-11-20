"""Recorded mutant of the CLI surface: ``apm install --frozen`` is renamed ``--locked`` (#4113).

The docs are not updated. ``tests/quality/test_cli_docs_surface.py``
installs these parameters on the real ``install`` command and requires the
docs-surface ratchet to fail in both directions.
"""

import click


def mutant_params(install: click.Command) -> list[click.Parameter]:
    """Return *install*'s parameters with ``--frozen`` renamed to ``--locked``."""
    params: list[click.Parameter] = []
    for param in install.params:
        if isinstance(param, click.Option) and "--frozen" in param.opts:
            param = click.Option(["--locked"], is_flag=True, help=param.help)
        params.append(param)
    return params
