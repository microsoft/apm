"""Marketplace alias validation.

Shared by `apm_cli.commands.marketplace` (legacy `apm marketplace add`)
and `apm_cli.commands.marketplace._source_ops` (top-level `apm add`),
extracted to a leaf module so neither command surface needs to import
the other to share the rule.

A legal alias is any non-empty string of ASCII letters, digits, dots,
underscores, and hyphens. The same character class is enforced by the
registry layer when manifests declare a `name`.
"""

from __future__ import annotations

import re

ALIAS_PATTERN = re.compile(r"^[a-zA-Z0-9._-]+$")


def is_valid_alias(value: str) -> bool:
    """Return True when ``value`` is a legal marketplace alias."""
    return bool(value) and ALIAS_PATTERN.match(value) is not None
