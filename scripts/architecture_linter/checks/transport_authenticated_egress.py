"""Authenticated HTTP egress and credential-header ownership.

One rule, two guards in ``.apm/architecture/owners/transport-auth-platform.json``.
Two AST facts are enforced over ``src/apm_cli/**``:

1. ``transport-platform-authenticated-egress`` (owner record
   ``host-credential-resolution``): HTTP egress primitives --
   ``requests.<verb>`` / ``requests.Session`` (called, aliased, or
   subclassed), verb methods on a session-named receiver, and
   ``urllib.request.urlopen`` / ``build_opener`` -- appear only in the egress
   owners or in :data:`_EGRESS_EXEMPTIONS`, a frozen, reasoned inventory.
2. ``transport-platform-authenticated-egress-credential-headers`` (owner
   record ``credential-origin-scope``): a literal credential header key (dict
   key, subscript store, keyword, or ``add_header``-style first argument;
   matched case-insensitively) appears only in the credential-header owners
   or in :data:`_HEADER_EXEMPTIONS`. The header vocabulary is read from
   ``utils/http_credentials.CREDENTIAL_HEADER_NAMES``, never restated here.

Both inventories map ``(path, enclosing definition qualname)`` to an exact
site count and a reason. They only shrink: an entry with no matching site, or a
changed count, is itself a violation, so a migrated call site must retire its
exemption and a new site inside an exempt function still needs review.
Session-receiver detection is best-effort (a receiver whose last name segment
ends in ``session``); module alias tracking covers ``import requests as x``
and ``from requests import get``. Type annotations are not egress.
"""

from __future__ import annotations

import ast
from collections import Counter
from collections.abc import Iterator, Mapping
from types import MappingProxyType

from scripts.architecture_linter.checks.transport_platform_shared import GROUP, _src_python
from scripts.architecture_linter.checks.tree_index import TreeIndex
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.groups.common import checked_facts, source_text, violation
from scripts.architecture_linter.models import Rule, Violation

_RULE_ID = "transport-platform-authenticated-egress"
_HEADER_GUARD_ID = "transport-platform-authenticated-egress-credential-headers"
_MODULE_SCOPE = "<module>"

_REQUESTS_MODULES = frozenset({"requests", "requests.api", "requests.sessions"})
_HTTP_VERBS = frozenset({"delete", "get", "head", "options", "patch", "post", "put", "request"})
_SESSION_FACTORIES = frozenset({"Session", "session"})
_SESSION_METHODS = _HTTP_VERBS | {"send"}
_URLLIB_EGRESS = frozenset({"build_opener", "urlopen"})
_HEADER_SETTERS = frozenset({"add_header", "add_unredirected_header", "putheader", "setdefault"})
_PREFILTER_EGRESS = ("requests", "urlopen", "build_opener")
# The credential header vocabulary is read from its runtime owner, never
# restated here.
_CREDENTIAL_NAMES_OWNER = "src/apm_cli/utils/http_credentials.py"
_CREDENTIAL_NAMES = "CREDENTIAL_HEADER_NAMES"

# Modules that own HTTP egress decisions outright.
_EGRESS_OWNERS = frozenset(
    {
        "src/apm_cli/core/auth.py",
        "src/apm_cli/utils/http_credentials.py",
    }
)

# Credential-header owners. ``None`` owns the whole module; a qualname owns
# only that definition.
_HEADER_OWNERS: Mapping[str, str | None] = MappingProxyType(
    {
        "src/apm_cli/core/auth.py": None,
        "src/apm_cli/core/auth_chain.py": None,
        "src/apm_cli/deps/registry/auth.py": "RegistryAuthContext",
        "src/apm_cli/deps/registry_proxy.py": None,
        "src/apm_cli/utils/github_host.py": "set_authorization_header_git_env",
        "src/apm_cli/utils/http_credentials.py": None,
    }
)

