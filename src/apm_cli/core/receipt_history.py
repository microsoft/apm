"""Short per-receipt history of the hashes one installation wrote (#4428).

The deployment ledger's installation-bound session (core/deployment_ledger.py)
is the only user: each receipt key keeps up to :data:`HISTORY_LIMIT` earlier hashes this
installation wrote at the same path, so bytes APM wrote here before a branch
round trip are still recognised as APM's output. The history is local to one
checkout's receipt file, never stored in ``apm.lock.yaml``, and never deletion
evidence.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from pathlib import Path
from typing import Any

HISTORY_LIMIT = 8
_SHA256_PREFIX = "sha256:"
_LOWER_HEX = frozenset("0123456789abcdef")


def is_canonical_bundle_hash(content_hash: str | None) -> bool:
    """Return whether *content_hash* is one canonical ``sha256:<hex>`` digest."""
    digest = (
        content_hash.removeprefix(_SHA256_PREFIX)
        if isinstance(content_hash, str) and content_hash.startswith(_SHA256_PREFIX)
        else ""
    )
    return len(digest) == 64 and all(character in _LOWER_HEX for character in digest)


class ReceiptHistory:
    """Earlier hashes per receipt key, oldest first."""

    def __init__(self, hashes: Mapping[str, list[str]] | None = None) -> None:
        self._hashes: dict[str, list[str]] = dict(hashes or {})

    @classmethod
    def load(cls, raw: Any, keys: Collection[str]) -> ReceiptHistory:
        """Validate the optional ``history`` field of a receipt file."""
        if not isinstance(raw, dict) or not set(raw) <= set(keys):
            raise ValueError("invalid rendered-write history")
        for hashes in raw.values():
            if (
                not isinstance(hashes, list)
                or len(hashes) > HISTORY_LIMIT
                or not all(is_canonical_bundle_hash(h) for h in hashes)
            ):
                raise ValueError("invalid rendered-write history")
        return cls({key: list(hashes) for key, hashes in raw.items()})

    def replace(
        self, previous: str | None, replaced: str | None, key: str, new: str, *, same_binding: bool
    ) -> None:
        """Key *key* now holds *new*; keep *replaced* only for a same-binding rewrite.

        A different key, owner or claim binding starts a fresh history, so
        hashes written under one binding never become a baseline for another.
        """
        earlier = self._hashes.pop(previous, []) if previous is not None else []
        if previous != key or not same_binding:
            self._hashes.pop(key, None)
            return
        if replaced:
            earlier = [*earlier, replaced]
        earlier = [h for h in earlier if h != new][-HISTORY_LIMIT:]
        if earlier:
            self._hashes[key] = earlier
        else:
            self._hashes.pop(key, None)

    def forget(self, key: str) -> None:
        """Drop the history of *key*, whose binding changed."""
        self._hashes.pop(key, None)

    def retain(self, keys: Collection[str]) -> None:
        """Forget the history of every key not in *keys*."""
        self._hashes = {k: v for k, v in self._hashes.items() if k in keys}

    def payload(self, keys: Collection[str]) -> dict[str, list[str]]:
        """The ``history`` field for the receipt file, for *keys* only."""
        return {k: list(v) for k, v in self._hashes.items() if k in keys and v}

    def earlier_write(self, key: str | None, path: Path) -> str | None:
        """The current hash of *path* when this installation wrote it earlier, else ``None``."""
        from apm_cli.utils.content_hash import compute_file_hash

        earlier = self._hashes.get(key or "")
        if not earlier:
            return None
        try:
            current = compute_file_hash(path) if path.is_file() else None
        except OSError:
            return None
        return current if current in earlier else None
