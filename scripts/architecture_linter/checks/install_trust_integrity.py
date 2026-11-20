"""Trust and integrity consistency guards (#4027).

* ``resolve_trust_policy`` in ``policy/outcome_routing.py`` is the one
  policy-outcome owner for executables-trust readers. Only registered,
  outcome-consuming call sites reach ``discover_policy_with_chain``; a new
  caller that reads ``.policy`` and drops the outcome fails closed here.
* ``apm update`` keeps the content-hash check for a dependency whose
  resolved commit did not move.
* ``apm unpack`` refuses executable content before it writes anything.
"""

from __future__ import annotations

from scripts.architecture_linter.checks.install_deployment_shared import (
    _facts_for,
    _first_line_re,
    _lines,
    _present,
    _python_paths,
    _summary,
)
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.groups.common import violation
from scripts.architecture_linter.models import Violation

GUARD_TRUST_POLICY_OUTCOME = "install-deployment-trust-policy-outcome"
GUARD_UPDATE_CONTENT_INTEGRITY = "install-deployment-update-content-integrity"
GUARD_UNPACK_EXECUTABLES = "install-deployment-unpack-executable-refusal"

_OWNER = "src/apm_cli/policy/outcome_routing.py"
_EXECUTABLES = "src/apm_cli/security/executables.py"
_EXEC_GATE = "src/apm_cli/install/exec_gate.py"
_LOCAL_BUNDLE = "src/apm_cli/install/local_bundle_handler.py"
_SOURCES = "src/apm_cli/install/sources.py"
_INTEGRATE = "src/apm_cli/install/phases/integrate.py"
_HELPERS = "src/apm_cli/install/source_helpers.py"
_UNPACKER = "src/apm_cli/bundle/unpacker.py"

_DISCOVERY = "discover_policy_with_chain"
# Every remaining direct discovery call consumes the outcome itself: the
# install gate and preflight route it, audit/policy commands report it,
# update and lifecycle scripts fail toward the gate. Trust readers are not
# here; they ask resolve_trust_policy.
_DISCOVERY_CALLERS = frozenset(
    {
        (_OWNER, "resolve_trust_policy"),
        ("src/apm_cli/policy/install_preflight.py", "run_policy_preflight"),
        ("src/apm_cli/install/phases/policy_gate.py", "_discover_with_chain"),
        ("src/apm_cli/commands/approve.py", "_load_org_policy"),
        ("src/apm_cli/commands/policy.py", "status"),
        ("src/apm_cli/commands/audit.py", "_audit_ci_gate"),
        ("src/apm_cli/commands/audit.py", "_resolve_fail_on_drift"),
        ("src/apm_cli/commands/update.py", "_policy"),
        ("src/apm_cli/core/lifecycle_scripts.py", "build_runner_from_context"),
        ("src/apm_cli/security/trust.py", "resolve"),
    }
)
_TRUST_READERS: dict[str, str] = {
    _EXECUTABLES: "policy = resolve_trust_policy(project_root, no_policy=no_policy, logger=logger)",
    _EXEC_GATE: "policy = resolve_trust_policy(self._project_root, announce=False)",
    _LOCAL_BUNDLE: "policy = resolve_trust_policy(",
}


