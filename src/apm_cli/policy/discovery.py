"""Auto-discover and fetch org-level apm-policy.yml files.

Discovery flow:
1. Extract org from git remote (github.com/contoso/my-project -> "contoso")
2. Determine host profile (default or ado) to select candidate repos
3. Try candidate repos in precedence order (.github-private > .github > .apm > _apm)
4. Fetch apm-policy.yml via GitHub Contents API or ADO Items API
5. Resolve inheritance chain via resolve_policy_chain
6. Cache the **merged effective policy** with chain metadata
7. Parse and return ApmPolicy

Candidate repo precedence:
- .github-private -- private org-wide config (preferred; skipped on ADO)
- .github  -- GitHub convention (skipped on ADO)
- .apm     -- cross-platform convention (skipped on ADO)
- _apm     -- universal fallback (valid on every git host)

Supports:
- GitHub.com and GitHub Enterprise (*.ghe.com)
- Azure DevOps (dev.azure.com, *.visualstudio.com)
- Manual override via --policy <path|url>
- Cache with TTL (default 1 hour), stale fallback up to MAX_STALE_TTL
- Atomic cache writes (temp file + os.replace)
- Garbage-response detection (200 OK with non-YAML body)
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import requests
import yaml

from ..cache.url_normalize import SCP_LIKE_RE
from ..utils.github_host import (
    build_ado_api_url,
    is_azure_devops_hostname,
    is_visualstudio_legacy_hostname,
)
from ..utils.path_security import PathTraversalError, ensure_path_within
from ..utils.yaml_io import load_yaml_str
from .parser import PolicyValidationError, load_policy
from .project_config import (
    _DEFAULT_HASH_ALGORITHM,
    _HASH_HEX_LEN,
    _HEX_RE,
    ALLOWED_HASH_ALGORITHMS,
    ProjectPolicyConfigError,
    compute_policy_hash,
    read_project_policy_hash_pin,
)
from .schema import ApmPolicy

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Policy repo discovery: cascading candidate repos per host profile
# ---------------------------------------------------------------------------

# Candidate repo names in precedence order (first valid policy wins).
# Host profiles select which candidates are valid for a given git host.
_DEFAULT_POLICY_REPOS: tuple[str, ...] = (".github-private", ".github", ".apm", "_apm")
_ADO_POLICY_REPOS: tuple[str, ...] = ("_apm",)

# ADO project name for the policy repo (ADO requires a project container).
ADO_POLICY_PROJECT = "_apm"


def _policy_repo_candidates(host: str) -> tuple[str, ...]:
    """Return candidate policy repo names for *host* in precedence order.

    ADO hosts cannot have repo names starting/ending with ``.``, so only
    ``_apm`` is valid.  All other hosts try the full cascade.
    """
    if is_azure_devops_hostname(host):
        return _ADO_POLICY_REPOS
    return _DEFAULT_POLICY_REPOS


def _split_hash_pin(expected_hash: str) -> tuple[str, str]:
    """Split an ``"<algo>:<hex>"`` pin into (algorithm, lowercase_hex).

    Bare hex (no prefix) is interpreted as sha256 for backwards
    compatibility -- callers that care about the algorithm should pass a
    fully-qualified pin. Raises :class:`ProjectPolicyConfigError` on a
    structurally invalid pin (unsupported algorithm, wrong length, non
    hex). The discovery helpers translate that into a fail-closed
    ``hash_mismatch`` outcome rather than crashing.
    """
    raw = expected_hash.strip()
    if ":" in raw:
        algo, _, hex_part = raw.partition(":")
        algo = algo.strip().lower()
    else:
        algo = _DEFAULT_HASH_ALGORITHM
        hex_part = raw
    hex_part = hex_part.strip().lower()
    if algo not in ALLOWED_HASH_ALGORITHMS:
        raise ProjectPolicyConfigError(f"Unsupported policy.hash algorithm '{algo}'")
    expected_len = _HASH_HEX_LEN[algo]
    if len(hex_part) != expected_len or not _HEX_RE.match(hex_part):
        raise ProjectPolicyConfigError(f"policy.hash is not a valid {algo} digest")
    return algo, hex_part


def _compute_hash_normalized(content: str, expected_hash: str | None) -> str:
    """Compute the digest of *content* under the algorithm declared by
    *expected_hash*, returning the canonical ``"<algo>:<hex>"`` form.

    When *expected_hash* is ``None`` the default algorithm (sha256) is
    used so the cache always carries a digest for later pin verification.
    """
    algo = _DEFAULT_HASH_ALGORITHM
    if expected_hash:
        try:
            algo, _ = _split_hash_pin(expected_hash)
        except ProjectPolicyConfigError:
            algo = _DEFAULT_HASH_ALGORITHM
    digest = compute_policy_hash(content, algo)
    return f"{algo}:{digest}"


def _verify_hash_pin(
    content: object,
    expected_hash: str | None,
    source_label: str,
) -> PolicyFetchResult | None:
    """Verify fetched policy bytes against the project's pin (#827).

    Returns ``None`` when there is no pin, or the digest matches. On
    mismatch -- or on a structurally invalid pin, which is treated as a
    mismatch to stay fail-closed -- returns a :class:`PolicyFetchResult`
    with ``outcome="hash_mismatch"`` that callers must propagate. The
    hash is computed on the raw UTF-8 bytes that get parsed (matching
    ``yaml.safe_load`` semantics) so a malicious mirror cannot bypass the
    check by re-serializing semantically-equivalent YAML.
    """
    if expected_hash is None:
        return None

    raw_bytes: bytes
    if isinstance(content, bytes):
        raw_bytes = content
    elif isinstance(content, str):
        raw_bytes = content.encode("utf-8")
    else:
        return PolicyFetchResult(
            outcome="hash_mismatch",
            source=source_label,
            error=(
                f"Policy hash mismatch from {source_label}: "
                "no content available to verify against pin"
            ),
            expected_hash=expected_hash,
        )

    try:
        algo, expected_hex = _split_hash_pin(expected_hash)
    except ProjectPolicyConfigError as exc:
        return PolicyFetchResult(
            outcome="hash_mismatch",
            source=source_label,
            error=(f"Policy hash mismatch from {source_label}: invalid pin ({exc})"),
            expected_hash=expected_hash,
        )

    digest = hashlib.new(algo)
    digest.update(raw_bytes)
    actual_hex = digest.hexdigest().lower()
    if actual_hex == expected_hex:
        return None

    expected_norm = f"{algo}:{expected_hex}"
    actual_norm = f"{algo}:{actual_hex}"
    return PolicyFetchResult(
        outcome="hash_mismatch",
        source=source_label,
        error=(
            f"Policy hash mismatch from {source_label}: expected {expected_norm}, got {actual_norm}"
        ),
        expected_hash=expected_norm,
        raw_bytes_hash=actual_norm,
    )


# Cache location: apm_modules/.policy-cache/<hash>.yml + <hash>.meta.json
POLICY_CACHE_DIR = ".policy-cache"
DEFAULT_CACHE_TTL = 3600  # 1 hour
MAX_STALE_TTL = 7 * 24 * 3600  # 7 days -- stale cache usable on refresh failure
CACHE_SCHEMA_VERSION = "5"  # Bump when cache format changes to auto-invalidate


@dataclass
class PolicyFetchResult:
    """Result of a policy fetch attempt.

    The ``outcome`` field discriminates the 9 discovery outcomes defined in
    the plan (section B):

    * ``found``               -- valid policy, enforce per ``enforcement``
    * ``absent``              -- no policy published (404 / empty repo)
    * ``cached_stale``        -- served from cache past TTL on refresh failure
    * ``cache_miss_fetch_fail`` -- no cache, fetch failed
    * ``malformed``           -- YAML valid but schema invalid (fail-closed)
    * ``disabled``            -- ``--no-policy`` / ``APM_POLICY_DISABLE=1``
    * ``garbage_response``    -- 200 OK but body is not valid YAML
    * ``no_git_remote``       -- cannot determine org from git remote
    * ``empty``               -- valid policy with no actionable rules
    * ``hash_mismatch``       -- ``policy.hash`` pin in apm.yml does not match
                                 the fetched policy bytes (always fail-closed)
    """

    policy: ApmPolicy | None = None
    source: str = ""  # "org:contoso/.github", "file:/path", "url:https://..."
    cached: bool = False  # True if served from cache
    error: str | None = None  # Error message if fetch failed

    # -- Outcome-matrix fields (W1-cache-redesign) --
    cache_age_seconds: int | None = None  # Age of cache entry in seconds
    cache_stale: bool = False  # True if cache was served past TTL
    fetch_error: str | None = None  # Network/parse error on refresh attempt
    outcome: str = ""  # See docstring for valid values
    warnings: list[str] = field(default_factory=list)

    # -- Hash-pin fields (#827 supply-chain hardening) --
    # raw_bytes_hash is the digest of the leaf policy bytes off the wire,
    # in canonical "<algo>:<hex>" form. Persisted to the cache so subsequent
    # cached reads can verify against the project's pin without re-fetching.
    raw_bytes_hash: str | None = None
    expected_hash: str | None = None  # The pin that was checked, if any

    @property
    def found(self) -> bool:
        return self.policy is not None


def discover_policy_with_chain(
    project_root: Path,
    *,
    expected_hash: str | None = None,
    policy_override: str | None = None,
    no_cache: bool = False,
    cache_only: bool = False,
) -> PolicyFetchResult:
    """Discover policy with full inheritance chain resolution.

    This is the **shared entry point** for all command sites that need
    chain-aware policy discovery (gate phase, ``--mcp`` preflight,
    ``--dry-run`` preflight).  It ensures every path resolves the same
    merged effective policy with real ``chain_refs``.

    Parameters
    ----------
    project_root:
        Project root directory (used for git-remote org extraction and cache).
    expected_hash:
        Optional pin in ``"<algo>:<hex>"`` form (sourced from
        ``policy.hash`` in the project's ``apm.yml``). When set, the
        digest of the leaf policy bytes must match exactly; otherwise the
        result outcome is set to ``"hash_mismatch"`` and ``policy`` is
        cleared. The pin applies only to the **leaf** -- parent policies
        in an ``extends:`` chain are the leaf author's responsibility.

    Notes
    -----
    The escape hatch (``--no-policy`` flag, ``APM_POLICY_DISABLE=1``
    env var) is enforced by the **callers** (the install pipeline gate
    and the preflight helpers in ``install_preflight``) **before** this
    function is invoked, so neither needs a ``no_policy`` parameter
    here.  The env-var check below remains as a defence-in-depth so
    third-party callers cannot accidentally bypass the disable switch.

    Returns
    -------
    PolicyFetchResult
        With merged effective policy and real chain_refs when inheritance
        is present.  Outcome follows the 9-outcome matrix (section B).
    """
    # -- Escape hatch (defence-in-depth) -------------------------------
    # The CLI's --no-policy flag is handled by callers; this env-var
    # check stays so third-party use of the API still respects the
    # global disable switch.
    if os.environ.get("APM_POLICY_DISABLE") == "1":
        return PolicyFetchResult(outcome="disabled")

    # -- Resolve project-side hash pin (#827) --------------------------
    # An explicit *expected_hash* argument always wins (test seam, future
    # CLI override). Otherwise fall back to ``policy.hash`` in the
    # project's apm.yml. A malformed pin surfaces as ``hash_mismatch``
    # rather than a crash so install fails closed with a clear error.
    if expected_hash is None:
        try:
            pin = read_project_policy_hash_pin(project_root)
        except ProjectPolicyConfigError as exc:
            return PolicyFetchResult(
                outcome="hash_mismatch",
                source="apm.yml",
                error=f"Invalid policy.hash in apm.yml: {exc}",
            )
        if pin is not None:
            expected_hash = pin.normalized

    local_override = bool(policy_override and Path(policy_override).is_file())
    if cache_only and not local_override and not policy_cache_available(project_root):
        if expected_hash is not None:
            return _unverifiable_cache_pin(expected_hash, "")
        return PolicyFetchResult(outcome="absent")

    # -- Base discovery ------------------------------------------------
    discovery_kwargs = {"cache_only": True} if cache_only else {}
    fetch_result = discover_policy(
        project_root,
        policy_override=policy_override,
        no_cache=no_cache,
        expected_hash=expected_hash,
        **discovery_kwargs,
    )

    # -- Chain resolution if leaf has extends: -------------------------
    if (
        fetch_result.policy is not None
        and fetch_result.policy.extends is not None
        and not fetch_result.cached  # Don't re-resolve if served from cache
    ):
        _resolve_and_persist_chain(
            fetch_result,
            project_root,
            no_cache=no_cache,
            cache_only=cache_only,
        )

    return fetch_result


def _strip_source_prefix(src: str) -> str:
    """Strip 'org:' / 'url:' / 'file:' prefix from a PolicyFetchResult.source."""
    return src.removeprefix("org:").removeprefix("url:").removeprefix("file:")


def _derive_leaf_host(source: str, project_root: Path) -> str | None:
    """Derive the origin host of the leaf policy.

    The leaf host pins which host an ``extends:`` reference may resolve
    against (Security Finding F1 -- prevents credential leakage to
    attacker-controlled hosts via cross-host extends chains).

    Returns the host in lowercase, or None if it cannot be determined.

    Source forms:
    * ``url:https://<host>/...`` -> ``<host>``
    * ``org:<host>/<owner>/<repo>`` (3+ slash-segments) -> ``<host>``
    * ``org:<owner>/<repo>`` (2 slash-segments) -> ``github.com`` (default)
    * ``file:<path>`` -> fall back to git remote of *project_root*
    """
    if not source:  # noqa: SIM108
        bare = ""
    else:
        bare = _strip_source_prefix(source)

    if source.startswith("url:") or bare.startswith("https://") or bare.startswith("http://"):
        try:
            parsed = urlparse(bare)
            if parsed.hostname:
                return parsed.hostname.lower()
        except Exception:
            return None
        return None

    if source.startswith("org:") or (bare and "://" not in bare and bare.count("/") >= 1):
        parts = bare.split("/")
        if len(parts) >= 3:
            return parts[0].lower()
        if len(parts) == 2:
            # owner/repo shorthand defaults to github.com (matches
            # _fetch_github_contents convention).
            return "github.com"

    # File source (or unrecognized): fall back to project's git remote.
    org_and_host = _extract_org_from_git_remote(project_root)
    if org_and_host is not None:
        _, host = org_and_host
        if host:
            return host.lower()
    return None


def _extract_extends_host(ref: str) -> str | None:
    """Return the host an ``extends:`` ref resolves against, if explicit.

    * Full URL -> URL host (lowercase)
    * ``<host>/<owner>/<repo>`` (3+ slash-segments) -> ``<host>`` (lowercase)
    * ``<owner>/<repo>`` shorthand -> None (intrinsically same-host)
    * ``<org>`` shorthand (no slash) -> None (intrinsically same-host)
    """
    if not ref:
        return None
    if ref.startswith("http://") or ref.startswith("https://"):
        try:
            parsed = urlparse(ref)
            if parsed.hostname:
                return parsed.hostname.lower()
        except Exception:
            return None
        return None
    if "/" not in ref:
        return None
    parts = ref.split("/")
    if len(parts) >= 3:
        return parts[0].lower()
    return None


def _validate_extends_host(leaf_host: str | None, extends_ref: str) -> None:
    """Reject ``extends:`` refs that point at a different host than the leaf.

    Raises :class:`PolicyInheritanceError` (imported lazily to avoid a
    module-level cycle) when the ``extends:`` ref names a host that does
    not match *leaf_host*. Pure shorthand refs (``owner/repo``, ``org``)
    are intrinsically same-host and always pass.

    See Security Finding F1: a malicious org policy author setting
    ``extends: "evil.example.com/org/.github"`` could otherwise route
    ``git credential fill`` against an attacker-controlled host.
    """
    from . import inheritance as _inheritance_mod

    extends_host = _extract_extends_host(extends_ref)
    if extends_host is None:
        return  # shorthand: intrinsically same-host, allowed.

    if leaf_host is None:
        raise _inheritance_mod.PolicyInheritanceError(
            f"Policy extends: cross-host reference rejected "
            f"(leaf host: <unknown>, extends host: {extends_host}); "
            f"cross-host policy chains are not allowed"
        )

    if extends_host != leaf_host.lower():
        raise _inheritance_mod.PolicyInheritanceError(
            f"Policy extends: cross-host reference rejected "
            f"(leaf host: {leaf_host}, extends host: {extends_host}); "
            f"cross-host policy chains are not allowed"
        )


def _resolve_ado_parent_ref(
    parent_ref: str,
    current_source: str,
    leaf_host: str,
) -> tuple[str, str, str, str] | None:
    """Normalize an ADO parent ref to ``(org, project, repo, host)``."""
    current_parts = _strip_source_prefix(current_source).split("/")
    if is_visualstudio_legacy_hostname(leaf_host):
        current_org = leaf_host[: -len(".visualstudio.com")]
    elif len(current_parts) >= 2:
        current_org = current_parts[1]
    else:
        current_org = ""

    if parent_ref == "org":
        resolved = (current_org, ADO_POLICY_PROJECT, "_apm", leaf_host)
    else:
        parts = parent_ref.strip("/").split("/")
        explicit_host = _extract_extends_host(parent_ref)
        if explicit_host is None and len(parts) == 2:
            resolved = (current_org, parts[0], parts[1], leaf_host)
        elif explicit_host and is_visualstudio_legacy_hostname(explicit_host) and len(parts) >= 3:
            resolved = (
                explicit_host[: -len(".visualstudio.com")],
                parts[1],
                "/".join(parts[2:]),
                explicit_host,
            )
        elif explicit_host and is_azure_devops_hostname(explicit_host) and len(parts) >= 4:
            resolved = (
                parts[1],
                parts[2],
                "/".join(parts[3:]),
                explicit_host,
            )
        else:
            return None

    return resolved if all(resolved[:3]) else None


def _fetch_chain_parent(
    parent_ref: str,
    *,
    current_source: str,
    leaf_host: str | None,
    project_root: Path,
    no_cache: bool,
    cache_only: bool = False,
) -> PolicyFetchResult:
    """Fetch one parent without losing the leaf's host or backend.

    GitHub-style ``owner/repo`` refs are qualified with a non-default leaf
    host. On ADO, ``project/repo`` means a repo in the current ancestor's org,
    while explicit refs use ``host/org/project/repo`` on ``dev.azure.com`` or
    ``host/project/repo`` on legacy ``*.visualstudio.com`` hosts. URLs and
    local-file refs continue through the public single-policy owner.
    """
    cache_kwargs = {"cache_only": True} if cache_only else {}
    if parent_ref.startswith(("http://", "https://")) or Path(parent_ref).is_file():
        return discover_policy(
            project_root,
            policy_override=parent_ref,
            no_cache=no_cache,
            **cache_kwargs,
        )

    if leaf_host and is_azure_devops_hostname(leaf_host):
        resolved = _resolve_ado_parent_ref(parent_ref, current_source, leaf_host)
        if resolved is None:
            return PolicyFetchResult(
                source=f"org:{parent_ref}",
                error=f"Invalid Azure DevOps policy reference: {parent_ref}",
                outcome="cache_miss_fetch_fail",
            )
        org, project, repo, host = resolved
        return _fetch_from_ado_repo(
            org=org,
            project=project,
            repo=repo,
            host=host,
            project_root=project_root,
            no_cache=no_cache,
            **cache_kwargs,
        )

    normalized_ref = parent_ref
    if leaf_host and leaf_host != "github.com" and _extract_extends_host(parent_ref) is None:
        if "/" in parent_ref:
            normalized_ref = f"{leaf_host}/{parent_ref}"
    return discover_policy(
        project_root,
        policy_override=normalized_ref,
        no_cache=no_cache,
        **cache_kwargs,
    )


def _resolve_and_persist_chain(
    fetch_result: PolicyFetchResult,
    project_root: Path,
    *,
    no_cache: bool = False,
    cache_only: bool = False,
) -> None:
    """Resolve inheritance chain and update cache with merged policy + chain_refs.

    Walks the ``extends:`` chain depth-first, fetching each parent via the
    single-policy ``discover_policy`` (so each fetch still hits the
    well-tested fetch path).  Cycle detection on normalized ``extends:``
    refs and ``MAX_CHAIN_DEPTH`` enforcement protect against runaway or
    self-referential chains.

    If any parent fetch fails, mark the chain incomplete and clear the
    partial policy so enforcement cannot proceed with weaker constraints.

    Mutates *fetch_result*.policy in-place with the merged effective policy.
    Called by :func:`discover_policy_with_chain` -- not intended for direct
    use.
    """
    from . import inheritance as _inheritance_mod

    leaf_policy = fetch_result.policy
    leaf_source = fetch_result.source

    # Host pin: extends: refs may only resolve against the leaf's origin
    # host. Prevents credential leakage to attacker-controlled hosts via
    # cross-host extends chains (Security Finding F1).
    leaf_host = _derive_leaf_host(leaf_source, project_root)

    # Ordered ancestors collected as we walk parents.  Built leaf-first
    # for traversal convenience; reversed before merging.
    chain_policies: list[ApmPolicy] = [leaf_policy]
    chain_sources: list[str] = [leaf_source]

    # Track normalized refs we've already followed to break cycles.
    # We seed with the leaf's source so an extends pointing back at the
    # leaf is also detected.
    visited: list[str] = [_strip_source_prefix(leaf_source)] if leaf_source else []

    current = leaf_policy
    incomplete_chain: tuple[str, int, int] | None = None
    stale_ancestor: PolicyFetchResult | None = None

    while current.extends:
        next_ref = current.extends

        # Host pin enforcement: must validate BEFORE any fetch so we never
        # call git credential fill against an attacker-controlled host.
        _validate_extends_host(leaf_host, next_ref)

        if _inheritance_mod.detect_cycle(visited, next_ref):
            raise _inheritance_mod.PolicyInheritanceError(
                f"Cycle detected in policy extends chain: {' -> '.join(visited)} -> {next_ref}"
            )

        # Depth check: chain_policies already has len() entries; next fetch
        # would push us to len()+1.  resolve_policy_chain enforces this
        # afterwards, but failing here gives a clearer error.
        if len(chain_policies) + 1 > _inheritance_mod.MAX_CHAIN_DEPTH:
            raise _inheritance_mod.PolicyInheritanceError(
                f"Policy chain depth exceeds maximum of "
                f"{_inheritance_mod.MAX_CHAIN_DEPTH} "
                f"(chain: {' -> '.join(visited)} -> {next_ref})"
            )

        parent_result = _fetch_chain_parent(
            next_ref,
            current_source=chain_sources[-1],
            leaf_host=leaf_host,
            project_root=project_root,
            no_cache=no_cache,
            cache_only=cache_only,
        )
        fetch_result.warnings.extend(parent_result.warnings)

        if parent_result.policy is None:
            # Parent fetch failed -- never enforce the weaker partial chain.
            attempted = len(chain_policies) + 1
            resolved = len(chain_policies)
            incomplete_chain = (next_ref, resolved, attempted)
            break

        # Keep the nearest stale ancestor's refresh diagnostic. Continue
        # merging its usable cached policy, but never relabel that ancestry
        # as fresh or replace a nearer failure with a farther one.
        if parent_result.outcome == "cached_stale" and stale_ancestor is None:
            stale_ancestor = parent_result

        chain_policies.append(parent_result.policy)
        chain_sources.append(parent_result.source)
        visited.append(next_ref)
        current = parent_result.policy

    # No actual ancestors fetched -- nothing to merge or re-cache.
    if len(chain_policies) == 1:
        if incomplete_chain is not None:
            ref, resolved, attempted = incomplete_chain
            fetch_result.outcome = "incomplete_chain"
            fetch_result.error = (
                f"Policy chain incomplete: {ref} unreachable "
                f"({resolved} of {attempted} policies resolved)"
            )
            fetch_result.policy = None
        return

    # Merge in [root, ..., leaf] order.  We collected leaf-first, so reverse.
    ordered = list(reversed(chain_policies))
    ordered_sources = list(reversed(chain_sources))

    try:
        merged = _inheritance_mod.resolve_policy_chain(ordered)
    except _inheritance_mod.PolicyInheritanceError:
        # Re-raise depth errors from the canonical validator so callers
        # see a single consistent error type.
        raise

    chain_refs: list[str] = [_strip_source_prefix(src) for src in ordered_sources if src]

    cache_key = _strip_source_prefix(leaf_source) if leaf_source else ""
    if cache_key and incomplete_chain is None and stale_ancestor is None:
        _write_cache(
            cache_key,
            merged,
            project_root,
            chain_refs=chain_refs,
            raw_bytes_hash=fetch_result.raw_bytes_hash,
            warnings=fetch_result.warnings,
        )

    if incomplete_chain is not None:
        ref, resolved, attempted = incomplete_chain
        fetch_result.outcome = "incomplete_chain"
        fetch_result.error = (
            f"Policy chain incomplete: {ref} unreachable "
            f"({resolved} of {attempted} policies resolved)"
        )
        fetch_result.policy = None
        return

    fetch_result.policy = merged
    if stale_ancestor is not None:
        fetch_result.outcome = "cached_stale"
        fetch_result.fetch_error = stale_ancestor.fetch_error or stale_ancestor.error
        fetch_result.cached = True
        fetch_result.cache_stale = True
        fetch_result.cache_age_seconds = stale_ancestor.cache_age_seconds
        return
    fetch_result.outcome = "empty" if _is_policy_empty(merged) else "found"


def discover_policy(
    project_root: Path,
    *,
    policy_override: str | None = None,
    no_cache: bool = False,
    expected_hash: str | None = None,
    cache_only: bool = False,
) -> PolicyFetchResult:
    """Discover and load the applicable policy for a project.

    Resolution order:
    1. If policy_override is a local file path -> load from file
    2. If policy_override is an https:// URL -> fetch from URL
       (http:// is rejected for security)
    3. If policy_override is "org" -> auto-discover from project's git remote
    4. If policy_override is "owner/repo" (or "host/owner/repo")
       -> fetch from that repo via GitHub Contents API
    5. If policy_override is None -> auto-discover from project's git remote

    The user-facing forms are documented in
    ``apm_cli.policy._help_text.POLICY_SOURCE_FORMS_HELP``; that constant
    is the single source of truth shared by ``apm audit --policy`` and
    ``apm policy status --policy-source``.

    The optional ``expected_hash`` (``"<algo>:<hex>"``) pins the leaf
    policy bytes; mismatches return ``outcome="hash_mismatch"`` and
    must always be treated fail-closed by callers.
    """
    if policy_override:
        path = Path(policy_override)
        if path.exists() and path.is_file():
            return _load_from_file(path, expected_hash=expected_hash)
        if policy_override.startswith("http://"):
            return PolicyFetchResult(
                error="Refusing plaintext http:// policy URL -- use https://",
                source=f"url:{policy_override}",
            )
        if policy_override.startswith("https://"):
            result = _fetch_from_url(
                policy_override,
                project_root,
                no_cache=no_cache,
                expected_hash=expected_hash,
                cache_only=cache_only,
            )
            if cache_only and expected_hash is not None and result.outcome == "absent":
                return _unverifiable_cache_pin(expected_hash, result.source)
            return result
        if policy_override != "org":
            # Try as owner/repo reference
            result = _fetch_from_repo(
                policy_override,
                project_root,
                no_cache=no_cache,
                expected_hash=expected_hash,
                cache_only=cache_only,
            )
            if cache_only and expected_hash is not None and result.outcome == "absent":
                return _unverifiable_cache_pin(expected_hash, result.source)
            return result

    # Auto-discover from git remote
    return _auto_discover(
        project_root,
        no_cache=no_cache,
        expected_hash=expected_hash,
        cache_only=cache_only,
    )


def _load_from_file(path: Path, *, expected_hash: str | None = None) -> PolicyFetchResult:
    """Load policy from a local file."""
    try:
        # Read raw bytes ourselves so we can verify the pin against the
        # exact bytes that get parsed (matches the on-the-wire semantics
        # used by the URL/repo fetchers).
        content = path.read_text(encoding="utf-8")
    except Exception as e:
        return PolicyFetchResult(
            error=f"Failed to read {path}: {e}",
            outcome="cache_miss_fetch_fail",
        )

    source_label = f"file:{path}"
    mismatch = _verify_hash_pin(content, expected_hash, source_label)
    if mismatch is not None:
        return mismatch

    try:
        policy, warnings = load_policy(content)
        outcome = "empty" if _is_policy_empty(policy) else "found"
        actual_hash = (
            _compute_hash_normalized(content, expected_hash) if expected_hash is not None else None
        )
        return PolicyFetchResult(
            policy=policy,
            source=source_label,
            outcome=outcome,
            raw_bytes_hash=actual_hash,
            expected_hash=expected_hash,
            warnings=warnings,
        )
    except PolicyValidationError as e:
        return PolicyFetchResult(
            error=f"Invalid policy file {path}: {e}",
            outcome="malformed",
            warnings=e.warnings,
        )


def _auto_discover(
    project_root: Path,
    *,
    no_cache: bool = False,
    expected_hash: str | None = None,
    cache_only: bool = False,
) -> PolicyFetchResult:
    """Auto-discover policy by cascading through candidate repos.

    1. Run git remote get-url origin
    2. Parse org + host from URL
    3. Select host profile to determine candidate repos
    4. Try each candidate in precedence order (.github-private > .github > .apm > _apm)
       - 404/absent -> continue to next candidate
       - Error (auth, timeout, malformed) -> fail-closed immediately
       - Found -> return (first match wins)
    5. All candidates exhausted -> outcome="absent"
    """
    org_and_host = _extract_org_from_git_remote(project_root)
    if org_and_host is None:
        return PolicyFetchResult(
            error="Could not determine org from git remote",
            outcome="no_git_remote",
        )

    org, host = org_and_host
    candidates = _policy_repo_candidates(host)
    is_ado = is_azure_devops_hostname(host)

    for candidate_repo in candidates:
        logger.debug("Trying org policy repo candidate %s on host %s", candidate_repo, host)
        if is_ado:
            result = _fetch_from_ado_repo(
                org=org,
                project=ADO_POLICY_PROJECT,
                repo=candidate_repo,
                host=host,
                project_root=project_root,
                no_cache=no_cache,
                expected_hash=expected_hash,
                cache_only=cache_only,
            )
        else:
            repo_ref = f"{org}/{candidate_repo}"
            if host and host != "github.com":
                repo_ref = f"{host}/{repo_ref}"
            result = _fetch_from_repo(
                repo_ref,
                project_root,
                no_cache=no_cache,
                expected_hash=expected_hash,
                cache_only=cache_only,
            )

        # 404 / absent -> try the next candidate
        if result.outcome == "absent":
            logger.debug(
                "Policy repo candidate %s absent on host %s; trying next candidate",
                candidate_repo,
                host,
            )
            # Warn when .github-private is absent on a GitHub host -- GitHub
            # returns 404 (not 403) when the token lacks private-repo scope,
            # so an absent result may mask a token-permission problem.
            if candidate_repo == ".github-private" and _is_github_host(host):
                logger.warning(
                    "Policy repo %s/%s not found (or token lacks private-repo "
                    "read scope). If your org publishes policy in .github-private, "
                    "ensure the token has Contents:read permission on that repo.",
                    org,
                    candidate_repo,
                )
            continue

        # Any other outcome (found, error, malformed, etc.) -> return immediately
        return result

    # All candidates exhausted: no policy published anywhere.
    if cache_only and expected_hash is not None:
        return _unverifiable_cache_pin(expected_hash, "")
    return PolicyFetchResult(error=None, outcome="absent")


def _extract_org_from_git_remote(
    project_root: Path,
) -> tuple[str, str] | None:
    """Extract (org, host) from git remote origin URL.

    Handles:
    - https://github.com/contoso/my-project.git -> ("contoso", "github.com")
    - git@github.com:contoso/my-project.git -> ("contoso", "github.com")
    - https://github.example.com/contoso/my-project.git -> ("contoso", "github.example.com")
    """
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=project_root,
            timeout=5,
        )
        if result.returncode != 0:
            return None
        return _parse_remote_url(result.stdout.strip())
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None


def _parse_remote_url(url: str) -> tuple[str, str] | None:
    """Parse a git remote URL into (org, host).

    Accepts SCP-style SSH URLs with any username (not just ``git@``), so
    EMU/GHE deployments that use a non-``git`` SSH user
    (e.g. ``enterprise-user@ghe.corp.com:org/repo.git``) parse correctly.
    Also handles Azure DevOps SSH URLs which carry an extra ``v3/``
    path prefix (``git@ssh.dev.azure.com:v3/<org>/<project>/<repo>``).

    Returns None if URL can't be parsed.
    """
    if not url:
        return None

    # SCP-like SSH: <user>@<host>:<path> -- any user, not just `git`.
    # Closes #1159 for non-`git` SSH users (EMU, custom GHE accounts).
    scp_match = SCP_LIKE_RE.match(url)
    if scp_match:
        host = scp_match.group("host")
        path_part = scp_match.group("path")
        try:
            parts = path_part.rstrip("/").removesuffix(".git").split("/")
            parts = [p for p in parts if p]
            if not parts:
                return None
            # Azure DevOps SSH carries a leading 'v3/' segment that is
            # NOT the org. The org is the second segment.
            if host == "ssh.dev.azure.com" and parts[0] == "v3" and len(parts) >= 2:
                return (parts[1], host)
            return (parts[0], host)
        except (ValueError, IndexError):
            return None

    # HTTPS: https://github.com/owner/repo.git
    # ADO:   https://dev.azure.com/org/project/_git/repo
    if "://" in url:
        try:
            parsed = urlparse(url)
            host = parsed.hostname or ""
            path_parts = parsed.path.strip("/").removesuffix(".git").rstrip("/").split("/")
            if is_visualstudio_legacy_hostname(host):
                return (host[: -len(".visualstudio.com")], host)
            if host and path_parts and path_parts[0]:
                return (path_parts[0], host)
        except Exception:
            return None

    return None


def _fetch_from_url(
    url: str,
    project_root: Path,
    *,
    no_cache: bool = False,
    expected_hash: str | None = None,
    cache_only: bool = False,
) -> PolicyFetchResult:
    """Fetch policy YAML from a direct URL."""
    source_label = f"url:{url}"
    cache_entry: _CacheEntry | None = None
    cache_entry_persisted = _cache_entry_files_exist(url, project_root)

    # Use URL as cache key
    if not no_cache:
        cache_entry = _read_cache_entry(url, project_root, expected_hash=expected_hash)
        if cache_entry is not None and not cache_entry.stale:
            outcome = "empty" if _is_policy_empty(cache_entry.policy) else "found"
            return PolicyFetchResult(
                policy=cache_entry.policy,
                source=cache_entry.source,
                cached=True,
                cache_age_seconds=cache_entry.age_seconds,
                outcome=outcome,
                raw_bytes_hash=cache_entry.raw_bytes_hash or None,
                expected_hash=expected_hash,
                warnings=cache_entry.warnings,
            )

    if cache_only:
        return _cache_only_policy_result(
            cache_entry,
            source_label=source_label,
            expected_hash=expected_hash,
            cache_entry_persisted=cache_entry_persisted,
        )

    fetch_error: str | None = None
    content: str | None = None

    try:
        resp = requests.get(url, timeout=10, allow_redirects=False)
        if resp.status_code == 404:
            return PolicyFetchResult(
                source=source_label,
                error="404: Policy file not found",
                outcome="absent",
            )
        if 300 <= resp.status_code < 400:
            # Redirects are refused: a malicious or compromised origin
            # could otherwise bounce us to an attacker-controlled host
            # (SSRF / Referer leakage). Treat as fetch failure.
            location = resp.headers.get("Location", "<no Location header>")
            fetch_error = f"Refusing HTTP redirect ({resp.status_code}) from {url} to {location}"
        elif resp.status_code != 200:
            fetch_error = f"HTTP {resp.status_code} fetching {url}"
        else:
            content = resp.text
    except requests.exceptions.Timeout:
        fetch_error = f"Timeout fetching {url}"
    except requests.exceptions.ConnectionError:
        fetch_error = f"Connection error fetching {url}"
    except Exception as e:
        fetch_error = f"Error fetching {url}: {e}"

    if fetch_error:
        return _stale_fallback_or_error(
            cache_entry, fetch_error, source_label, "cache_miss_fetch_fail"
        )

    # Garbage-response detection: body must be valid YAML mapping
    garbage_result = _detect_garbage(content, url, source_label, cache_entry)
    if garbage_result is not None:
        return garbage_result

    # Hash pin verification (#827) -- BEFORE parse, on raw bytes off wire.
    # A mismatch is a hard failure regardless of cache_entry availability:
    # falling back to a "good" cache when the pin doesn't match would mask
    # exactly the compromise this pin is designed to catch.
    mismatch = _verify_hash_pin(content, expected_hash, source_label)
    if mismatch is not None:
        return mismatch

    try:
        policy, warnings = load_policy(content)
    except PolicyValidationError as e:
        return PolicyFetchResult(
            error=f"Invalid policy from {url}: {e}",
            source=source_label,
            outcome="malformed",
            warnings=e.warnings,
        )

    chain_refs = [url]
    actual_hash = _compute_hash_normalized(content, expected_hash)
    if policy.extends is None:
        _write_cache(
            url,
            policy,
            project_root,
            chain_refs=chain_refs,
            raw_bytes_hash=actual_hash,
            warnings=warnings,
        )
    outcome = "empty" if _is_policy_empty(policy) else "found"
    return PolicyFetchResult(
        policy=policy,
        source=source_label,
        outcome=outcome,
        raw_bytes_hash=actual_hash,
        expected_hash=expected_hash,
        warnings=warnings,
    )


def _fetch_from_repo(
    repo_ref: str,
    project_root: Path,
    *,
    no_cache: bool = False,
    expected_hash: str | None = None,
    cache_only: bool = False,
) -> PolicyFetchResult:
    """Fetch apm-policy.yml from a GitHub repo via Contents API.

    repo_ref format: "owner/.github" or "host/owner/.github"
    """
    source_label = f"org:{repo_ref}"
    cache_entry: _CacheEntry | None = None
    cache_entry_persisted = _cache_entry_files_exist(repo_ref, project_root)

    if not no_cache:
        cache_entry = _read_cache_entry(repo_ref, project_root, expected_hash=expected_hash)
        if cache_entry is not None and not cache_entry.stale:
            outcome = "empty" if _is_policy_empty(cache_entry.policy) else "found"
            return PolicyFetchResult(
                policy=cache_entry.policy,
                source=cache_entry.source,
                cached=True,
                cache_age_seconds=cache_entry.age_seconds,
                outcome=outcome,
                raw_bytes_hash=cache_entry.raw_bytes_hash or None,
                expected_hash=expected_hash,
                warnings=cache_entry.warnings,
            )

    if cache_only:
        return _cache_only_policy_result(
            cache_entry,
            source_label=source_label,
            expected_hash=expected_hash,
            cache_entry_persisted=cache_entry_persisted,
        )

    content, error = _fetch_github_contents(repo_ref, "apm-policy.yml")

    if error:
        # 404 = no policy, not an error
        if "404" in error:
            return PolicyFetchResult(source=source_label, outcome="absent")
        # Fetch failed -- try stale cache fallback
        return _stale_fallback_or_error(cache_entry, error, source_label, "cache_miss_fetch_fail")

    if content is None:
        return PolicyFetchResult(source=source_label, outcome="absent")

    # Garbage-response detection
    garbage_result = _detect_garbage(content, repo_ref, source_label, cache_entry)
    if garbage_result is not None:
        return garbage_result

    # Hash pin verification (#827) -- BEFORE parse, on raw bytes off wire.
    mismatch = _verify_hash_pin(content, expected_hash, source_label)
    if mismatch is not None:
        return mismatch

    try:
        policy, warnings = load_policy(content)
    except PolicyValidationError as e:
        return PolicyFetchResult(
            error=f"Invalid policy in {repo_ref}: {e}",
            source=source_label,
            outcome="malformed",
            warnings=e.warnings,
        )

    chain_refs = [repo_ref]
    actual_hash = _compute_hash_normalized(content, expected_hash)
    if policy.extends is None:
        _write_cache(
            repo_ref,
            policy,
            project_root,
            chain_refs=chain_refs,
            raw_bytes_hash=actual_hash,
            warnings=warnings,
        )
    outcome = "empty" if _is_policy_empty(policy) else "found"
    return PolicyFetchResult(
        policy=policy,
        source=source_label,
        outcome=outcome,
        raw_bytes_hash=actual_hash,
        expected_hash=expected_hash,
        warnings=warnings,
    )


def _fetch_github_contents(
    repo_ref: str,
    file_path: str,
) -> tuple[str | None, str | None]:
    """Fetch file contents from GitHub API.

    Returns (content_string, error_string). One will be None.
    """

    # Parse repo_ref: "owner/repo" or "host/owner/repo"
    parts = repo_ref.split("/")
    if len(parts) == 2:
        host = "github.com"
        owner, repo = parts
    elif len(parts) >= 3:
        host = parts[0]
        owner = parts[1]
        repo = "/".join(parts[2:])
    else:
        return None, f"Invalid repo reference: {repo_ref}"

    # Build API URL
    if host == "github.com":
        api_url = f"https://api.github.com/repos/{owner}/{repo}/contents/{file_path}"
    else:
        api_url = f"https://{host}/api/v3/repos/{owner}/{repo}/contents/{file_path}"

    headers = {"Accept": "application/vnd.github.v3+json"}
    token = _get_token_for_host(host)
    if token:
        headers["Authorization"] = f"token {token}"

    try:
        resp = requests.get(api_url, headers=headers, timeout=10, allow_redirects=False)
        if resp.status_code == 404:
            return None, "404: Policy file not found"
        if resp.status_code == 403:
            return None, f"403: Access denied to {repo_ref}"
        if 300 <= resp.status_code < 400:
            location = resp.headers.get("Location", "<no Location header>")
            return None, (
                f"Refusing HTTP redirect ({resp.status_code}) from {api_url} to {location}"
            )
        if resp.status_code != 200:
            return None, f"HTTP {resp.status_code} fetching policy from {repo_ref}"

        data = resp.json()
        if data.get("encoding") == "base64" and data.get("content"):
            content = base64.b64decode(data["content"]).decode("utf-8")
            return content, None
        elif data.get("content"):
            return data["content"], None
        else:
            return None, f"Unexpected response format from {repo_ref}"
    except requests.exceptions.Timeout:
        return None, f"Timeout fetching policy from {repo_ref}"
    except requests.exceptions.ConnectionError:
        return None, f"Connection error fetching policy from {repo_ref}"
    except Exception as e:
        return None, f"Error fetching policy from {repo_ref}: {e}"


# ---------------------------------------------------------------------------
# ADO policy fetch
# ---------------------------------------------------------------------------


def _fetch_from_ado_repo(
    *,
    org: str,
    project: str,
    repo: str,
    host: str,
    project_root: Path,
    no_cache: bool = False,
    expected_hash: str | None = None,
    cache_only: bool = False,
) -> PolicyFetchResult:
    """Fetch apm-policy.yml from an Azure DevOps repo.

    Mirrors ``_fetch_from_repo`` but uses ``_fetch_ado_contents`` (ADO
    Items API) instead of ``_fetch_github_contents`` (GitHub Contents API).
    """
    repo_ref = f"{host}/{org}/{project}/{repo}"
    source_label = f"org:{repo_ref}"
    cache_entry: _CacheEntry | None = None
    cache_entry_persisted = _cache_entry_files_exist(repo_ref, project_root)

    if not no_cache:
        cache_entry = _read_cache_entry(repo_ref, project_root, expected_hash=expected_hash)
        if cache_entry is not None and not cache_entry.stale:
            outcome = "empty" if _is_policy_empty(cache_entry.policy) else "found"
            return PolicyFetchResult(
                policy=cache_entry.policy,
                source=cache_entry.source,
                cached=True,
                cache_age_seconds=cache_entry.age_seconds,
                outcome=outcome,
                raw_bytes_hash=cache_entry.raw_bytes_hash or None,
                expected_hash=expected_hash,
                warnings=cache_entry.warnings,
            )

    if cache_only:
        return _cache_only_policy_result(
            cache_entry,
            source_label=source_label,
            expected_hash=expected_hash,
            cache_entry_persisted=cache_entry_persisted,
        )

    content, error = _fetch_ado_contents(org, project, repo, "apm-policy.yml", host=host)

    if error:
        if "404" in error:
            return PolicyFetchResult(source=source_label, outcome="absent")
        return _stale_fallback_or_error(cache_entry, error, source_label, "cache_miss_fetch_fail")

    if content is None:
        return PolicyFetchResult(source=source_label, outcome="absent")

    garbage_result = _detect_garbage(content, repo_ref, source_label, cache_entry)
    if garbage_result is not None:
        return garbage_result

    mismatch = _verify_hash_pin(content, expected_hash, source_label)
    if mismatch is not None:
        return mismatch

    try:
        policy, warnings = load_policy(content)
    except PolicyValidationError as e:
        return PolicyFetchResult(
            error=f"Invalid policy in {repo_ref}: {e}",
            source=source_label,
            outcome="malformed",
            warnings=e.warnings,
        )

    chain_refs = [repo_ref]
    actual_hash = _compute_hash_normalized(content, expected_hash)
    if policy.extends is None:
        _write_cache(
            repo_ref,
            policy,
            project_root,
            chain_refs=chain_refs,
            raw_bytes_hash=actual_hash,
            warnings=warnings,
        )
    outcome = "empty" if _is_policy_empty(policy) else "found"
    return PolicyFetchResult(
        policy=policy,
        source=source_label,
        outcome=outcome,
        raw_bytes_hash=actual_hash,
        expected_hash=expected_hash,
        warnings=warnings,
    )


def _fetch_ado_contents(
    org: str,
    project: str,
    repo: str,
    file_path: str,
    *,
    host: str = "dev.azure.com",
) -> tuple[str | None, str | None]:
    """Fetch file contents from Azure DevOps Items API.

    Returns ``(content_string, error_string)``. One will be ``None``.
    """
    api_url = build_ado_api_url(org, project, repo, file_path, host=host)
    repo_ref = f"{host}/{org}/{project}/{repo}"

    # ADO auth is centralized in AuthResolver: ADO_APM_PAT uses Basic auth,
    # and az CLI AAD tokens use Bearer auth. No GitHub PATs are consulted.
    from ..core.auth import AuthResolver

    headers: dict[str, str] = {}
    auth_resolver = AuthResolver()
    auth_ctx = auth_resolver.resolve(host, org=org)
    if auth_ctx.token:
        if auth_ctx.auth_scheme == "bearer":
            headers["Authorization"] = f"Bearer {auth_ctx.token}"
        else:
            basic_cred = base64.b64encode(f":{auth_ctx.token}".encode()).decode()
            headers["Authorization"] = f"Basic {basic_cred}"

    try:
        resp = requests.get(api_url, headers=headers, timeout=10, allow_redirects=False)
        if resp.status_code == 404:
            return None, "404: Policy file not found"
        if resp.status_code in (401, 403):
            remediation = auth_resolver.build_error_context(host, "fetch org policy", org=org)
            return None, (f"{resp.status_code}: Access denied to {repo_ref}{remediation}")
        if 300 <= resp.status_code < 400:
            location = resp.headers.get("Location", "<no Location header>")
            return None, (
                f"Refusing HTTP redirect ({resp.status_code}) from {api_url} to {location}"
            )
        if resp.status_code != 200:
            return None, f"HTTP {resp.status_code} fetching policy from {repo_ref}"
        # ADO Items API returns raw file content by default
        return resp.text, None
    except requests.exceptions.Timeout:
        return None, f"Timeout fetching policy from {repo_ref}"
    except requests.exceptions.ConnectionError:
        return None, f"Connection error fetching policy from {repo_ref}"
    except Exception as e:
        return None, f"Error fetching policy from {repo_ref}: {e}"


def _is_github_host(host: str) -> bool:
    """Return True if *host* is a known GitHub-family hostname."""
    if host == "github.com":
        return True
    if host.endswith(".ghe.com"):
        return True
    gh_host = os.environ.get("GITHUB_HOST", "")
    if gh_host and host == gh_host:  # noqa: SIM103
        return True
    return False


def _get_token_for_host(host: str) -> str | None:
    """Get authentication token for a given host.

    Environment-variable tokens (GITHUB_TOKEN, GITHUB_APM_PAT, GH_TOKEN)
    are only returned when *host* is a recognized GitHub-family hostname.
    For other hosts the token manager + git credential helpers are used.
    """
    try:
        from ..core.token_manager import GitHubTokenManager

        manager = GitHubTokenManager()
        return manager.get_token_with_credential_fallback("modules", host)
    except Exception as exc:
        logger.debug("Token manager failed for %s: %s", host, exc)
        if _is_github_host(host):
            return (
                os.environ.get("GITHUB_TOKEN")
                or os.environ.get("GITHUB_APM_PAT")
                or os.environ.get("GH_TOKEN")
            )
        return None


# -- Cache ----------------------------------------------------------


@dataclass
class _CacheEntry:
    """Internal representation of a cached policy read."""

    policy: ApmPolicy
    source: str
    age_seconds: int
    stale: bool  # True if past TTL (but within MAX_STALE_TTL)
    chain_refs: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    fingerprint: str = ""
    raw_bytes_hash: str = ""  # "<algo>:<hex>" of leaf bytes off wire (#827)


def _cache_only_policy_result(
    cache_entry: _CacheEntry | None,
    *,
    source_label: str,
    expected_hash: str | None,
    cache_entry_persisted: bool,
) -> PolicyFetchResult:
    """Return a cached policy or an offline-safe absent outcome."""
    if cache_entry is None:
        if expected_hash is not None and cache_entry_persisted:
            return PolicyFetchResult(
                source=source_label,
                outcome="hash_mismatch",
                error="Cached policy bytes do not match the project policy.hash pin",
                expected_hash=expected_hash,
            )
        return PolicyFetchResult(source=source_label, outcome="absent")
    return PolicyFetchResult(
        policy=cache_entry.policy,
        source=cache_entry.source,
        cached=True,
        cache_stale=True,
        cache_age_seconds=cache_entry.age_seconds,
        outcome="cached_stale",
        raw_bytes_hash=cache_entry.raw_bytes_hash or None,
        expected_hash=expected_hash,
        warnings=cache_entry.warnings,
    )


def _unverifiable_cache_pin(
    expected_hash: str,
    source: str,
) -> PolicyFetchResult:
    """Fail closed when cache-only discovery cannot verify a project pin."""
    return PolicyFetchResult(
        source=source,
        outcome="hash_mismatch",
        error="Policy hash pin cannot be verified without cached policy bytes",
        expected_hash=expected_hash,
    )


def _get_cache_dir(project_root: Path) -> Path:
    """Get the policy cache directory.

    Path-security guard (#832): the resulting path is asserted to live
    within ``project_root``.  This catches the edge case where
    ``apm_modules`` itself is a symlink that points outside the
    project root -- a configuration that, while unusual, would let
    cache reads/writes escape the project tree.
    """
    # Resolve early so candidate inherits long-name form on Windows;
    # without this, resolve() on a not-yet-existing candidate keeps
    # 8.3 short names while the base resolves to long names (#886).
    project_root = project_root.resolve()
    base = project_root / "apm_modules"
    candidate = base / POLICY_CACHE_DIR
    # Resolve both ends and assert containment under ``project_root``,
    # not under ``base`` -- otherwise a symlinked apm_modules pointing
    # outside the project would resolve through the symlink on both
    # sides and the check would silently pass.
    try:
        ensure_path_within(candidate, project_root)
    except PathTraversalError:
        raise PathTraversalError(  # noqa: B904
            f"Policy cache path '{candidate}' resolves outside "
            f"project root '{project_root}' -- refusing to read or "
            "write the cache here."
        )
    return candidate


def _cache_key(repo_ref: str) -> str:
    """Generate a deterministic cache filename from repo ref."""
    return hashlib.sha256(repo_ref.encode()).hexdigest()[:16]


def _cache_entry_files_exist(repo_ref: str, project_root: Path) -> bool:
    """Return whether both persisted files exist for one policy source."""
    cache_dir = _get_cache_dir(project_root)
    key = _cache_key(repo_ref)
    return (cache_dir / f"{key}.yml").is_file() and (cache_dir / f"{key}.meta.json").is_file()


def policy_cache_available(project_root: Path) -> bool:
    """Return whether the project has any complete persisted policy entry."""
    cache_dir = _get_cache_dir(project_root)
    if not cache_dir.is_dir():
        return False
    for policy_file in cache_dir.glob("*.yml"):
        metadata_file = cache_dir / f"{policy_file.stem}.meta.json"
        if metadata_file.is_file():
            return True
    return False


def _policy_to_dict(policy: ApmPolicy) -> dict:
    """Serialize an ApmPolicy to a dict matching the YAML schema."""

    def _opt_list(val: tuple[str, ...] | None) -> list | None:
        return None if val is None else list(val)

    serialized = {
        "name": policy.name,
        "version": policy.version,
        "extends": policy.extends,
        "enforcement": policy.enforcement,
        "fetch_failure": policy.fetch_failure,
        "cache": {"ttl": policy.cache.ttl},
        "dependencies": {
            "allow": _opt_list(policy.dependencies.allow),
            "deny": _opt_list(policy.dependencies.deny),
            "require": _opt_list(policy.dependencies.require),
            "require_resolution": policy.dependencies.require_resolution,
            "max_depth": policy.dependencies.max_depth,
            "require_pinned_constraint": policy.dependencies.require_pinned_constraint,
        },
        "mcp": {
            "allow": _opt_list(policy.mcp.allow),
            "deny": list(policy.mcp.deny),
            "transport": {
                "allow": _opt_list(policy.mcp.transport.allow),
            },
            "self_defined": policy.mcp.self_defined,
            "trust_transitive": policy.mcp.trust_transitive,
        },
        "compilation": {
            "target": {
                "allow": _opt_list(policy.compilation.target.allow),
                "enforce": policy.compilation.target.enforce,
            },
            "strategy": {
                "enforce": policy.compilation.strategy.enforce,
            },
            "source_attribution": policy.compilation.source_attribution,
        },
        "manifest": {
            "required_fields": list(policy.manifest.required_fields),
            "scripts": policy.manifest.scripts,
            "content_types": policy.manifest.content_types,
            "require_explicit_includes": policy.manifest.require_explicit_includes,
        },
        "unmanaged_files": {
            "action": policy.unmanaged_files.action,
            "directories": _opt_list(policy.unmanaged_files.directories),
            "exclude": _opt_list(policy.unmanaged_files.exclude),
        },
        "registry_source": {
            "require": list(policy.registry_source.require),
            "allow_non_registry": policy.registry_source.allow_non_registry,
        },
        "security": {
            "audit": {
                "on_install": policy.security.audit.on_install,
                "external": _opt_list(policy.security.audit.external),
                "scanners": None
                if policy.security.audit.scanners is None
                else {
                    name: {"allow_args": governance.allow_args}
                    for name, governance in policy.security.audit.scanners
                },
                "fail_on_drift": policy.security.audit.fail_on_drift,
            },
            "integrity": {
                "require_hashes": policy.security.integrity.require_hashes,
            },
        },
        "executables": {
            "deny_all": policy.executables.deny_all,
            "deny": list(policy.executables.deny),
            "require": list(policy.executables.require),
            "recommend": list(policy.executables.recommend),
            "enforce": list(policy.executables.enforce),
        },
    }
    if policy.bin_deploy.deny_all or policy.bin_deploy.deny:
        serialized["bin_deploy"] = {
            "deny_all": policy.bin_deploy.deny_all,
            "deny": list(policy.bin_deploy.deny),
        }
    return serialized


def _serialize_policy(policy: ApmPolicy) -> str:
    """Serialize an ApmPolicy to deterministic YAML for caching."""
    return yaml.dump(
        _policy_to_dict(policy), default_flow_style=False, sort_keys=True
    )  # yaml-io-exempt


def _policy_fingerprint(serialized: str) -> str:
    """Compute a fingerprint of a serialized policy."""
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:32]


def _is_policy_empty(policy: ApmPolicy) -> bool:
    """Return True only when the policy has no actionable restrictions."""
    return not any(
        (
            policy.fetch_failure != "warn",
            policy.dependencies.effective_deny,
            policy.dependencies.allow is not None,
            policy.dependencies.effective_require,
            policy.dependencies.require_resolution != "project-wins",
            policy.dependencies.max_depth != 50,
            policy.dependencies.require_pinned_constraint,
            policy.mcp.deny,
            policy.mcp.allow is not None,
            policy.mcp.transport.allow is not None,
            policy.mcp.self_defined != "warn",
            policy.mcp.trust_transitive,
            policy.compilation.target.allow is not None,
            policy.compilation.target.enforce is not None,
            policy.compilation.strategy.enforce is not None,
            policy.compilation.source_attribution,
            policy.manifest.required_fields,
            policy.manifest.scripts != "allow",
            policy.manifest.content_types is not None,
            policy.manifest.require_explicit_includes,
            policy.unmanaged_files.effective_action != "ignore",
            policy.unmanaged_files.directories is not None,
            policy.unmanaged_files.exclude is not None,
            policy.registry_source.require,
            not policy.registry_source.allow_non_registry,
            policy.security.audit.on_install is not None,
            policy.security.audit.external is not None,
            policy.security.audit.scanners is not None,
            policy.security.audit.fail_on_drift,
            policy.security.integrity.require_hashes,
            policy.bin_deploy.deny_all,
            policy.bin_deploy.deny,
            policy.executables.deny_all,
            policy.executables.deny,
            policy.executables.require,
            policy.executables.recommend,
            policy.executables.enforce,
        )
    )


def _stale_fallback_or_error(
    cache_entry: _CacheEntry | None,
    fetch_error_msg: str,
    source_label: str,
    outcome_on_miss: str,
) -> PolicyFetchResult:
    """Return stale cache if available, otherwise error with given outcome."""
    if cache_entry is not None:
        return PolicyFetchResult(
            policy=cache_entry.policy,
            source=cache_entry.source,
            cached=True,
            cache_stale=True,
            cache_age_seconds=cache_entry.age_seconds,
            fetch_error=fetch_error_msg,
            outcome="cached_stale",
            warnings=cache_entry.warnings,
        )
    return PolicyFetchResult(
        error=fetch_error_msg,
        source=source_label,
        fetch_error=fetch_error_msg,
        outcome=outcome_on_miss,
    )


def _detect_garbage(
    content: str | None,
    identifier: str,
    source_label: str,
    cache_entry: _CacheEntry | None,
) -> PolicyFetchResult | None:
    """Detect garbage responses (200 OK with non-YAML body).

    Returns a PolicyFetchResult if the content is garbage (stale fallback
    or garbage_response outcome), or None if the content looks parseable.
    """
    if content is None:
        return None

    try:
        raw_data = load_yaml_str(content)
    except yaml.YAMLError:
        msg = f"Response from {identifier} is not valid YAML"
        if cache_entry is not None:
            return PolicyFetchResult(
                policy=cache_entry.policy,
                source=cache_entry.source,
                cached=True,
                cache_stale=True,
                cache_age_seconds=cache_entry.age_seconds,
                fetch_error=msg,
                outcome="cached_stale",
                warnings=cache_entry.warnings,
            )
        return PolicyFetchResult(
            error=msg + " (possible captive portal or redirect)",
            source=source_label,
            fetch_error=msg,
            outcome="garbage_response",
        )

    if raw_data is not None and not isinstance(raw_data, dict):
        msg = f"Response from {identifier} is not a YAML mapping"
        if cache_entry is not None:
            return PolicyFetchResult(
                policy=cache_entry.policy,
                source=cache_entry.source,
                cached=True,
                cache_stale=True,
                cache_age_seconds=cache_entry.age_seconds,
                fetch_error=msg,
                outcome="cached_stale",
                warnings=cache_entry.warnings,
            )
        return PolicyFetchResult(
            error=msg,
            source=source_label,
            fetch_error=msg,
            outcome="garbage_response",
        )

    return None  # Not garbage -- proceed with normal parsing


def _read_cache_entry(
    repo_ref: str,
    project_root: Path,
    ttl: int | None = None,
    *,
    expected_hash: str | None = None,
) -> _CacheEntry | None:
    """Read cache entry with stale-awareness.

    Returns:
    * ``_CacheEntry(stale=False)`` -- within TTL, ready for immediate use
    * ``_CacheEntry(stale=True)``  -- past TTL but within MAX_STALE_TTL
    * ``None``                     -- no cache file, corrupt, past MAX_STALE_TTL,
                                       or pin verification failure (#827).

    When *expected_hash* is provided the cached ``raw_bytes_hash`` is
    compared against it; a mismatch invalidates the cache entry so the
    caller falls through to a fresh fetch where the pin can be verified
    against authoritative bytes off the wire.

    When *ttl* is omitted, staleness follows the cached effective policy's
    ``cache.ttl``. Callers may pass an explicit TTL for diagnostics or tests.
    """
    cache_dir = _get_cache_dir(project_root)
    key = _cache_key(repo_ref)
    policy_file = cache_dir / f"{key}.yml"
    meta_file = cache_dir / f"{key}.meta.json"

    if not policy_file.exists() or not meta_file.exists():
        return None

    try:
        meta = json.loads(meta_file.read_text(encoding="utf-8"))

        # Schema version check -- auto-invalidate on format change
        if meta.get("schema_version") != CACHE_SCHEMA_VERSION:
            return None

        cached_at = meta.get("cached_at", 0)
        age = int(time.time() - cached_at)

        if age > MAX_STALE_TTL:
            return None  # Past MAX_STALE_TTL, unusable

        raw_bytes_hash = meta.get("raw_bytes_hash", "") or ""

        # Pin verification (#827): if the project pinned a hash and the
        # cache was written without one (legacy entry) or with a different
        # one, ignore the cache so the fetcher can verify the pin against
        # fresh authoritative bytes.
        if expected_hash is not None:
            try:
                exp_algo, exp_hex = _split_hash_pin(expected_hash)
                expected_norm = f"{exp_algo}:{exp_hex}"
            except ProjectPolicyConfigError:
                return None
            if raw_bytes_hash.lower() != expected_norm:
                return None

        # The sidecar preserves warnings from the authored cold input. Parser
        # warnings from canonical cache YAML are intentionally not user-facing.
        policy, _warnings = load_policy(policy_file)
        effective_ttl = policy.cache.ttl if ttl is None else ttl
        cached_warnings = meta.get("warnings", [])
        if not isinstance(cached_warnings, list):
            cached_warnings = []
        else:
            cached_warnings = [str(warning) for warning in cached_warnings]

        # Determine source label
        if repo_ref.startswith("http://") or repo_ref.startswith("https://"):
            source = f"url:{repo_ref}"
        else:
            source = f"org:{repo_ref}"

        return _CacheEntry(
            policy=policy,
            source=source,
            age_seconds=age,
            stale=age > effective_ttl,
            chain_refs=meta.get("chain_refs", [repo_ref]),
            warnings=cached_warnings,
            fingerprint=meta.get("fingerprint", ""),
            raw_bytes_hash=raw_bytes_hash,
        )
    except Exception:
        return None


def _read_cache(
    repo_ref: str,
    project_root: Path,
    ttl: int | None = None,
) -> PolicyFetchResult | None:
    """Read policy from cache if still valid (within TTL).

    Legacy wrapper around ``_read_cache_entry`` for backward compatibility.
    The cached effective policy's TTL applies unless *ttl* is explicit.
    Returns None if cache miss, expired, or past MAX_STALE_TTL.
    """
    entry = _read_cache_entry(repo_ref, project_root, ttl=ttl)
    if entry is None or entry.stale:
        return None
    outcome = "empty" if _is_policy_empty(entry.policy) else "found"
    return PolicyFetchResult(
        policy=entry.policy,
        source=entry.source,
        cached=True,
        cache_age_seconds=entry.age_seconds,
        outcome=outcome,
        warnings=entry.warnings,
    )


def _write_cache(
    repo_ref: str,
    policy: ApmPolicy,
    project_root: Path,
    *,
    chain_refs: list[str] | None = None,
    raw_bytes_hash: str | None = None,
    warnings: list[str] | None = None,
) -> None:
    """Write merged effective policy and metadata to cache atomically.

    Uses temp file + ``os.replace()`` to prevent torn writes from parallel
    installs.  Both the policy file and metadata sidecar are written
    atomically and independently.

    The optional ``raw_bytes_hash`` (canonical ``"<algo>:<hex>"``) is the
    digest of the leaf bytes off the wire and is persisted to the meta
    sidecar so subsequent cached reads can verify against the project's
    pin without re-fetching (#827).
    """
    cache_dir = _get_cache_dir(project_root)
    cache_dir.mkdir(parents=True, exist_ok=True)

    key = _cache_key(repo_ref)
    policy_file = cache_dir / f"{key}.yml"
    meta_file = cache_dir / f"{key}.meta.json"

    serialized = _serialize_policy(policy)
    fingerprint = _policy_fingerprint(serialized)

    # Unique tmp suffix to avoid collisions from parallel writers
    uid = f"{os.getpid()}.{threading.get_ident()}"

    # Atomic write: policy file
    tmp_policy = cache_dir / f"{key}.{uid}.yml.tmp"
    try:
        tmp_policy.write_text(serialized, encoding="utf-8")
        os.replace(str(tmp_policy), str(policy_file))
    except OSError:
        # Best-effort cleanup
        try:  # noqa: SIM105
            tmp_policy.unlink(missing_ok=True)
        except OSError:
            pass
        return

    # Atomic write: metadata sidecar
    meta = {
        "repo_ref": repo_ref,
        "cached_at": time.time(),
        "chain_refs": chain_refs if chain_refs is not None else [repo_ref],
        "warnings": warnings or [],
        "schema_version": CACHE_SCHEMA_VERSION,
        "fingerprint": fingerprint,
        "raw_bytes_hash": raw_bytes_hash or "",
    }
    tmp_meta = cache_dir / f"{key}.{uid}.meta.json.tmp"
    try:
        tmp_meta.write_text(json.dumps(meta), encoding="utf-8")
        os.replace(str(tmp_meta), str(meta_file))
    except OSError:
        try:  # noqa: SIM105
            tmp_meta.unlink(missing_ok=True)
        except OSError:
            pass
