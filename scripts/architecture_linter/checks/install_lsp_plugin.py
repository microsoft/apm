"""Claude LSP plugin and executable-trust architecture guards."""

from __future__ import annotations

import ast
import textwrap

from scripts.architecture_linter.checks.install_deployment_shared import (
    _facts_for,
    _lines,
    _present,
    _python_paths,
    _summary,
)
from scripts.architecture_linter.checks.install_exec_provenance import (
    check_allow_map_provenance,
)
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.groups.common import violation
from scripts.architecture_linter.models import Violation

GUARD_EXECUTABLE_TRUST = "install-deployment-executable-trust-context"
GUARD_LSP_TARGET_CONTRACT = "install-deployment-lsp-target-contract"
GUARD_LSP_LIFECYCLE = "install-deployment-lsp-lifecycle"
GUARD_CONSUMER_MANIFEST_REWRITE = "install-deployment-consumer-manifest-rewrite"

_EXECUTABLES = "src/apm_cli/security/executables.py"
_APPROVE_COMMAND = "src/apm_cli/commands/approve.py"
_INSTALL_COMMAND = "src/apm_cli/commands/install.py"
_INSTALL_TEMPLATE = "src/apm_cli/install/template.py"
_LSP_INTEGRATOR = "src/apm_cli/integration/lsp_integrator.py"
_LSP_PIPELINE = "src/apm_cli/install/lsp/integration.py"
_LOCAL_BUNDLE = "src/apm_cli/install/local_bundle_handler.py"
_INSTALL_SERVICES = "src/apm_cli/install/services.py"
_SKILL_SUPPORT = "src/apm_cli/integration/skill_support.py"
_EXEC_GATE = "src/apm_cli/install/exec_gate.py"
_MCP_WRITER = "src/apm_cli/install/mcp/writer.py"
_MANIFEST_HELPERS = "src/apm_cli/install/manifest_helpers.py"
_UNINSTALL_COMMAND = "src/apm_cli/commands/uninstall/cli.py"
_MANIFEST_IO = "src/apm_cli/models/manifest_io.py"
_COMMAND_HELPERS = "src/apm_cli/commands/_helpers.py"
_APM_PACKAGE = "src/apm_cli/models/apm_package.py"
_ADOPT_MANIFEST_EDIT = "src/apm_cli/adopt/manifest_edit.py"
_MCP_PIPELINE = "src/apm_cli/install/mcp/integration.py"
_MCP_VIEW = "src/apm_cli/integration/mcp_config_view.py"
_UNINSTALL_ENGINE = "src/apm_cli/commands/uninstall/engine.py"
_HOOK_INTEGRATOR = "src/apm_cli/integration/hook_integrator.py"
_AUDIT_COMMAND = "src/apm_cli/commands/audit.py"
_DRIFT_REPLAY = "src/apm_cli/install/drift.py"
_AUDIT_REPLAY = "src/apm_cli/install/audit_replay.py"
_CI_CHECKS = "src/apm_cli/policy/ci_checks.py"
_MANIFEST_TARGETS = "src/apm_cli/models/manifest_targets.py"
# The remaining apm.yml target rewrite paths (apm init re-init, #3857/#3868).
_TARGET_REWRITE_PATHS = ("src/apm_cli/commands/init.py", "src/apm_cli/commands/_helpers.py")
_YAML_IO = "src/apm_cli/utils/yaml_io.py"
# Anchors are refused on read (req-mf-020(b), #3858), so no rewrite may keep one.
_ANCHOR_REWRITE_TOKENS = (".anchor.value", "_detached_sequence")
# The trust owner reads the scope's manifest file it is handed; it never derives
# one from a root (#3954). Every call site is registered, so a new caller that
# picks its own root fails closed instead of silently reading the wrong apm.yml.
_TRUST_OWNER = "exec_trust_context_for_project"
_TRUST_OWNER_CALLERS = frozenset(
    {
        (_INSTALL_TEMPLATE, "effective_allow_map"),
        ("src/apm_cli/security/trust.py", "resolve"),
        ("src/apm_cli/security/trust.py", "pins"),
        (_EXEC_GATE, "_trust_ctx"),
        (_EXEC_GATE, "_resolve"),
        (_EXECUTABLES, "effective_exec_map_for_project"),
        # The offline dry-run preview reads the same layers, policy-free (#4018).
        ("src/apm_cli/install/dry_run_plan.py", "with_allowed_lsp_dependencies"),
        # ``apm install <bundle>`` reads grants from the scope manifest, never
        # the working directory's apm.yml under -g (#3989 M2).
        (_LOCAL_BUNDLE, "effective_bundle_allow_map"),
    }
)
# The root-taking convenience wrapper derives the PROJECT manifest from its
# root, so only the caller that first maps a root through the scope owner
# (core.scope.manifest_root) may use it.
_ROOT_TRUST_WRAPPER = "effective_exec_map_for_project"
_ROOT_TRUST_WRAPPER_CALLERS = frozenset({(_EXECUTABLES, "resolve_project_exec_allow_map")})
# One owner decides a package's exec status and the types a partial approval
# still deploys (#3991); one comparer reads them back. A second reader would let
# update and install disagree on whether a recorded decision still holds.
_EXEC_DECISION_CALLERS = {
    "package_exec_decision": frozenset(
        {
            ("src/apm_cli/security/trust.py", "package_exec_status"),
            (_EXEC_GATE, "check_executable_approval"),
            (_EXEC_GATE, "current_exec_decision"),
        }
    ),
    "recorded_exec_status_is_stale": frozenset(
        {(_EXEC_GATE, "lock_exec_decisions_stale"), (_EXEC_GATE, "for_dependency")}
    ),
}
_MANIFEST_DERIVATION_TOKENS = ('"apm.yml"', "APM_YML_FILENAME")
# approve/deny select a package by exact name: a partial name could grant or deny
# execution for another package, so prefix matching on names is refused.
_APPROVE_LOOSE_MATCH_TOKENS = ("package_name.startswith(",)
# approve.py must not re-derive a locked row's trust principal (#3710).
_APPROVE_PARALLEL_TRUST_TOKENS = (
    "approval_identity=locked.",
    ".replacement is not None",
    "original_identity=locked.",
)


