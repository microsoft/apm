"""Frozen-mutation, MCP-ownership, uninstall-reachability, and audit-replay
install analyzers.

Ports four owner guards recorded in
``.apm/architecture/owners/install-deployment.json``:
``install-deployment-frozen-mutation-eligibility``,
``install-deployment-mcp-ownership-migration``,
``install-deployment-uninstall-reachability``, and
``install-deployment-audit-replay``.
"""

from __future__ import annotations

import re

from scripts.architecture_linter.checks.install_deployment_shared import (
    _INSTALL_ADAPTER,
    _SRC_PREFIX,
    _UNINSTALL_ENGINE,
    _awk_body,
    _body_has,
    _duplicate_definition_lines,
    _facts_for,
    _lines,
    _name_calls_in,
    _present,
    _present_re,
    _python_paths,
    _summary,
)
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.groups.common import EXEMPT_MARKER, violation
from scripts.architecture_linter.models import Violation

_GUARD_MCP_OWNERSHIP = "install-deployment-mcp-ownership-migration"


_GUARD_FROZEN = "install-deployment-frozen-mutation-eligibility"


_GUARD_UNINSTALL_REACHABILITY = "install-deployment-uninstall-reachability"


_GUARD_AUDIT_REPLAY = "install-deployment-audit-replay"
_GUARD_LIFECYCLE_SERIALIZATION = "install-deployment-lifecycle-serialization"


def _first_line(facts: object, needle: str) -> int | None:
    """Return the 1-based line number of the first literal match, else None."""
    for number, line in enumerate(_lines(facts), start=1):
        if needle in line:
            return number
    return None


def _last_line_re(facts: object, pattern: re.Pattern[str]) -> int | None:
    """Return the 1-based line number of the last regex match, else None."""
    found: int | None = None
    for number, line in enumerate(_lines(facts), start=1):
        if pattern.search(line) is not None:
            found = number
    return found


_FROZEN_OWNER = "src/apm_cli/install/service.py"


def check_frozen(provider: FactsProvider) -> tuple[Violation, ...]:
    """Frozen install decisions must route through InstallService before mutation."""
    rule_id = _GUARD_FROZEN
    owner, owner_fail = _facts_for(provider, _FROZEN_OWNER, rule_id)
    adapter, adapter_fail = _facts_for(provider, _INSTALL_ADAPTER, rule_id)
    if owner_fail or adapter_fail:
        return tuple(list(owner_fail) + list(adapter_fail))

    preflight = _first_line(adapter, "InstallService.enforce_frozen(")
    migration = _first_line(adapter, "migrate_lockfile_if_needed(ctx.apm_dir)")
    add_guard = _first_line(adapter, "InstallService.reject_frozen_mutation(")
    root_guard = _first_line(adapter, "InstallService.reject_missing_frozen_root(")
    root_redirect = _first_line(adapter, "_root_redirect = install_root_redirect(")
    dedicated_mcp = _last_line_re(adapter, re.compile(r"^[ \t]*_handle_mcp_install\("))
    local_bundle = _first_line(adapter, "if len(packages) == 1 and not mcp_name")

    duplicates = _duplicate_definition_lines(
        provider,
        rule_id=rule_id,
        prefix=_SRC_PREFIX,
        pattern=re.compile(r"raise FrozenInstallError"),
        owner=_FROZEN_OWNER,
        message="Frozen install decisions must route through InstallService before mutation",
        respect_exempt=True,
    )
    owner_methods = (
        _present_re(owner, re.compile(r"^    def enforce_frozen\("))
        and _present_re(owner, re.compile(r"^    def reject_frozen_mutation\("))
        and _present_re(owner, re.compile(r"^    def reject_missing_frozen_root\("))
    )

    def _before(first: int | None, second: int | None) -> bool:
        return first is not None and second is not None and first < second

    failed = (
        not owner_methods
        or preflight is None
        or migration is None
        or not _before(preflight, migration)
        or add_guard is None
        or root_guard is None
        or root_redirect is None
        or not _before(root_guard, root_redirect)
        or dedicated_mcp is None
        or local_bundle is None
        or not _before(add_guard, dedicated_mcp)
        or not _before(add_guard, local_bundle)
        or bool(duplicates)
    )
    if not failed:
        return ()
    findings = [
        _summary(
            rule_id,
            _FROZEN_OWNER,
            "Frozen install decisions must route through InstallService before mutation",
        )
    ]
    findings.extend(duplicates)
    return tuple(findings)


_MCP_OWNERSHIP_OWNER = "src/apm_cli/install/mcp/ownership.py"


_MCP_INTEGRATOR = "src/apm_cli/integration/mcp_integrator_install.py"
_MCP_OWNERSHIP_CONSUMERS = (
    "src/apm_cli/install/mcp/integration.py",
    "src/apm_cli/install/mcp/command.py",
    "src/apm_cli/commands/uninstall/engine.py",
)


