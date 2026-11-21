"""Core package.

``AuthContext``, ``AuthResolver`` and ``HostInfo`` resolve lazily (PEP 562):
importing any ``apm_cli.core`` submodule, such as ``tls_trust`` during CLI
startup, must not load the whole auth, dependency-model and cache import graph.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from apm_cli.core.auth import AuthContext, AuthResolver, HostInfo

__all__ = ["AuthContext", "AuthResolver", "HostInfo"]


def __getattr__(name: str) -> Any:
    if name in __all__:
        return getattr(importlib.import_module("apm_cli.core.auth"), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
