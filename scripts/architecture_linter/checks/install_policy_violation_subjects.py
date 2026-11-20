"""Install policy violations are reported per subject through one owner.

Guard ``install-deployment-policy-violation-subjects`` (#3835): every install
policy violation line names the offending dependency or MCP server, never the
check name. ``CheckResult.violation_subjects`` (policy/models.py) owns the
check-to-subject projection and ``emit_check_violations``
(policy/install_preflight.py) is the only caller of
``logger.policy_violation``; the install gate, the target check and the
preflight all route through it.
"""

from __future__ import annotations

import re

from scripts.architecture_linter.checks.install_policy_shared import (
    _banned,
    _configured,
    _require_all,
    _tree_python_paths,
)
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.models import Violation

RULE_POLICY_VIOLATION_SUBJECTS = "install-deployment-policy-violation-subjects"

_SRC_TREE = "src/apm_cli/"
_MODELS = "src/apm_cli/policy/models.py"
_EMITTER = "src/apm_cli/policy/install_preflight.py"
_GATE = "src/apm_cli/install/phases/policy_gate.py"
_TARGET_CHECK = "src/apm_cli/install/phases/policy_target_check.py"

# A code (not comment) call of ``.policy_violation(``.
_DIRECT_EMIT = re.compile(r"^(?!\s*#).*\.policy_violation\(")
# The #3835 defect shape: the check name passed as the dependency.
_CHECK_AS_DEP = re.compile(r"dep_ref\s*=\s*check\.name")

_REQUIRED: tuple[tuple[str, tuple[str, ...], str], ...] = (
    (
        _MODELS,
        ("def violation_subjects(self)",),
        "CheckResult must own the check-to-subject projection",
    ),
    (
        _EMITTER,
        ("for subject in check.violation_subjects():", "dep_ref=subject.ref,"),
        "emit_check_violations must report one policy_violation per subject",
    ),
    (
        _GATE,
        ("emit_check_violations(",),
        "the install policy gate must report through emit_check_violations",
    ),
    (
        _TARGET_CHECK,
        ("emit_check_violations(",),
        "the install target policy check must report through emit_check_violations",
    ),
)


def check_policy_violation_subjects(provider: FactsProvider) -> tuple[Violation, ...]:
    """Policy violations name their subject via the single emitter."""
    rule_id = RULE_POLICY_VIOLATION_SUBJECTS
    findings: list[Violation] = []
    for path, needles, message in _REQUIRED:
        lines, failures = _configured(provider, path, rule_id)
        findings.extend(failures)
        if not failures:
            findings.extend(_require_all(rule_id, path, lines, needles, message))
    findings.extend(
        _banned(
            provider,
            rule_id=rule_id,
            paths=_tree_python_paths(provider, _SRC_TREE, excluded=(_EMITTER,)),
            pattern=_DIRECT_EMIT,
            message=(
                "Direct logger.policy_violation call; report policy checks through "
                "policy/install_preflight.py::emit_check_violations"
            ),
            configured=False,
            respect_exempt=True,
        )
    )
    findings.extend(
        _banned(
            provider,
            rule_id=rule_id,
            paths=_tree_python_paths(provider, _SRC_TREE),
            pattern=_CHECK_AS_DEP,
            message="A policy check name is not a dependency; use CheckResult.violation_subjects",
            configured=False,
            respect_exempt=True,
        )
    )
    return tuple(findings)
