"""A marketplace catalog ref names one of its origin's own branches or tags.

``marketplace.allow`` judges a catalog by its registered origin, so the bytes
APM reads must be ones that origin publishes on a branch or tag. A ref in
another namespace (a review ref such as ``refs/pull/N/head``) or a commit no
branch or tag points to is refused, and a pin that cannot be verified fails
closed.
"""

from __future__ import annotations

import re
import subprocess

from .errors import MarketplaceFetchError
from .models import MarketplaceSource

# First path segments Git also resolves a short ref name against (review, CI and
# remote-tracking namespaces): such a name must exist as an origin branch or tag.
REVIEW_NAMESPACES = frozenset(
    {
        "changes",
        "environments",
        "keep-around",
        "merge-requests",
        "notes",
        "pipelines",
        "pull",
        "pull-requests",
        "remotes",
        "stash",
    }
)
_COMMIT_PIN = re.compile(r"^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$")
_FORBIDDEN = re.compile(r"[\x00-\x20\x7f~^:?*\[\\]")


_ABBREVIATED = re.compile(r"[0-9a-fA-F]{7,63}")


def is_commit_pin(ref: str | None) -> bool:
    """Return whether *ref* is a full commit object name."""
    return bool(ref and _COMMIT_PIN.match(ref))


def check_catalog_ref(ref: str) -> str:
    """Return *ref* when it can only name a branch, tag or commit; raise otherwise.

    Raises:
        ValueError: *ref* is malformed, or names a ref outside the origin's
            branches and tags.
    """
    if not ref or _FORBIDDEN.search(ref) or ref.startswith(("-", "/")) or ref == "@":
        raise ValueError(f"Catalog ref {ref!r} is not a valid branch, tag or commit.")
    parts = ref.split("/")
    if (
        ".." in ref
        or "@{" in ref
        or ref.endswith((".", "/"))
        or any(not part or part.startswith(".") or part.endswith(".lock") for part in parts)
    ):
        raise ValueError(f"Catalog ref {ref!r} is not a valid branch, tag or commit.")
    if parts[0] == "refs" and (len(parts) < 3 or parts[1] not in ("heads", "tags")):
        raise ValueError(
            f"Catalog ref {ref!r} must name a branch or tag of the catalog origin, "
            "or a commit one of them points to."
        )
    return ref


def origin_ref_tips(source: MarketplaceSource) -> dict[str, str]:
    """Return ``{ref name: commit}`` for every branch and tag the origin advertises.

    Lists the origin with ``git ls-remote`` over the source's own transport
    and credentials. An annotated tag contributes both its tag object and the
    commit it peels to.

    Raises:
        RuntimeError: The listing failed.
    """
    from urllib.parse import urlsplit

    from ..core.auth import AuthResolver
    from .source_identity import parse_marketplace_source

    identity = parse_marketplace_source(source.url)
    host = identity.host or source.host
    resolver = AuthResolver()
    org = source.owner or None
    host_info = AuthResolver.classify_host(host, port=identity.port) if host else None
    if getattr(host_info, "kind", "") == "ado":
        # The same PAT -> bearer -> helper chain the catalog clone uses.
        result = resolver.try_with_fallback(
            host_info.host,
            lambda _token, env: _listed(source.url, env),
            org=org,
            path=urlsplit(source.url).path.lstrip("/"),
            unauth_first=False,
            base_env=resolver.hardened_git_base_env(),
            **({"port": identity.port} if identity.port is not None else {}),
        )
    else:
        context = (
            resolver.resolve_for_remote(host, source.url, org, port=identity.port)
            if identity.port is not None
            else resolver.resolve_for_remote(host, source.url, org)
        )
        result = _listed(source.url, resolver.git_env_for_remote(context, source.url))
    tips: dict[str, str] = {}
    for line in (result.stdout or "").splitlines():
        sha, _, name = line.partition("\t")
        if name.startswith(("refs/heads/", "refs/tags/")):
            tips[name] = sha.strip()
    return tips