# Frozen inventory of HTTP egress outside the owners: (path, enclosing
# qualname) -> (exact site count, reason). It only shrinks; a stale entry or a
# changed count is a violation, so a new egress site inside an exempt function
# still needs review. Credential-bearing egress belongs in
# utils/http_credentials.origin_bounded_request.
_EGRESS_EXEMPTIONS: Mapping[tuple[str, str], tuple[int, str]] = MappingProxyType(
    {
        ("src/apm_cli/commands/auth.py", "check_token"): (
            2,
            "auth status probe: fresh session, trust_env=False, redirects refused",
        ),
        ("src/apm_cli/commands/self_update.py", "self_update"): (
            1,
            "anonymous installer download owned by release-metadata-discovery",
        ),
        ("src/apm_cli/core/script_executors.py", "_build_capturing_session"): (
            1,
            "anonymous lifecycle HTTP dispatch: destination-guarded capturing session",
        ),
        ("src/apm_cli/core/script_executors.py", "_build_guarded_session"): (
            1,
            "anonymous lifecycle HTTP dispatch: destination-pinned guarded session",
        ),
        ("src/apm_cli/core/script_executors.py", "_dispatch_http_request._run"): (
            3,
            "anonymous lifecycle HTTP dispatch through the guarded sessions",
        ),
        ("src/apm_cli/deps/artifactory_entry.py", "_NoNetrcSession"): (
            1,
            "registry-proxy session without ambient netrc (artifactory-netrc-isolation)",
        ),
        ("src/apm_cli/deps/artifactory_entry.py", "_fetch_entry"): (
            1,
            "registry-proxy entry fetch; headers from RegistryConfig.headers_for",
        ),
        ("src/apm_cli/deps/download_strategies.py", "DownloadDelegate.resilient_get"): (
            2,
            "shared download retry transport. Callers pass AuthResolver or RegistryConfig"
            " headers. redirect_base callers go through the redirect owner; allow_netrc=False"
            " callers use the no-netrc session; other callers (GitHub contents, generic"
            " hosts) still use requests defaults for netrc and redirects -- follow-up",
        ),
        ("src/apm_cli/deps/download_strategies.py", "DownloadDelegate.try_raw_download"): (
            1,
            "anonymous pre-auth raw content CDN fetch; never carries a credential",
        ),
        ("src/apm_cli/registry/client.py", "SimpleRegistryClient.__init__"): (
            1,
            "anonymous MCP registry session",
        ),
        ("src/apm_cli/registry/client.py", "SimpleRegistryClient._cached_get_json"): (
            3,
            "anonymous MCP registry metadata fetch",
        ),
        ("src/apm_cli/utils/archive.py", _MODULE_SCOPE): (
            1,
            "anonymous archive download session",
        ),
        ("src/apm_cli/utils/archive.py", "_archive_get"): (1, "anonymous archive download"),
        ("src/apm_cli/utils/version_checker.py", "get_latest_version_from_github.fetch"): (
            1,
            "release metadata lookup owned by release-metadata-discovery; redirects refused",
        ),
    }
)

# Frozen inventory of literal credential-header keys outside the owners, in
# the same (count, reason) shape.
_HEADER_EXEMPTIONS: Mapping[tuple[str, str], tuple[int, str]] = MappingProxyType(
    {
        ("src/apm_cli/adapters/client/base.py", "MCPClientAdapter._apply_auth_and_headers_impl"): (
            1,
            "writes MCP server config for the harness client; owned by the harness track",
        ),
        ("src/apm_cli/utils/version_checker.py", "get_latest_version_from_github"): (
            1,
            "release metadata lookup owned by release-metadata-discovery",
        ),
    }
)


def _scope(index: TreeIndex, node: ast.AST) -> str:
    anchor = index.definition_anchor(node)
    return _MODULE_SCOPE if anchor is None else index.function_qualname(anchor)