def _missing_tokens(
    provider: FactsProvider,
    rule_id: str,
    required: dict[str, tuple[str, ...]],
) -> tuple[Violation, ...]:
    findings: list[Violation] = []
    for path, tokens in required.items():
        facts, failures = _facts_for(provider, path, rule_id)
        if failures:
            findings.extend(failures)
            continue
        missing = [token for token in tokens if not _present(facts, token)]
        if missing:
            findings.append(
                _summary(
                    rule_id,
                    path,
                    "Canonical routing tokens are missing: " + ", ".join(missing),
                )
            )
    return tuple(findings)


def check_executable_trust_context(provider: FactsProvider) -> tuple[Violation, ...]:
    """Install and update must consume one effective executable-trust owner."""
    findings = list(
        _missing_tokens(
            provider,
            GUARD_EXECUTABLE_TRUST,
            {
                _EXECUTABLES: (
                    "def exec_trust_context_for_project(",
                    "def resolve_project_exec_allow_map(",
                    "def locked_dependency_approval(",
                    "def locked_dependency_approval_keys(",
                    "def dependency_approval_key(",
                    "identity=dependency_approval_key(dependency.to_dependency_ref())",
                    "approval = locked_dependency_approval(dependency)",
                    "def local_bundle_approval_key(",
                    'owner = getattr(dependency, "resolved_by", None)',
                    'approval_keys = getattr(dependency, "approval_keys", ())',
                    "def denied_exec_types(",
                    "def authorized_exec_map(",
                    "def exec_map_authorizes_all(",
                    "def package_type_approved(",
                    "def compose_trust_bin(",
                ),
                _APPROVE_COMMAND: (
                    "approval = locked_dependency_approval(locked",
                    "approval_identity=approval.identity",
                    "original_identity=approval.original_identity",
                    "return package_type_verdicts(",
                ),
                _EXEC_GATE: (
                    "return dependency_approval_key(dep_ref)",
                    "denied |= denied_exec_types(ctx.exec_trust_ctx, candidate_keys, unapproved)",
                    # A pinned item the item owner denies is reported as denied (#4044).
                    "denied = frozenset(t for t, status in pinned_status.items() "
                    "if status == TRUST_DENIED)",
                    "self._trust_ctx_value = exec_trust_context_for_project(",
                ),
                # Audit replay withholds what the install gate withheld, under
                # the audit's own single policy fetch (#3924), and what it would now
                # withhold after an unreconciled deny (#3958).
                _DRIFT_REPLAY: (
                    "replay_allow = gated_allow_map.for_dependency(lock_dep, package_info)",
                    "allow_executables=replay_allow,",
                    "project_root, config.policy_provider, user_scope=config.user_scope",
                ),
                _AUDIT_REPLAY: ("policy_provider=policy_provider,",),
                _CI_CHECKS: ("policy_provider=policy_provider,",),
                _INSTALL_COMMAND: ("effective_bundle_allow_map as _effective_bundle_allow_map",),
                _INSTALL_TEMPLATE: (
                    "trust_ctx = exec_trust_context_for_project(",
                    "manifest_root(project_root, user_scope=is_user_scope(",
                ),
                # McpAdmission reads grants from the scope manifest (#3954, #3986).
                "src/apm_cli/security/trust.py": (
                    "grants_manifest = project_manifest_path(manifest_root(project_root, "
                    "user_scope=user_scope))",
                ),
                "src/apm_cli/commands/update.py": ("get_manifest_path(scope),",),
                _LSP_PIPELINE: ("effective_allow_executables = resolve_project_exec_allow_map(",),
                _MCP_PIPELINE: ("admission = McpAdmission.for_project(",),
                _MCP_VIEW: ("approval_keys = locked_dependency_approval_keys(dependency)",),
                _UNINSTALL_ENGINE: ("_allow_executables = resolve_project_exec_allow_map(",),
                _HOOK_INTEGRATOR: ("allow_map = resolve_project_exec_allow_map(",),
                _AUDIT_COMMAND: ("policy_provider=replay_policy",),
                _LOCAL_BUNDLE: (
                    "bundle_approval_key = local_bundle_approval_key(",
                    "approval_key=bundle_approval_key,",
                    "grants_manifest = project_manifest_path(manifest_root(project_root, "
                    "user_scope=user_scope))",
                ),
                _INSTALL_SERVICES: (
                    # Bundle hooks and canvas are approved for the exact bundle content.
                    "_bundle_keys = () if approval_key is None else (approval_key,)",
                    "package_type_approved(allow_executables, _bundle_keys, EXEC_TYPE_HOOKS)",
                    "allow_executables, _bundle_keys, EXEC_TYPE_CANVAS",
                    # A gated package's hooks retire through the hook owner (#3924).
                    "reconcile_package_targets(package_info, project_root, targets),",
                ),
            },
        )
    )
    findings.extend(_trust_owner_closed_world(provider))
    findings.extend(check_allow_map_provenance(provider, GUARD_EXECUTABLE_TRUST))
    findings.extend(_bundle_trust_scope_findings(provider))
    # A second locked-row trust derivation beside the owner is a split authority.
    facts, failures = _facts_for(provider, _APPROVE_COMMAND, GUARD_EXECUTABLE_TRUST)
    if not failures:
        findings.extend(
            _summary(
                GUARD_EXECUTABLE_TRUST,
                _APPROVE_COMMAND,
                f"Locked-row trust must come from locked_dependency_approval; remove {token}",
            )
            for token in _APPROVE_PARALLEL_TRUST_TOKENS
            if _present(facts, token)
        )
        findings.extend(
            _summary(
                GUARD_EXECUTABLE_TRUST,
                _APPROVE_COMMAND,
                f"approve/deny must match package names exactly; remove {token}",
            )
            for token in _APPROVE_LOOSE_MATCH_TOKENS
            if _present(facts, token)
        )
        if not _present(facts, "matches more than one installed package"):
            findings.append(
                _summary(
                    GUARD_EXECUTABLE_TRUST,
                    _APPROVE_COMMAND,
                    "approve/deny must refuse a name that matches more than one package",
                )
            )
    return tuple(findings)


