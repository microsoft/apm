"""Remediation advice routes through ``core/remediation.py`` (#4090).

Every "run 'apm ...' to ..." line APM prints is a structured
:class:`~apm_cli.core.remediation.Remediation` from the owner's closed
catalog, so an installed-CLI proof can run the advised command and observe that
it clears the reported state. Free-text advice anywhere else in
``src/apm_cli`` is a second, unproven authority.

Four rules:

* ``contracts-tooling-remediation-advice`` -- closed world. No non-docstring
  string literal outside the owner may spell remediation advice, and only the
  owner constructs ``Remediation``. Files not yet migrated are pinned by an
  exact per-file count in ``_UNMIGRATED_ADVICE``: a count may only shrink, and
  the entry must shrink with it.
* ``contracts-tooling-remediation-drift-kinds`` (#3994 H7) -- an edited
  (``modified``) drift finding is restored by deleting it and reinstalling;
  plain re-sync keeps the edit, so it is never the remediation for that kind.
* ``contracts-tooling-frozen-help`` (#3994 DX5) -- ``install --frozen`` help
  names the deployed-hash check it enforces.
* ``contracts-tooling-init-apply-discover`` (#3994 DX6) -- ``init --apply``
  implies ``--discover``.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator

from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.groups.common import checked_facts, inventory_paths, violation
from scripts.architecture_linter.models import Rule, Violation

ADVICE_RULE = "contracts-tooling-remediation-advice"
DRIFT_KINDS_RULE = "contracts-tooling-remediation-drift-kinds"
FROZEN_HELP_RULE = "contracts-tooling-frozen-help"
INIT_APPLY_RULE = "contracts-tooling-init-apply-discover"

_OWNER = "src/apm_cli/core/remediation.py"
_DRIFT_RENDER = "src/apm_cli/install/drift_render.py"
_INSTALL_COMMAND = "src/apm_cli/commands/install.py"
_INIT_COMMAND = "src/apm_cli/commands/init.py"
_SCANNED_PREFIX = "src/apm_cli/"

_COMMAND = r"apm\s+[a-z][a-z-]*"
_VERB = r"(?i:run|rerun|re-run|use|try|repeat|retry|then)"
ADVICE_PATTERN = re.compile(
    rf"\b{_VERB}\b:?\s+[`'\"]?{_COMMAND}"
    rf"|[`']{_COMMAND}[^`'\n]*[`']\s+(?:to|first|again|instead|without|per)\b"
    rf"|\b(?i:with|via)\s+[`']{_COMMAND}"
    rf"|:\s+[`']?{_COMMAND}"
    rf"|\({_COMMAND}\)"
)

# Files whose advice predates the owner, with their exact count of advice
# literals. Migrating a site lowers its count; the entry must follow (#4090).
_UNMIGRATED_ADVICE: dict[str, int] = {
    "src/apm_cli/adapters/client/base.py": 2,
    "src/apm_cli/adapters/client/intellij.py": 5,
    "src/apm_cli/adapters/client/vscode.py": 1,
    "src/apm_cli/agent_plugins/errors.py": 2,
    "src/apm_cli/bundle/agent_plugin_exporter.py": 6,
    "src/apm_cli/bundle/attest.py": 1,
    "src/apm_cli/bundle/packer.py": 3,
    "src/apm_cli/bundle/plugin_exporter.py": 5,
    "src/apm_cli/bundle/unpacker.py": 4,
    "src/apm_cli/claude_plugins/registrar.py": 4,
    "src/apm_cli/cli.py": 2,
    "src/apm_cli/codex_plugins/client.py": 1,
    "src/apm_cli/codex_plugins/registrar.py": 5,
    "src/apm_cli/codex_plugins/snapshot.py": 1,
    "src/apm_cli/commands/approve.py": 12,
    "src/apm_cli/commands/auth.py": 1,
    "src/apm_cli/commands/cache.py": 1,
    "src/apm_cli/commands/compile/cli.py": 16,
    "src/apm_cli/commands/compile/staging.py": 1,
    "src/apm_cli/commands/compile/watcher.py": 1,
    "src/apm_cli/commands/config.py": 6,
    "src/apm_cli/commands/deps/cli.py": 9,
    "src/apm_cli/commands/deps/why.py": 3,
    "src/apm_cli/commands/experimental.py": 4,
    "src/apm_cli/commands/export_patch.py": 4,
    "src/apm_cli/commands/find.py": 1,
    "src/apm_cli/commands/init.py": 13,
    "src/apm_cli/commands/lifecycle.py": 10,
    "src/apm_cli/commands/lock.py": 4,
    "src/apm_cli/commands/marketplace/__init__.py": 16,
    "src/apm_cli/commands/marketplace/audit.py": 2,
    "src/apm_cli/commands/marketplace/doctor.py": 2,
    "src/apm_cli/commands/marketplace/init.py": 1,
    "src/apm_cli/commands/marketplace/plugin/__init__.py": 2,
    "src/apm_cli/commands/mcp.py": 2,
    "src/apm_cli/commands/outdated.py": 1,
    "src/apm_cli/commands/pack.py": 11,
    "src/apm_cli/commands/prune.py": 6,
    "src/apm_cli/commands/publish.py": 1,
    "src/apm_cli/commands/runtime.py": 2,
    "src/apm_cli/commands/self_update.py": 4,
    "src/apm_cli/commands/uninstall/cli.py": 13,
    "src/apm_cli/commands/uninstall/engine.py": 11,
    "src/apm_cli/commands/update.py": 9,
    "src/apm_cli/commands/view.py": 3,
    "src/apm_cli/commands/view_file.py": 3,
    "src/apm_cli/compilation/agents_compiler.py": 5,
    "src/apm_cli/compilation/distributed_compiler.py": 2,
    "src/apm_cli/compilation/footer.py": 1,
    "src/apm_cli/compilation/root_context_protection.py": 1,
    "src/apm_cli/compilation/user_root_context.py": 2,
    "src/apm_cli/copilot_plugins/settings.py": 1,
    "src/apm_cli/core/apm_yml.py": 1,
    "src/apm_cli/core/auth.py": 3,
    "src/apm_cli/core/build_orchestrator.py": 2,
    "src/apm_cli/core/command_logger.py": 3,
    "src/apm_cli/core/errors.py": 5,
    "src/apm_cli/core/experimental.py": 2,
    "src/apm_cli/core/lifecycle_scripts.py": 1,
    "src/apm_cli/core/plugin_manifest.py": 1,
    "src/apm_cli/core/script_runner.py": 1,
    "src/apm_cli/deps/file_provenance.py": 5,
    "src/apm_cli/deps/github_downloader.py": 2,
    "src/apm_cli/deps/immutable_requirements.py": 4,
    "src/apm_cli/deps/lockfile_generator.py": 1,
    "src/apm_cli/deps/registry/feature_gate.py": 1,
    "src/apm_cli/deps/transport_selection.py": 1,
    "src/apm_cli/drift.py": 2,
    "src/apm_cli/export/sbom.py": 2,
    "src/apm_cli/integration/_hook_dropped_targets.py": 2,
    "src/apm_cli/integration/agent_integrator.py": 4,
    "src/apm_cli/integration/base_integrator.py": 1,
    "src/apm_cli/integration/canvas_integrator.py": 2,
    "src/apm_cli/integration/cleanup.py": 1,
    "src/apm_cli/integration/command_integrator.py": 2,
    "src/apm_cli/integration/copilot_app_workflow_integrator.py": 2,
    "src/apm_cli/integration/hook_command_warnings.py": 1,
    "src/apm_cli/integration/hook_integrator.py": 1,
    "src/apm_cli/integration/instruction_integrator.py": 1,
    "src/apm_cli/integration/mcp_config_view.py": 10,
    "src/apm_cli/integration/mcp_integrator.py": 1,
    "src/apm_cli/integration/mcp_integrator_install.py": 6,
    "src/apm_cli/integration/skill_integrator.py": 4,
    "src/apm_cli/integration/skill_preflight.py": 1,
    "src/apm_cli/integration/targets.py": 1,
    "src/apm_cli/marketplace/catalog_origin.py": 1,
    "src/apm_cli/marketplace/client.py": 4,
    "src/apm_cli/marketplace/errors.py": 9,
    "src/apm_cli/marketplace/git_stderr.py": 1,
    "src/apm_cli/marketplace/init_template.py": 2,
    "src/apm_cli/marketplace/migration.py": 4,
    "src/apm_cli/marketplace/reconcile.py": 3,
    "src/apm_cli/marketplace/source_identity.py": 1,
    "src/apm_cli/marketplace/user_manifest.py": 1,
    "src/apm_cli/marketplace/yml_schema.py": 1,
    "src/apm_cli/models/dependency/materialization.py": 5,
    "src/apm_cli/models/dependency/object_fields.py": 1,
    "src/apm_cli/models/dependency/provider_coordinates.py": 1,
    "src/apm_cli/models/dependency/subsets.py": 1,
    "src/apm_cli/models/manifest_targets.py": 1,
    "src/apm_cli/models/validation.py": 1,
    "src/apm_cli/native_plugins/layout.py": 1,
    "src/apm_cli/output/formatters.py": 2,
    "src/apm_cli/runtime/manager.py": 1,
    "src/apm_cli/security/audit_report.py": 2,
    "src/apm_cli/security/executables.py": 8,
    "src/apm_cli/security/external/gate.py": 1,
    "src/apm_cli/security/external/skillspector.py": 2,
    "src/apm_cli/security/gate.py": 3,
    "src/apm_cli/security/trust.py": 4,
    "src/apm_cli/update_policy.py": 1,
    "src/apm_cli/utils/diagnostics.py": 2,
}

_DRIFT_KINDS = frozenset(
    {"unmanaged-conflict", "modified", "unintegrated", "orphaned", "unrecorded"}
)
_FROZEN_HASH_CLAUSE = "deployed files do not match the hashes it records"


def _docstring_ids(nodes: tuple[ast.AST, ...]) -> set[int]:
    found: set[int] = set()
    for node in nodes:
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                found.add(id(body[0].value))
    return found


def advice_literals(nodes: tuple[ast.AST, ...]) -> Iterator[ast.Constant]:
    """Yield every non-docstring string literal that spells remediation advice."""
    docstrings = _docstring_ids(nodes)
    for node in nodes:
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
            and ADVICE_PATTERN.search(node.value)
        ):
            yield node


def _check_advice(provider: FactsProvider) -> tuple[Violation, ...]:
    findings: list[Violation] = []
    owner, failures = checked_facts(provider, _OWNER, ADVICE_RULE, require_python=True)
    findings.extend(failures)
    if owner is not None and not {"Remediation", "_entry"} <= {d.name for d in owner.definitions}:
        findings.append(violation(ADVICE_RULE, _OWNER, "owner must define Remediation and _entry"))

    seen: set[str] = set()
    for path in inventory_paths(provider, prefixes=(_SCANNED_PREFIX,)):
        if path == _OWNER or not path.endswith(".py"):
            continue
        facts, path_failures = checked_facts(provider, path, ADVICE_RULE, require_python=True)
        findings.extend(path_failures)
        if facts is None or facts.tree_index is None:
            continue
        findings.extend(
            violation(
                ADVICE_RULE,
                path,
                "construct Remediation only in core/remediation.py; add a catalog entry",
                line=call.line,
                column=call.column,
            )
            for call in facts.calls
            if call.qualname.rsplit(".", 1)[-1] == "Remediation"
        )
        literals = tuple(advice_literals(facts.tree_index.nodes))
        allowed = _UNMIGRATED_ADVICE.get(path)
        if allowed is not None:
            seen.add(path)
            if len(literals) != allowed:
                findings.append(
                    violation(
                        ADVICE_RULE,
                        path,
                        f"{len(literals)} free-text advice literal(s), baseline pins {allowed}; "
                        "route new advice through core/remediation.py and lower the baseline "
                        "in contracts_remediation_advice.py when sites migrate",
                    )
                )
            continue
        findings.extend(
            violation(
                ADVICE_RULE,
                path,
                "free-text remediation advice; render a core/remediation.py catalog entry "
                "(Remediation.clause/sentence or either) instead",
                line=node.lineno,
                column=node.col_offset + 1,
            )
            for node in literals
        )
    findings.extend(
        violation(ADVICE_RULE, path, "stale _UNMIGRATED_ADVICE entry; remove it")
        for path in sorted(set(_UNMIGRATED_ADVICE) - seen)
    )
    return tuple(findings)


def _drift_kind_map(nodes: tuple[ast.AST, ...]) -> dict[str, ast.expr] | None:
    for node in nodes:
        if not (
            isinstance(node, ast.AnnAssign | ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "DRIFT_KIND_REMEDIATION"
                for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
            )
        ):
            continue
        value = node.value
        if isinstance(value, ast.Call) and value.args:
            value = value.args[0]
        if isinstance(value, ast.Dict) and all(
            isinstance(key, ast.Constant) and isinstance(key.value, str) for key in value.keys
        ):
            return {key.value: item for key, item in zip(value.keys, value.values, strict=True)}
    return None


def _check_drift_kinds(provider: FactsProvider) -> tuple[Violation, ...]:
    findings: list[Violation] = []
    owner, failures = checked_facts(provider, _OWNER, DRIFT_KINDS_RULE, require_python=True)
    findings.extend(failures)
    if owner is not None and owner.tree_index is not None:
        mapping = _drift_kind_map(owner.tree_index.nodes)
        if mapping is None or set(mapping) != _DRIFT_KINDS:
            findings.append(
                violation(
                    DRIFT_KINDS_RULE,
                    _OWNER,
                    "DRIFT_KIND_REMEDIATION must map exactly the drift kinds "
                    f"{sorted(_DRIFT_KINDS)} to remediations",
                )
            )
        else:
            modified = mapping["modified"]
            if not (isinstance(modified, ast.Name) and modified.id == "RESTORE_EDITED"):
                findings.append(
                    violation(
                        DRIFT_KINDS_RULE,
                        _OWNER,
                        "an edited (modified) drift finding must map to RESTORE_EDITED: "
                        "plain re-sync keeps the local edit (#3994)",
                        line=modified.lineno,
                    )
                )
            conflict = mapping["unmanaged-conflict"]
            if not (isinstance(conflict, ast.Constant) and conflict.value is None):
                findings.append(
                    violation(
                        DRIFT_KINDS_RULE,
                        _OWNER,
                        "unmanaged conflicts belong to the user and map to no command",
                        line=conflict.lineno,
                    )
                )
    render, failures = checked_facts(provider, _DRIFT_RENDER, DRIFT_KINDS_RULE, require_python=True)
    findings.extend(failures)
    if render is not None and render.tree_index is not None:
        if not any(
            isinstance(node, ast.Name) and node.id == "DRIFT_KIND_REMEDIATION"
            for node in render.tree_index.nodes
        ):
            findings.append(
                violation(
                    DRIFT_KINDS_RULE,
                    _DRIFT_RENDER,
                    "drift text must pick remediations through DRIFT_KIND_REMEDIATION",
                )
            )
    return tuple(findings)


def _check_frozen_help(provider: FactsProvider) -> tuple[Violation, ...]:
    facts, failures = checked_facts(
        provider, _INSTALL_COMMAND, FROZEN_HELP_RULE, require_python=True
    )
    if facts is None or facts.tree_index is None:
        return failures
    for node in facts.tree_index.nodes:
        if not (
            isinstance(node, ast.Call)
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value == "--frozen"
        ):
            continue
        help_text = next(
            (
                keyword.value.value
                for keyword in node.keywords
                if keyword.arg == "help"
                and isinstance(keyword.value, ast.Constant)
                and isinstance(keyword.value.value, str)
            ),
            "",
        )
        if _FROZEN_HASH_CLAUSE in " ".join(help_text.split()):
            return failures
        return (
            *failures,
            violation(
                FROZEN_HELP_RULE,
                _INSTALL_COMMAND,
                "install --frozen help must name the deployed-hash check it enforces "
                f"({_FROZEN_HASH_CLAUSE!r}; #3994)",
                line=node.lineno,
            ),
        )
    return (
        *failures,
        violation(FROZEN_HELP_RULE, _INSTALL_COMMAND, "install must declare a --frozen option"),
    )


def _implies_discover(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.If)
        and isinstance(node.test, ast.Name)
        and node.test.id == "write"
        and any(
            isinstance(statement, ast.Assign)
            and [getattr(target, "id", None) for target in statement.targets] == ["discover_flag"]
            and isinstance(statement.value, ast.Constant)
            and statement.value.value is True
            for statement in node.body
        )
    )


def _check_init_apply(provider: FactsProvider) -> tuple[Violation, ...]:
    facts, failures = checked_facts(provider, _INIT_COMMAND, INIT_APPLY_RULE, require_python=True)
    if facts is None or facts.tree_index is None:
        return failures
    if any(_implies_discover(node) for node in facts.tree_index.nodes):
        return failures
    return (
        *failures,
        violation(
            INIT_APPLY_RULE,
            _INIT_COMMAND,
            "init --apply must imply --discover ('if write: discover_flag = True'; #3994)",
        ),
    )


def _rule(rule_id: str, description: str, check) -> Rule:
    return Rule(rule_id, "contracts_tests", (rule_id,), description, check)


RULES = (
    _rule(
        ADVICE_RULE,
        "Remediation advice is a structured core/remediation.py catalog entry, never free text.",
        _check_advice,
    ),
    _rule(
        DRIFT_KINDS_RULE,
        "Edited drift findings are restored by delete-and-reinstall, never plain re-sync.",
        _check_drift_kinds,
    ),
    _rule(
        FROZEN_HELP_RULE,
        "install --frozen help names the deployed-hash check it enforces.",
        _check_frozen_help,
    ),
    _rule(
        INIT_APPLY_RULE,
        "init --apply implies --discover.",
        _check_init_apply,
    ),
)

__all__ = ["ADVICE_PATTERN", "RULES", "advice_literals"]