def _dotted(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return None if base is None else f"{base}.{node.attr}"
    return None


class _Aliases:
    """Module-local names bound to ``requests`` / ``urllib.request`` egress."""

    def __init__(self, index: TreeIndex) -> None:
        self.requests_modules: set[str] = set()
        self.urllib_modules: set[str] = {"urllib.request"}
        self.direct: set[str] = set()
        for node in index.nodes:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in _REQUESTS_MODULES:
                        self.requests_modules.add(alias.asname or alias.name)
                    elif alias.name == "urllib.request" and alias.asname:
                        self.urllib_modules.add(alias.asname)
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                self._from_import(node)

    def _from_import(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            bound = alias.asname or alias.name
            requests_name = node.module in _REQUESTS_MODULES and (
                alias.name in _HTTP_VERBS or alias.name in _SESSION_FACTORIES
            )
            urllib_name = node.module == "urllib.request" and alias.name in _URLLIB_EGRESS
            if requests_name or urllib_name:
                self.direct.add(bound)
            elif node.module == "urllib" and alias.name == "request":
                self.urllib_modules.add(bound)

    def is_egress_attribute(self, node: ast.Attribute) -> bool:
        owner = _dotted(node.value)
        if owner in self.requests_modules:
            return node.attr in _HTTP_VERBS or node.attr in _SESSION_FACTORIES
        if owner in self.urllib_modules:
            return node.attr in _URLLIB_EGRESS
        if node.attr in _SESSION_METHODS and isinstance(node.ctx, ast.Load):
            receiver = node.value
            name = receiver.attr if isinstance(receiver, ast.Attribute) else None
            if isinstance(receiver, ast.Name):
                name = receiver.id
            return name is not None and name.lower().endswith("session")
        return False


def _annotation_node_ids(index: TreeIndex) -> frozenset[int]:
    """Return ids of nodes inside type annotations (references, not egress)."""
    roots: list[ast.AST] = []
    for node in index.nodes:
        if isinstance(node, ast.arg | ast.AnnAssign) and node.annotation is not None:
            roots.append(node.annotation)
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.returns:
            roots.append(node.returns)
    return frozenset(id(child) for root in roots for child in index.walk(root))


def _egress_sites(index: TreeIndex) -> Iterator[ast.AST]:
    aliases = _Aliases(index)
    annotations = _annotation_node_ids(index)
    for node in index.nodes:
        if id(node) in annotations:
            continue
        attribute = isinstance(node, ast.Attribute) and aliases.is_egress_attribute(node)
        direct = (
            isinstance(node, ast.Name)
            and isinstance(node.ctx, ast.Load)
            and node.id in aliases.direct
        )
        if attribute or direct:
            yield node


def _is_credential_key(node: ast.AST | None, keys: frozenset[str]) -> bool:
    return (
        isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value.lower() in keys
    )


def _header_sites(index: TreeIndex, keys: frozenset[str]) -> Iterator[ast.AST]:
    for node in index.nodes:
        if isinstance(node, ast.Dict):
            yield from (key for key in node.keys if _is_credential_key(key, keys))
        elif isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Store):
            if _is_credential_key(node.slice, keys):
                yield node
        elif isinstance(node, ast.Call):
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr in _HEADER_SETTERS
                and node.args
                and _is_credential_key(node.args[0], keys)
            ):
                yield node
            for keyword in node.keywords:
                if keyword.arg is not None and keyword.arg.lower() in keys:
                    yield keyword


def _header_owned(path: str, scope: str) -> bool:
    if path not in _HEADER_OWNERS:
        return False
    owned = _HEADER_OWNERS[path]
    return owned is None or scope == owned or scope.startswith(f"{owned}.")


