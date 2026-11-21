"""Pin the GitHub credential class to github.com, *.ghe.com and GITHUB_HOST.

``GITHUB_APM_PAT`` and the rest of the GitHub token chain serve exactly one
host class: ``github.com``, any ``*.ghe.com`` host, and the single GHES host the
operator names in the ``GITHUB_HOST`` environment variable (OpenAPM req-sc-005
/ req-sc-013). ``core/host_providers.py`` decides credential purpose and
``utils/github_host.is_github_hostname`` mirrors it at parse time. Any widening
of either (a new suffix, a new literal host, a new provider sharing the
``modules`` credential purpose) fails this check.
"""

from __future__ import annotations

import re

from scripts.architecture_linter.checks.transport_platform_shared import (
    GROUP,
    _count_checks,
    _load,
)
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.groups.common import violation
from scripts.architecture_linter.models import FileFacts, Rule, Violation

_RULE_ID = "transport-platform-github-credential-class"
_PROVIDERS = "src/apm_cli/core/host_providers.py"
_GITHUB_HOST = "src/apm_cli/utils/github_host.py"
_HOST_PREDICATE = re.compile(r"\bh\s*(?:==|!=|\bin\b)|\bh\.(?:endswith|startswith)\(")


def _body(facts: FileFacts, name: str) -> list[str]:
    for definition in reversed(facts.definitions):
        if definition.name == name:
            return list(facts.lines[definition.line - 1 : definition.end_line])
    return []


def _check_is_github_hostname(provider: FactsProvider, inv: frozenset[str]) -> list[Violation]:
    facts, failures = _load(provider, inv, _RULE_ID, _GITHUB_HOST, parse=True)
    if failures:
        return list(failures)
    body = _body(facts, "is_github_hostname")
    stripped = [line.strip() for line in body]
    problems: list[str] = []
    for needle in (
        'if h == "github.com":',
        'if h.endswith(".ghe.com"):',
        'ghes_host = os.environ.get("GITHUB_HOST", "").lower()',
        "and ghes_host == h",
    ):
        if stripped.count(needle) != 1:
            problems.append(f"{needle!r} must appear exactly once")
    if stripped.count("return True") != 2:
        problems.append("exactly two 'return True' branches (github.com, *.ghe.com)")
    if sum(1 for line in body if _HOST_PREDICATE.search(line)) != 2:
        problems.append("no host predicate beyond github.com and *.ghe.com")
    if not problems:
        return []
    return [
        violation(
            _RULE_ID,
            _GITHUB_HOST,
            "is_github_hostname must accept only github.com, *.ghe.com and the "
            f"GITHUB_HOST literal; {'; '.join(problems)}",
        )
    ]


def _check_github_credential_class(provider: FactsProvider) -> tuple[Violation, ...]:
    inv = frozenset(provider.inventory)
    findings: list[Violation] = []
    findings.extend(
        _count_checks(
            provider,
            inv,
            _RULE_ID,
            _PROVIDERS,
            (
                ("sub", 'return host == "github.com"', 1, "eq"),
                ("sub", 'return host.endswith(".ghe.com")', 1, "eq"),
                ("sub", 'configured = os.environ.get("GITHUB_HOST", "").lower()', 1, "eq"),
                ("sub", "and configured == host", 1, "eq"),
                ("sub", "matcher=_matches_github,", 1, "eq"),
                ("sub", "matcher=_matches_ghe_cloud,", 1, "eq"),
                ("sub", "matcher=_matches_ghes,", 1, "eq"),
                ("sub", 'credential_purpose="modules",', 3, "eq"),
            ),
            "the GitHub credential class is exactly github.com, *.ghe.com and the "
            "GITHUB_HOST literal",
        )
    )
    findings.extend(_check_is_github_hostname(provider, inv))
    return tuple(findings)


RULES: tuple[Rule, ...] = (
    Rule(
        id=_RULE_ID,
        group=GROUP,
        guard_ids=(_RULE_ID,),
        description="The GitHub token chain serves only github.com, *.ghe.com and GITHUB_HOST.",
        check=_check_github_credential_class,
    ),
)

COLLECTORS: tuple[object, ...] = ()

__all__ = ["COLLECTORS", "RULES"]
