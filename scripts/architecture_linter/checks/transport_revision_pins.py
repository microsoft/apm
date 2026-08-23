"""Revision-pin outcome ownership checks for the transport/platform group."""

from __future__ import annotations

import re

from scripts.architecture_linter.checks.transport_platform_shared import (
    GROUP,
    _count_checks,
    _forbid_scan,
    _require_subs,
    _src_python,
)
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.models import Rule, Violation

_RULE_ID = "transport-platform-revision-pin-outcome"
_OWNER = "src/apm_cli/deps/revision_pins.py"
_COMMAND = "src/apm_cli/commands/update.py"
_OWNER_DEFINITIONS = re.compile(
    r"^class (RevisionPinResolutionResult|RevisionPinSkip):"
    r"|^def resolve_revision_pin_updates\("
)
_COMMAND_TAG_LOOKUP = re.compile(r"find_latest_annotated_tag\(")


def _check_revision_pin_outcome(provider: FactsProvider) -> tuple[Violation, ...]:
    """Require update commands to consume the revision-pin owner's full result."""
    inventory = frozenset(provider.inventory)
    findings: list[Violation] = []
    findings.extend(
        _count_checks(
            provider,
            inventory,
            _RULE_ID,
            _OWNER,
            (
                ("re", _OWNER_DEFINITIONS.pattern, 3, "eq"),
                ("sub", "return RevisionPinResolutionResult(", 2, "eq"),
            ),
            "Revision-pin outcomes must stay owned by RevisionPinResolutionResult",
        )
    )
    findings.extend(
        _forbid_scan(
            provider,
            inventory,
            _RULE_ID,
            _src_python(provider, exclude={_OWNER}),
            _OWNER_DEFINITIONS,
            "Revision-pin outcome definitions must stay in deps/revision_pins.py",
            exempt=True,
        )
    )
    findings.extend(
        _require_subs(
            provider,
            inventory,
            _RULE_ID,
            _COMMAND,
            (
                "resolution = resolve_revision_pin_updates(",
                "for skipped in resolution.skips:",
                "revision_pin_updates = revision_pin_resolution.updates",
            ),
            "The update command must consume both revision-pin outcome collections",
        )
    )
    findings.extend(
        _forbid_scan(
            provider,
            inventory,
            _RULE_ID,
            (_COMMAND,),
            _COMMAND_TAG_LOOKUP,
            "The update command must not independently resolve annotated tags",
            exempt=True,
        )
    )
    return tuple(findings)


RULES: tuple[Rule, ...] = (
    Rule(
        id=_RULE_ID,
        group=GROUP,
        guard_ids=(_RULE_ID,),
        description="Revision-pin updates and retained SHAs share one typed outcome owner.",
        check=_check_revision_pin_outcome,
    ),
)

COLLECTORS: tuple[object, ...] = ()

__all__ = ["COLLECTORS", "RULES"]
