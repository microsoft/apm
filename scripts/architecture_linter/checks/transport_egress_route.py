"""Egress route ownership, HTTP session construction, and credential-free URL builders.

Three rules guard owner records in ``.apm/architecture/owners/transport-auth-platform.json``:

1. ``transport-platform-egress-route-policy`` (owner record
   ``egress-route-policy``): registry-only mode is read only by
   ``deps/egress_route.py`` (and parsed as configuration by
   ``deps/registry_proxy.py``). Every surface consumer is a frozen,
   counted list of ``EgressSurface.<member>`` references, every surface has
   a consumer, the owner's registry-only table covers every surface, and the
   only ``DIRECT`` exceptions are the org policy fetch and explicit self-update.
2. ``transport-platform-http-session-construction`` (owner record
   ``credential-origin-scope``): ``requests.Session`` is constructed or
   subclassed only in ``utils/http_credentials.py`` or a frozen, reasoned
   inventory; every other client uses ``CredentialScopedSession``.
3. ``transport-platform-url-builder-credential-free`` (owner record
   ``credential-origin-scope``): no URL builder (a function named
   ``build_*url`` / ``_build_*url``) takes a credential parameter.

Frozen inventories only shrink: a stale entry or a changed count is a
violation, so a new site needs review.
"""

from __future__ import annotations

import ast
import re
from collections import Counter
from collections.abc import Iterator, Mapping
from types import MappingProxyType

from scripts.architecture_linter.checks.transport_platform_shared import GROUP, _src_python
from scripts.architecture_linter.checks.tree_index import TreeIndex
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.groups.common import checked_facts, source_text, violation
from scripts.architecture_linter.models import Rule, Violation

_ROUTE_RULE = "transport-platform-egress-route-policy"
_SESSION_RULE = "transport-platform-http-session-construction"
_URL_RULE = "transport-platform-url-builder-credential-free"
_MODULE_SCOPE = "<module>"

_ROUTE_OWNER = "src/apm_cli/deps/egress_route.py"
_ROUTE_CONFIG = "src/apm_cli/deps/registry_proxy.py"
_ROUTE_TABLE = "_REGISTRY_ONLY_ROUTES"
# The only surfaces that stay DIRECT in registry-only mode, by decision.
_DIRECT_EXCEPTIONS = frozenset({"POLICY_FETCH", "SELF_UPDATE"})
_FLAG_NAMES = frozenset({"PROXY_REGISTRY_ONLY", "ARTIFACTORY_ONLY"})
_FLAG_READERS = frozenset({"is_enforce_only", "_registry_only_active"})
_ENV_READERS = frozenset({"get", "getenv", "pop", "setdefault"})

# Closed world of route consumers: (path, surface member) -> exact reference
# count. A new outbound surface registers here; a dropped route check fails.
_ROUTE_CONSUMERS: Mapping[tuple[str, str], int] = MappingProxyType(
    {
        ("src/apm_cli/commands/_helpers.py", "VERSION_CHECK"): 1,
        ("src/apm_cli/commands/outdated.py", "SOURCE_ENUMERATION"): 1,
        ("src/apm_cli/commands/self_update.py", "SELF_UPDATE"): 1,
        ("src/apm_cli/commands/view.py", "SOURCE_ENUMERATION"): 1,
        ("src/apm_cli/deps/artifactory_orchestrator.py", "SOURCE_ARCHIVE"): 1,
        ("src/apm_cli/deps/git_reference_resolver.py", "SOURCE_ENUMERATION"): 2,
        ("src/apm_cli/deps/git_semver_resolver.py", "SOURCE_ENUMERATION"): 1,
        ("src/apm_cli/install/helpers/ref_reuse.py", "SOURCE_ENUMERATION"): 1,
        ("src/apm_cli/install/phases/integrate.py", "SOURCE_ARCHIVE"): 1,
        ("src/apm_cli/install/pipeline.py", "SOURCE_ARCHIVE"): 1,
        ("src/apm_cli/install/pipeline.py", "SOURCE_PROBE"): 1,
        ("src/apm_cli/install/validation.py", "SOURCE_PROBE"): 4,
        ("src/apm_cli/marketplace/client.py", "MARKETPLACE_FILE"): 3,
        ("src/apm_cli/models/dependency/reference.py", "SOURCE_ARCHIVE"): 1,
        ("src/apm_cli/policy/_policy_http.py", "POLICY_FETCH"): 2,
        ("src/apm_cli/registry/client.py", "MCP_REGISTRY"): 1,
    }
)