def _bundle_trust_scope_findings(provider: FactsProvider) -> list[Violation]:
    """``apm install <bundle>`` hands the bundle trust owner the command's scope."""
    facts, failures = _facts_for(provider, _INSTALL_COMMAND, GUARD_EXECUTABLE_TRUST)
    if failures:
        return list(failures)
    text = "\n".join(_lines(facts))
    marker = "_effective_bundle_allow_map("
    calls = [
        _balanced_call(text, index) for index in range(len(text)) if text.startswith(marker, index)
    ]
    if len(calls) == 1 and "user_scope=global_," in calls[0]:
        return []
    return [
        _summary(
            GUARD_EXECUTABLE_TRUST,
            _INSTALL_COMMAND,
            "apm install <bundle> must pass effective_bundle_allow_map user_scope=global_ "
            "so -g reads grants from the user manifest (#3989)",
        )
    ]


# What a registered caller may hand the trust owner as the manifest (A8): a
# call to the scope owner's manifest helpers, or a name bound from one, or the
# owner-contract parameter that carries the resolved manifest file.
_MANIFEST_PATH_CALLS = frozenset({"project_manifest_path", "get_manifest_path"})
_MANIFEST_PATH_PARAMS = frozenset({"manifest_path", "grants_manifest"})


def _callee_name(node: ast.AST) -> str:
    func = getattr(node, "func", None)
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _is_manifest_call(node: ast.AST) -> bool:
    return isinstance(node, ast.Call) and _callee_name(node) in _MANIFEST_PATH_CALLS


