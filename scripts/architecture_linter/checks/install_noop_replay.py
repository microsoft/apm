"""No-op install replay ownership guard (#4770).

``install/noop_fast_path.py`` owns the decision to replay a recorded no-op
install. Its proof must keep the live checks it adds on top of the
recorded inputs: org policy re-discovery routed by the outcome owner
(``discover_trust_policy`` + ``classify_project_policy``) and
the lockfile content-hash verifier (``verify_deployed_file_hashes``). Only the
``apm install`` command (commands/install.py) may engage it, and no other
module installs process-wide audit hooks.
"""

from __future__ import annotations

import re

from scripts.architecture_linter.checks.install_policy_shared import (
    _banned,
    _configured,
    _count_re,
    _report,
    _tree_python_paths,
)
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.models import Violation

RULE_NOOP_REPLAY = "install-deployment-noop-replay-owner"

_OWNER = "src/apm_cli/install/noop_fast_path.py"
_ENGAGER = "src/apm_cli/commands/install.py"
_SOURCE_TREE = "src/apm_cli/"
_LIVE_CHECKS = (
    (re.compile(r"\bdiscover_trust_policy\(project_root\)"), "live org policy discovery"),
    (
        re.compile(r"\bclassify_project_policy\(project_root, fetch_result=result\)"),
        "the canonical policy outcome route",
    ),
    (
        re.compile(r"\bverify_deployed_file_hashes\(project_root, lock\)"),
        "the lockfile deployed-file content-hash verifier",
    ),
    (re.compile(r"\bdeployed_hashes_intact\(project_root\)"), "the deployed-hash check on replay"),
)
_ENGAGE = re.compile(r"\bnoop_fast_path\b")
# The decorator must sit directly on the install click callback.
_DECORATED_INSTALL = re.compile(r"^@noop_fast_path$")
_INSTALL_DEF = re.compile(r"^def install\(")
_AUDIT_HOOK = re.compile(r"\bsys\.addaudithook\(")


def _decorates_install(lines) -> bool:
    """``@noop_fast_path`` is the decorator line right above ``def install(``."""
    texts = [text.strip() for _, text in lines]
    return (
        any(
            _INSTALL_DEF.match(texts[i]) and i > 0 and _DECORATED_INSTALL.match(texts[i - 1])
            for i in range(len(texts))
        )
        and _count_re(lines, _DECORATED_INSTALL) == 1
    )


def check_noop_replay_owner(provider: FactsProvider) -> tuple[Violation, ...]:
    """A no-op install replays only through install/noop_fast_path.py, live checks kept."""
    rule_id = RULE_NOOP_REPLAY
    owner, owner_fail = _configured(provider, _OWNER, rule_id)
    engager, engager_fail = _configured(provider, _ENGAGER, rule_id)
    findings: list[Violation] = [*owner_fail, *engager_fail]
    if owner_fail or engager_fail:
        return tuple(findings)
    for pattern, what in _LIVE_CHECKS:
        if _count_re(owner, pattern) != 1:
            findings.append(
                _report(rule_id, _OWNER, f"a replayed no-op must re-run {what} exactly once")
            )
    if not _decorates_install(engager):
        findings.append(
            _report(
                rule_id,
                _ENGAGER,
                "apm install's callback must be decorated with @noop_fast_path directly",
            )
        )
    others = _tree_python_paths(provider, _SOURCE_TREE, excluded=(_OWNER, _ENGAGER))
    findings.extend(
        _banned(
            provider,
            rule_id=rule_id,
            paths=others,
            pattern=_ENGAGE,
            message="Only apm install (commands/install.py) engages the no-op replay",
            configured=False,
            respect_exempt=False,
        )
    )
    findings.extend(
        _banned(
            provider,
            rule_id=rule_id,
            paths=others,
            pattern=_AUDIT_HOOK,
            message="Only install/noop_fast_path.py installs a process-wide audit hook",
            configured=False,
            respect_exempt=False,
        )
    )
    return tuple(findings)
