"""Shared help text fragments for policy-related CLI commands.

The canonical contract for what ``--policy`` / ``--policy-source`` accept
is enforced by ``discover_policy`` in ``discovery.py``. This module is the
single source of truth for the user-facing rendering of that contract,
shared by ``apm audit``, ``apm policy status``, and the consistency tests
that pin them together.

Adding or removing a form here without a matching change in
``discover_policy`` (and an updated regression test in
``tests/unit/policy/test_help_consistency.py``) will fail CI.
"""

POLICY_SOURCE_FORMS_HELP = (
    "Accepts: 'org' (auto-discover from your project's git remote), "
    "'owner/repo' (defaults to github.com), an https:// URL, or a "
    "local file path."
)
