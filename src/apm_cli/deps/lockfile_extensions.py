"""Top-level ``x-`` vendor extension keys in ``apm.lock.yaml`` (req-lk-014, #4261).

APM never interprets these keys, but every lockfile rewrite keeps them: a lock
read from disk remembers its keys, and a lock rebuilt from resolution carries
over the keys of the document it replaces unless it sets them itself.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def vendor_extensions(data: Mapping[Any, Any]) -> dict[str, Any]:
    """Return the top-level ``x-`` keys of a lockfile document."""
    return {
        key: value for key, value in data.items() if isinstance(key, str) and key.startswith("x-")
    }


def carry_vendor_extensions(lock: Any, existing: Any) -> None:
    """Keep *existing*'s top-level ``x-`` keys on *lock* where it sets none."""
    for key, value in existing._extensions.items():
        lock._extensions.setdefault(key, value)
