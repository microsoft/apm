"""Top-level ``apm add`` / ``apm remove`` commands.

These are thin Click wrappers that delegate to
:mod:`apm_cli.commands.marketplace._source_ops`. Issue #1075 promoted the
legacy ``apm marketplace add``/``apm marketplace remove`` surface to a
top-level command so plugin source registration matches the muscle memory
of ``npm install`` / ``brew tap`` / Claude Code's ``/plugin marketplace add``.

Differences from the legacy commands:

* ``apm add`` accepts MULTIPLE positional ``OWNER/REPO`` arguments and
  registers each in turn. Non-security failures continue the batch and
  emit a summary; security-class failures (path traversal) abort.
* A bare argument with no slash (``apm add cool-plugin``) is treated as a
  smart typo and surfaces an explicit error suggesting ``apm install``.
* ``--name`` is mutually exclusive with multi-source.

The legacy ``apm marketplace add``/``apm marketplace remove`` continue to
work and emit a one-line stderr deprecation tip on success.
"""

from __future__ import annotations

import sys

import click


@click.command(
    name="add",
    help=(
        "Register one or more plugin marketplaces from OWNER/REPO. "
        "Legacy spelling: `apm marketplace add`."
    ),
)
@click.argument("repos", nargs=-1, required=True, metavar="OWNER/REPO [OWNER/REPO ...]")
@click.option(
    "--name",
    "-n",
    default=None,
    help="Display name for the source (single-source only; defaults to repo name).",
)
@click.option("--branch", "-b", default="main", show_default=True, help="Branch to use")
@click.option("--host", default=None, help="Git host FQDN (default: github.com)")
@click.option("--verbose", "-v", is_flag=True, help="Show detailed output")
def add(repos, name, branch, host, verbose):
    """Register marketplace sources.

    Examples:

      apm add github/awesome-copilot
      apm add microsoft/azure-skills acme/security-skills
      apm add --name corp acme-corp/internal-marketplace
    """
    from apm_cli.commands.marketplace._source_ops import do_add_sources

    rc = do_add_sources(
        repos=tuple(repos),
        name=name,
        branch=branch,
        host=host,
        verbose=verbose,
        invoked_as_legacy=False,
    )
    if rc != 0:
        sys.exit(rc)


@click.command(
    name="remove",
    help=("Unregister a plugin marketplace by name. Legacy spelling: `apm marketplace remove`."),
)
@click.argument("name", required=True, metavar="NAME")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt")
@click.option("--verbose", "-v", is_flag=True, help="Show detailed output")
def remove(name, yes, verbose):
    """Unregister a marketplace by name.

    Example:

      apm remove awesome-copilot
    """
    from apm_cli.commands.marketplace._source_ops import do_remove_source

    rc = do_remove_source(
        name=name,
        yes=yes,
        verbose=verbose,
        invoked_as_legacy=False,
    )
    if rc != 0:
        sys.exit(rc)
