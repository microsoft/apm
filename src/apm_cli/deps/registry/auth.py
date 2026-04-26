"""Registry token resolution.

Per docs/proposals/registry-api.md §7.1: a single env-var convention,
``APM_REGISTRY_TOKEN_{NAME}`` where ``{NAME}`` is the uppercased registry name
with hyphens replaced by underscores. No precedence chain (registries are
always declared by name in apm.yml — there's nothing to discover).

URL-based lookup (§6.2) is also implemented here: a user who clones a project
whose lockfile references a registry URL they've never configured needs to
install. The chain is::

    resolved_url  ─→  registry name (apm.yml registries: block)  ─→  env-var token
"""

from __future__ import annotations

import os
import urllib.parse
from dataclasses import dataclass
from typing import Dict, Optional


@dataclass(frozen=True)
class RegistryAuthContext:
    """Auth payload for a single registry HTTP call.

    Empty ``token`` is legitimate — anonymous fetch is the first attempt when
    no env var is set (§6.2 rule 2). The client tries the request anonymously
    and only surfaces the remediation message if the server replies 401/403.
    """

    registry_name: Optional[str]
    token: Optional[str]

    def auth_header(self) -> Optional[str]:
        """Return the ``Authorization`` header value, or ``None`` when anonymous."""
        if not self.token:
            return None
        return f"Bearer {self.token}"


def _env_key(registry_name: str) -> str:
    """Translate a registry name into the env-var key per §7.1.

    ``corp-main``  -> ``APM_REGISTRY_TOKEN_CORP_MAIN``
    ``corp.main``  -> ``APM_REGISTRY_TOKEN_CORP_MAIN``
    """
    sanitized = registry_name.upper().replace("-", "_").replace(".", "_")
    return f"APM_REGISTRY_TOKEN_{sanitized}"


def resolve_registry_token(registry_name: str) -> Optional[str]:
    """Look up the env-var token for *registry_name*. ``None`` means anonymous."""
    return os.environ.get(_env_key(registry_name))


def _normalize_url_prefix(url: str) -> str:
    """Normalize a URL for prefix matching.

    Strips trailing slashes; lowercases the scheme + host. Path segments stay
    case-sensitive (registries running on case-sensitive filesystems may treat
    ``/Foo`` and ``/foo`` distinctly).
    """
    parsed = urllib.parse.urlparse(url)
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()
    port = f":{parsed.port}" if parsed.port else ""
    path = parsed.path.rstrip("/")
    return f"{scheme}://{host}{port}{path}"


def lookup_name_for_url(
    target_url: str, registries: Dict[str, str]
) -> Optional[str]:
    """Find which configured registry owns *target_url* by URL prefix.

    *registries* is the ``name -> url`` mapping from apm.yml's ``registries:``
    block (or merged with user config). Returns the longest-prefix-matching
    name, or ``None`` if no registered URL is a prefix of *target_url*.

    The longest-prefix rule is what lets a user safely register both
    ``https://corp/apm`` and ``https://corp/apm/team-a`` without one shadowing
    the other.
    """
    if not target_url or not registries:
        return None
    normalized_target = _normalize_url_prefix(target_url)
    best_name: Optional[str] = None
    best_len = -1
    for name, url in registries.items():
        if not isinstance(url, str) or not url:
            continue
        prefix = _normalize_url_prefix(url)
        if normalized_target == prefix or normalized_target.startswith(prefix + "/"):
            if len(prefix) > best_len:
                best_name = name
                best_len = len(prefix)
    return best_name


def resolve_for_url(
    target_url: str, registries: Dict[str, str]
) -> RegistryAuthContext:
    """End-to-end auth resolution for a lockfile-recorded URL.

    Looks up which registered name owns *target_url* and reads the env-var
    token for that name. If no registered URL matches, returns an anonymous
    context — the caller will try anonymous fetch and surface the §6.2
    remediation message on 401/403.
    """
    name = lookup_name_for_url(target_url, registries)
    if name is None:
        return RegistryAuthContext(registry_name=None, token=None)
    return RegistryAuthContext(
        registry_name=name, token=resolve_registry_token(name)
    )


def remediation_message(target_url: str) -> str:
    """The standard 401/403 remediation per §6.2 rule 3."""
    return (
        f"error: this project depends on a package from\n"
        f"  {target_url}\n"
        f"but no credentials for that registry are configured on this machine.\n"
        f"Add a registry entry whose URL matches (in apm.yml or ~/.apm/config.yml)\n"
        f"and set APM_REGISTRY_TOKEN_<NAME>=<token> in your environment."
    )
