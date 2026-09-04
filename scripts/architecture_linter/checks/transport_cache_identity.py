"""Repository cache-identity transport analyzer.

Ports ``transport-platform-git-cache-identity`` -- ``cache/url_normalize.py``
owns repository cache-key normalization (legacy AC11 + cache-identity
helper). This rule is the single in-process owner of BOTH halves of that
decision: the legacy lexical greps, and the structural shapes ported from
``scripts/check_repository_cache_identity_owner.py`` into
:mod:`scripts.architecture_linter.checks.repository_cache_identity`, which
catch renamed, indirect, or post-normalization truncation that no literal
grep can see.

Kept in its own module (rather than folded into a larger family file) because
``tests/integration/test_architecture_cache_identity_linter.py`` asserts this
exact module never reads, parses, or traverses on its own -- an isolated
module keeps that boundary easy to audit.
"""

from __future__ import annotations

import re

from scripts.architecture_linter.checks import repository_cache_identity as cache_identity
from scripts.architecture_linter.checks.transport_platform_shared import (
    _GH_DOWNLOADER,
    _PY,
    _SENTINEL,
    _TIERED,
    GROUP,
    _count_checks,
    _forbid_scan,
    _paths_under,
    _require_subs,
    _src_python,
)
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.groups.common import violation
from scripts.architecture_linter.models import Rule, Violation


def _subdir_python(provider: FactsProvider, prefix: str) -> tuple[str, ...]:
    return _paths_under(provider, prefix, _PY)


_RID_CACHE = "transport-platform-git-cache-identity"


_SHARED_CLONE = cache_identity.SHARED_CACHE_PATH


def _cache_identity_state(
    provider: FactsProvider, inv: frozenset[str], path: str
) -> cache_identity.SourceState:
    """Describe one owner module from facts the shared traversal already built.

    The inventory answers "does this file exist"; the memoized
    :class:`~scripts.architecture_linter.models.FileFacts` answer everything
    else. Nothing here opens a file, walks a directory, or parses source.
    """
    if path not in inv:
        return cache_identity.SourceState(
            path=path, in_inventory=False, read_error=None, parse_error=None, index=None
        )
    facts = provider.file_facts(path)
    return cache_identity.SourceState(
        path=path,
        in_inventory=True,
        read_error=facts.read_error,
        parse_error=facts.parse_error,
        index=provider.tree_index(path),
    )


def _as_cache_violation(inv: frozenset[str], finding: cache_identity.IdentityFinding) -> Violation:
    """Adapt one structural finding into an engine violation.

    A finding about an absent path is re-anchored to a present sentinel so the
    annotation remains navigable, with the missing path preserved verbatim in
    the message.
    """
    if finding.path in inv:
        return violation(_RID_CACHE, finding.path, finding.message, line=finding.line)
    return violation(_RID_CACHE, _SENTINEL, f"{finding.path}: {finding.message}")


def _semantic_cache_identity(provider: FactsProvider, inv: frozenset[str]) -> tuple[Violation, ...]:
    """Structural half: the shapes a literal grep cannot see.

    Ports ``scripts/check_repository_cache_identity_owner.py`` in full --
    exact assign/call counts in ``SharedCloneCache.get_or_clone``, the direct
    ``_repository_cache_identity`` composition, its three consumers, and every
    fail-closed case -- so renamed helpers, indirect bindings, and
    post-normalization truncation are caught here rather than only by the
    standalone CLI.
    """
    findings = cache_identity.analyze(
        _cache_identity_state(provider, inv, _SHARED_CLONE),
        _cache_identity_state(provider, inv, _TIERED),
    )
    return tuple(_as_cache_violation(inv, finding) for finding in findings)


def _check_git_cache_identity(provider: FactsProvider) -> tuple[Violation, ...]:
    inv = frozenset(provider.inventory)
    findings: list[Violation] = []

    findings.extend(_semantic_cache_identity(provider, inv))
    findings.extend(
        _require_subs(
            provider,
            inv,
            _RID_CACHE,
            _SHARED_CLONE,
            ("repository = normalize_repo_url(repository_url)", "key = (repository, ref)"),
            "SharedCloneCache must key by the fully normalized repository identity",
        )
    )
    findings.extend(
        _require_subs(
            provider,
            inv,
            _RID_CACHE,
            _GH_DOWNLOADER,
            ("repository_url = dep_ref.to_github_url()",),
            "Downloader cache consumers must pass the complete canonical Git URL",
        )
    )
    findings.extend(
        _require_subs(
            provider,
            inv,
            _RID_CACHE,
            _TIERED,
            (
                "cache_shard_key(dep_ref.to_github_url())",
                "return normalize_repo_url(dep_ref.to_github_url())",
            ),
            "Tiered ref resolution must reuse the persistent Git cache identity",
        )
    )
    findings.extend(
        _count_checks(
            provider,
            inv,
            _RID_CACHE,
            _TIERED,
            (("sub", "_repository_cache_identity(dep_ref)", 2, "ge"),),
            "Per-run ref resolution must reuse the full repository cache identity",
        )
    )
    findings.extend(
        _forbid_scan(
            provider,
            inv,
            _RID_CACHE,
            (_GH_DOWNLOADER,),
            re.compile(r'cache_(host|owner|repo)|_canonical_url\s*=\s*f?"https://'),
            "Repository cache identity must not truncate repository paths",
            exempt=True,
        )
    )
    findings.extend(
        _forbid_scan(
            provider,
            inv,
            _RID_CACHE,
            _subdir_python(provider, "src/apm_cli/deps/"),
            re.compile(r"cache_shard_key\(dep_ref\.repo_url\)"),
            "Tiered ref resolution must not derive cache shards from bare repo_url",
            exempt=True,
        )
    )
    findings.extend(
        _forbid_scan(
            provider,
            inv,
            _RID_CACHE,
            (_TIERED,),
            re.compile(r"cache\.(get|put)\(dep_ref\.repo_url|key\s*=\s*\(dep_ref\.repo_url"),
            "Per-run ref resolution must not key caches by bare repo_url",
            exempt=True,
        )
    )
    findings.extend(
        _forbid_scan(
            provider,
            inv,
            _RID_CACHE,
            _src_python(provider),
            re.compile(r"to_repository_cache_url"),
            "Repository cache keys must stay owned by cache/url_normalize.py",
            exempt=True,
        )
    )
    return tuple(findings)


RULES: tuple[Rule, ...] = (
    Rule(
        id=_RID_CACHE,
        group=GROUP,
        guard_ids=(_RID_CACHE,),
        description="Repository cache-key normalization stays owned by cache/url_normalize.py.",
        check=_check_git_cache_identity,
    ),
)


COLLECTORS: tuple[object, ...] = ()


__all__ = ["COLLECTORS", "RULES"]
