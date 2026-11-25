"""Accept every cached repository in tests whose repositories are fakes.

Tests that lay down an empty directory or a bare ``.git`` to drive cache
hit, miss and fetch mechanics have no APM-written config to check. The trust
check itself is covered against real repositories in
``tests/integration/test_cached_repo_config.py``.
"""

from __future__ import annotations

from unittest.mock import patch


def trust_cached_repos():
    """Return a patcher (decorator or context manager) that trusts every cached repo."""
    return patch(
        "apm_cli.cache.git_cache.cached_repo_is_trusted",
        new=lambda *_args, **_kwargs: True,
    )
