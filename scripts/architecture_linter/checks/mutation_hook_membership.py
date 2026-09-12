"""User-root-scope, drift, and hook-cleanup mutation analyzers.

Ports ``hooks-integrations-user-root-scope`` (AC1 user-root scoped
instruction) plus two additional in-domain rules that carry no owner guard
because their canonical owners sit outside the hooks-integrations owner set,
yet their decisions are hook/MCP-integration semantics in this group's
domain: ``mutation_writes.drift_hook_membership`` (AC4 hook config
membership, owner ``install/manifest_reconcile.py``) and
``mutation_writes.hook_cleanup_scope`` (AC15 hook cleanup scope, owners
``commands/prune.py`` and ``commands/uninstall/``). Whoever later authors the
install/deployment group must not re-port these, to avoid split authority.
"""

from __future__ import annotations

from collections.abc import Iterable

from scripts.architecture_linter.checks.mutation_write_shared import (
    GROUP,
    _function_span,
    _has_fixed,
    _has_regex,
    _python_paths,
    _read_required,
    _require,
    _span_has_fixed,
    _span_has_regex,
)
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.groups.common import (
    EXEMPT_MARKER,
    checked_facts,
    line_pattern_violations,
    violation,
)
from scripts.architecture_linter.models import Rule, Violation

_MANIFEST_RECONCILE = "src/apm_cli/install/manifest_reconcile.py"


_PRUNE_COMMAND = "src/apm_cli/commands/prune.py"


_UNINSTALL_COMMANDS = "src/apm_cli/commands/uninstall/"


_USER_ROOT_OWNER = "src/apm_cli/integration/targets.py"


_USER_ROOT_CONSUMER = "src/apm_cli/compilation/user_root_context.py"


def _check_user_root_scope(provider: FactsProvider) -> Iterable[Violation]:
    """User-root scoped instruction eligibility must come from TargetProfile.

    Ports the AC1 guard: ``TargetProfile`` owns
    ``include_scoped_in_user_root_context``, the user-root context consumer
    reads it, and the consumer never branches on a hard-coded ``opencode``
    target name.
    """
    rule_id = "mutation_writes.user_root_scope"
    facts_by_path, failures = _read_required(
        provider, rule_id, (_USER_ROOT_OWNER, _USER_ROOT_CONSUMER)
    )
    if failures:
        return failures
    owner = facts_by_path[_USER_ROOT_OWNER]
    consumer = facts_by_path[_USER_ROOT_CONSUMER]
    findings: list[Violation] = []
    findings.extend(
        _require(
            _has_fixed(owner, "include_scoped_in_user_root_context: bool = False"),
            rule_id,
            _USER_ROOT_OWNER,
            "TargetProfile must own include_scoped_in_user_root_context metadata",
        )
    )
    findings.extend(
        _require(
            _has_fixed(consumer, "scoped.include_scoped_in_user_root_context"),
            rule_id,
            _USER_ROOT_CONSUMER,
            "user-root context consumer must read TargetProfile eligibility",
        )
    )
    findings.extend(
        _require(
            not _has_regex(consumer, r"scoped\.name\s*==\s*[\"']opencode[\"']"),
            rule_id,
            _USER_ROOT_CONSUMER,
            "user-root context consumer must not branch on a hard-coded target name",
        )
    )
    return findings


