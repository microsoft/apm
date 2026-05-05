"""Content-addressed cache for upstream ``marketplace.json`` manifests.

Where :mod:`apm_cli.marketplace.client` is the *consumer-side* cache
(TTL-keyed, browse-oriented, scoped to the curator's own
marketplaces.json), this module is the *curator-side* cache used by
:class:`UpstreamResolver` when building a marketplace whose packages
are sourced from an external marketplace.

Design contract
---------------

- **Content-addressed by manifest SHA.** Each cache entry is keyed by
  the immutable git commit SHA of the upstream repo at fetch time.
  Entries are never invalidated by TTL; once a SHA-pinned entry is
  written, all subsequent reads at the same SHA serve the same bytes.

- **Windows-safe key.** The on-disk cache key uses the ``__`` (double
  underscore) delimiter. Colons (``:``) are illegal in NTFS file
  names; using them silently breaks Windows curators who run
  ``apm marketplace upstream refresh`` (test-coverage panel item 5).

- **Hashed composite + delimiter rejection.** The composite key
  ``upstream__<host>__<owner>__<repo>__<sha>__<sanitised-path>`` is
  hashed (SHA-256, hex-truncated to 16 chars) so neither path length
  nor exotic characters in inputs can cause filesystem failures.
  All inputs are also rejected if they contain the ``__`` delimiter
  (defence-in-depth), and path-derived inputs route through
  :func:`apm_cli.utils.path_security.validate_path_segments`.

- **Per-upstream-host auth.** Tokens are resolved against the
  *upstream* host (``upstream.host``, ``upstream.owner``), never
  inherited from the curator's marketplace-source auth context. We
  pass ``unauth_first=True`` so public upstream repos do NOT have
  the curator's ``repo``-scoped PAT attached -- that PAT belongs to
  the curator's repos, not the upstream's.

- **Defence-in-depth integrity check.** Every cache hit re-verifies
  that the on-disk recorded SHA matches the SHA the caller requested.
  A poisoned cache file with a different SHA in its sidecar manifest
  is treated as a miss and re-fetched.

The resolver layer (:mod:`upstream_resolver`) is responsible for
turning refs/branches/tags into immutable SHAs and for the
canonical-``full_name`` repo-rename guard. This module assumes the
caller already pinned an explicit SHA.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from apm_cli.utils.path_security import (
    PathTraversalError,
    ensure_path_within,
    validate_path_segments,
)

logger = logging.getLogger(__name__)


__all__ = [
    "DEFAULT_UPSTREAM_CACHE_DIRNAME",
    "UpstreamCache",
    "UpstreamCacheError",
    "UpstreamCacheKey",
    "compute_cache_key",
]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_UPSTREAM_CACHE_DIRNAME = os.path.join("cache", "upstream")

# Cache-key delimiter -- ``__`` is Windows-safe (no colons), and we
# reject the delimiter in user-supplied inputs to keep the composite
# unambiguously parseable (defence-in-depth: even though the composite
# is hashed, we don't want forge-able collisions).
_KEY_DELIM = "__"

# Truncated SHA-256 length used in the on-disk cache directory name.
# 16 hex chars = 64 bits of entropy, enough to make collision-by-typo
# astronomically unlikely while keeping cache paths short on
# Windows where MAX_PATH still bites legacy tooling.
_KEY_HASH_LEN = 16

# 40-char hex git SHA. The cache is content-addressed; non-SHA refs
# (branches, tags) MUST be resolved upstream by the resolver layer
# before reaching this cache. Aliased to the shared canonical pattern.
from .ref_resolver import FULL_SHA_RE as _FULL_SHA_RE  # noqa: E402
from .ref_resolver import OWNER_REPO_RE as _REMOTE_SOURCE_RE  # noqa: E402

# Conservative host shape -- defence-in-depth on top of the regex in
# yml_schema.py. The cache layer never trusts that the caller already validated.
_HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.\-]{0,253}$")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class UpstreamCacheError(Exception):
    """Raised when an upstream cache operation cannot be completed safely.

    Distinct from :class:`MarketplaceFetchError` so callers can branch
    on cache-specific failures (poisoned entry, key validation,
    on-disk corruption) without catching network errors.
    """


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UpstreamCacheKey:
    """Validated, fully-resolved cache-key inputs.

    Constructed exclusively by :func:`compute_cache_key`. Once an
    instance exists, callers can rely on every field being:
    - non-empty
    - free of the ``__`` delimiter
    - free of path-traversal sequences (for ``path``)
    - shape-validated (host, owner/repo, full SHA)
    """

    host: str
    owner: str
    repo: str
    sha: str
    path: str

    @property
    def composite(self) -> str:
        """Plain-text composite -- for diagnostics, NOT for filesystem."""
        return _KEY_DELIM.join(["upstream", self.host, self.owner, self.repo, self.sha, self.path])

    @property
    def fingerprint(self) -> str:
        """Short SHA-256 fingerprint of the composite (filesystem-safe)."""
        digest = hashlib.sha256(self.composite.encode("utf-8")).hexdigest()
        return digest[:_KEY_HASH_LEN]

    @property
    def directory_name(self) -> str:
        """On-disk directory name for this cache entry.

        Format: ``upstream__<host>__<owner>__<repo>__<sha-prefix>__<hash>``

        Embeds a human-readable prefix so curators can grep ``~/.apm/cache``
        for an upstream they recognise, while the trailing hash makes the
        directory uniquely keyed.
        """
        sha_prefix = self.sha[:8]
        # Replace ``/`` in repo (only the slash between owner/repo would
        # appear) with ``-`` so the on-disk name stays a single segment.
        repo_safe = self.repo.replace("/", "-")
        owner_safe = self.owner
        return _KEY_DELIM.join(
            [
                "upstream",
                _sanitise_for_dirname(self.host),
                _sanitise_for_dirname(owner_safe),
                _sanitise_for_dirname(repo_safe),
                sha_prefix,
                self.fingerprint,
            ]
        )


# ---------------------------------------------------------------------------
# Public functions
# ---------------------------------------------------------------------------


def compute_cache_key(
    *,
    host: str,
    owner: str,
    repo: str,
    sha: str,
    path: str,
) -> UpstreamCacheKey:
    """Validate raw inputs and return an :class:`UpstreamCacheKey`.

    Raises
    ------
    UpstreamCacheError
        If any input is empty, contains the ``__`` delimiter, contains
        path-traversal sequences, or fails its shape regex.
    """
    _check_no_delimiter("host", host)
    _check_no_delimiter("owner", owner)
    _check_no_delimiter("repo", repo)
    _check_no_delimiter("sha", sha)
    _check_no_delimiter("path", path)

    if not _HOST_RE.match(host):
        raise UpstreamCacheError(f"invalid upstream host: {host!r}")

    repo_combined = f"{owner}/{repo}"
    if not _REMOTE_SOURCE_RE.match(repo_combined):
        raise UpstreamCacheError(
            f"invalid upstream owner/repo: {repo_combined!r} "
            f"(must match '<owner>/<repo>' shape, no leading dot)"
        )

    if not _FULL_SHA_RE.match(sha):
        raise UpstreamCacheError(
            f"invalid upstream sha: {sha!r}; cache keys require a full 40-char hex SHA"
        )

    if not path or path.startswith("/"):
        raise UpstreamCacheError(f"invalid upstream path: {path!r}; must be non-empty and relative")

    try:
        validate_path_segments(path, context="upstream cache path")
    except PathTraversalError as exc:
        raise UpstreamCacheError(f"invalid upstream path {path!r}: {type(exc).__name__}") from exc

    return UpstreamCacheKey(
        host=host,
        owner=owner,
        repo=repo,
        sha=sha,
        path=path,
    )


# ---------------------------------------------------------------------------
# UpstreamCache class
# ---------------------------------------------------------------------------


class UpstreamCache:
    """Filesystem-backed, SHA-keyed cache for upstream marketplace JSON.

    Two-file layout per entry inside ``<base>/<directory_name>/``::

        manifest.json  -- raw bytes the upstream returned (decoded JSON)
        meta.json      -- {"sha": "...", "host": "...", ...}

    The ``meta.json`` exists solely for the defence-in-depth integrity
    check on every read: a cached file whose meta SHA does not match
    the requested SHA is treated as a miss and silently re-fetched.

    The class is intentionally side-effect-light to make injection
    straightforward in tests:

    - ``base_dir`` is overridable at construction time.
    - ``fetch_callback`` is the single I/O boundary; the default
      implementation uses :class:`AuthResolver` per upstream host but
      tests can pass a stub.
    """

    def __init__(
        self,
        *,
        base_dir: Path | None = None,
        fetch_callback: Callable[[UpstreamCacheKey, Any], Any] | None = None,
    ) -> None:
        if base_dir is None:
            from apm_cli.config import CONFIG_DIR

            base_dir = Path(CONFIG_DIR) / DEFAULT_UPSTREAM_CACHE_DIRNAME
        self._base_dir = Path(base_dir)
        self._fetch_callback = fetch_callback or _default_fetch_via_github_api

    # -- Public API ---------------------------------------------------------

    @property
    def base_dir(self) -> Path:
        """Cache root directory (read-only)."""
        return self._base_dir

    def get(self, key: UpstreamCacheKey) -> dict[str, Any] | None:
        """Return cached JSON for *key* if present and integrity-valid."""
        entry_dir = self._entry_dir(key)
        manifest_path = entry_dir / "manifest.json"
        meta_path = entry_dir / "meta.json"
        if not manifest_path.exists() or not meta_path.exists():
            return None
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.debug(
                "Upstream cache meta unreadable for %s: %s", key.directory_name, type(exc).__name__
            )
            return None
        recorded_sha = meta.get("sha")
        if recorded_sha != key.sha:
            # Poisoned or stale entry: treat as miss. We do NOT delete
            # because deletion could mask a legitimate concurrent
            # writer; the next put() will overwrite.
            logger.warning(
                "Upstream cache integrity miss for %s: meta sha %r != requested sha %r",
                key.directory_name,
                recorded_sha,
                key.sha,
            )
            return None
        try:
            raw_bytes = manifest_path.read_bytes()
        except OSError as exc:
            logger.debug(
                "Upstream cache manifest unreadable for %s: %s",
                key.directory_name,
                type(exc).__name__,
            )
            return None
        recorded_content_sha256 = meta.get("content_sha256")
        if recorded_content_sha256 is not None:
            actual_sha256 = hashlib.sha256(raw_bytes).hexdigest()
            if actual_sha256 != recorded_content_sha256:
                logger.warning(
                    "Upstream cache content integrity miss for %s: expected %s, got %s",
                    key.directory_name,
                    recorded_content_sha256,
                    actual_sha256,
                )
                return None
        try:
            return json.loads(raw_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            logger.debug(
                "Upstream cache manifest parse error for %s: %s",
                key.directory_name,
                type(exc).__name__,
            )
            return None

    def put(self, key: UpstreamCacheKey, manifest: dict[str, Any]) -> None:
        """Write a fetched manifest into the cache atomically (best-effort)."""
        entry_dir = self._entry_dir(key)
        entry_dir.mkdir(parents=True, exist_ok=True)

        # Order: manifest first, then meta. ``get()`` reads meta last
        # via ``_load_meta()``, which short-circuits the cache hit when
        # meta is absent. If a crash interleaves these writes, an
        # incomplete entry is skipped (manifest without meta) rather
        # than served stale.
        manifest_path = entry_dir / "manifest.json"
        meta_path = entry_dir / "meta.json"

        manifest_bytes = json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8")
        content_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
        try:
            manifest_path.write_bytes(manifest_bytes)
            meta_path.write_text(
                json.dumps(
                    {
                        "content_sha256": content_sha256,
                        "host": key.host,
                        "owner": key.owner,
                        "path": key.path,
                        "repo": key.repo,
                        "sha": key.sha,
                    },
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
        except OSError as exc:
            raise UpstreamCacheError(
                f"failed to write upstream cache for {key.composite}: {type(exc).__name__}"
            ) from exc

    def get_or_fetch(
        self,
        key: UpstreamCacheKey,
        *,
        auth_resolver: Any = None,
        offline: bool = False,
    ) -> dict[str, Any]:
        """Return cached manifest, fetching on miss.

        Parameters
        ----------
        key
            Validated cache key.
        auth_resolver
            Optional :class:`apm_cli.core.auth.AuthResolver`. Threaded
            through to the fetch callback so tests can inject a mock.
        offline
            If *True*, raise :class:`UpstreamCacheError` on miss
            instead of attempting a network fetch. Wired to the
            curator-build ``--offline`` flag.

        Returns
        -------
        dict
            Decoded JSON of the upstream ``marketplace.json`` at the
            pinned SHA.
        """
        cached = self.get(key)
        if cached is not None:
            logger.debug("Upstream cache hit for %s", key.directory_name)
            return cached

        if offline:
            raise UpstreamCacheError(
                f"offline mode: no cached upstream entry for "
                f"{key.host}/{key.owner}/{key.repo}@{key.sha} (path={key.path}). "
                f"Run a build online first."
            )

        logger.debug("Upstream cache miss for %s", key.directory_name)
        manifest = self._fetch_callback(key, auth_resolver)
        if not isinstance(manifest, dict):
            raise UpstreamCacheError(
                f"upstream fetch returned non-JSON-object content for {key.composite}"
            )
        self.put(key, manifest)
        return manifest

    # -- Internal -----------------------------------------------------------

    def _entry_dir(self, key: UpstreamCacheKey) -> Path:
        """Resolve the on-disk directory for *key*, with containment guard."""
        candidate = self._base_dir / key.directory_name
        # Belt-and-braces: ensure the resolved directory is still
        # under the cache base, even if a future contributor changes
        # the directory_name format.
        try:
            ensure_path_within(candidate, self._base_dir)
        except PathTraversalError as exc:
            raise UpstreamCacheError(f"upstream cache directory escapes base: {exc}") from exc
        return candidate


# ---------------------------------------------------------------------------
# Default fetch callback (network)
# ---------------------------------------------------------------------------


def _default_fetch_via_github_api(
    key: UpstreamCacheKey,
    auth_resolver: Any,
) -> dict[str, Any]:
    """Fetch ``manifest.json`` from GitHub Contents API at ``key.sha``.

    Uses :class:`AuthResolver.try_with_fallback` with
    ``unauth_first=True``: the upstream is typically a public repo,
    and we MUST NOT attach the curator's PAT to a request that does
    not need it (supply-chain panel item 3 -- never leak curator
    credentials to upstream-host endpoints).

    The host classification refuses non-GitHub hosts in v1, mirroring
    the strict parser's host allow-list.
    """
    import requests

    from apm_cli.core.auth import AuthResolver

    if auth_resolver is None:
        auth_resolver = AuthResolver()

    host_info = AuthResolver.classify_host(key.host)
    if host_info.kind not in ("github", "ghe_cloud", "ghes"):
        raise UpstreamCacheError(
            f"upstream host {key.host!r} is not a supported GitHub variant; "
            f"refusing to fetch to avoid forwarding GitHub credentials elsewhere"
        )

    api_base = host_info.api_base
    url = f"{api_base}/repos/{key.owner}/{key.repo}/contents/{key.path}?ref={key.sha}"

    def _do_fetch(token: str | None, _git_env: dict[str, str]) -> dict[str, Any]:
        headers = {
            "Accept": "application/vnd.github.v3.raw",
            "User-Agent": "apm-cli (upstream-cache)",
        }
        if token and host_info.kind in ("github", "ghe_cloud", "ghes"):
            headers["Authorization"] = f"token {token}"
        resp = requests.get(url, headers=headers, timeout=30)
        if resp.status_code == 404:
            raise UpstreamCacheError(
                f"upstream marketplace.json not found: "
                f"{key.owner}/{key.repo}@{key.sha[:8]} {key.path}"
            )
        resp.raise_for_status()
        return resp.json()

    try:
        return auth_resolver.try_with_fallback(
            key.host,
            _do_fetch,
            org=key.owner,
            unauth_first=True,
        )
    except UpstreamCacheError:
        raise
    except requests.HTTPError as exc:
        # Drop the response object from the exception chain. ``exc.response``
        # carries the original ``request.headers`` dict which includes the
        # ``Authorization: token <PAT>`` header set in ``_do_fetch``. Logging
        # frameworks that walk ``__cause__`` could otherwise leak the
        # curator's PAT. Re-raise with status + URL only.
        status = exc.response.status_code if exc.response is not None else "?"
        raise UpstreamCacheError(
            f"failed to fetch upstream manifest for "
            f"{key.host}/{key.owner}/{key.repo}@{key.sha[:8]} {key.path}: "
            f"HTTP {status}"
        ) from None
    except Exception as exc:
        raise UpstreamCacheError(
            f"failed to fetch upstream manifest for "
            f"{key.host}/{key.owner}/{key.repo}@{key.sha[:8]} {key.path}: {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _check_no_delimiter(field: str, value: Any) -> None:
    if not isinstance(value, str) or not value:
        raise UpstreamCacheError(f"upstream cache {field} must be a non-empty string")
    if _KEY_DELIM in value:
        raise UpstreamCacheError(
            f"upstream cache {field} must not contain the cache delimiter '{_KEY_DELIM}': {value!r}"
        )


def _sanitise_for_dirname(value: str) -> str:
    """Strip / replace any character that could trip filesystem rules.

    The composite is already hashed so collision risk is negligible;
    this purely keeps the human-readable prefix visually clean and
    POSIX/Windows safe.
    """
    return re.sub(r"[^A-Za-z0-9._\-]", "-", value)
