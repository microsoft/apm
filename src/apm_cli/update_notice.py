"""Deferred update notice for the root CLI.

The notice lives in ``apm_cli.commands._helpers``, which imports most of the
install stack; importing it lazily keeps ``apm --help`` and ``apm --version``
within the startup import budget. On most runs the notice has nothing to do
(the daily check already ran, self-update is off, or E2E mode), and that is
decided here from the same owners without importing the helpers at all.
"""

from __future__ import annotations

import os

# The notice's first line; the verified-state record never records a run that printed it.
NEW_VERSION_NOTICE = "A new version of APM is available: "


def _gate() -> bool | None:
    """Every gate but the daily schedule: True when the notice has nothing to do,
    False when the helper has work whatever the schedule, None when the schedule decides."""
    from apm_cli.update_policy import is_self_update_enabled

    if os.environ.get("APM_INSTALL_FAST_PATH", "").strip().lower() == "decide":
        return True  # the install decision seam reads only: no update cache, no fetch
    if not is_self_update_enabled():
        return True
    if os.environ.get("APM_E2E_TESTS", "").lower() in ("1", "true", "yes"):
        return True
    from apm_cli.deps.egress_route import EgressSurface, is_refused

    if is_refused(EgressSurface.VERSION_CHECK):
        return False  # the helper prints a verbose note
    return None


def schedule_decides() -> bool:
    """Whether the daily check schedule alone decides this run's notice."""
    return _gate() is None


def _nothing_to_do() -> bool:
    """True only when ``_check_and_notify_updates`` would print nothing and fetch nothing."""
    gate = _gate()
    if gate is not None:
        return gate
    from apm_cli.utils.version_checker import should_check_for_updates

    # check_for_updates returns None without a fetch while the daily check is fresh.
    return not should_check_for_updates()


def check_and_notify_updates() -> None:
    """Run the update notice, importing its helpers only when it has work to do."""
    try:
        if _nothing_to_do():
            return
    except Exception:
        pass  # any doubt: the helper decides, as before
    from apm_cli.commands._helpers import _check_and_notify_updates

    _check_and_notify_updates()