def _check_drift_hook_membership(provider: FactsProvider) -> Iterable[Violation]:
    """Drift hook membership exemptions must derive from HookIntegrator registries.

    Ports the AC4 guard (no owner guard: the file is ``manifest_reconcile.py``,
    outside the hooks-integrations owner set, but the decision is hook-config
    membership semantics in this group's domain): ``merge_hook_config_paths``
    derives membership through ``merge_hook_config_projection_specs``, which in
    turn derives it from the ``_MERGE_HOOK_TARGETS`` / ``_APM_HOOKS_SIDECAR``
    HookIntegrator registries and never hard-codes a merge-hook config filename.
    """
    rule_id = "mutation_writes.drift_hook_membership"
    facts, failures = checked_facts(provider, _MANIFEST_RECONCILE, rule_id, require_python=True)
    if failures:
        return failures
    paths_span = _function_span(facts, "merge_hook_config_paths")
    specs_span = _function_span(facts, "merge_hook_config_projection_specs")
    missing = [
        name
        for name, span in (
            ("merge_hook_config_paths", paths_span),
            ("merge_hook_config_projection_specs", specs_span),
        )
        if span is None
    ]
    if missing:
        return tuple(
            violation(
                rule_id, _MANIFEST_RECONCILE, f"drift hook membership owner {name} is missing"
            )
            for name in missing
        )
    if paths_span is None or specs_span is None:
        return ()
    findings: list[Violation] = []
    if not _span_has_fixed(facts, paths_span, "merge_hook_config_projection_specs(targets)"):
        findings.append(
            violation(
                rule_id,
                _MANIFEST_RECONCILE,
                "merge_hook_config_paths must derive membership via projection specs",
            )
        )
    if not _span_has_fixed(facts, specs_span, "_MERGE_HOOK_TARGETS"):
        findings.append(
            violation(
                rule_id, _MANIFEST_RECONCILE, "hook membership must derive from _MERGE_HOOK_TARGETS"
            )
        )
    if not _span_has_fixed(facts, specs_span, "_APM_HOOKS_SIDECAR"):
        findings.append(
            violation(
                rule_id, _MANIFEST_RECONCILE, "hook membership must derive from _APM_HOOKS_SIDECAR"
            )
        )
    if _span_has_regex(facts, specs_span, r"settings\.json|hooks\.json|apm-hooks\.json"):
        findings.append(
            violation(
                rule_id,
                _MANIFEST_RECONCILE,
                "hook membership must not hard-code merge-hook config filenames",
            )
        )
    return findings


def _check_hook_cleanup_scope(provider: FactsProvider) -> Iterable[Violation]:
    """Prune/uninstall must stay outside target-contraction hook cleanup.

    Ports the AC15 ``check_pattern`` guard (no owner guard: the files are
    prune/uninstall commands, but the decision is hook-cleanup ownership in
    this group's domain): neither the prune command nor any uninstall command
    may invoke ``reconcile_dropped_merge_hook_targets`` /
    ``reconcile_dropped_targets`` (#2250 scope).
    """
    rule_id = "mutation_writes.hook_cleanup_scope"
    paths = (_PRUNE_COMMAND, *_python_paths(provider, under=_UNINSTALL_COMMANDS))
    return tuple(
        line_pattern_violations(
            provider,
            rule_id=rule_id,
            paths=paths,
            pattern=r"reconcile_dropped_merge_hook_targets\(|reconcile_dropped_targets\(",
            message="prune/uninstall must stay outside target-contraction hook cleanup",
            exempt_marker=EXEMPT_MARKER,
        )
    )


RULES: tuple[Rule, ...] = (
    Rule(
        id="mutation_writes.user_root_scope",
        group=GROUP,
        guard_ids=("hooks-integrations-user-root-scope",),
        description="User-root scoped instruction eligibility must come from TargetProfile.",
        check=_check_user_root_scope,
    ),
    Rule(
        id="mutation_writes.drift_hook_membership",
        group=GROUP,
        guard_ids=(),
        description="Drift hook membership must derive from HookIntegrator registries.",
        check=_check_drift_hook_membership,
    ),
    Rule(
        id="mutation_writes.hook_cleanup_scope",
        group=GROUP,
        guard_ids=(),
        description="Prune/uninstall must stay outside target-contraction hook cleanup.",
        check=_check_hook_cleanup_scope,
    ),
)


COLLECTORS: tuple[object, ...] = ()


__all__ = ["COLLECTORS", "RULES"]
