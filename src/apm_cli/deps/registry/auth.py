"""Registry credential resolution.

Per docs/proposals/registry-api.md §7.1, the primary convention is
``APM_REGISTRY_TOKEN_{NAME}`` (HTTP Bearer). A second convention covers
HTTP Basic auth — required by enterprise registries like JFrog Artifactory
that don't expose Bearer-token issuance via Basic-auth themselves:

    APM_REGISTRY_TOKEN_{NAME}  -> Authorization: Bearer <token>
    APM_REGISTRY_USER_{NAME}   ┐
    APM_REGISTRY_PASS_{NAME}   ┴ -> Authorization: Basic <base64(user:pass)>

If both are set, ``TOKEN_*`` wins. ``{NAME}`` is the uppercased registry
name with ``-`` and ``.`` mapped to ``_``.

URL-based lookup (§6.2) is also implemented here: a user who clones a project
whose lockfile references a registry URL they've never configured needs to
install. The chain is::

    resolved_url ─→ registry name (apm.yml registries: block) ─→ env-var creds
"""

from __future__ import annotations

import base64
import os
import urllib.parse
from dataclasses import dataclass, replace
from typing import Any

from ...models.dependency.reference import DependencyReference


@dataclass(frozen=True)
class RegistryAuthContext:
    """Auth payload for a single registry HTTP call.

    All-empty fields mean "anonymous" — the first attempt when no env vars
    are set (§6.2 rule 2). The client tries anonymously and only surfaces
    the remediation message on 401/403.

    Bearer beats Basic: when both ``token`` and ``username``/``password``
    are populated, the ``Authorization`` header is the Bearer form.
    """

    registry_name: str | None
    token: str | None
    # HTTP Basic auth (alternative to Bearer; required for some enterprise
    # registries — JFrog Artifactory's /access endpoints, for example).
    username: str | None = None
    password: str | None = None

    def auth_header(self) -> str | None:
        """Return the ``Authorization`` header value, or ``None`` when anonymous."""
        if self.token:
            return f"Bearer {self.token}"
        if self.username is not None and self.password is not None:
            credentials = f"{self.username}:{self.password}".encode()
            return f"Basic {base64.b64encode(credentials).decode('ascii')}"
        return None


def _sanitized(registry_name: str) -> str:
    return registry_name.upper().replace("-", "_").replace(".", "_")


def registry_token_env_var(registry_name: str) -> str:
    """Return the ``APM_REGISTRY_TOKEN_*`` env var name for *registry_name*.

    ``corp-main`` -> ``APM_REGISTRY_TOKEN_CORP_MAIN``
    ``corp.main`` -> ``APM_REGISTRY_TOKEN_CORP_MAIN``
    """
    return f"APM_REGISTRY_TOKEN_{_sanitized(registry_name)}"


def _env_key(registry_name: str) -> str:
    """Bearer-token env-var key per §7.1."""
    return registry_token_env_var(registry_name)


def _env_key_user(registry_name: str) -> str:
    """HTTP Basic auth username env-var key.

    ``corp-main`` -> ``APM_REGISTRY_USER_CORP_MAIN``
    """
    return f"APM_REGISTRY_USER_{_sanitized(registry_name)}"


def _env_key_pass(registry_name: str) -> str:
    """HTTP Basic auth password env-var key.

    ``corp-main`` -> ``APM_REGISTRY_PASS_CORP_MAIN``
    """
    return f"APM_REGISTRY_PASS_{_sanitized(registry_name)}"


def resolve_registry_token(registry_name: str) -> str | None:
    """Look up the Bearer token for *registry_name*: env var first, then config.json."""
    token = os.environ.get(_env_key(registry_name))
    if token:
        return token
    from ...config import get_registry_config

    cfg = get_registry_config(registry_name)
    if cfg and isinstance(cfg.get("token"), str) and cfg["token"]:
        return cfg["token"]
    return None


def resolve_registry_basic(
    registry_name: str,
) -> tuple[str | None, str | None]:
    """Look up the (username, password) pair for HTTP Basic auth.

    Returns ``(None, None)`` when either is missing — the caller treats this
    as "no Basic auth available" and falls back to whatever else applies.
    """
    user = os.environ.get(_env_key_user(registry_name))
    pwd = os.environ.get(_env_key_pass(registry_name))
    if user is None or pwd is None:
        return None, None
    return user, pwd


def make_auth_context(registry_name: str) -> RegistryAuthContext:
    """Build a ``RegistryAuthContext`` from env vars for *registry_name*.

    Reads both Bearer and Basic env vars; both can populate the context but
    Bearer wins at the header-rendering level (see ``auth_header``).
    """
    token = resolve_registry_token(registry_name)
    user, pwd = resolve_registry_basic(registry_name)
    return RegistryAuthContext(
        registry_name=registry_name,
        token=token,
        username=user,
        password=pwd,
    )


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


def lookup_name_for_url(target_url: str, registries: dict[str, str]) -> str | None:
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
    best_name: str | None = None
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


def dependency_ref_with_registry_name_from_lockfile(
    dep_ref: DependencyReference,
    registries_map: dict[str, str],
    *,
    locked_dep: Any = None,
    existing_lockfile: Any = None,
) -> DependencyReference:
    """Set ``registry_name`` from a lockfile URL when missing (clone / reinstall).

    ``locked_dep`` wins when provided (fresh-install path); otherwise the row
    is loaded from ``existing_lockfile`` (resolve-phase callback path).
    """
    if dep_ref.registry_name is not None or not registries_map:
        return dep_ref
    if getattr(dep_ref, "source", None) != "registry":
        return dep_ref

    locked = locked_dep
    if locked is None and existing_lockfile is not None:
        locked = existing_lockfile.get_dependency(dep_ref.get_unique_key())

    resolved_url = getattr(locked, "resolved_url", None) if locked else None
    if not resolved_url:
        return dep_ref

    name = lookup_name_for_url(resolved_url, registries_map)
    if not name:
        return dep_ref
    return replace(dep_ref, registry_name=name)


def resolve_for_url(target_url: str, registries: dict[str, str]) -> RegistryAuthContext:
    """End-to-end auth resolution for a lockfile-recorded URL.

    Looks up which registered name owns *target_url* and reads its env-var
    credentials (Bearer + Basic) for that name. If no registered URL
    matches, returns an anonymous context — the caller will try anonymous
    fetch and surface the §6.2 remediation message on 401/403.
    """
    name = lookup_name_for_url(target_url, registries)
    if name is None:
        return RegistryAuthContext(registry_name=None, token=None)
    return make_auth_context(name)


def remediation_message(target_url: str) -> str:
    """The standard 401/403 remediation per §6.2 rule 3."""
    return (
        f"error: this project depends on a package from\n"
        f"  {target_url}\n"
        f"but no credentials for that registry are configured on this machine.\n"
        f"Add a registry entry whose URL matches (in apm.yml or ~/.apm/config.json)\n"
        f"and set APM_REGISTRY_TOKEN_<NAME>=<token> in your environment."
    )
