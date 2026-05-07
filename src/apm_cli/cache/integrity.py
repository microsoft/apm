"""Integrity verification for cached git checkouts.

On every cache HIT, the checkout's HEAD must be verified against the
expected SHA to defend against poisoned cache content. A mismatch
triggers eviction and a fresh fetch.

Reads ``.git/HEAD`` directly rather than spawning ``git rev-parse``:
- ~1 ms per call vs ~250 ms for a subprocess (closes warm-install gap).
- Cannot be biased by a poisoned ``.git/config`` (no alias / hook
  expansion possible when reading a plain text file).
- For worktrees the file contains ``gitdir: <path>`` indirection;
  resolve once.
"""

from __future__ import annotations

import logging
from pathlib import Path

_log = logging.getLogger(__name__)


def _read_head_sha(checkout_dir: Path) -> str | None:
    """Return the resolved 40-char SHA at HEAD, or None on any failure.

    Handles three layouts:
    - ``.git`` is a directory: read ``.git/HEAD``; if it starts with
      ``ref: refs/...``, read that ref file.
    - ``.git`` is a file (worktree pointer): follow the ``gitdir: ...``
      indirection once.
    - Detached HEAD: ``HEAD`` already contains the raw SHA.
    """
    git_path = checkout_dir / ".git"
    try:
        if git_path.is_file():
            content = git_path.read_text(encoding="utf-8").strip()
            if content.startswith("gitdir:"):
                target = content.split(":", 1)[1].strip()
                git_dir = (checkout_dir / target).resolve()
            else:
                return None
        elif git_path.is_dir():
            git_dir = git_path
        else:
            return None

        head_path = git_dir / "HEAD"
        if not head_path.is_file():
            return None
        head_content = head_path.read_text(encoding="utf-8").strip()
        if head_content.startswith("ref:"):
            ref_target = head_content.split(":", 1)[1].strip()
            ref_path = git_dir / ref_target
            if ref_path.is_file():
                return ref_path.read_text(encoding="utf-8").strip().lower()
            packed = git_dir / "packed-refs"
            if packed.is_file():
                for raw in packed.read_text(encoding="utf-8").splitlines():
                    line = raw.strip()
                    if not line or line.startswith(("#", "^")):
                        continue
                    parts = line.split(maxsplit=1)
                    if len(parts) == 2 and parts[1] == ref_target:
                        return parts[0].lower()
            return None
        if len(head_content) == 40 and all(c in "0123456789abcdef" for c in head_content.lower()):
            return head_content.lower()
        return None
    except OSError as exc:
        _log.debug("Failed to read HEAD in %s: %s", checkout_dir, exc)
        return None


def verify_checkout_sha(checkout_dir: Path, expected_sha: str) -> bool:
    """Verify that a cached checkout's HEAD matches the expected SHA.

    Reads ``.git/HEAD`` (and follows refs / packed-refs as needed)
    rather than spawning ``git rev-parse``: faster, and cannot be
    influenced by a poisoned local ``.git/config``.

    Args:
        checkout_dir: Path to the cached checkout directory.
        expected_sha: Expected full 40-char hexadecimal SHA.

    Returns:
        ``True`` if HEAD matches, ``False`` otherwise.
    """
    if not checkout_dir.is_dir():
        return False

    actual_sha = _read_head_sha(checkout_dir)
    if actual_sha is None:
        return False

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
