"""Host-routing utilities for APM git download strategies.

Contains URL building, auth header construction, and response-payload
extraction helpers shared across GitHub, ADO, and generic git backends.
All names are private to the ``download_strategies`` package; the public
API surface lives in :mod:`git_strategy` which re-exports everything.
"""

import base64
import json
import os
from dataclasses import dataclass

import requests  # noqa: F401 – imported for type consistency with callers

from ...core.auth import HostInfo
from ...utils.github_host import is_github_hostname


def _is_configured_ghes(host: str) -> bool:
    """Return True when *host* matches the user's declared GHES via GITHUB_HOST.

    ``GITHUB_HOST=<custom-domain>`` is the documented opt-in for treating
    a non-``*.ghe.com`` FQDN as GitHub-family. Centralised so the routing
    check, header builder, and Contents-API URL builder cannot drift.
    """
    configured = os.environ.get("GITHUB_HOST", "").strip().lower()
    if not configured:
        return False
    return (host or "").lower() == configured


def _build_contents_api_urls(
    host: str,
    owner: str,
    repo: str,
    file_path: str,
    ref: str,
) -> list[str]:
    """Return the ordered list of Contents-API URL candidates for *host*.

    Thin wrapper around the per-host backends -- the actual URL shape
    lives on the backend. Kept as a static method on
    :class:`DownloadDelegate` for back-compat with existing callers
    and tests that monkey-patch it.
    """
    from ..host_backends import GenericGitBackend, GHECloudBackend, GHESBackend, GitHubBackend

    is_github_host = is_github_hostname(host) or _is_configured_ghes(host)

    host_lower = (host or "").lower()
    if not is_github_host:
        backend = GenericGitBackend(
            host_info=HostInfo(
                host=host,
                kind="generic",
                has_public_repos=False,
                api_base=f"https://{host}",
            )
        )
    elif host_lower == "github.com":
        backend = GitHubBackend(
            host_info=HostInfo(
                host=host,
                kind="github",
                has_public_repos=True,
                api_base="https://api.github.com",
            )
        )
    elif host_lower.endswith(".ghe.com"):
        backend = GHECloudBackend(
            host_info=HostInfo(
                host=host,
                kind="ghe_cloud",
                has_public_repos=False,
                api_base=f"https://{host}/api/v3",
            )
        )
    else:
        # Configured GHES (GITHUB_HOST=<custom-host>): api_base is
        # ``https://{host}/api/v3``, not ``https://api.{host}``.
        backend = GHESBackend(
            host_info=HostInfo(
                host=host,
                kind="ghes",
                has_public_repos=False,
                api_base=f"https://{host}/api/v3",
            )
        )
    return backend.build_contents_api_urls(owner, repo, file_path, ref)


def _build_generic_host_auth_headers(
    host: str, auth_ctx, *, accept: str | None = None
) -> dict[str, str]:
    """Build HTTP headers for a generic-host (non-GitHub) request.

    SECURITY GUARD: Only attach Authorization when the token is
    unambiguously intended for this host. A token resolved from a
    global env var (GITHUB_APM_PAT, GITHUB_TOKEN, GH_TOKEN) MUST NOT
    be sent to an arbitrary non-GitHub host -- doing so leaks the
    user's GitHub PAT to whatever FQDN is in the dependency line.
    The clone path at ``get_clone_url`` already enforces the same
    guard via ``is_github_hostname``; this mirrors it for HTTP file
    downloads.

    Forwarding is allowed when:
    - source == ``git-credential-fill``: git's credential helper
      looks tokens up by host, so they are host-scoped by
      construction.
    - source == ``GITHUB_APM_PAT_<ORG>``: per-org env var is
      explicit user opt-in for that org's host.
    - the user opted into this host as their GitHub Enterprise
      Server via ``GITHUB_HOST=<host>``: the token is intended for
      this host, even if the FQDN is not under ``*.ghe.com``.
    """
    headers: dict[str, str] = {}
    if accept:
        headers["Accept"] = accept
    if auth_ctx is None or not getattr(auth_ctx, "token", None):
        return headers
    source = getattr(auth_ctx, "source", None) or ""
    host_scoped = source == "git-credential-fill"
    org_scoped = source.startswith("GITHUB_APM_PAT_")
    configured_ghes = _is_configured_ghes(host)  # direct call; avoids circular import
    if host_scoped or org_scoped or configured_ghes:
        headers["Authorization"] = f"token {auth_ctx.token}"
    return headers


