"""Cached Git checkouts reach apm_modules only through the verify-before-reuse owner."""

from __future__ import annotations

import re

from scripts.architecture_linter.checks.transport_platform_shared import (
    _GH_DOWNLOADER,
    GROUP,
    _forbid_scan,
    _require_subs,
    _src_python,
)
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.models import Rule, Violation

_RULE_ID = "transport-platform-git-cache-materialization"
_OWNER = "src/apm_cli/cache/tree_store.py"


def _check_cache_materialization(provider: FactsProvider) -> tuple[Violation, ...]:
    """Keep the seal, its verification and materialization in one owner."""
    inventory = frozenset(provider.inventory)
    findings: list[Violation] = []
    findings.extend(
        _forbid_scan(
            provider,
            inventory,
            _RULE_ID,
            _src_python(provider, exclude={_OWNER}),
            re.compile(r"^def (materialize_cached_checkout|verified_seal|write_seal)\(|apm-seal"),
            "Cache checkout sealing and verification must stay in cache/tree_store.py",
            exempt=False,
        )
    )
    findings.extend(
        _require_subs(
            provider,
            inventory,
            _RULE_ID,
            _OWNER,
            (
                "seal = verified_seal(entry)",
                "_evict(entry)",
                "resolved_dest = _checked_destination(dest, seal.top_level())",
                "return materialize_tree(entry, seal, dest)",
            ),
            "A cache entry must be re-hashed against its seal, evicted on mismatch, "
            "and materialized only into a checked destination",
        )
    )
    findings.extend(
        _require_subs(
            provider,
            inventory,
            _RULE_ID,
            _GH_DOWNLOADER,
            ("materialize_cached_checkout(",),
            "Downloader cache hits must materialize through the verify-before-reuse owner",
        )
    )
    findings.extend(
        _forbid_scan(
            provider,
            inventory,
            _RULE_ID,
            (_GH_DOWNLOADER,),
            re.compile(r"_cached\.iterdir\(\)|robust_copy(tree|2)\(_cached"),
            "Downloader cache hits must not copy a cached checkout without verifying it",
            exempt=False,
        )
    )
    return tuple(findings)


RULES = (
    Rule(
        id=_RULE_ID,
        group=GROUP,
        guard_ids=(_RULE_ID,),
        description="Cached Git checkouts are verified against their seal before materialization.",
        check=_check_cache_materialization,
    ),
)
COLLECTORS: tuple[object, ...] = ()
