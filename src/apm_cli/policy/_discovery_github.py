"""GitHub Contents API helpers for policy discovery.

Extracted from discovery.py (#1078) to keep that file under the 800-line
budget.  Re-exported from discovery.py so ``apm_cli.policy.discovery.<name>``
monkeypatching targets remain byte-identical.

Rule B: ``requests.get`` and ``_get_token_for_host`` are patched by tests via
``apm_cli.policy.discovery.requests`` and
``apm_cli.policy.discovery._get_token_for_host``.  Functions that call those
use deferred ``from apm_cli.policy import discovery as _d`` and route through
``_d.requests.get(...)`` / ``_d._get_token_for_host(host)`` so patches apply.
Exception classes (``Timeout``, ``ConnectionError``) are imported directly so
``except`` clauses work regardless of whether ``_d.requests`` is a mock.
"""

from __future__ import annotations

import base64

from requests.exceptions import ConnectionError as _ConnError
from requests.exceptions import Timeout as _Timeout


def _parse_github_repo_ref(repo_ref: str) -> tuple[str, str, str] | None:
    """Parse repo_ref into (host, owner, repo_path), or None if invalid."""
    parts = repo_ref.split("/")
    if len(parts) == 2:
        return ("github.com", parts[0], parts[1])
    if len(parts) >= 3:
        return (parts[0], parts[1], "/".join(parts[2:]))
    return None


def _decode_github_content(data: dict, repo_ref: str) -> tuple[str | None, str | None]:
    """Decode GitHub API response body to (content_str, error_str)."""
    if data.get("encoding") == "base64" and data.get("content"):
        content = base64.b64decode(data["content"]).decode("utf-8")
        return content, None
    if data.get("content"):
        return data["content"], None
    return None, f"Unexpected response format from {repo_ref}"


def _call_github_api(
    api_url: str,
    headers: dict,
    repo_ref: str,
) -> tuple[str | None, str | None]:
    """Call GitHub Contents API and return (content_str, error_str).

    Rule B: uses ``_d.requests.get`` so test patches on
    ``apm_cli.policy.discovery.requests`` are honoured.
    """
    from apm_cli.policy import discovery as _d

    try:
        resp = _d.requests.get(api_url, headers=headers, timeout=10, allow_redirects=False)
    except _Timeout:
        return None, f"Timeout fetching policy from {repo_ref}"
    except _ConnError:
        return None, f"Connection error fetching policy from {repo_ref}"
    except Exception as e:
        return None, f"Error fetching policy from {repo_ref}: {e}"

    if resp.status_code == 404:
        return None, "404: Policy file not found"
    if resp.status_code == 403:
        return None, f"403: Access denied to {repo_ref}"
    if 300 <= resp.status_code < 400:
        location = resp.headers.get("Location", "<no Location header>")
        return None, (f"Refusing HTTP redirect ({resp.status_code}) from {api_url} to {location}")
    if resp.status_code != 200:
        return None, f"HTTP {resp.status_code} fetching policy from {repo_ref}"
    return _decode_github_content(resp.json(), repo_ref)


def _fetch_github_contents(
    repo_ref: str,
    file_path: str,
) -> tuple[str | None, str | None]:
    """Fetch file contents from GitHub API.

    Returns (content_string, error_string). One will be None.

    Rule B: uses ``_d._get_token_for_host`` so test patches on
    ``apm_cli.policy.discovery._get_token_for_host`` are honoured.
    """
    from apm_cli.policy import discovery as _d

    parsed = _parse_github_repo_ref(repo_ref)
    if parsed is None:
        return None, f"Invalid repo reference: {repo_ref}"

    host, owner, repo = parsed
    if host == "github.com":
        api_url = f"https://api.github.com/repos/{owner}/{repo}/contents/{file_path}"
    else:
        api_url = f"https://{host}/api/v3/repos/{owner}/{repo}/contents/{file_path}"

    headers = {"Accept": "application/vnd.github.v3+json"}
    token = _d._get_token_for_host(host)
    if token:
        headers["Authorization"] = f"token {token}"

    return _call_github_api(api_url, headers, repo_ref)