def _decode_json_envelope(body: bytes) -> bytes:
    """Decode a JSON envelope response (Gitea/Gogs) into raw file bytes.

    Handles the ``{"content": "<base64>", "encoding": "base64", ...}``
    shape emitted by generic git hosts when the raw-media accept header is
    ignored.  Falls back to the original body bytes for any unrecognised or
    partially-formed envelope.
    """
    try:
        payload = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, AttributeError):
        return body
    if not isinstance(payload, dict) or "content" not in payload:
        return body
    encoding = (payload.get("encoding") or "").lower()
    content_field = payload.get("content") or ""
    if encoding == "base64":
        try:
            return base64.b64decode(content_field, validate=False)
        except (ValueError, TypeError):
            return body
    # Non-base64 envelope (rare): return literal content if it's a string,
    # otherwise fall back to the raw body.
    if isinstance(content_field, str):
        return content_field.encode("utf-8")
    return body


def _extract_contents_api_payload(response, is_github_host: bool) -> bytes:
    """Decode a Contents-API response into raw file bytes.

    - GitHub family: ``Accept: application/vnd.github.v3.raw`` returns
      the file bytes directly; pass through ``response.content``.
    - Generic hosts (Gitea, Gogs): the raw-media accept header is
      ignored and the server returns a JSON envelope of the form::

          {"content": "<base64>", "encoding": "base64", ...}

      Decode ``content`` as base64 and return the resulting bytes.
      Some Gitea installations also emit ``encoding: ""`` with raw
      content -- pass that through unchanged. If the response is not
      a JSON envelope at all (custom proxy, raw bytes), fall back to
      ``response.content``.
    """
    if is_github_host:
        return response.content

    body = response.content
    try:
        ctype = str((response.headers or {}).get("Content-Type") or "").lower()
    except (AttributeError, TypeError):
        ctype = ""
    if "json" not in ctype and not (
        isinstance(body, (bytes, bytearray)) and body.lstrip().startswith(b"{")
    ):
        return body
    return _decode_json_envelope(body)


@dataclass(frozen=True, slots=True)
class _MissingFileCtx:
    host: str
    repo_url: str
    file_path: str
    ref: str
    api_url_candidates: list[str]
    is_github_host: bool
    fallback_ref: str | None = None


def _build_unsupported_or_missing_error(ctx: _MissingFileCtx) -> str:
    """Build a discoverable error when no Contents-API candidate hits 200."""
    ref_part = (
        f"(tried refs: {ctx.ref}, {ctx.fallback_ref})"
        if ctx.fallback_ref
        else f"at ref '{ctx.ref}'"
    )
    if ctx.is_github_host:
        return f"File not found: {ctx.file_path} in {ctx.repo_url} {ref_part}"
    # Non-GitHub host: name what was tried so users can diagnose
    # GitLab / unsupported-host cases without re-reading source.
    tried = ", ".join(["raw"] + [u.split("/api/")[1].split("/")[0] for u in ctx.api_url_candidates])
    canonical_url = f"https://{ctx.host}/{ctx.repo_url}/raw/{ctx.ref}/{ctx.file_path}"
    return (
        f"File not found on generic host {ctx.host}: {canonical_url} {ref_part}. "
        f"Tried URL families: {tried}. "
        "If this is GitLab, virtual subdirectory packages are not "
        "supported (use the dict-form full repo URL instead)."
    )