_SESSION_OWNER = "src/apm_cli/utils/http_credentials.py"
_REQUESTS_MODULES = frozenset({"requests", "requests.sessions"})
_SESSION_NAMES = frozenset({"Session", "session"})

# Frozen inventory of requests.Session construction outside the owner:
# (path, enclosing qualname) -> (exact site count, reason).
_SESSION_EXEMPTIONS: Mapping[tuple[str, str], tuple[int, str]] = MappingProxyType(
    {
        ("src/apm_cli/commands/auth.py", "check_token"): (
            1,
            "auth status probe: fresh session with trust_env=False and redirects refused",
        ),
        ("src/apm_cli/core/script_executors.py", "_build_capturing_session"): (
            1,
            "anonymous lifecycle HTTP dispatch: destination-guarded capturing adapter",
        ),
        ("src/apm_cli/core/script_executors.py", "_build_guarded_session"): (
            1,
            "anonymous lifecycle HTTP dispatch: destination-pinned adapter",
        ),
        ("src/apm_cli/deps/artifactory_entry.py", "_NoNetrcSession"): (
            1,
            "registry-proxy session without ambient netrc (artifactory-netrc-isolation)",
        ),
        ("src/apm_cli/utils/archive.py", _MODULE_SCOPE): (
            1,
            "anonymous archive download session",
        ),
    }
)

_URL_BUILDER_NAME = re.compile(r"^_?build_[a-z0-9_]*url$")
_CREDENTIAL_WORDS = frozenset({"token", "credential", "credentials", "password", "secret", "pat"})


def _scope(index: TreeIndex, node: ast.AST) -> str:
    anchor = index.definition_anchor(node)
    return _MODULE_SCOPE if anchor is None else index.function_qualname(anchor)


def _index(
    provider: FactsProvider, path: str, rule_id: str, findings: list[Violation]
) -> TreeIndex | None:
    _facts, failures = checked_facts(provider, path, rule_id, require_python=True)
    findings.extend(failures)
    return None if failures else provider.tree_index(path)


# -- egress route ------------------------------------------------------------


def _is_flag_read(node: ast.AST) -> bool:
    """Return whether *node* reads the registry-only flag directly."""
    if isinstance(node, ast.Name):
        return node.id in _FLAG_READERS
    if isinstance(node, ast.Attribute):
        return node.attr in _FLAG_READERS or (
            node.attr == "enforce_only" and isinstance(node.ctx, ast.Load)
        )
    if isinstance(node, ast.alias):
        return node.name in _FLAG_READERS
    if isinstance(node, ast.Subscript):
        key = node.slice
        return isinstance(key, ast.Constant) and key.value in _FLAG_NAMES
    if isinstance(node, ast.Call) and node.args:
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        first = node.args[0]
        return (
            name in _ENV_READERS and isinstance(first, ast.Constant) and first.value in _FLAG_NAMES
        )
    return False


def _surface_references(index: TreeIndex) -> Counter[str]:
    return Counter(
        node.attr
        for node in index.nodes
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "EgressSurface"
    )


