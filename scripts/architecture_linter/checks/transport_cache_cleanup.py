"""Require cache-clean callers to consume the verified cleanup owner."""

from __future__ import annotations

import re

from scripts.architecture_linter.checks.transport_platform_shared import (
    GROUP,
    _forbid_scan,
    _load,
    _require_subs,
    _src_python,
)
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.groups.common import violation
from scripts.architecture_linter.models import Rule, Violation

_RULE_ID = "transport-platform-cache-cleanup-outcome"
_OWNER = "src/apm_cli/cache/cleanup.py"
_CONSUMERS = ("src/apm_cli/cache/git_cache.py", "src/apm_cli/cache/http_cache.py")


def _check_cache_cleanup(provider: FactsProvider) -> tuple[Violation, ...]:
    """Keep traversal and residual classification out of clean_all consumers."""
    inventory = frozenset(provider.inventory)
    findings: list[Violation] = []
    findings.extend(
        _forbid_scan(
            provider,
            inventory,
            _RULE_ID,
            _src_python(provider, exclude={_OWNER}),
            re.compile(r"^def clean_cache_buckets\("),
            "Cache-clean outcome authority must stay in cache/cleanup.py",
            exempt=False,
        )
    )
    for path in _CONSUMERS:
        facts, errors = _load(provider, inventory, _RULE_ID, path, parse=True)
        findings.extend(errors)
        if facts is None:
            continue
        definitions = [
            definition for definition in facts.definitions if definition.name == "clean_all"
        ]
        for definition in definitions:
            calls = [
                call for call in facts.calls if definition.line <= call.line <= definition.end_line
            ]
            if [call.qualname for call in calls] != ["clean_cache_buckets"]:
                findings.append(
                    violation(_RULE_ID, path, "clean_all must delegate only to clean_cache_buckets")
                )
        findings.extend(
            _require_subs(
                provider,
                inventory,
                _RULE_ID,
                path,
                ("from .cleanup import clean_cache_buckets", "return clean_cache_buckets("),
                "clean_all must return the canonical cleanup outcome",
            )
        )
    findings.extend(
        _require_subs(
            provider,
            inventory,
            _RULE_ID,
            _OWNER,
            ("path.lstat()", "entry remains after deletion"),
            "Cleanup must verify residual state after removal",
        )
    )
    findings.extend(
        _require_subs(
            provider,
            inventory,
            _RULE_ID,
            "src/apm_cli/commands/cache.py",
            (
                "failures.extend(cache_type(root).clean_all())",
                "if failures:",
                "raise SystemExit(1)",
            ),
            "The command must consume incomplete cleanup outcomes",
        )
    )
    return tuple(findings)


RULES = (
    Rule(
        id=_RULE_ID,
        group=GROUP,
        guard_ids=(_RULE_ID,),
        description="Cache cleaning has one verified removal-outcome owner.",
        check=_check_cache_cleanup,
    ),
)
COLLECTORS: tuple[object, ...] = ()
