"""Shared, side-effect-free helpers for transport/platform analyzers.

Every helper here is used by two or more of the cohesive check-family
modules (:mod:`transport_auth_platform`, :mod:`transport_cache_identity`,
:mod:`transport_sparse_and_updates`, :mod:`transport_network_and_runtime`);
splitting them out avoids duplicating the same grep/require/forbid
primitives in every family module.
"""

from __future__ import annotations

import re

from scripts.architecture_linter.checks import repository_cache_identity as cache_identity
from scripts.architecture_linter.checks.lexical_shared import (
    count_contracts,
    count_literal,
    forbid_scan,
    load_required,
    paths_under,
    require_literals,
    require_regexes,
    source_python_paths,
)
from scripts.architecture_linter.groups.common import source_text
from scripts.architecture_linter.models import FileFacts

GROUP = "transport_platform"

_count_checks = count_contracts
_count_sub = count_literal
_forbid_scan = forbid_scan
_load = load_required
_paths_under = paths_under
_require_res = require_regexes
_require_subs = require_literals
_src_python = source_python_paths

_SENTINEL = "pyproject.toml"
_SRC_PREFIX = "src/apm_cli/"
_PY: tuple[str, ...] = (".py",)


def _has(facts: FileFacts, needle: str) -> bool:
    """Return True when a single-line literal appears anywhere (grep -q / -Fq)."""
    return needle in source_text(facts)


_TIERED = cache_identity.TIERED_RESOLVER_PATH


_GH_DOWNLOADER = "src/apm_cli/deps/github_downloader.py"


_NET_OWNER_DEFS = re.compile(r"^def (parse_host_address|is_loopback_host)\(")
