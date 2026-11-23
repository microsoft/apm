"""Registry URL scheme acceptance is closed-world (#4286).

* ``deps/registry/url_scheme.py`` is the one owner of whether a
  ``registries.<name>.url`` is accepted (OpenAPM v0.1 req-sc-006); it asks the
  shared network-host owner (``utils/net.py``) whether a host is local.
* Every function that reads or writes a ``registries`` entry's ``url`` asks
  the owner (``registry_url_error``) and holds no ``http://`` scheme decision of
  its own: both manifest contracts, the workspace manifest, and user config
  (``apm config set registry.<name>.url``, ``$APM_HOME/config.json``).
"""

from __future__ import annotations

import ast

from scripts.architecture_linter.checks.install_deployment_shared import (
    _facts_for,
    _present,
    _python_paths,
    _summary,
)
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.groups.common import violation
from scripts.architecture_linter.models import Violation

GUARD_REGISTRY_URL_SCHEME = "install-deployment-registry-url-scheme"

_OWNER = "src/apm_cli/deps/registry/url_scheme.py"
_OWNER_CALL = "registry_url_error"
# Display-only readers that echo the configured URL (``apm config``); they
# accept nothing, so they need no scheme decision.
_DISPLAY_SCOPES = frozenset({("src/apm_cli/commands/config.py", "_show_all_user_config")})
# User config (``apm config set registry.<name>.url``) is read through these.
_REGISTRY_CONFIG_READERS = frozenset({"_get_registries_section", "get_registry_config"})
_OWNER_NEEDLES = (
    "def registry_url_error(",
    "from ...utils.net import is_local_network_host",
    "return is_local_network_host(host)",
    'if parts.scheme == "https" or insecure is True or _is_local_host(host):',
)


def _reads_registry_url(index, function: ast.AST) -> tuple[bool, set[str], set[str]]:
    """Whether *function* reads or writes registry entry URLs, its strings and callees."""
    strings: set[str] = set()
    callees: set[str] = set()
    reads_url = False
    for node in index.walk(function):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            strings.add(node.value)
        elif isinstance(node, ast.Call):
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name:
                callees.add(name)
            if (
                name == "get"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and node.args[0].value == "url"
            ):
                reads_url = True
        elif (
            isinstance(node, ast.Subscript)
            and isinstance(node.slice, ast.Constant)
            and node.slice.value == "url"
        ):
            reads_url = True
    registries = "registries" in strings or bool(callees & _REGISTRY_CONFIG_READERS)
    return reads_url and registries, strings, callees


def check_registry_url_scheme(provider: FactsProvider) -> tuple[Violation, ...]:
    """Every registries URL reader asks the one scheme-acceptance owner."""
    rule = GUARD_REGISTRY_URL_SCHEME
    owner, failures = _facts_for(provider, _OWNER, rule)
    if failures:
        return tuple(failures)
    findings = [
        _summary(rule, _OWNER, f"The registry URL scheme owner lost: {needle}")
        for needle in _OWNER_NEEDLES
        if not _present(owner, needle)
    ]
    for path in _python_paths(provider, "src/apm_cli/"):
        if path == _OWNER:
            continue
        facts, failed = _facts_for(provider, path, rule)
        if failed:
            findings.extend(failed)
            continue
        index = facts.tree_index
        if index is None:
            continue
        for function in index.functions():
            if (path, function.name) in _DISPLAY_SCOPES:
                continue
            reads, strings, callees = _reads_registry_url(index, function)
            if not reads:
                continue
            if _OWNER_CALL not in callees:
                findings.append(
                    violation(
                        rule,
                        path,
                        f"{function.name} reads a registries URL without {_OWNER_CALL}",
                        line=function.lineno,
                    )
                )
            if any(value.startswith("http://") for value in strings):
                findings.append(
                    violation(
                        rule,
                        path,
                        f"{function.name} decides the http:// scheme beside {_OWNER}",
                        line=function.lineno,
                    )
                )
    return tuple(findings)
