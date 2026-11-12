"""Argv ``--`` boundary helpers shared by the install and mcp commands.

Click's ``nargs=-1`` silently swallows the ``--`` separator and merges
everything after it into the positional argument tuple.  For
``apm install --mcp foo -- npx -y srv`` we cannot distinguish that from
``apm install --mcp foo npx -y srv`` once Click is done parsing.

We therefore inspect ``sys.argv`` ourselves to detect the boundary and
extract the post-``--`` portion as the stdio command argv.  ``--`` IS
present in ``sys.argv`` even though Click strips it from the parsed
arguments.  The pre-``--`` portion is used to flag conflicts.

``_get_invocation_argv`` exists as a tiny seam so tests using
``CliRunner`` (which does not modify ``sys.argv``) can patch it without
resorting to ``monkeypatch.setattr('sys.argv', ...)``.
"""

import sys


def _get_invocation_argv():
    """Return the process invocation argv. Wrapped for test injection."""
    return sys.argv


def _split_argv_at_double_dash(argv):
    """Return ``(clean_argv, command_argv_tuple)``.

    If ``--`` is not present, ``command_argv_tuple`` is ``()``.
    """
    if "--" not in argv:
        return argv, ()
    idx = argv.index("--")
    return argv[:idx], tuple(argv[idx + 1 :])
