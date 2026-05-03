"""Integrity verification for cached git checkouts.

On every cache HIT, the checkout's HEAD must be verified against the
expected SHA to defend against poisoned cache content. A mismatch
triggers eviction and a fresh fetch.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

_log = logging.getLogger(__name__)


def verify_checkout_sha(checkout_dir: Path, expected_sha: str) -> bool:
    """Verify that a cached checkout's HEAD matches the expected SHA.

    Runs ``git rev-parse HEAD`` in the checkout directory and compares
    the result against *expected_sha*.

    Args:
        checkout_dir: Path to the cached checkout directory.
        expected_sha: Expected full 40-char hexadecimal SHA.

    Returns:
        ``True`` if HEAD matches, ``False`` otherwise.
    """
    if not checkout_dir.is_dir():
        return False

    try:
        result = subprocess.run(
            ["git", "-C", str(checkout_dir), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            _log.debug(
                "git rev-parse HEAD failed in %s: %s",
                checkout_dir,
                result.stderr.strip(),
            )
            return False

        actual_sha = result.stdout.strip().lower()
        expected_lower = expected_sha.strip().lower()
        if actual_sha != expected_lower:
            _log.warning(
                "[!] Cache integrity mismatch in %s: expected %s, got %s -- evicting",
                checkout_dir,
                expected_lower[:12],
                actual_sha[:12],
            )
            return False
        return True

    except (subprocess.TimeoutExpired, OSError) as exc:
        _log.debug("Integrity check failed for %s: %s", checkout_dir, exc)
        return False
