"""Bare-repo clone + materialization helpers for the WS2 dedup pipeline.

Semantic package split of the original ``bare_cache`` module:

* :mod:`._scrub`       -- ``_rmtree``, ``_scrub_bare_remote_url``
* :mod:`._bare_clone`  -- ``bare_clone_with_fallback``
* :mod:`._fetch_sha`   -- ``fetch_sha_into_bare`` (McCabe-C901-safe)
* :mod:`._materialize` -- ``materialize_from_bare``
* :mod:`._wt_clone`    -- ``clone_with_fallback``, ``build_clone_failure_message``

All public names are re-exported here so every existing import of the
form ``from apm_cli.deps.bare_cache import <name>`` continues to work
without change.

Patch-seam contract
-------------------
Unit tests patch ``apm_cli.deps.bare_cache.subprocess.run``.  This
resolves to the real :mod:`subprocess` module via the attribute lookup
``apm_cli.deps.bare_cache.subprocess``.  The ``import subprocess``
below makes that attribute available on this package namespace; patching
``.run`` on it replaces ``subprocess.run`` globally, which is exactly
what the sub-module functions observe.
"""

from __future__ import annotations

# Explicit import so ``apm_cli.deps.bare_cache.subprocess`` resolves for
# test patches: patch("apm_cli.deps.bare_cache.subprocess.run", ...).
import subprocess  # noqa: F401

from ._bare_clone import BareCloneOpts, bare_clone_with_fallback
from ._fetch_sha import fetch_sha_into_bare
from ._materialize import materialize_from_bare
from ._scrub import _rmtree, _scrub_bare_remote_url
from ._wt_clone import (
    CloneFailureContext,
    WtCloneOpts,
    build_clone_failure_message,
    clone_with_fallback,
)

# Includes private names that existing tests import directly.
__all__ = [
    "BareCloneOpts",
    "CloneFailureContext",
    "WtCloneOpts",
    "_rmtree",
    "_scrub_bare_remote_url",
    "bare_clone_with_fallback",
    "build_clone_failure_message",
    "clone_with_fallback",
    "fetch_sha_into_bare",
    "materialize_from_bare",
]
