"""Executable ownership edges for the bounded GitLab sparse-fetch consumer."""

from __future__ import annotations

import ast

from scripts.architecture_linter.checks.python_semantics import assignments_to
from scripts.architecture_linter.checks.transport_platform_shared import GROUP
from scripts.architecture_linter.checks.tree_index import TreeIndex
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.models import Rule, Violation

RULE_ID = "transport-platform-gitlab-sparse-plan"
_CONSUMER = "src/apm_cli/deps/download_strategies.py"
_OWNER = "src/apm_cli/deps/transport_selection.py"


def _expr(node: ast.AST | None) -> str:
    """Compare executable expressions, never comments or source substrings."""
    return "" if node is None else ast.unparse(node)


def _calls(index: TreeIndex, scope: ast.AST, target: str) -> tuple[ast.Call, ...]:
    return tuple(
        node
        for node in index.own_scope(scope)
        if isinstance(node, ast.Call) and _expr(node.func) == target
    )


def _binding(index: TreeIndex, scope: ast.AST, name: str) -> ast.AST | None:
    bindings = assignments_to(index, scope, name)
    return bindings[0].value if len(bindings) == 1 else None


def _keywords(call: ast.Call) -> dict[str, str]:
    return {item.arg: _expr(item.value) for item in call.keywords if item.arg is not None}


def check_gitlab_prepared_remote(provider: FactsProvider, rule_id: str) -> tuple[Violation, ...]:
    """Share tokenless construction and prepared-callback evidence with auth guards."""
    index = provider.tree_index(_CONSUMER)
    method = None if index is None else index.function("DownloadDelegate.download_gitlab_file")
    executor = (
        None if index is None else index.function("DownloadDelegate._download_gitlab_file_via_git")
    )
    valid = False
    if (
        index is not None
        and isinstance(method, ast.FunctionDef)
        and isinstance(executor, ast.FunctionDef)
    ):
        builders = _calls(index, method, "self.build_repo_url")
        constructors = [
            node
            for node in index.own_scope(executor)
            if isinstance(node, ast.Call) and "build_repo_url_fn" in _keywords(node)
        ]
        if len(constructors) == 1:
            callback = index.function(
                "DownloadDelegate._download_gitlab_file_via_git."
                + _keywords(constructors[0])["build_repo_url_fn"]
            )
            valid = (
                bool(builders)
                and all(_keywords(call).get("token") == "''" for call in builders)
                and isinstance(callback, ast.FunctionDef)
                and len(callback.body) == 1
                and isinstance(callback.body[0], ast.Return)
                and _expr(callback.body[0].value) == "requested_url"
                and _keywords(constructors[0]).get("git_env") == "git_env"
            )
    if valid:
        return ()
    return (
        Violation(
            rule_id,
            _CONSUMER,
            getattr(executor, "lineno", 1),
            1,
            "GitLab sparse transport must preserve the prepared URL callback ownership "
            "edge and keep managed credentials out of every constructed remote URL",
        ),
    )


def _rest_gate(index: TreeIndex, method: ast.FunctionDef, loop: ast.For) -> bool:
    """Require authorization accumulated from executed attempts before REST."""
    bindings = assignments_to(index, method, "rest_eligible")
    if len(bindings) != 2 or _expr(bindings[0].value) != "False":
        return False
    update = bindings[1]
    value = update.value
    if not (
        isinstance(value, ast.BoolOp)
        and isinstance(value.op, ast.Or)
        and len(value.values) == 2
        and _expr(value.values[0]) == "rest_eligible"
        and isinstance(value.values[1], ast.Call)
        and _expr(value.values[1].func) == "self._gitlab_rest_eligible"
        and tuple(map(_expr, value.values[1].args)) == ("effective_url", "host_info.api_base")
    ):
        return False
    handler = index.parent(update.node)
    if not (
        isinstance(handler, ast.ExceptHandler)
        and _expr(handler.type) == "GitFileTransportError"
        and handler in index.walk(loop)
    ):
        return False
    rest_calls = _calls(index, method, "self._download_gitlab_file_via_rest")
    gates = [
        statement
        for statement in method.body
        if isinstance(statement, ast.If) and _expr(statement.test) == "rest_eligible"
    ]
    return (
        len(rest_calls) == 1
        and len(gates) == 1
        and method.body.index(gates[0]) > method.body.index(loop)
        and any(rest_calls[0] in index.walk(statement) for statement in gates[0].body)
        and not gates[0].orelse
    )