def _owner_table_findings(index: TreeIndex) -> Iterator[Violation]:
    surfaces: set[str] = set()
    table: dict[str, str] | None = None
    for node in index.module_children():
        if isinstance(node, ast.ClassDef) and node.name == "EgressSurface":
            surfaces = {
                target.id
                for stmt in node.body
                if isinstance(stmt, ast.Assign)
                for target in stmt.targets
                if isinstance(target, ast.Name)
            }
        target = node.target if isinstance(node, ast.AnnAssign) else None
        if isinstance(target, ast.Name) and target.id == _ROUTE_TABLE:
            literal = next((child for child in ast.walk(node) if isinstance(child, ast.Dict)), None)
            table = (
                {}
                if literal is None
                else {
                    key.attr: getattr(value, "attr", "")
                    for key, value in zip(literal.keys, literal.values, strict=True)
                    if isinstance(key, ast.Attribute)
                }
            )
    if not surfaces or table is None:
        yield violation(
            _ROUTE_RULE, _ROUTE_OWNER, f"EgressSurface and {_ROUTE_TABLE} must stay literal"
        )
        return
    if set(table) != surfaces:
        missing = ", ".join(sorted(surfaces ^ set(table)))
        yield violation(
            _ROUTE_RULE, _ROUTE_OWNER, f"{_ROUTE_TABLE} must route every surface: {missing}"
        )
    direct = {surface for surface, route in table.items() if route == "DIRECT"}
    if direct != _DIRECT_EXCEPTIONS:
        yield violation(
            _ROUTE_RULE,
            _ROUTE_OWNER,
            "registry-only DIRECT exceptions are exactly the org policy fetch and explicit"
            f" self-update; found {', '.join(sorted(direct)) or 'none'}",
        )
    unconsumed = surfaces - {surface for _path, surface in _ROUTE_CONSUMERS}
    for surface in sorted(unconsumed):
        yield violation(_ROUTE_RULE, _ROUTE_OWNER, f"no registered consumer routes {surface}")


def _check_egress_route(provider: FactsProvider) -> tuple[Violation, ...]:
    """Registry-only routing is decided by the owner and asked by every surface."""
    findings: list[Violation] = []
    owner_index = _index(provider, _ROUTE_OWNER, _ROUTE_RULE, findings)
    if owner_index is not None:
        findings.extend(_owner_table_findings(owner_index))
    consumer_paths = {path for path, _surface in _ROUTE_CONSUMERS}
    for path in _src_python(provider):
        if path in (_ROUTE_OWNER, _ROUTE_CONFIG):
            continue
        facts, failures = checked_facts(provider, path, _ROUTE_RULE, require_python=True)
        if failures:
            findings.extend(failures)
            continue
        text = source_text(facts)
        if path not in consumer_paths and not any(
            token in text for token in (*_FLAG_NAMES, *_FLAG_READERS, "enforce_only")
        ):
            continue
        index = provider.tree_index(path)
        if index is None:
            continue
        for node in index.nodes:
            if _is_flag_read(node):
                findings.append(
                    violation(
                        _ROUTE_RULE,
                        path,
                        "registry-only mode is read outside the egress-route owner; ask"
                        " deps/egress_route.route_for for the route",
                        line=getattr(node, "lineno", 1),
                        column=getattr(node, "col_offset", 0) + 1,
                    )
                )
        if path in consumer_paths:
            seen = _surface_references(index)
            for (consumer, surface), expected in sorted(_ROUTE_CONSUMERS.items()):
                if consumer == path and seen.get(surface, 0) != expected:
                    findings.append(
                        violation(
                            _ROUTE_RULE,
                            path,
                            f"frozen route consumers expect {expected} EgressSurface.{surface}"
                            f" reference(s), found {seen.get(surface, 0)}",
                        )
                    )
    return tuple(findings)


# -- HTTP session construction ----------------------------------------------


def _session_aliases(index: TreeIndex) -> tuple[set[str], set[str]]:
    """Return (module aliases of requests, names bound to requests.Session)."""
    modules: set[str] = set()
    direct: set[str] = set()
    for node in index.nodes:
        if isinstance(node, ast.Import):
            modules.update(
                alias.asname or alias.name
                for alias in node.names
                if alias.name in _REQUESTS_MODULES
            )
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            if node.module in _REQUESTS_MODULES:
                direct.update(
                    alias.asname or alias.name
                    for alias in node.names
                    if alias.name in _SESSION_NAMES
                )
    return modules, direct


