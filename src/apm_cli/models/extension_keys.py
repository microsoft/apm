"""OpenAPM vendor-extension keys (req-ext-001): the one ``x-`` key predicate."""

from __future__ import annotations

import re

# A bare ``x-`` prefix is not an extension.
EXTENSION_KEY_RE = re.compile(r"^x-[a-z][a-z0-9-]*$")


def is_extension_key(key: object) -> bool:
    """True when *key* matches OpenAPM req-ext-001 (``x-[a-z][a-z0-9-]*``)."""
    return isinstance(key, str) and EXTENSION_KEY_RE.fullmatch(key) is not None