def _check_gitlab_sparse_plan(provider: FactsProvider) -> tuple[Violation, ...]:
    """Defend selector, prepared remote, auth owner, and REST authorization edges."""
    index = provider.tree_index(_CONSUMER)
    owner = provider.tree_index(_OWNER)
    findings: list[Violation] = []

    def require(condition: bool, edge: str, node: ast.AST | None = None) -> None:
        if not condition:
            findings.append(
                Violation(
                    RULE_ID,
                    _CONSUMER,
                    getattr(node, "lineno", 1),
                    1,
                    f"GitLab sparse transport must preserve the {edge} ownership edge",
                )
            )

    if index is None or owner is None:
        require(False, "parseable transport policy")
        return tuple(findings)
    method = index.function("DownloadDelegate.download_gitlab_file")
    executor = index.function("DownloadDelegate._download_gitlab_file_via_git")
    require(
        owner.function("initial_transport_scheme") is not None
        and owner.function("TransportSelector.select") is not None,
        "transport-selection authority",
    )
    if not isinstance(method, ast.FunctionDef) or not isinstance(executor, ast.FunctionDef):
        require(False, "orchestrator and prepared-attempt executor")
        return tuple(findings)

    initial_calls = _calls(index, method, "initial_transport_scheme")
    candidate = _binding(index, method, "candidate_url")
    require(
        len(initial_calls) == 1
        and isinstance(candidate, ast.Call)
        and initial_calls[0] in index.walk(candidate)
        and _keywords(candidate).get("token") == "''",
        "initial-scheme helper",
        method,
    )
    plan = _binding(index, method, "plan")
    anonymous_plan = _binding(index, method, "anonymous_plan")
    loops = [
        node
        for node in method.body
        if isinstance(node, ast.For)
        and _expr(node.target) == "attempt"
        and _expr(node.iter) == "plan.attempts"
    ]
    require(
        isinstance(plan, ast.Call)
        and _expr(plan.func) == "self._host._transport_selector.select"
        and isinstance(anonymous_plan, ast.Call)
        and _expr(anonymous_plan.func) == "self._host._transport_selector.select"
        and _keywords(plan).get("candidate_url") == "candidate_url"
        and _keywords(plan).get("allow_fallback") == "self._host._allow_fallback"
        and len(loops) == 1,
        "selector plan execution",
        method,
    )
    attempt_ctx = _binding(index, method, "attempt_ctx")
    git_env = _binding(index, method, "git_env")
    require(
        _expr(_binding(index, method, "resolver")) == "self._host.auth_resolver"
        and isinstance(attempt_ctx, ast.Call)
        and _expr(attempt_ctx.func) == "resolver.resolve_for_remote"
        and tuple(map(_expr, attempt_ctx.args[:2])) == ("host", "effective_url")
        and isinstance(git_env, ast.IfExp)
        and _expr(git_env.test) == "attempt.use_token"
        and _expr(git_env.body) == "resolver.git_env_for_remote(attempt_ctx, effective_url)"
        and _expr(git_env.orelse)
        == "resolver.build_native_git_credential_env(host_info, effective_url)",
        "AuthResolver routing",
        method,
    )
    prepared = _calls(index, method, "self._download_gitlab_file_via_git")
    require(
        len(prepared) == 1
        and len(loops) == 1
        and prepared[0] in index.walk(loops[0])
        and all(
            _keywords(prepared[0]).get(name) == name
            for name in ("requested_url", "effective_url", "git_env")
        ),
        "prepared attempt arguments",
        method,
    )
    findings.extend(check_gitlab_prepared_remote(provider, RULE_ID))
    require(
        len(loops) == 1 and _rest_gate(index, method, loops[0]),
        "executed HTTPS REST gate",
        method,
    )
    return tuple(findings)


RULES = (
    Rule(
        id=RULE_ID,
        group=GROUP,
        guard_ids=(RULE_ID,),
        description="GitLab sparse fetch consumes transport selection and prepared auth before gated REST.",
        check=_check_gitlab_sparse_plan,
    ),
)
