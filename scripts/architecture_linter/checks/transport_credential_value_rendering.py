"""Owner check for how credential values are rendered in command output.

Ports the ``credential-value-rendering`` owner decision: one module,
``utils/credential_rendering.py``, decides what a credential-bearing value
looks like when APM prints, logs or exports it.

* The owner defines the mask, the credential-key test, the config value
  renderer, the free-text pattern, URL userinfo stripping and known-value
  masking exactly once.
* No other product module regrows the secret pattern, a URL userinfo
  stripper or a ``[REDACTED]`` replacement loop (closed world).
* ``apm config`` output is closed-world: every argument of an echo, print,
  logger, console, Rich-helper or table call is a fixed literal, a key or
  label name, or a value rendered through the owner (AST check, so no call
  shape such as ``click.echo(body["token"])`` slips past).
* Known consumers (debug log filter, script log, SBOM export, proxy and
  registry diagnostics) delegate to the owner.
"""

from __future__ import annotations

import ast
import re

from scripts.architecture_linter.checks.transport_platform_shared import (
    GROUP,
    _count_checks,
    _forbid_scan,
    _load,
    _require_subs,
    _src_python,
)
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.groups.common import source_text, violation
from scripts.architecture_linter.models import Rule, Violation

_RULE_ID = "transport-platform-credential-value-rendering"

_OWNER = "src/apm_cli/utils/credential_rendering.py"
_CONFIG_COMMAND = "src/apm_cli/commands/config.py"

_OWNER_DEFINITIONS: tuple[tuple[str, str, int, str], ...] = (
    ("sub", 'CREDENTIAL_MASK = "********"', 1, "eq"),
    ("sub", "SECRET_RE = re.compile(", 1, "eq"),
    ("sub", "def redact_secrets(", 1, "eq"),
    ("sub", "def mask_known_values(", 1, "eq"),
    ("sub", "def redact_url_userinfo(", 1, "eq"),
    ("sub", "def is_credential_key(", 1, "eq"),
    ("sub", "def render_credential_value(", 1, "eq"),
    ("sub", "def render_config_value(", 1, "eq"),
    ("sub", "        return render_credential_value(value)", 1, "eq"),
)

# Redaction mechanisms that must not regrow outside the owner.
_FORBIDDEN_MECHANISMS = re.compile(
    r"^\s*SECRET_RE\s*=\s*re\.compile\("
    r"|^\s*def redact_secrets\("
    r"|^\s*def _?redact_url_userinfo\("
    r"|\.replace\([^)\n]*,\s*[\"']\[REDACTED\][\"']\)"
)

# Config output paths whose sinks are checked closed-world: every argument of
# an echo, print, logger, console, Rich-helper or table call is a fixed
# literal, a key or label name, or a value rendered through the owner.
_CONFIG_OUTPUT_PATHS = (_CONFIG_COMMAND,)
_SINK_NAMES = frozenset({"print", "echo", "secho"})
_SINK_ATTRS = frozenset(
    {
        "add_row",
        "critical",
        "debug",
        "echo",
        "error",
        "exception",
        "info",
        "log",
        "print",
        "progress",
        "secho",
        "success",
        "verbose_detail",
        "warning",
        "write",
    }
)
_RENDERING_CALLS = frozenset(
    {
        "render_config_value",
        "render_credential_value",
        "redact_secrets",
        "redact_url_userinfo",
        "_valid_config_keys",
        "len",
    }
)
_SAFE_NAMES = frozenset(
    {
        "HIGHLIGHT",
        "RESET",
        "category",
        "config_table",
        "indent",
        "key",
        "label",
        "name",
        "reg_name",
        "scanner",
        "scanner_name",
        "setting",
    }
)
_SAFE_EXPRESSIONS = frozenset(
    {
        "', '.join(SUPPORTED_SCANNERS)",
        "_DEPRECATED_CONFIG_KEYS[key]",
        "config_module.CONFIG_FILE",
    }
)


def _is_sink(func: ast.expr) -> bool:
    if isinstance(func, ast.Name):
        return func.id in _SINK_NAMES or func.id.startswith("_rich_")
    return isinstance(func, ast.Attribute) and func.attr in _SINK_ATTRS


def _is_rendered(node: ast.expr) -> bool:
    """Return whether *node* is a literal, a key name, or owner-rendered."""
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, ast.Name):
        return node.id in _SAFE_NAMES
    if isinstance(node, ast.JoinedStr):
        return all(_is_rendered(part) for part in node.values)
    if isinstance(node, ast.FormattedValue):
        return _is_rendered(node.value)
    if isinstance(node, ast.IfExp):
        return _is_rendered(node.body) and _is_rendered(node.orelse)
    if isinstance(node, ast.BinOp):
        return _is_rendered(node.left) and _is_rendered(node.right)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        if node.func.id in _RENDERING_CALLS:
            return True
    return ast.unparse(node) in _SAFE_EXPRESSIONS


