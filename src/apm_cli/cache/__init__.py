"""Persistent content-addressable cache for APM install.

Public API
----------
- :func:`get_cache_root` -- resolve the platform cache directory
- :class:`GitCache` -- content-addressable git repository + checkout cache
- :class:`HttpCache` -- HTTP response cache with conditional revalidation
"""

from __future__ import annotations

from .git_cache import GitCache
from .http_cache import HttpCache
from .paths import get_cache_root

__all__ = ["GitCache", "HttpCache", "get_cache_root"]