def check_mcp_ownership_migration(provider: FactsProvider) -> tuple[Violation, ...]:
    """Legacy MCP ownership migration and adoption stay owned by one module."""
    rule_id = _GUARD_MCP_OWNERSHIP
    owner, owner_fail = _facts_for(provider, _MCP_OWNERSHIP_OWNER, rule_id)
    consumer, consumer_fail = _facts_for(provider, _MCP_INTEGRATOR, rule_id)
    if owner_fail or consumer_fail:
        return tuple(list(owner_fail) + list(consumer_fail))

    duplicates = _duplicate_definition_lines(
        provider,
        rule_id=rule_id,
        prefix=_SRC_PREFIX,
        pattern=re.compile(r"^[ \t]*def migrate_legacy_project_target_servers\("),
        owner=_MCP_OWNERSHIP_OWNER,
        message="Legacy MCP target ownership migration must stay owned by install/mcp/ownership.py",
        respect_exempt=False,
    )
    duplicates += _duplicate_definition_lines(
        provider,
        rule_id=rule_id,
        prefix=_SRC_PREFIX,
        pattern=re.compile(r"^[ \t]*def resolve_mcp_target_servers\("),
        owner=_MCP_OWNERSHIP_OWNER,
        message="Legacy MCP target ownership adoption must stay owned by install/mcp/ownership.py",
        respect_exempt=False,
    )
    findings = list(duplicates)
    if not _present_re(
        owner, re.compile(r"^def migrate_legacy_project_target_servers\(")
    ) or not _present(consumer, "migrate_legacy_project_target_servers("):
        findings.append(
            _summary(
                rule_id,
                _MCP_OWNERSHIP_OWNER,
                "Legacy MCP target ownership migration must stay owned by install/mcp/ownership.py",
            )
        )
    if not _present_re(owner, re.compile(r"^def resolve_mcp_target_servers\(")):
        findings.append(
            _summary(
                rule_id,
                _MCP_OWNERSHIP_OWNER,
                "Legacy MCP target ownership adoption must stay owned by install/mcp/ownership.py",
            )
        )
    for path in _MCP_OWNERSHIP_CONSUMERS:
        consumer, consumer_fail = _facts_for(provider, path, rule_id)
        if consumer_fail:
            findings.extend(consumer_fail)
        elif not _present(consumer, "resolve_mcp_target_servers("):
            findings.append(
                _summary(
                    rule_id,
                    path,
                    "MCP ownership consumers must route legacy adoption through install/mcp/ownership.py",
                )
            )
    return tuple(findings)


def check_uninstall_reachability(provider: FactsProvider) -> tuple[Violation, ...]:
    """Uninstall engine must reuse deps/reachability.py's forward-reachability walk."""
    rule_id = _GUARD_UNINSTALL_REACHABILITY
    findings: list[Violation] = []
    engine, engine_fail = _facts_for(provider, _UNINSTALL_ENGINE, rule_id)
    if engine_fail:
        return tuple(engine_fail)

    reach_pattern = re.compile(
        r"reachability\.compute_forward_reachable_keys"
        r"|from \.\.\.deps\.reachability import"
        r"|from apm_cli\.deps\.reachability import"
    )
    if not _present_re(engine, reach_pattern):
        findings.append(
            _summary(
                rule_id,
                _UNINSTALL_ENGINE,
                "Uninstall engine must call deps/reachability.py's compute_forward_reachable_keys",
            )
        )
    for path in _python_paths(provider, "src/apm_cli/commands/uninstall/"):
        facts = provider.file_facts(path)
        if getattr(facts, "read_error", None) is not None:
            continue
        for needle, message in (
            (
                "get_apm_dependencies",
                "Only deps/reachability.py may walk an installed package's own manifest dependencies",
            ),
            (
                "resolve_local_dep_dir",
                "Uninstall must not re-derive a parallel local-anchor reachability walk",
            ),
        ):
            for number, line in enumerate(_lines(facts), start=1):
                if EXEMPT_MARKER in line:
                    continue
                column = line.find(needle)
                if column >= 0:
                    findings.append(
                        violation(rule_id, path, message, line=number, column=column + 1)
                    )
    return tuple(findings)


_AUDIT_REPLAY_OWNER = "src/apm_cli/install/audit_replay.py"


