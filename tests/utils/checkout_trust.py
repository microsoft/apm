"""Stub the checkout content verifier for tests whose checkouts are fakes.

Tests that lay down a bare ``.git`` directory to drive cache hit, miss and
eviction mechanics have no real objects to verify. The verifier itself is
covered against real repositories in ``tests/unit/cache/test_checkout_verify.py``.
"""

from __future__ import annotations

from unittest.mock import patch


def trust_checkout_contents():
    """Return a patcher (decorator or context manager) that accepts every checkout."""
    return patch(
        "apm_cli.cache.checkout_verify.verify_checkout",
        new=lambda *_args, **_kwargs: True,
    )