def check_trust_policy_outcome(provider: FactsProvider) -> tuple[Violation, ...]:
    """Trust readers route policy outcomes through one owner."""
    findings: list[Violation] = []
    facts, failures = _facts_for(provider, _OWNER, GUARD_TRUST_POLICY_OUTCOME)
    findings.extend(failures)
    if not failures:
        for needle in (
            "def resolve_trust_policy(",
            "fetch_failure_default=read_project_fetch_failure_default(root),",
        ):
            if not _present(facts, needle):
                findings.append(
                    _summary(
                        GUARD_TRUST_POLICY_OUTCOME,
                        _OWNER,
                        f"The trust policy owner must route the outcome; missing {needle}",
                    )
                )
    for path, needle in _TRUST_READERS.items():
        facts, failures = _facts_for(provider, path, GUARD_TRUST_POLICY_OUTCOME)
        findings.extend(failures)
        if not failures and not _present(facts, needle):
            findings.append(
                _summary(
                    GUARD_TRUST_POLICY_OUTCOME,
                    path,
                    "Executables-trust readers must ask resolve_trust_policy for org policy",
                )
            )
    facts, failures = _facts_for(provider, _LOCAL_BUNDLE, GUARD_TRUST_POLICY_OUTCOME)
    if not failures and _present(facts, "cache_only"):
        findings.append(
            _summary(
                GUARD_TRUST_POLICY_OUTCOME,
                _LOCAL_BUNDLE,
                "Local bundle installs resolve org policy as package installs do, never cache-only",
            )
        )
    for path in _python_paths(provider, "src/apm_cli/"):
        path_facts, path_failures = _facts_for(provider, path, GUARD_TRUST_POLICY_OUTCOME)
        if path_failures:
            continue
        findings.extend(
            violation(
                GUARD_TRUST_POLICY_OUTCOME,
                path,
                f"{call.scope} calls {_DISCOVERY} outside the registered outcome consumers; "
                "ask policy.outcome_routing.resolve_trust_policy instead",
                line=call.line,
            )
            for call in path_facts.calls
            if call.qualname.rsplit(".", 1)[-1] == _DISCOVERY
            and (path, call.scope) not in _DISCOVERY_CALLERS
        )
    return tuple(findings)


def check_update_content_integrity(provider: FactsProvider) -> tuple[Violation, ...]:
    """``apm update`` skips the content-hash check only for a moved commit."""
    facts, failures = _facts_for(provider, _SOURCES, GUARD_UPDATE_CONTENT_INTEGRITY)
    if failures:
        return tuple(failures)
    findings: list[Violation] = []
    if not _present(facts, "not update_accepts_new_content(ctx, dep_locked_chk, resolved_commit)"):
        findings.append(
            _summary(
                GUARD_UPDATE_CONTENT_INTEGRITY,
                _SOURCES,
                "The fresh-download hash check must ask update_accepts_new_content",
            )
        )
    helpers, helper_failures = _facts_for(provider, _HELPERS, GUARD_UPDATE_CONTENT_INTEGRITY)
    findings.extend(helper_failures)
    if not helper_failures and not _present(
        helpers, "and not (resolved_commit and locked_commit and resolved_commit == locked_commit)"
    ):
        findings.append(
            _summary(
                GUARD_UPDATE_CONTENT_INTEGRITY,
                _HELPERS,
                "apm update may skip the content-hash check only for a moved commit",
            )
        )
    integrate, integrate_failures = _facts_for(provider, _INTEGRATE, GUARD_UPDATE_CONTENT_INTEGRITY)
    findings.extend(integrate_failures)
    if not integrate_failures and not _present(integrate, "and not _locked_commit_unchanged"):
        findings.append(
            _summary(
                GUARD_UPDATE_CONTENT_INTEGRITY,
                _INTEGRATE,
                "A same-commit callback receipt must not stand in for the locked content hash",
            )
        )
    lines = _lines(facts)
    for number, line in enumerate(lines, start=1):
        if line.strip() == "not ctx.update_refs" and any(
            "dep_key not in _expected_hash_deps" in later for later in lines[number : number + 2]
        ):
            findings.append(
                violation(
                    GUARD_UPDATE_CONTENT_INTEGRITY,
                    _SOURCES,
                    "apm update must not skip the content-hash check for every dependency",
                    line=number,
                )
            )
    return tuple(findings)


def check_unpack_executable_refusal(provider: FactsProvider) -> tuple[Violation, ...]:
    """``apm unpack`` refuses executable content before any write."""
    facts, failures = _facts_for(provider, _UNPACKER, GUARD_UNPACK_EXECUTABLES)
    if failures:
        return tuple(failures)
    import re

    refusal = _first_line_re(
        facts, re.compile(r"_refuse_executable_content\(unique_files, source_dir, bundle_path\)")
    )
    first_exit = min(
        (
            line
            for line in (
                _first_line_re(facts, re.compile(r"^\s+if dry_run:")),
                _first_line_re(facts, re.compile(r"shutil\.copy")),
            )
            if line is not None
        ),
        default=None,
    )
    if refusal is None or (first_exit is not None and refusal > first_exit):
        return (
            _summary(
                GUARD_UNPACK_EXECUTABLES,
                _UNPACKER,
                "apm unpack must refuse executable content before a preview or a write",
            ),
        )
    return ()