def check_audit_replay(provider: FactsProvider) -> tuple[Violation, ...]:
    """CI audit scratch materialization and replay intent must route through owners."""
    rule_id = _GUARD_AUDIT_REPLAY
    findings: list[Violation] = []
    drift, drift_fail = _facts_for(provider, "src/apm_cli/install/drift.py", rule_id)
    owner, owner_fail = _facts_for(provider, _AUDIT_REPLAY_OWNER, rule_id)
    audit, audit_fail = _facts_for(provider, "src/apm_cli/commands/audit.py", rule_id)
    ci_checks, ci_fail = _facts_for(provider, "src/apm_cli/policy/ci_checks.py", rule_id)
    failures = list(drift_fail) + list(owner_fail) + list(audit_fail) + list(ci_fail)
    if failures:
        return tuple(failures)

    run_replay_body = _awk_body(
        drift,
        re.compile(r"^def run_replay\("),
        re.compile(r"^def "),
        keep=re.compile(r"run_replay"),
    )
    if not (
        _body_has(run_replay_body, "integrate_package_primitives(")
        and _body_has(run_replay_body, "skill_subset=")
        and _body_has(run_replay_body, "package_info.dependency_ref.skill_subset")
    ):
        findings.append(
            _summary(
                rule_id,
                "src/apm_cli/install/drift.py",
                "Audit replay must preserve locked skill subset intent",
            )
        )

    audit_gate_calls = _name_calls_in(audit, "_audit_ci_gate")
    config_body = _awk_body(
        ci_checks, re.compile(r"^def _check_config_consistency\("), re.compile(r"^def ")
    )
    if (
        not _present_re(owner, re.compile(r"^def prepare_ci_audit_replay\("))
        or "prepare_ci_audit_replay" not in audit_gate_calls
        or "run_replay" in audit_gate_calls
        or not _body_has(config_body, "prepared_replay.modules_root")
    ):
        findings.append(
            _summary(
                rule_id,
                _AUDIT_REPLAY_OWNER,
                "CI audit scratch materialization must route through install/audit_replay.py",
            )
        )
    return tuple(findings)


def check_lifecycle_serialization(provider: FactsProvider) -> tuple[Violation, ...]:
    """Every declared lifecycle mutator must route through install/locking.py."""
    rule_id = _GUARD_LIFECYCLE_SERIALIZATION
    required = {
        "src/apm_cli/commands/audit.py": {
            "audit": "serialized_lifecycle_when",
        },
        "src/apm_cli/commands/approve.py": {
            "approve_cmd": "serialized_lifecycle",
            "deny_cmd": "serialized_lifecycle",
        },
        "src/apm_cli/commands/compile/cli.py": {
            "_handle_global_flag": "serialized_lifecycle_unless",
            "_run_compilation": "serialized_lifecycle_unless",
        },
        "src/apm_cli/commands/config.py": {
            "set": "serialized_lifecycle",
            "unset": "serialized_lifecycle",
        },
        "src/apm_cli/commands/deps/cli.py": {
            "clean": "serialized_lifecycle_unless",
            "update": "serialized_lifecycle",
        },
        "src/apm_cli/commands/experimental.py": {
            "enable_flag": "serialized_lifecycle",
            "disable_flag": "serialized_lifecycle",
            "reset_flags": "serialized_lifecycle",
        },
        "src/apm_cli/commands/init.py": {"init": "serialized_lifecycle"},
        "src/apm_cli/commands/install.py": {"install": "serialized_lifecycle_unless"},
        "src/apm_cli/commands/lifecycle.py": {
            "lifecycle_init": "serialized_lifecycle",
        },
        "src/apm_cli/commands/lock.py": {"_run_lock": "serialized_lifecycle"},
        "src/apm_cli/commands/marketplace/__init__.py": {
            "add": "serialized_lifecycle",
            "update": "serialized_lifecycle",
            "remove": "serialized_lifecycle",
        },
        "src/apm_cli/commands/marketplace/init.py": {"init": "serialized_lifecycle"},
        "src/apm_cli/commands/marketplace/plugin/add.py": {"add": "serialized_lifecycle"},
        "src/apm_cli/commands/marketplace/plugin/remove.py": {"remove": "serialized_lifecycle"},
        "src/apm_cli/commands/marketplace/plugin/set.py": {"set_cmd": "serialized_lifecycle"},
        "src/apm_cli/commands/plugin/init.py": {"init": "serialized_lifecycle"},
        "src/apm_cli/commands/prune.py": {"prune": "serialized_lifecycle_unless"},
        "src/apm_cli/commands/uninstall/cli.py": {"uninstall": "serialized_lifecycle"},
        "src/apm_cli/commands/update.py": {"update": "serialized_lifecycle"},
    }
    findings: list[Violation] = []
    for path, functions in required.items():
        facts, failures = _facts_for(provider, path, rule_id)
        if failures:
            findings.extend(failures)
            continue
        definitions = {definition.name: definition for definition in facts.definitions}
        for name, decorator in functions.items():
            definition = definitions.get(name)
            if definition is None or not any(
                item == decorator or item.startswith(f"{decorator}(")
                for item in definition.decorators
            ):
                findings.append(
                    _summary(
                        rule_id,
                        path,
                        f"{name} must route through @{decorator}",
                    )
                )

    watcher_path = "src/apm_cli/commands/compile/watcher.py"
    watcher, failures = _facts_for(provider, watcher_path, rule_id)
    if failures:
        findings.extend(failures)
    else:
        for name in ("_recompile", "_watch_mode"):
            if "lifecycle_operation" not in _name_calls_in(watcher, name):
                findings.append(
                    _summary(
                        rule_id,
                        watcher_path,
                        f"{name} must route through lifecycle_operation",
                    )
                )
    return tuple(findings)
