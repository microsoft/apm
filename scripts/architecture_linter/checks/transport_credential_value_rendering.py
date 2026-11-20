"""Owner check for how credential values are rendered in command output.

Ports the ``credential-value-rendering`` owner decision: one module,
``utils/credential_rendering.py``, decides what a credential-bearing value
looks like when APM prints, logs or exports it.

* The owner defines the mask, the credential-key test, the config value
  renderer, the free-text pattern, URL userinfo stripping and known-value
  masking exactly once.
* No other product module regrows the secret pattern, a URL userinfo
  stripper or a ``[REDACTED]`` replacement loop (closed world).
* ``apm config`` prints every ``key: value`` line through one helper that
  renders the value through the owner.
* Known consumers (debug log filter, script log, SBOM export, proxy and
  registry diagnostics) delegate to the owner.
"""

from __future__ import annotations

import re

from scripts.architecture_linter.checks.transport_platform_shared import (
    GROUP,
    _count_checks,
    _forbid_scan,
    _require_subs,
    _src_python,
)
from scripts.architecture_linter.facts import FactsProvider
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

# A ``label: {value}`` echo in apm config bypasses the value renderer.
_RAW_CONFIG_ECHO = re.compile(r"click\.echo\(\s*f[\"'][^\"'\n]*:\s+\{(?!render_config_value\()")

_DELEGATING_CONSUMERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        _CONFIG_COMMAND,
        (
            "from ..utils.credential_rendering import render_config_value",
            'click.echo(f"{indent}{key}: {render_config_value(key, value)}")',
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
    ("src/apm_cli/models/apm_package.py", ("redact_url_userinfo(url)!r",)),
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
    findings.extend(
        _forbid_scan(
            provider,
            inventory,
            _RULE_ID,
            (_CONFIG_COMMAND,),
            _RAW_CONFIG_ECHO,
            "apm config prints values only through _echo_setting (render_config_value)",
            exempt=False,
        )
    )
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
