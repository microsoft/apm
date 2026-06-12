"""``apm doctor`` top-level command.

Thin Click wrapper around :func:`apm_cli.commands.marketplace.doctor.run_doctor`.
The diagnostics are owned by the marketplace doctor module today because that
is where the existing implementation lives; promoting the entry point to the
top level is a discoverability fix without scope expansion. Future PRs may
add additional domains (lockfile, cache, runtime, config) by extending
``run_doctor`` -- each behind its own scope justification.
"""

from __future__ import annotations

import sys

import click

from .marketplace.doctor import run_doctor


@click.command(
    help=(
        "Run environment diagnostics (git, network, auth, gh CLI, "
        "marketplace config). Reports a pass/fail table and exits non-zero "
        "if a critical check fails."
    )
)
@click.option("--verbose", "-v", is_flag=True, help="Show detailed output")
def doctor(verbose):
    """Top-level diagnostic entry point."""
    exit_code = run_doctor(verbose, logger_name="doctor")
    if exit_code != 0:
        sys.exit(exit_code)