def _balanced_call(text: str, start: int) -> str:
    """The call starting at *start*, through its balanced closing parenthesis."""
    depth = 0
    for index in range(text.index("(", start), len(text)):
        depth += {"(": 1, ")": -1}.get(text[index], 0)
        if depth == 0:
            return text[start : index + 1]
    return text[start:]


def _trust_owner_argument(lines: tuple[str, ...], call: object) -> ast.AST | None:
    """Parse the first argument of the trust-owner call at *call*."""
    text = "\n".join(lines[call.line - 1 :])
    try:
        node = ast.parse(_balanced_call(text, call.column), mode="eval").body
    except (SyntaxError, ValueError):
        return None
    if isinstance(node, ast.Call) and node.args:
        return node.args[0]
    return None


def _manifest_name_is_resolved(name: str, scope_source: str) -> bool:
    """Whether *name* only ever holds a resolved manifest in the enclosing function."""
    try:
        tree = ast.parse(textwrap.dedent(scope_source))
    except SyntaxError:
        return False
    params = {
        arg.arg
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))
        for arg in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)
    }
    bound = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == name for target in node.targets)
    ]
    if any(not _is_manifest_call(value) for value in bound):
        return False
    return bool(bound) or (name in params and name in _MANIFEST_PATH_PARAMS)


def _trust_owner_argument_findings(path: str, facts: object) -> list[Violation]:
    """A registered caller hands the owner a resolved manifest, never a root (A8)."""
    lines = _lines(facts)
    functions = [d for d in facts.definitions if d.kind != "class"]
    findings: list[Violation] = []
    for call in facts.calls:
        if call.qualname.rsplit(".", 1)[-1] != _TRUST_OWNER:
            continue
        if (path, call.scope) not in _TRUST_OWNER_CALLERS:
            continue
        argument = _trust_owner_argument(lines, call)
        enclosing = [d for d in functions if d.line <= call.line <= d.end_line]
        outer = min(enclosing, key=lambda d: d.line) if enclosing else None
        source = "\n".join(lines[outer.line - 1 : outer.end_line]) if outer else ""
        if _is_manifest_call(argument) or (
            isinstance(argument, ast.Name) and _manifest_name_is_resolved(argument.id, source)
        ):
            continue
        findings.append(
            violation(
                GUARD_EXECUTABLE_TRUST,
                path,
                f"{call.scope} must pass {_TRUST_OWNER} the scope's manifest file "
                "(project_manifest_path(...) / get_manifest_path(...) or a name bound from "
                "one), not a directory (#3954)",
                line=call.line,
            )
        )
    return findings


