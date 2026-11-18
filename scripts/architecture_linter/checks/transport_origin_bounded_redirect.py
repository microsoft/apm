"""Redirect follow decisions stay in the origin-bounded HTTP owner.

Ports the redirect half of the ``credential-origin-scope`` decision in
``.apm/architecture/owners/transport-auth-platform.json``:
``utils/http_credentials.py::origin_bounded_request`` alone decides whether a
redirect is followed, keeps credentials on in-bound hops, and keeps ambient
``.netrc`` credentials from replacing the chosen one.

* Only the owner (and a frozen, reasoned inventory of call sites that refuse
  every redirect or carry no APM credential) may pass ``allow_redirects=`` or
  tune ``max_redirects``.
* A module that builds a credential header must not send it through
  ``urllib`` ``urlopen``, which copies every header to any redirect target.
* The registry client, marketplace client and marketplace builder route their
  requests through the owner.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from scripts.architecture_linter.checks.transport_platform_shared import (
    GROUP,
    _load,
    _require_subs,
    _src_python,
)
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.groups.common import violation
from scripts.architecture_linter.models import Rule, Violation

_RULE_ID = "transport-platform-origin-bounded-redirect"
_OWNER = "src/apm_cli/utils/http_credentials.py"
_REGISTRY_CLIENT = "src/apm_cli/deps/registry/client.py"
_MARKETPLACE_CLIENT = "src/apm_cli/marketplace/client.py"
_MARKETPLACE_BUILDER = "src/apm_cli/marketplace/builder.py"

_CODE_LINE = r"^(?!\s*#)"
_ALLOW_REDIRECTS = re.compile(_CODE_LINE + r".*\ballow_redirects\s*=")
_MAX_REDIRECTS = re.compile(_CODE_LINE + r".*\.max_redirects\s*=")
_URLOPEN = re.compile(_CODE_LINE + r".*\burlopen\(")
_CREDENTIAL_LITERAL = re.compile(
    _CODE_LINE + r".*[\"'](?:Authorization|PRIVATE-TOKEN|Proxy-Authorization)[\"']"
)
_CROSS_ORIGIN_FOLLOW = re.compile(_CODE_LINE + r".*follow_cross_origin_without_credentials=True")

# Frozen inventory: each site either refuses every redirect or carries no APM
# credential. Counts are exact so a new site (or a migrated one) must update
# this table on purpose.
_ALLOW_REDIRECTS_EXEMPT: Mapping[str, tuple[int, str]] = {
    "src/apm_cli/commands/auth.py": (
        1,
        "auth status probe refuses every redirect with trust_env disabled",
    ),
    "src/apm_cli/core/script_executors.py": (
        1,
        "lifecycle HTTP dispatch validates each hop against SSRF; no APM credential",
    ),
    "src/apm_cli/deps/download_strategies.py": (
        1,
        "ADO file download refuses every redirect; the auth-outcome owner classifies sign-in 3xx",
    ),
    "src/apm_cli/install/artifactory_resolver.py": (
        1,
        "registry boundary HEAD probe refuses every redirect; migrates in the guard PR",
    ),
    "src/apm_cli/policy/_gitlab.py": (
        1,
        "policy fetch refuses every redirect; auth-outcome owner, migrates in the guard PR",
    ),
    "src/apm_cli/policy/discovery.py": (
        3,
        "policy fetches refuse every redirect; auth-outcome owner, migrates in the guard PR",
    ),
    "src/apm_cli/utils/version_checker.py": (
        1,
        "anonymous release-metadata discovery owned by release-metadata-discovery",
    ),
}
_MAX_REDIRECTS_EXEMPT: Mapping[str, tuple[int, str]] = {
    "src/apm_cli/utils/archive.py": (1, "anonymous archive download; no APM credential"),
}
_CROSS_ORIGIN_FOLLOW_ALLOWED = frozenset({_MARKETPLACE_CLIENT})

_OWNER_CONTRACT: tuple[str, ...] = (
    '"Proxy-Authorization",',
    "MAX_REDIRECT_HOPS = 3",
    "def origin_bounded_request(",
    "allow_redirects=False,",
    'kwargs["auth"] = _NO_AMBIENT_CREDENTIALS',
    "reason = origin_bound_refusal(target, base_url=base_url)",
    "def _remove_dot_segments(",
    "_comparable_host(parts.hostname)",
)

_CONSUMERS: tuple[tuple[str, tuple[str, ...], tuple[str, ...], str], ...] = (
    (
        _REGISTRY_CLIENT,
        ("return origin_bounded_request(", "session or CredentialScopedSession()"),
        ("self._session.request(", "requests.Session()"),
        "Registry requests must follow redirects only through origin_bounded_request",
    ),
    (
        _MARKETPLACE_CLIENT,
        ("response = origin_bounded_request(",),
        ("_HTTP_SESSION.get(", "_HTTP_SESSION.request("),
        "Marketplace HTTP fetches must follow redirects only through origin_bounded_request",
    ),
    (
        _MARKETPLACE_BUILDER,
        ("resp = origin_bounded_request(",),
        ("urllib.request", "add_header("),
        "Marketplace metadata fetches must use the shared credential-scoped HTTP owner",
    ),
)


def _matching_lines(lines: tuple[str, ...], pattern: re.Pattern[str]) -> list[int]:
    return [number for number, line in enumerate(lines, start=1) if pattern.search(line)]


def _scan_inventory(
    provider: FactsProvider,
    paths: tuple[str, ...],
    pattern: re.Pattern[str],
    exempt: Mapping[str, tuple[int, str]],
    message: str,
) -> list[Violation]:
    findings: list[Violation] = []
    seen: set[str] = set()
    for path in paths:
        lines, error = provider.lexical_lines(path)
        if error is not None:
            findings.append(violation(_RULE_ID, path, f"required source unavailable: {error}"))
            continue
        hits = _matching_lines(lines, pattern)
        if not hits:
            continue
        seen.add(path)
        expected = exempt.get(path)
        if expected is None:
            findings.extend(violation(_RULE_ID, path, message, line=line) for line in hits)
        elif len(hits) != expected[0]:
            findings.append(
                violation(
                    _RULE_ID,
                    path,
                    f"frozen redirect inventory expects {expected[0]} site(s), found "
                    f"{len(hits)}; route new sites through origin_bounded_request",
                    line=hits[0],
                )
            )
    for path in sorted(set(exempt) - seen):
        findings.append(
            violation(
                _RULE_ID,
                path,
                "stale redirect exemption: the site is gone; remove it from the frozen inventory",
            )
        )
    return findings


def _check_origin_bounded_redirect(provider: FactsProvider) -> tuple[Violation, ...]:
    """Keep the redirect follow decision in utils/http_credentials.py."""
    inventory = frozenset(provider.inventory)
    findings: list[Violation] = []
    findings.extend(
        _require_subs(
            provider,
            inventory,
            _RULE_ID,
            _OWNER,
            _OWNER_CONTRACT,
            "http_credentials.py must own the origin-bounded redirect follow decision",
        )
    )
    paths = _src_python(provider, exclude={_OWNER})
    findings.extend(
        _scan_inventory(
            provider,
            paths,
            _ALLOW_REDIRECTS,
            _ALLOW_REDIRECTS_EXEMPT,
            "redirect follow decision outside utils/http_credentials.py; "
            "use origin_bounded_request",
        )
    )
    findings.extend(
        _scan_inventory(
            provider,
            paths,
            _MAX_REDIRECTS,
            _MAX_REDIRECTS_EXEMPT,
            "session redirect limit outside utils/http_credentials.py; use origin_bounded_request",
        )
    )
    for path in paths:
        lines, error = provider.lexical_lines(path)
        if error is not None:
            continue
        urlopen_lines = _matching_lines(lines, _URLOPEN)
        if urlopen_lines and _matching_lines(lines, _CREDENTIAL_LITERAL):
            findings.append(
                violation(
                    _RULE_ID,
                    path,
                    "urllib urlopen copies credential headers to any redirect target; "
                    "use origin_bounded_request",
                    line=urlopen_lines[0],
                )
            )
        if path not in _CROSS_ORIGIN_FOLLOW_ALLOWED:
            findings.extend(
                violation(
                    _RULE_ID,
                    path,
                    "anonymous cross-origin redirect follow is reserved for the marketplace client",
                    line=line,
                )
                for line in _matching_lines(lines, _CROSS_ORIGIN_FOLLOW)
            )
    for path, required, forbidden, message in _CONSUMERS:
        facts, failures = _load(provider, inventory, _RULE_ID, path)
        findings.extend(failures)
        if facts is None:
            continue
        findings.extend(_require_subs(provider, inventory, _RULE_ID, path, required, message))
        for number, line in enumerate(facts.lines, start=1):
            if line.lstrip().startswith("#"):
                continue
            for needle in forbidden:
                if needle in line:
                    findings.append(violation(_RULE_ID, path, message, line=number))
    return tuple(findings)


RULES = (
    Rule(
        id=_RULE_ID,
        group=GROUP,
        guard_ids=(_RULE_ID,),
        description="Redirects are followed only inside the configured origin by one owner.",
        check=_check_origin_bounded_redirect,
    ),
)
COLLECTORS: tuple[object, ...] = ()
