"""Ownership check for how ``git ls-remote`` tag records resolve to commits."""

from __future__ import annotations

import re

from scripts.architecture_linter.checks.transport_platform_shared import (
    GROUP,
    _count_checks,
    _forbid_scan,
    _src_python,
)
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.models import Rule, Violation

_RULE_ID = "transport-platform-ls-remote-tag-commits"
_OWNER = "src/apm_cli/deps/git_remote_ops.py"
_OWNER_DEFINITION = re.compile(r"^def tag_commit_shas\(")
# A peeled ``refs/tags/<name>^{}`` record is recognised only by the owner.
_PEELED_RECORD = re.compile(r"""["']\^\{\}["']""")


def _check_ls_remote_tag_commits(provider: FactsProvider) -> tuple[Violation, ...]:
    """Keep the tag-to-commit decision for ls-remote records in one function."""
    inventory = frozenset(provider.inventory)
    findings: list[Violation] = []
    findings.extend(
        _count_checks(
            provider,
            inventory,
            _RULE_ID,
            _OWNER,
            (("re", _OWNER_DEFINITION.pattern, 1, "eq"),),
            "ls-remote tag-to-commit resolution must stay owned by tag_commit_shas",
        )
    )
    findings.extend(
        _forbid_scan(
            provider,
            inventory,
            _RULE_ID,
            _src_python(provider, exclude={_OWNER}),
            _PEELED_RECORD,
            "Only deps/git_remote_ops.py may interpret peeled ^{} ls-remote records",
            exempt=True,
        )
    )
    return tuple(findings)


RULES: tuple[Rule, ...] = (
    Rule(
        id=_RULE_ID,
        group=GROUP,
        guard_ids=(_RULE_ID,),
        description="ls-remote tag records resolve to commits only through tag_commit_shas.",
        check=_check_ls_remote_tag_commits,
    ),
)

COLLECTORS: tuple[object, ...] = ()

__all__ = ["COLLECTORS", "RULES"]