def _listed(url: str, env) -> subprocess.CompletedProcess[str]:
    """List the origin's heads and tags; a failure keeps git's outcome for auth retries."""
    from ..utils.git_env import git_remote_refs

    result = git_remote_refs(url, "refs/heads/*", "refs/tags/*", env=env)
    if result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode, ["git", "ls-remote"], result.stdout, result.stderr
        )
    return result


def _is_ssh(source: MarketplaceSource) -> bool:
    from .source_identity import parse_marketplace_source

    try:
        return parse_marketplace_source(source.url).transport in ("ssh", "scp")
    except Exception:
        return False


def require_origin_pin(source: MarketplaceSource) -> str:
    """Refuse a catalog ref the origin does not publish, before any contents request.

    An empty ref reads the origin's default branch. A commit pin must be the
    tip of an origin branch or tag, and a short name that a review namespace
    could also match must exist as an origin branch or tag; when the ref
    listing cannot be read, the pin is refused.

    Returns the ref the contents request must use: the ref itself, or, for
    a short name a review namespace or a commit abbreviation could also
    match, the commit of the origin branch or tag it was verified as, so a
    revision-style lookup can never resolve it elsewhere.

    Raises:
        MarketplaceFetchError: The ref is malformed, outside the origin's
            branches and tags, or cannot be verified.
    """
    ref = source.ref or ""
    if not ref:
        return ref
    try:
        check_catalog_ref(ref)
    except ValueError as exc:
        raise MarketplaceFetchError(source.name, str(exc)) from exc
    commit_pin = is_commit_pin(ref)
    # An abbreviated commit could name any object a host resolves; it must be
    # a published branch or tag name, like a review-namespace short name.
    abbreviated = not commit_pin and bool(_ABBREVIATED.fullmatch(ref))
    if not commit_pin and not abbreviated and ref.split("/", 1)[0] not in REVIEW_NAMESPACES:
        return ref
    from ..deps.egress_route import EgressSurface, is_refused

    if is_refused(EgressSurface.SOURCE_ENUMERATION):
        raise MarketplaceFetchError(
            source.name,
            f"PROXY_REGISTRY_ONLY is set: catalog ref {ref!r} cannot be verified against "
            "the origin's branches and tags without contacting it. Pin a branch or tag name.",
        )
    try:
        tips = origin_ref_tips(source)
    except Exception as exc:
        from ..core.remediation import MARKETPLACE_GIT_ACCESS_FAILED

        detail = (
            "git ls-remote over SSH failed -- verify that your SSH key has read access "
            "to the repository"
            if _is_ssh(source)
            else str(exc)
        )
        raise MarketplaceFetchError(
            source.name,
            f"Cannot verify catalog ref {ref!r} against the origin's branches and tags: {detail}",
            retry_hint=MARKETPLACE_GIT_ACCESS_FAILED.bind(name=source.name).sentence(),
        ) from exc
    if commit_pin:
        published = ref.lower() in {sha.lower() for sha in tips.values()}
        bound = ref
    else:
        named = []
        if f"refs/heads/{ref}" in tips:
            named.append(tips[f"refs/heads/{ref}"].lower())
        if f"refs/tags/{ref}" in tips:
            # An annotated tag is bound to the commit it peels to.
            named.append(tips.get(f"refs/tags/{ref}^{{}}", tips[f"refs/tags/{ref}"]).lower())
        if len(named) > 1:
            raise MarketplaceFetchError(
                source.name,
                f"Catalog ref {ref!r} names both a branch and a tag of the catalog origin. "
                f"Use refs/heads/{ref} or refs/tags/{ref}.",
            )
        published = bool(named)
        bound = next(iter(named), ref)
    if not published:
        raise MarketplaceFetchError(
            source.name,
            f"Catalog ref {ref!r} is not the tip of a branch or tag of the catalog origin. "
            "Pin a branch, a tag, or the commit one of them points to.",
        )
    return bound