def _session_sites(index: TreeIndex) -> Iterator[ast.AST]:
    modules, direct = _session_aliases(index)

    def is_session(expr: ast.AST) -> bool:
        if isinstance(expr, ast.Name):
            return expr.id in direct
        return (
            isinstance(expr, ast.Attribute)
            and expr.attr in _SESSION_NAMES
            and ast.unparse(expr.value) in modules
        )

    for node in index.nodes:
        if isinstance(node, ast.Call) and is_session(node.func):
            yield node
        elif isinstance(node, ast.ClassDef):
            yield from (base for base in node.bases if is_session(base))


def _check_session_construction(provider: FactsProvider) -> tuple[Violation, ...]:
    """requests.Session is built only by the credential-origin-scope owner."""
    findings: list[Violation] = []
    seen: Counter[tuple[str, str]] = Counter()
    for path in _src_python(provider):
        if path == _SESSION_OWNER:
            continue
        facts, failures = checked_facts(provider, path, _SESSION_RULE, require_python=True)
        if failures:
            findings.extend(failures)
            continue
        if "requests" not in source_text(facts):
            continue
        index = provider.tree_index(path)
        if index is None:
            continue
        for node in _session_sites(index):
            key = (path, _scope(index, node))
            if key in _SESSION_EXEMPTIONS:
                seen[key] += 1
                continue
            findings.append(
                violation(
                    _SESSION_RULE,
                    path,
                    f"ad hoc requests.Session in {key[1]}; use"
                    " utils/http_credentials.CredentialScopedSession",
                    line=node.lineno,
                    column=node.col_offset + 1,
                )
            )
    for key, (expected, _reason) in sorted(_SESSION_EXEMPTIONS.items()):
        found = seen.get(key, 0)
        if found != expected:
            findings.append(
                violation(
                    _SESSION_RULE,
                    key[0],
                    f"frozen session inventory expects {expected} construction(s) in"
                    f" {key[1]}, found {found}; the inventory only shrinks",
                )
            )
    return tuple(findings)


# -- credential-free URL builders --------------------------------------------


def _credential_parameter(function: ast.FunctionDef | ast.AsyncFunctionDef) -> str | None:
    arguments = function.args
    for argument in (
        *arguments.posonlyargs,
        *arguments.args,
        *arguments.kwonlyargs,
        *(a for a in (arguments.vararg, arguments.kwarg) if a is not None),
    ):
        if _CREDENTIAL_WORDS & set(argument.arg.lower().split("_")):
            return argument.arg
    return None


def _check_url_builders(provider: FactsProvider) -> tuple[Violation, ...]:
    """No URL builder accepts a credential: credentials never travel in URLs."""
    findings: list[Violation] = []
    for path in _src_python(provider):
        facts, failures = checked_facts(provider, path, _URL_RULE, require_python=True)
        if failures:
            findings.extend(failures)
            continue
        if "url(" not in source_text(facts):
            continue
        index = provider.tree_index(path)
        if index is None:
            continue
        for node in index.nodes:
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            if not _URL_BUILDER_NAME.match(node.name):
                continue
            parameter = _credential_parameter(node)
            if parameter is not None:
                findings.append(
                    violation(
                        _URL_RULE,
                        path,
                        f"URL builder {node.name} takes credential parameter {parameter!r};"
                        " send credentials through git env or origin-scoped headers",
                        line=node.lineno,
                        column=node.col_offset + 1,
                    )
                )
    return tuple(findings)


RULES: tuple[Rule, ...] = (
    Rule(
        id=_ROUTE_RULE,
        group=GROUP,
        guard_ids=(_ROUTE_RULE,),
        description="Every outbound surface asks the egress-route owner for its route.",
        check=_check_egress_route,
    ),
    Rule(
        id=_SESSION_RULE,
        group=GROUP,
        guard_ids=(_SESSION_RULE,),
        description="requests.Session is built only by the credential-origin-scope owner.",
        check=_check_session_construction,
    ),
    Rule(
        id=_URL_RULE,
        group=GROUP,
        guard_ids=(_URL_RULE,),
        description="No URL builder accepts a credential parameter.",
        check=_check_url_builders,
    ),
)

COLLECTORS: tuple[object, ...] = ()

__all__ = ["COLLECTORS", "RULES"]
