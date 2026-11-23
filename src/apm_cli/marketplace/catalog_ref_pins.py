"""A marketplace catalog ref names one of its origin's own branches or tags.

``marketplace.allow`` judges a catalog by its registered origin, so the bytes
APM reads must be ones that origin publishes on a branch or tag. A ref in
another namespace (a review ref such as ``refs/pull/N/head``) or a commit no
branch or tag points to is refused, and a pin that cannot be verified fails
closed.
"""

from __future__ import annotations

import re

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
        "remotes",
        "stash",
    }
)
_COMMIT_PIN = re.compile(r"^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$")
_FORBIDDEN = re.compile(r"[\x00-\x20\x7f~^:?*\[\\]")


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
    from ..core.auth import AuthResolver
    from ..utils.git_env import git_remote_refs
    from .source_identity import parse_marketplace_source

    identity = parse_marketplace_source(source.url)
    host = identity.host or source.host
    resolver = AuthResolver()
    org = source.owner or None
    context = (
        resolver.resolve_for_remote(host, source.url, org, port=identity.port)
        if identity.port is not None
        else resolver.resolve_for_remote(host, source.url, org)
    )
    env = resolver.git_env_for_remote(context, source.url)
    result = git_remote_refs(source.url, "refs/heads/*", "refs/tags/*", env=env)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or "git ls-remote failed").strip())
    tips: dict[str, str] = {}
    for line in (result.stdout or "").splitlines():
        sha, _, name = line.partition("\t")
        if name.startswith(("refs/heads/", "refs/tags/")):
            tips[name] = sha.strip()
    return tips


def _is_ssh(source: MarketplaceSource) -> bool:
    from .source_identity import parse_marketplace_source

    try:
        return parse_marketplace_source(source.url).transport in ("ssh", "scp")
    except Exception:
        return False


def require_origin_pin(source: MarketplaceSource) -> None:
    """Refuse a catalog ref the origin does not publish, before any contents request.

    An empty ref reads the origin's default branch. A commit pin must be the
    tip of an origin branch or tag, and a short name that a review namespace
    could also match must exist as an origin branch or tag; when the ref
    listing cannot be read, the pin is refused.

    Raises:
        MarketplaceFetchError: The ref is malformed, outside the origin's
            branches and tags, or cannot be verified.
    """
    ref = source.ref or ""
    if not ref:
        return
    try:
        check_catalog_ref(ref)
    except ValueError as exc:
        raise MarketplaceFetchError(source.name, str(exc)) from exc
    commit_pin = is_commit_pin(ref)
    if not commit_pin and ref.split("/", 1)[0] not in REVIEW_NAMESPACES:
        return
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
    published = (
        ref.lower() in {sha.lower() for sha in tips.values()}
        if commit_pin
        else bool({f"refs/heads/{ref}", f"refs/tags/{ref}"} & set(tips))
    )
    if not published:
        raise MarketplaceFetchError(
            source.name,
            f"Catalog ref {ref!r} is not the tip of a branch or tag of the catalog origin. "
            "Pin a branch, a tag, or the commit one of them points to.",
        )
