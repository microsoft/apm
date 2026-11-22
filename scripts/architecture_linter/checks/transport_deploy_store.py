"""Keep the deployed-file byte store behind its verifying owner."""

from __future__ import annotations

import re

from scripts.architecture_linter.checks.transport_platform_shared import (
    GROUP,
    _forbid_scan,
    _require_subs,
    _src_python,
)
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.models import Rule, Violation

_RULE_ID = "transport-platform-deploy-store-verified-reads"
_OWNER = "src/apm_cli/cache/deploy_store.py"


def _check_deploy_store(provider: FactsProvider) -> tuple[Violation, ...]:
    """Only the owner addresses the bucket; it re-hashes reads and never follows links."""
    inventory = frozenset(provider.inventory)
    findings: list[Violation] = []
    findings.extend(
        _forbid_scan(
            provider,
            inventory,
            _RULE_ID,
            _src_python(provider, exclude={_OWNER}),
            re.compile(r"[\"']deploy_v1[\"']"),
            "The deploy store bucket must be addressed only through cache/deploy_store.py",
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
                "_KEY_RE.fullmatch(key)",
                "os.O_RDONLY | _NOFOLLOW",
                "if not stat.S_ISREG(info.st_mode)",
                "if canonical_digest(data) != key:",
                "tempfile.mkstemp(dir=entry.parent",
                "os.replace(temp, entry)",
            ),
            "Deploy store reads must verify the digest and refuse links; writes stay atomic",
        )
    )
    return tuple(findings)


RULES = (
    Rule(
        id=_RULE_ID,
        group=GROUP,
        guard_ids=(_RULE_ID,),
        description="Deployed-file bytes have one owner that verifies every read.",
        check=_check_deploy_store,
    ),
)
COLLECTORS: tuple[object, ...] = ()
