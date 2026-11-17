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
   record ``credential-origin-scope``): a literal ``Authorization`` /
   ``PRIVATE-TOKEN`` header key (dict key, subscript store, keyword, or
   ``add_header``-style first argument; matched case-insensitively) appears
   only in the credential-header owners or in :data:`_HEADER_EXEMPTIONS`.

Both inventories are keyed by ``(path, enclosing definition qualname)``. They
only shrink: an entry that no longer matches a site is itself a violation, so a
migrated call site must also retire its exemption. Session-receiver detection is
best-effort (a receiver whose last name segment ends in ``session``); module
alias tracking covers ``import requests as x`` and ``from requests import get``.
"""

from __future__ import annotations

import ast
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
_CREDENTIAL_HEADER_KEYS = frozenset({"authorization", "private-token"})
_HEADER_SETTERS = frozenset({"add_header", "add_unredirected_header", "putheader", "setdefault"})
_PREFILTER_EGRESS = ("requests", "urlopen", "build_opener")
_PREFILTER_HEADERS = ("authorization", "private-token")

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
        "src/apm_cli/deps/registry/auth.py": None,
        "src/apm_cli/deps/registry_proxy.py": None,
        "src/apm_cli/utils/github_host.py": "set_authorization_header_git_env",
        "src/apm_cli/utils/http_credentials.py": None,
    }
)

# Frozen inventory of HTTP egress outside the owners. Reasons prefixed
# ``pending PR <n>:`` are sites the 0.36.1 auth track migrates onto the owners.
_EGRESS_EXEMPTIONS: Mapping[tuple[str, str], str] = MappingProxyType(
    {
        ("src/apm_cli/commands/auth.py", "check_token"): (
            "auth status probe: fresh session, trust_env=False, redirects refused"
        ),
        ("src/apm_cli/commands/self_update.py", "self_update"): (
            "anonymous installer download owned by release-metadata-discovery"
        ),
        ("src/apm_cli/core/script_executors.py", "_build_capturing_session"): (
            "anonymous lifecycle HTTP dispatch: destination-guarded capturing session"
        ),
        ("src/apm_cli/core/script_executors.py", "_build_guarded_session"): (
            "anonymous lifecycle HTTP dispatch: destination-pinned guarded session"
        ),
        ("src/apm_cli/core/script_executors.py", "_dispatch_http_request._run"): (
            "anonymous lifecycle HTTP dispatch through the guarded sessions"
        ),
        ("src/apm_cli/deps/artifactory_entry.py", "_NoNetrcSession"): (
            "registry-proxy session without ambient netrc (artifactory-netrc-isolation)"
        ),
        ("src/apm_cli/deps/artifactory_entry.py", "_fetch_entry"): (
            "registry-proxy entry fetch; headers from RegistryConfig.headers_for"
        ),
        ("src/apm_cli/deps/download_strategies.py", "DownloadDelegate.resilient_get"): (
            "pending PR 3: shared download retry transport; callers pass owner-built headers"
        ),
        ("src/apm_cli/deps/download_strategies.py", "DownloadDelegate.try_raw_download"): (
            "anonymous pre-auth raw content CDN fetch; never carries a credential"
        ),
        ("src/apm_cli/deps/registry/client.py", "RegistryClient.__init__"): (
            "pending PR 2: registry session construction"
        ),
        ("src/apm_cli/deps/registry/client.py", "RegistryClient._request"): (
            "pending PR 2: registry GET through origin_bounded_request"
        ),
        ("src/apm_cli/deps/registry/client.py", "RegistryClient.fetch_from_url"): (
            "pending PR 2: registry download through origin_bounded_request"
        ),
        ("src/apm_cli/deps/registry/client.py", "RegistryClient.publish_version"): (
            "pending PR 2: registry publish through origin_bounded_request"
        ),
        ("src/apm_cli/install/artifactory_resolver.py", "_candidate_archive_status"): (
            "registry-proxy boundary HEAD; registry headers, redirects refused"
        ),
        ("src/apm_cli/install/validation.py", "_validate_github_package._check_repo"): (
            "pending PR 1: validation API probe routed through the auth chain"
        ),
        (
            "src/apm_cli/install/validation.py",
            "_validate_parse_failure_fallback._check_repo_fallback",
        ): "pending PR 1: validation API probe routed through the auth chain",
        (
            "src/apm_cli/marketplace/builder.py",
            "MarketplaceBuilder._fetch_remote_metadata_outcome._open_metadata._request_text",
        ): "pending PR 2: metadata fetch through origin_bounded_request",
        ("src/apm_cli/marketplace/client.py", "_http_get"): (
            "pending PR 2: CredentialScopedSession redirect follow moves to the owner"
        ),
        ("src/apm_cli/policy/_gitlab.py", "_authenticated_gitlab_request._request"): (
            "pending PR 1: policy GitLab fetch routed through the auth chain"
        ),
        ("src/apm_cli/policy/discovery.py", "_fetch_ado_contents._request"): (
            "pending PR 1: policy ADO fetch routed through the auth chain"
        ),
        ("src/apm_cli/policy/discovery.py", "_fetch_from_url"): (
            "anonymous policy URL fetch; redirects refused"
        ),
        ("src/apm_cli/policy/discovery.py", "_fetch_github_contents"): (
            "pending PR 1: policy GitHub fetch routed through the auth chain"
        ),
        ("src/apm_cli/registry/client.py", "SimpleRegistryClient.__init__"): (
            "anonymous MCP registry session"
        ),
        ("src/apm_cli/registry/client.py", "SimpleRegistryClient._cached_get_json"): (
            "anonymous MCP registry metadata fetch"
        ),
        ("src/apm_cli/utils/archive.py", _MODULE_SCOPE): ("anonymous archive download session"),
        ("src/apm_cli/utils/archive.py", "_archive_get"): ("anonymous archive download"),
        ("src/apm_cli/utils/version_checker.py", "get_latest_version_from_github.fetch"): (
            "release metadata lookup owned by release-metadata-discovery; redirects refused"
        ),
    }
)

# Frozen inventory of literal credential-header keys outside the owners.
_HEADER_EXEMPTIONS: Mapping[tuple[str, str], str] = MappingProxyType(
    {
        ("src/apm_cli/adapters/client/base.py", "MCPClientAdapter._apply_auth_and_headers_impl"): (
            "writes MCP server config for the harness client; owned by the harness track"
        ),
        ("src/apm_cli/commands/auth.py", "check_token"): (
            "pending PR 3: status probe header moves to an AuthResolver header builder"
        ),
        (
            "src/apm_cli/deps/download_strategies.py",
            "DownloadDelegate._build_generic_host_auth_headers",
        ): "pending PR 3: generic host header moves to an AuthResolver header builder",
        (
            "src/apm_cli/deps/download_strategies.py",
            "DownloadDelegate._download_public_github_file_anonymous_first._contents_request",
        ): "pending PR 3: GitHub contents header moves to an AuthResolver header builder",
        (
            "src/apm_cli/deps/download_strategies.py",
            "DownloadDelegate.download_ado_file._headers_for",
        ): ("pending PR 1: ADO download credential chosen by the auth chain"),
        ("src/apm_cli/deps/download_strategies.py", "DownloadDelegate.download_github_file"): (
            "pending PR 3: GitHub contents header moves to an AuthResolver header builder"
        ),
        (
            "src/apm_cli/deps/git_reference_resolver.py",
            "GitReferenceResolver.resolve_commit_sha_for_ref._request",
        ): "pending PR 1: ref resolution routed through the auth chain",
        ("src/apm_cli/deps/github_downloader_validation.py", "_directory_exists_at_ref._request"): (
            "pending PR 1: validation probe routed through the auth chain"
        ),
        ("src/apm_cli/deps/registry/client.py", "RegistryClient._headers"): (
            "pending PR 3: registry header assembly moves into deps/registry/auth.py"
        ),
        ("src/apm_cli/install/artifactory_resolver.py", "_proxy_probe_headers"): (
            "pending PR 3: registry-proxy probe header moves to RegistryConfig"
        ),
        ("src/apm_cli/install/validation.py", "_validate_github_package._check_repo"): (
            "pending PR 1: validation API probe routed through the auth chain"
        ),
        (
            "src/apm_cli/install/validation.py",
            "_validate_parse_failure_fallback._check_repo_fallback",
        ): "pending PR 1: validation API probe routed through the auth chain",
        (
            "src/apm_cli/marketplace/builder.py",
            "MarketplaceBuilder._fetch_remote_metadata_outcome._open_metadata._request_text",
        ): "pending PR 2: metadata fetch through origin_bounded_request",
        ("src/apm_cli/marketplace/client.py", "_ado_auth_header"): (
            "pending PR 1: marketplace ADO credential chosen by the auth chain"
        ),
        ("src/apm_cli/marketplace/client.py", "_github_headers"): (
            "pending PR 3: marketplace GitHub header moves to an AuthResolver header builder"
        ),
        ("src/apm_cli/policy/discovery.py", "_fetch_ado_contents._headers"): (
            "pending PR 1: policy ADO credential chosen by the auth chain"
        ),
        ("src/apm_cli/policy/discovery.py", "_fetch_github_contents"): (
            "pending PR 1: policy GitHub fetch routed through the auth chain"
        ),
        ("src/apm_cli/utils/version_checker.py", "get_latest_version_from_github"): (
            "release metadata lookup owned by release-metadata-discovery"
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


def _egress_sites(index: TreeIndex) -> Iterator[ast.AST]:
    aliases = _Aliases(index)
    for node in index.nodes:
        attribute = isinstance(node, ast.Attribute) and aliases.is_egress_attribute(node)
        direct = (
            isinstance(node, ast.Name)
            and isinstance(node.ctx, ast.Load)
            and node.id in aliases.direct
        )
        if attribute or direct:
            yield node


def _is_credential_key(node: ast.AST | None) -> bool:
    return (
        isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value.lower() in _CREDENTIAL_HEADER_KEYS
    )


def _header_sites(index: TreeIndex) -> Iterator[ast.AST]:
    for node in index.nodes:
        if isinstance(node, ast.Dict):
            yield from (key for key in node.keys if _is_credential_key(key))
        elif isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Store):
            if _is_credential_key(node.slice):
                yield node
        elif isinstance(node, ast.Call):
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr in _HEADER_SETTERS
                and node.args
                and _is_credential_key(node.args[0])
            ):
                yield node
            for keyword in node.keywords:
                if keyword.arg is not None and keyword.arg.lower() in _CREDENTIAL_HEADER_KEYS:
                    yield keyword


def _header_owned(path: str, scope: str) -> bool:
    if path not in _HEADER_OWNERS:
        return False
    owned = _HEADER_OWNERS[path]
    return owned is None or scope == owned or scope.startswith(f"{owned}.")


def _check_authenticated_egress(provider: FactsProvider) -> tuple[Violation, ...]:
    """HTTP egress and credential headers stay with their owners or a frozen inventory."""
    findings: list[Violation] = []
    seen_egress: set[tuple[str, str]] = set()
    seen_headers: set[tuple[str, str]] = set()
    for path in _src_python(provider):
        facts, failures = checked_facts(provider, path, _RULE_ID, require_python=True)
        if failures:
            findings.extend(failures)
            continue
        lowered = source_text(facts).lower()
        scan_egress = path not in _EGRESS_OWNERS and any(
            token in lowered for token in _PREFILTER_EGRESS
        )
        scan_headers = any(token in lowered for token in _PREFILTER_HEADERS)
        if not (scan_egress or scan_headers):
            continue
        index = provider.tree_index(path)
        if index is None:
            continue
        if scan_egress:
            for node in _egress_sites(index):
                key = (path, _scope(index, node))
                if key in _EGRESS_EXEMPTIONS:
                    seen_egress.add(key)
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
            for node in _header_sites(index):
                key = (path, _scope(index, node))
                if _header_owned(*key):
                    continue
                if key in _HEADER_EXEMPTIONS:
                    seen_headers.add(key)
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
    for label, inventory, seen in (
        ("egress", _EGRESS_EXEMPTIONS, seen_egress),
        ("credential-header", _HEADER_EXEMPTIONS, seen_headers),
    ):
        for path, scope in sorted(set(inventory) - seen):
            findings.append(
                violation(
                    _RULE_ID,
                    path,
                    f"stale {label} exemption for {scope}; the frozen inventory only shrinks,"
                    " remove the entry",
                )
            )
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