def _raw_output_sinks(provider: FactsProvider, inventory: frozenset[str]) -> list[Violation]:
    findings: list[Violation] = []
    for path in _CONFIG_OUTPUT_PATHS:
        facts, failures = _load(provider, inventory, _RULE_ID, path, parse=True)
        if failures:
            findings.extend(failures)
            continue
        try:
            tree = ast.parse(source_text(facts))
        except SyntaxError as exc:
            findings.append(violation(_RULE_ID, path, f"unparsable source: {exc.msg}"))
            continue
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and _is_sink(node.func)):
                continue
            arguments = [*node.args, *(keyword.value for keyword in node.keywords)]
            raw = [arg for arg in arguments if not _is_rendered(arg)]
            if raw:
                findings.append(
                    violation(
                        _RULE_ID,
                        path,
                        "config output must render values through render_config_value "
                        f"(raw {ast.unparse(raw[0])!r} in {ast.unparse(node.func)})",
                        line=node.lineno,
                        column=node.col_offset + 1,
                    )
                )
    return findings


_DELEGATING_CONSUMERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        _CONFIG_COMMAND,
        (
            "from ..utils.credential_rendering import redact_secrets, render_config_value",
            'click.echo(f"{indent}{key}: {render_config_value(key, value)}")',
            "table.add_row(category, setting, render_config_value(setting, value))",
            "_echo_setting(key, (get_registry_config(reg_name) or {}).get(field))",
            '_echo_setting(f"registry.{reg_name}.token", body["token"], indent="  ")',
        ),
    ),
    (
        "src/apm_cli/core/auth.py",
        ("from apm_cli.utils.credential_rendering import redact_secrets as _redact_secrets",),
    ),
    (
        "src/apm_cli/core/auth_outcome.py",
        ("from ..utils.credential_rendering import redact_secrets",),
    ),
    (
        "src/apm_cli/core/script_executors.py",
        (
            "return mask_known_values(text, secrets)",
            "_redact_url_credentials = redact_url_userinfo",
        ),
    ),
    ("src/apm_cli/export/purl.py", ("return redact_url_userinfo(url, drop_query=True)",)),
    ("src/apm_cli/install/mcp/warnings.py", ("redact_url_userinfo(url)",)),
    ("src/apm_cli/deps/registry_proxy.py", ("redact_url_userinfo(url)!r",)),
    ("src/apm_cli/deps/registry/client.py", ("    url = redact_url_userinfo(url)",)),
    ("src/apm_cli/deps/registry/auth.py", ("{redact_url_userinfo(target_url)}",)),
    ("src/apm_cli/deps/registry/resolver.py", ("redact_url_userinfo(exc.url)",)),
    ("src/apm_cli/deps/registry/url_scheme.py", ("redact_url_userinfo(url)!r",)),
)


def _check_credential_value_rendering(provider: FactsProvider) -> tuple[Violation, ...]:
    inventory = frozenset(provider.inventory)
    findings: list[Violation] = []
    findings.extend(
        _count_checks(
            provider,
            inventory,
            _RULE_ID,
            _OWNER,
            _OWNER_DEFINITIONS,
            "Credential value rendering must have one canonical owner",
        )
    )
    findings.extend(
        _forbid_scan(
            provider,
            inventory,
            _RULE_ID,
            _src_python(provider, exclude={_OWNER}),
            _FORBIDDEN_MECHANISMS,
            "Credential redaction mechanisms live only in utils/credential_rendering.py; "
            "import them from the owner",
            exempt=False,
        )
    )
    findings.extend(_raw_output_sinks(provider, inventory))
    for path, needles in _DELEGATING_CONSUMERS:
        findings.extend(
            _require_subs(
                provider,
                inventory,
                _RULE_ID,
                path,
                needles,
                "Credential-bearing output must render through utils/credential_rendering.py",
            )
        )
    return tuple(findings)


RULES: tuple[Rule, ...] = (
    Rule(
        id=_RULE_ID,
        group=GROUP,
        guard_ids=(_RULE_ID,),
        description=(
            "Credential values reach command output only through "
            "utils/credential_rendering.py; no module regrows its own redaction."
        ),
        check=_check_credential_value_rendering,
    ),
)

COLLECTORS: tuple[object, ...] = ()

__all__ = ["COLLECTORS", "RULES"]
