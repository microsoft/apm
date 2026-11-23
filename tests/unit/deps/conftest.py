"""Shared fixtures for the dependency unit tests."""

import pytest


@pytest.fixture(autouse=True)
def _reachable_pins(monkeypatch: pytest.MonkeyPatch) -> None:
    """Treat pinned commits as reachable; the admission has its own cells.

    These tests script git children for download paths; the origin
    reachability check (tests/integration/test_git_cache_sha_reachability.py
    and the install lifecycle cells) would otherwise reach the network.
    """
    monkeypatch.setattr(
        "apm_cli.cache.commit_reachability.require_reachable_commit",
        lambda *_args, **_kwargs: None,
    )