def _trust_owner_closed_world(provider: FactsProvider) -> list[Violation]:
    """Only registered callers reach the trust owner, which never derives apm.yml."""
    findings: list[Violation] = []
    facts, failures = _facts_for(provider, _EXECUTABLES, GUARD_EXECUTABLE_TRUST)
    if failures:
        return list(failures)
    owner = [d for d in facts.definitions if d.name == _TRUST_OWNER]
    if owner:
        body = _lines(facts)[owner[0].line - 1 : owner[0].end_line]
        if any(token in line for line in body for token in _MANIFEST_DERIVATION_TOKENS):
            findings.append(
                _summary(
                    GUARD_EXECUTABLE_TRUST,
                    _EXECUTABLES,
                    f"{_TRUST_OWNER} must read the manifest path it is given, not derive "
                    "apm.yml from a root (#3954)",
                )
            )
    for path in _python_paths(provider, "src/apm_cli/"):
        path_facts, path_failures = _facts_for(provider, path, GUARD_EXECUTABLE_TRUST)
        if path_failures:
            continue
        # A registered caller passes a manifest the scope owner named
        # (core.scope.get_manifest_path / manifest_root + project_manifest_path);
        # it never builds apm.yml from a root itself (#3954, #3986).
        registered = {scope for caller, scope in _TRUST_OWNER_CALLERS if caller == path}
        findings.extend(_trust_owner_argument_findings(path, path_facts))
        lines = _lines(path_facts)
        for definition in path_facts.definitions:
            if definition.name not in registered:
                continue
            body = lines[definition.line - 1 : definition.end_line]
            if any(token in line for line in body for token in _MANIFEST_DERIVATION_TOKENS):
                findings.append(
                    violation(
                        GUARD_EXECUTABLE_TRUST,
                        path,
                        f"{definition.name} derives apm.yml from a root; pass the manifest "
                        "the scope owner names (project_manifest_path(manifest_root(...)))",
                        line=definition.line,
                    )
                )
        findings.extend(
            violation(
                GUARD_EXECUTABLE_TRUST,
                path,
                f"{call.scope} calls {_TRUST_OWNER} outside the registered callers; pass the "
                "scope's manifest (core.scope.get_manifest_path) and register the call site",
                line=call.line,
            )
            for call in path_facts.calls
            if call.qualname.rsplit(".", 1)[-1] == _TRUST_OWNER
            and (path, call.scope) not in _TRUST_OWNER_CALLERS
        )
        findings.extend(
            violation(
                GUARD_EXECUTABLE_TRUST,
                path,
                f"{call.scope} calls {_ROOT_TRUST_WRAPPER} with a root; read grants from "
                "the scope manifest (project_manifest_path(manifest_root(...)))",
                line=call.line,
            )
            for call in path_facts.calls
            if call.qualname.rsplit(".", 1)[-1] == _ROOT_TRUST_WRAPPER
            and (path, call.scope) not in _ROOT_TRUST_WRAPPER_CALLERS
        )
        findings.extend(
            violation(
                GUARD_EXECUTABLE_TRUST,
                path,
                f"{call.scope} calls {name} outside its registered callers; the exec "
                "decision and its staleness check have one owner each (#3990, #3991)",
                line=call.line,
            )
            for call in path_facts.calls
            if (name := call.qualname.rsplit(".", 1)[-1]) in _EXEC_DECISION_CALLERS
            and (path, call.scope) not in _EXEC_DECISION_CALLERS[name]
        )
    return findings


_CONSUMER_MANIFEST_TOKENS: dict[str, tuple[str, ...]] = {
    _MANIFEST_IO: (
        "def reject_duplicate_manifest_keys(",
        "def reject_manifest_anchors(",
        "reject_duplicate_manifest_keys(manifest_path, text)",
        "reject_manifest_anchors(manifest_path, text)",
        "return check_manifest_text(path, Path(path).read_text(",
        "def load_manifest(",
        "def load_manifest_roundtrip(",
    ),
    _MANIFEST_TARGETS: ("data = load_manifest(manifest_path)",),
    # Writers never emit the anchors the read owner refuses.
    _YAML_IO: (
        "data, Dumper=_NoAliasSafeDumper,",
        "rt.representer.ignore_aliases = lambda _data: True",
    ),
    _AUDIT_COMMAND: ("read_manifest_text(manifest_path)",),
    _APM_PACKAGE: ("data = load_manifest(apm_yml_path)",),
    _APPROVE_COMMAND: ("data = load_manifest(manifest)",),
    _COMMAND_HELPERS: ("return load_manifest(APM_YML_FILENAME)",),
    _INSTALL_COMMAND: ("data, manifest_host = _load_manifest_for_add(apm_yml_path, logger)",),
    _ADOPT_MANIFEST_EDIT: ("data = load_manifest_roundtrip(path)",),
    _MCP_WRITER: (
        "data = load_manifest_roundtrip(apm_yml_path) or {}",
        "dump_yaml_roundtrip(data, apm_yml_path)",
    ),
    _EXECUTABLES: (
        "data = load_manifest(manifest_path)",
        "data = load_manifest_roundtrip(manifest_path)",
        "dump_yaml_roundtrip(data, manifest_path)",
    ),
    _MANIFEST_HELPERS: (
        "data = load_manifest_roundtrip(apm_yml_path) or {}",
        "dump_yaml_roundtrip(data, apm_yml_path)",
    ),
    _UNINSTALL_COMMAND: (
        "data = load_manifest_roundtrip(apm_yml_path) or {}",
        "dump_yaml_roundtrip(data, apm_yml_path)",
    ),
}


