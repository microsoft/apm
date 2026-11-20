"""Deferred update notice for the root CLI.

The notice lives in ``apm_cli.commands._helpers``, which imports most of the
install stack; importing it lazily keeps ``apm --help`` and ``apm --version``
within the startup import budget.
"""

from __future__ import annotations


def check_and_notify_updates() -> None:
    """Run the update notice, importing its helpers only when it runs."""
    from apm_cli.commands._helpers import _check_and_notify_updates

    _check_and_notify_updates()
