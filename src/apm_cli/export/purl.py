"""Package URL (purl) identity + URL credential scrubbing for SBOM export.

Component identity is derived ONLY from lockfile-recorded fields. Export never
re-resolves, re-hashes, or touches the network -- the purl reflects exactly
what the lockfile recorded, nothing more.

purl scheme (per design D3):

* git deps          -> ``pkg:<host>/<owner>/<repo>@<resolved_commit>``
* OCI registry deps -> ``pkg:oci/<name>@<digest>``
* local / generic   -> ``pkg:generic/<name>@<content_hash>`` (version omitted
                       when no hash was recorded)

Credentials embedded in any recorded URL are scrubbed before the URL appears in
SBOM output -- a token must never leak through provenance metadata.
"""

from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

from apm_cli.deps.lockfile import LockedDependency

# host_type values that have a dedicated purl type. Anything else falls back to
# ``generic`` -- honest about identity rather than asserting a forge we can't
# vouch for.
_HOST_TYPE_TO_PURL = {
    "github": "github",
    "gitlab": "gitlab",
    "bitbucket": "bitbucket",
}

# Canonical forge domains. github is the implicit default and is NOT persisted
# as a lockfile host_type (only gitlab is), so the forge for a loaded dep is
# inferred from the leading host segment of the recorded repo_url.
_HOST_DOMAIN_TO_PURL = {
    "github.com": "github",
    "gitlab.com": "gitlab",
    "bitbucket.org": "bitbucket",
}


def _host_segment(repo_url: str) -> str:
    """Return the leading host segment of ``repo_url`` (``""`` when host-less).

    ``github.com/acme/git-utils`` -> ``github.com``;
    ``acme/git-utils`` -> ``""``.
    """
    parts = [p for p in repo_url.split("/") if p]
    if parts and "." in parts[0]:
        return parts[0].lower()
    return ""


def _purl_type_for(dep: LockedDependency) -> str | None:
    """Resolve the forge purl type from host_type, else the repo_url domain."""
    explicit = _HOST_TYPE_TO_PURL.get((dep.host_type or "").lower())
    if explicit:
        return explicit
    return _HOST_DOMAIN_TO_PURL.get(_host_segment(dep.repo_url))


def _owner_repo(repo_url: str) -> str:
    """Strip a leading host segment from ``repo_url`` -> ``<owner>/<repo>``.

    ``github.com/acme/git-utils`` -> ``acme/git-utils``;
    ``acme/git-utils`` -> ``acme/git-utils`` (already host-less).
    """
    parts = [p for p in repo_url.split("/") if p]
    if parts and "." in parts[0]:
        parts = parts[1:]
    return "/".join(parts)


def _basename(repo_url: str) -> str:
    """Last path segment of ``repo_url`` -- the package's short name."""
    owner_repo = _owner_repo(repo_url)
    return owner_repo.split("/")[-1] if owner_repo else repo_url


def scrub_url(url: str) -> str:
    """Remove embedded credentials from *url* before it appears in SBOM output.

    Strips BOTH userinfo (``user:pass@``) and the entire query string. Query
    parameters carry no provenance value for an inventory export and are a known
    credential-leak vector (``?access_token=``, ``?token=``, SAS ``?sig=``...),
    so they are dropped wholesale rather than allow-listed. Scheme, host, port,
    path, and fragment are preserved verbatim. Returns the URL unchanged when it
    carries neither userinfo nor a query string.
    """
    if not url:
        return url
    parts = urlsplit(url)
    has_userinfo = "@" in parts.netloc
    if not has_userinfo and not parts.query:
        return url
    if has_userinfo:
        netloc = parts.hostname or ""
        if parts.port is not None:
            netloc = f"{netloc}:{parts.port}"
    else:
        netloc = parts.netloc
    return urlunsplit((parts.scheme, netloc, parts.path, "", parts.fragment))


def _is_oci(dep: LockedDependency) -> bool:
    return bool(dep.resolved_url and dep.resolved_url.startswith("oci://"))


def _is_local(dep: LockedDependency) -> bool:
    return dep.source == "local"


def build_purl(dep: LockedDependency) -> str:
    """Build the Package URL identity for *dep* from lockfile fields only."""
    if _is_oci(dep):
        name = _basename(dep.repo_url)
        digest = dep.resolved_hash or dep.content_hash
        return f"pkg:oci/{name}@{digest}" if digest else f"pkg:oci/{name}"

    if not _is_local(dep) and dep.resolved_commit:
        purl_type = _purl_type_for(dep)
        if purl_type:
            return f"pkg:{purl_type}/{_owner_repo(dep.repo_url)}@{dep.resolved_commit}"
        # Unknown forge: stay honest with a generic identity keyed on the commit.
        return f"pkg:generic/{_basename(dep.repo_url)}@{dep.resolved_commit}"

    # Local / primitive / hash-only identity.
    name = _basename(dep.repo_url)
    version = dep.content_hash
    return f"pkg:generic/{name}@{version}" if version else f"pkg:generic/{name}"


__all__ = ["build_purl", "scrub_url"]