def _credential_header_keys(
    provider: FactsProvider,
) -> tuple[frozenset[str], tuple[Violation, ...]]:
    """Read the credential header vocabulary from its runtime owner."""
    _facts, failures = checked_facts(
        provider, _CREDENTIAL_NAMES_OWNER, _RULE_ID, require_python=True
    )
    index = None if failures else provider.tree_index(_CREDENTIAL_NAMES_OWNER)
    for node in () if index is None else index.module_children():
        target = node.target if isinstance(node, ast.AnnAssign) else None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
        value = getattr(node, "value", None)
        if (
            isinstance(target, ast.Name)
            and target.id == _CREDENTIAL_NAMES
            and isinstance(value, ast.Tuple)
            and value.elts
            and all(isinstance(e, ast.Constant) and isinstance(e.value, str) for e in value.elts)
        ):
            return frozenset(e.value.lower() for e in value.elts), ()
    return frozenset(), (
        *failures,
        violation(
            _RULE_ID,
            _CREDENTIAL_NAMES_OWNER,
            f"{_CREDENTIAL_NAMES} must stay a literal tuple of header names in the owner",
        ),
    )


def _inventory_findings(
    label: str,
    inventory: Mapping[tuple[str, str], tuple[int, str]],
    seen: Mapping[tuple[str, str], int],
) -> Iterator[Violation]:
    for (path, scope), (expected, _reason) in sorted(inventory.items()):
        found = seen.get((path, scope), 0)
        if found == 0:
            message = (
                f"stale {label} exemption for {scope}; the frozen inventory only shrinks,"
                " remove the entry"
            )
        elif found != expected:
            message = (
                f"frozen {label} inventory expects {expected} site(s) in {scope}, found"
                f" {found}; route new sites through the owner"
            )
        else:
            continue
        yield violation(_RULE_ID, path, message)


def _check_authenticated_egress(provider: FactsProvider) -> tuple[Violation, ...]:
    """HTTP egress and credential headers stay with their owners or a frozen inventory."""
    keys, findings_init = _credential_header_keys(provider)
    findings: list[Violation] = list(findings_init)
    seen_egress: Counter[tuple[str, str]] = Counter()
    seen_headers: Counter[tuple[str, str]] = Counter()
    for path in _src_python(provider):
        facts, failures = checked_facts(provider, path, _RULE_ID, require_python=True)
        if failures:
            findings.extend(failures)
            continue
        lowered = source_text(facts).lower()
        scan_egress = path not in _EGRESS_OWNERS and any(
            token in lowered for token in _PREFILTER_EGRESS
        )
        scan_headers = any(key in lowered for key in keys)
        if not (scan_egress or scan_headers):
            continue
        index = provider.tree_index(path)
        if index is None:
            continue
        if scan_egress:
            for node in _egress_sites(index):
                key = (path, _scope(index, node))
                if key in _EGRESS_EXEMPTIONS:
                    seen_egress[key] += 1
                    continue
                findings.append(
                    violation(
                        _RULE_ID,
                        path,
                        f"HTTP egress in {key[1]} outside the owners (core/auth.py,"
                        " utils/http_credentials.py); route through the owner or add a"
                        " reasoned anonymous-egress exemption",
                        line=node.lineno,
                        column=node.col_offset + 1,
                    )
                )
        if scan_headers:
            for node in _header_sites(index, keys):
                key = (path, _scope(index, node))
                if _header_owned(*key):
                    continue
                if key in _HEADER_EXEMPTIONS:
                    seen_headers[key] += 1
                    continue
                findings.append(
                    violation(
                        _RULE_ID,
                        path,
                        f"literal credential header key in {key[1]}; build credential"
                        " headers through AuthResolver, RegistryConfig or http_credentials",
                        line=node.lineno,
                        column=node.col_offset + 1,
                    )
                )
    findings.extend(_inventory_findings("egress", _EGRESS_EXEMPTIONS, seen_egress))
    findings.extend(_inventory_findings("credential-header", _HEADER_EXEMPTIONS, seen_headers))
    return tuple(findings)


RULES: tuple[Rule, ...] = (
    Rule(
        id=_RULE_ID,
        group=GROUP,
        guard_ids=(_RULE_ID, _HEADER_GUARD_ID),
        description=(
            "HTTP egress and credential header keys stay in their owners or a frozen inventory."
        ),
        check=_check_authenticated_egress,
    ),
)

COLLECTORS: tuple[object, ...] = ()

__all__ = ["COLLECTORS", "RULES"]