def check_consumer_manifest_rewrite(provider: FactsProvider) -> tuple[Violation, ...]:
    """Every apm.yml read rejects duplicate keys and anchors; rewrites keep authoring."""
    findings = list(
        _missing_tokens(
            provider,
            GUARD_CONSUMER_MANIFEST_REWRITE,
            _CONSUMER_MANIFEST_TOKENS,
        )
    )
    for path in _TARGET_REWRITE_PATHS:
        facts, failures = _facts_for(provider, path, GUARD_CONSUMER_MANIFEST_REWRITE)
        findings.extend(failures)
        findings.extend(
            _summary(
                GUARD_CONSUMER_MANIFEST_REWRITE,
                path,
                f"apm.yml anchors are refused on read; remove the anchor rewrite path {token}",
            )
            for token in _ANCHOR_REWRITE_TOKENS
            if not failures and _present(facts, token)
        )
    return tuple(findings)


def check_lsp_target_contract(provider: FactsProvider) -> tuple[Violation, ...]:
    """LSP target shape and deployment paths must route through LSPIntegrator."""
    return _missing_tokens(
        provider,
        GUARD_LSP_TARGET_CONTRACT,
        {
            _LSP_INTEGRATOR: (
                "def _prepare_target_config(",
                "prepared_targets.append((runtime, spec, prepared))",
                "root = spec.user_root()",
                "relative = Path(*spec.project_relative_path)",
                "return BaseIntegrator.resolve_deploy_path(",
                "allowed_prefixes=(relative_path,)",
                "locked_dependency_approval_keys(locked_dependency)",
                "approval_keys=approval_keys",
            ),
        },
    )


def check_lsp_lifecycle(provider: FactsProvider) -> tuple[Violation, ...]:
    """LSP collection and reconciliation must route through the pipeline owner."""
    findings = list(
        _missing_tokens(
            provider,
            GUARD_LSP_LIFECYCLE,
            {
                _LSP_PIPELINE: (
                    "transitive_lsp = filter_lsp_by_allow_executables(",
                    "lsp_deps = LSPIntegrator.deduplicate(lsp_deps + transitive_lsp)",
                    "def reconcile_lsp_after_uninstall(",
                    "lsp_target_servers=new_targets",
                    "managed_target_servers=old_lsp_targets",
                ),
                _LOCAL_BUNDLE: (
                    "def effective_bundle_allow_map(",
                    "exec_trust_context_for_project(",
                    "def _wire_bundle_lsp_servers(",
                    "force=force,",
                ),
            },
        )
    )
    pipeline, failures = _facts_for(provider, _LSP_PIPELINE, GUARD_LSP_LIFECYCLE)
    if failures:
        return tuple([*findings, *failures])
    lines = getattr(pipeline, "lines", ())
    filter_line = next(
        (
            index
            for index, line in enumerate(lines)
            if "transitive_lsp = filter_lsp_by_allow_executables(" in line
        ),
        None,
    )
    dedup_line = next(
        (
            index
            for index, line in enumerate(lines)
            if "lsp_deps = LSPIntegrator.deduplicate(lsp_deps + transitive_lsp)" in line
        ),
        None,
    )
    if filter_line is not None and dedup_line is not None and filter_line >= dedup_line:
        findings.append(
            _summary(
                GUARD_LSP_LIFECYCLE,
                _LSP_PIPELINE,
                "Transitive LSP trust filtering must run before first-wins deduplication",
            )
        )
    lifecycle_calls = (
        "LSPIntegrator.collect_transitive(",
        "LSPIntegrator.resolve_target_runtimes(",
        "LSPIntegrator.install(",
        "LSPIntegrator.remove_stale(",
        "LSPIntegrator.update_lockfile(",
    )
    for path in _python_paths(provider, "src/apm_cli/"):
        if path in {_LSP_INTEGRATOR, _LSP_PIPELINE}:
            continue
        facts, failures = _facts_for(provider, path, GUARD_LSP_LIFECYCLE)
        if failures:
            findings.extend(failures)
            continue
        direct_calls = [token for token in lifecycle_calls if _present(facts, token)]
        if direct_calls:
            findings.append(
                _summary(
                    GUARD_LSP_LIFECYCLE,
                    path,
                    "LSP lifecycle calls must route through install/lsp/integration.py: "
                    + ", ".join(direct_calls),
                )
            )
    return tuple(findings)
