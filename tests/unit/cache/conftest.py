"""Shared fixtures for the cache unit tests."""

import pytest


@pytest.fixture(autouse=True)
def _reachable_pins(monkeypatch: pytest.MonkeyPatch) -> None:
    """Treat every pinned commit as reachable; its own cells cover the check.

    These tests script git children for checkout behaviour; the origin
    reachability check (tests/integration/test_git_cache_sha_reachability.py)
    would otherwise consume their scripted replies.
    """
    monkeypatch.setattr(
        "apm_cli.cache.commit_reachability.require_reachable_commit",
        lambda *_args, **_kwargs: None,
    )
