"""Uninstall proves an aborted sync's removed file absent only through its owner (#4851).

After an aborted hook sync, ``_retain_failed_claims`` keeps every removed
file's lock claim unless ``hook_absence.absence_proven`` proves the file gone
under the deploy root ``uninstall`` pinned before changing anything. A
direct filesystem probe there would follow a swapped link and drop the claim.
"""

from __future__ import annotations

from scripts.architecture_linter.checks.install_deployment_shared import _facts_for
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.groups.common import source_text, violation
from scripts.architecture_linter.models import Violation

GUARD_UNINSTALL_ABSENCE_PROOF = "install-deployment-uninstall-absence-proof"
_CLI = "src/apm_cli/commands/uninstall/cli.py"
_OWNER = "src/apm_cli/commands/uninstall/hook_absence.py"
_PROBES = frozenset({"exists", "is_file", "is_dir", "lexists", "lstat", "stat", "isfile", "isdir"})
_OWNER_LITERALS = (
    "os.O_NOFOLLOW",
    "dir_fd=handle",
    "!= pinned",
    "follow_symlinks=False",
)


def _calls_in(facts, name: str) -> list[str] | None:
    definitions = [item for item in facts.definitions if item.name == name]
    if len(definitions) != 1:
        return None
    start, end = definitions[0].line, definitions[0].end_line
    return [call.qualname for call in facts.calls if start <= call.line <= end]


def check_uninstall_absence_proof(provider: FactsProvider) -> tuple[Violation, ...]:
    """Route aborted-sync absence through the pinned, no-follow owner."""
    rule_id = GUARD_UNINSTALL_ABSENCE_PROOF
    findings: list[Violation] = []
    facts, failures = _facts_for(provider, _CLI, rule_id)
    findings.extend(failures)
    if not failures:
        retain = _calls_in(facts, "_retain_failed_claims")
        if retain is None or "absence_proven" not in retain:
            findings.append(
                violation(
                    rule_id, _CLI, "_retain_failed_claims must prove absence via absence_proven"
                )
            )
        elif any(name.rsplit(".", 1)[-1] in _PROBES for name in retain):
            findings.append(
                violation(
                    rule_id, _CLI, "_retain_failed_claims must not probe the filesystem itself"
                )
            )
        command = _calls_in(facts, "uninstall")
        if command is None or "root_identity" not in command:
            findings.append(
                violation(
                    rule_id, _CLI, "uninstall must pin root_identity before changing anything"
                )
            )
    owner, failures = _facts_for(provider, _OWNER, rule_id)
    findings.extend(failures)
    if not failures:
        text = source_text(owner)
        findings.extend(
            violation(rule_id, _OWNER, f"the absence proof must keep {literal}")
            for literal in _OWNER_LITERALS
            if literal not in text
        )
    return tuple(findings)
