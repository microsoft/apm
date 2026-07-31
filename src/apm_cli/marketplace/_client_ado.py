"""Azure DevOps REST items-API helpers for the marketplace client.

Contains the auth header builder and REST fetch helper used by ``_fetch_ado``
in the main ``client`` module. Extracted here to keep ``client.py`` under the
800-line budget.

RULE B: ``_http_get`` and ``_read_capped_json`` are accessed via ``_client``
(the facade module reference) so that tests that monkeypatch
``apm_cli.marketplace.client._http_get`` continue to work without change.
"""

from __future__ import annotations

import base64
import logging
from typing import TYPE_CHECKING

# Circular-import safe: ``client.py`` is already being loaded when this module
# is imported, so ``_client`` refers to the partially-initialized facade.
# Attribute lookups happen only at call time, when both modules are complete.
from . import client as _client  # facade reference for RULE B

if TYPE_CHECKING:
    from .models import MarketplaceSource

logger = logging.getLogger(__name__)


class _AdoItemNotFound(Exception):
    """Sentinel: the ADO items API returned a confirmed 404 for the path.

    Distinguishes "the file is definitively absent at this ref" (map to
    ``None`` so ``_auto_detect_path`` can probe the next candidate) from a
    transport/auth failure (fall back to the generic-git clone path).
    """


def _ado_auth_header(token: str | None, git_env: dict | None) -> dict[str, str]:
    """Build the Azure DevOps ``Authorization`` header for a resolved token.

    ``AuthResolver.try_with_fallback`` hands the operation a ``(token, git_env)``
    pair but not the auth scheme. Bearer contexts carry the full
    ``Authorization: Bearer <jwt>`` header in a ``GIT_CONFIG_VALUE_<i>`` slot
    (see ``AuthResolver._build_git_env``); detect that and emit the Bearer
    scheme. The slot index is not fixed -- since #2368 the header is appended
    after any retained non-auth entries -- so the whole indexed set is scanned.

    Only indices below ``GIT_CONFIG_COUNT`` are read, because that is all git
    itself reads. A slot above the count is either a leftover from an env this
    process did not build or an injected one, and honouring it would send an
    ADO PAT under the Bearer scheme, which the server rejects.

    Otherwise treat the token as an ADO PAT and use HTTP Basic with
    ``base64(":" + PAT)`` per ADO's convention (empty username, PAT as
    password). Returns an empty dict for an anonymous request.

    The returned dict carries the credential -- callers MUST NOT log it.
    """
    if not token:
        return {}
    env = git_env or {}
    try:
        count = max(0, int(env.get("GIT_CONFIG_COUNT", "0") or "0"))
    except (TypeError, ValueError):
        count = 0
    for index in range(count):
        value = str(env.get(f"GIT_CONFIG_VALUE_{index}", ""))
        if value.strip().lower().startswith("authorization: bearer "):
            return {"Authorization": f"Bearer {token}"}
    encoded = base64.b64encode(f":{token}".encode()).decode("ascii")
    return {"Authorization": f"Basic {encoded}"}


def _fetch_ado_rest(
    source: MarketplaceSource,
    file_path: str,
    *,
    org: str,
    project: str,
    repo: str,
    host: str,
    auth_resolver,
) -> dict | None:
    """Read a single metadata file from Azure DevOps via the REST items API.

    Routes auth through ``AuthResolver.try_with_fallback`` for the ADO host.
    Raises ``_AdoItemNotFound`` for a confirmed 404.
    """
    from ..utils.github_host import build_ado_api_url
    from .errors import MarketplaceFetchError

    url = build_ado_api_url(org, project, repo, file_path, source.ref, host)

    def _do_fetch(token, git_env):
        headers = {"User-Agent": "apm-cli"}
        headers.update(_ado_auth_header(token, git_env))
        resp = _client._http_get(url, headers=headers, timeout=30, stream=True)
        try:
            if resp.status_code == 404:
                raise _AdoItemNotFound
            if resp.status_code == 200:
                content_type = resp.headers.get("Content-Type", "").lower()
                if "text/html" in content_type:
                    raise MarketplaceFetchError(
                        source.name,
                        "Azure DevOps returned a sign-in page (unauthorized: authentication required)",
                    )
            resp.raise_for_status()
            return _client._read_capped_json(resp, source.name)
        finally:
            close = getattr(resp, "close", None)
            if callable(close):
                close()

    return auth_resolver.try_with_fallback(
        host,
        _do_fetch,
        org=org,
        path=f"{org}/{project}/{repo}",
        unauth_first=False,
    )
