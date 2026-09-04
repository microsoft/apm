"""Validated transport identity for marketplace registration sources.

This module is the only marketplace-layer owner of source syntax.  It keeps
the persisted fetch transport distinct from the normalized host and port used
for comparison and host classification.
"""

from __future__ import annotations

import os.path
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from apm_cli.cache.url_normalize import SCP_LIKE_RE
from apm_cli.utils.github_host import default_host, is_valid_fqdn, validate_ssh_user
from apm_cli.utils.path_security import decode_url_path_segments, validate_path_segments

_SCP_PREFIX_RE = re.compile(r"^[^/@:\s]+@(?:\[[^\]]+\]|[^/:\s]+):")


@dataclass(frozen=True)
class MarketplaceSourceIdentity:
    """A validated marketplace source with fetch and comparison identities."""

    url: str
    kind: str
    host: str | None
    port: int | None
    transport: str


def parse_marketplace_source(
    source: str, host_flag: str | None = None
) -> MarketplaceSourceIdentity:
    """Parse and validate a marketplace source before host classification.

    The returned URL is safe to persist and pass to Git.  For explicit SSH,
    it preserves the validated user, bracketed host, port, path case, and
    optional ``.git`` suffix exactly as entered, except that ``ssh`` is
    lowercased as required by URL scheme comparison.
    """
    raw = (source or "").strip()
    if not raw:
        raise ValueError("Empty source argument")
    if any(ord(char) < 32 for char in raw):
        raise ValueError("Source argument contains invalid control characters")
    if host_flag is not None and not is_valid_fqdn(host_flag.strip().lower()):
        raise ValueError(
            f"Invalid host: '{host_flag}'. Expected a valid host FQDN (for example, 'github.com')."
        )

    if _looks_like_local_marketplace_source(raw):
        url = raw if raw.lower().startswith("file://") else f"file://{_expand_local_path(raw)}"
        return MarketplaceSourceIdentity(url, "local", None, None, "local")

    lowered = raw.lower()
    if lowered.startswith("http://"):
        raise ValueError("Insecure HTTP URL rejected. Use HTTPS for marketplace registration.")
    if lowered.startswith("ssh://"):
        return _parse_ssh_url(raw, host_flag)

    if "?" in raw and _SCP_PREFIX_RE.match(raw):
        raise ValueError("SSH URLs cannot include queries; remove the query and use --ref REF.")

    scp_match = SCP_LIKE_RE.fullmatch(raw)
    if scp_match:
        if "#" in raw:
            raise ValueError("SSH URL fragments are not supported; use --ref REF instead.")
        user = scp_match.group("user")
        host = scp_match.group("host")
        path = scp_match.group("path")
        validate_ssh_user(user)
        _validate_remote_path(path, context="marketplace SSH path")
        normalized_host = _comparison_host(host)
        return MarketplaceSourceIdentity(
            raw,
            "git",
            normalized_host,
            None,
            "scp",
        )

    if lowered.startswith("https://"):
        return _parse_https_url(raw, host_flag)

    return _parse_shorthand(raw, host_flag)


def _parse_ssh_url(raw: str, host_flag: str | None) -> MarketplaceSourceIdentity:
    """Validate a fully qualified SSH URL without rewriting its transport."""
    normalized_url = f"ssh://{raw[len('ssh://') :]}"
    authority = _authority(normalized_url)
    if authority.count("@") > 1:
        raise ValueError("SSH URL has invalid userinfo")
    if "@" in authority:
        userinfo = authority.split("@", 1)[0]
        if "%" in userinfo:
            raise ValueError("Percent-encoded characters are not allowed in SSH userinfo")
        if ":" in userinfo:
            raise ValueError(
                "SSH URL must not include a password; configure SSH authentication instead."
            )
    if "%" in authority.rsplit("@", 1)[-1]:
        raise ValueError("Percent-encoded characters are not allowed in SSH hosts or ports")

    try:
        parsed = urlsplit(normalized_url)
        host = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ValueError("SSH URL has an invalid host or port") from exc

    if not host:
        raise ValueError("SSH URL is missing a host")
    if parsed.query:
        raise ValueError("SSH URL must not include a query")
    if parsed.fragment:
        raise ValueError("SSH URL fragments are not supported; use --ref REF instead.")
    if parsed.password is not None:
        raise ValueError(
            "SSH URL must not include a password; configure SSH authentication instead."
        )
    if parsed.username:
        validate_ssh_user(parsed.username)
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("SSH URL has an invalid port")

    _validate_remote_path(
        parsed.path,
        context="marketplace SSH URL path",
        require_absolute=True,
    )
    normalized_host = _comparison_host(host)
    return MarketplaceSourceIdentity(
        normalized_url,
        "git",
        normalized_host,
        port,
        "ssh",
    )


def _parse_https_url(raw: str, host_flag: str | None) -> MarketplaceSourceIdentity:
    """Validate an HTTPS source, then classify the already-parsed host."""
    try:
        parsed = urlsplit(raw)
        host = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ValueError("HTTPS URL has an invalid host or port") from exc
    if not host:
        raise ValueError("HTTPS URL is missing a host")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("HTTPS marketplace URLs must not include userinfo")

    _validate_remote_path(parsed.path, context="marketplace URL path", require_absolute=True)
    normalized_host = _comparison_host(host)
    _reject_conflicting_host(host_flag, normalized_host, raw)
    kind = _kind_for_host(normalized_host, port)
    path_segments = [segment for segment in parsed.path.split("/") if segment]
    if kind in {"github", "gitlab"} and len(path_segments) < 2:
        raise ValueError("Invalid marketplace URL: expected an OWNER/REPO path")
    return MarketplaceSourceIdentity(raw, kind, normalized_host, port, "https")


def _parse_shorthand(raw: str, host_flag: str | None) -> MarketplaceSourceIdentity:
    """Build a validated HTTPS identity from shorthand source syntax."""
    segments = list(decode_url_path_segments(raw, context="marketplace shorthand"))
    if len(segments) < 2:
        raise ValueError(
            "Invalid format. Expected OWNER/REPO, HOST/OWNER/REPO, a full HTTPS URL, "
            "a local path, or an SSH URL."
        )

    embedded_host: str | None = None
    if is_valid_fqdn(segments[0]):
        if len(segments) < 3:
            raise ValueError("Invalid format: HOST shorthand requires HOST/OWNER/REPO")
        embedded_host = segments.pop(0).lower()

    repo = segments[-1]
    owner = "/".join(segments[:-1])
    validate_path_segments(owner, context="marketplace owner path", reject_empty=True)
    validate_path_segments(repo, context="marketplace repo name", reject_empty=True)
    _reject_conflicting_host(host_flag, embedded_host, raw)

    resolved_host = (host_flag or "").strip().lower() or embedded_host or default_host()
    kind = _kind_for_host(resolved_host, None)
    return MarketplaceSourceIdentity(
        f"https://{resolved_host}/{owner}/{repo}",
        kind,
        resolved_host,
        None,
        "https",
    )


def _kind_for_host(host: str, port: int | None) -> str:
    """Classify a syntactically validated host through the auth owner."""
    from apm_cli.core.auth import AuthResolver

    host_kind = AuthResolver.classify_host(host, port=port).kind
    if host_kind in {"github", "ghe_cloud", "ghes"}:
        return "github"
    if host_kind == "gitlab":
        return "gitlab"
    if host_kind == "ado":
        return "ado"
    return "git"


def _validate_remote_path(path: str, *, context: str, require_absolute: bool = False) -> None:
    """Reject malformed encodings and path forms that alter Git's target."""
    if not path or path == "/":
        raise ValueError(f"{context} is missing a repo path")
    if require_absolute:
        if not path.startswith("/") or path.startswith("//"):
            raise ValueError(f"{context} must begin with one repository path separator")
    decoded_segments = decode_url_path_segments(path, context=context)
    validate_path_segments(
        "/".join(decoded_segments),
        context=context,
        reject_empty=True,
    )


def _authority(url: str) -> str:
    """Return the raw authority section of a syntactically SSH-shaped URL."""
    remainder = url[len("ssh://") :]
    return re.split(r"[/?#]", remainder, maxsplit=1)[0]


def _comparison_host(host: str) -> str:
    """Normalize only the host comparison identity, not the stored transport."""
    return host.strip().lower()


def _reject_conflicting_host(host_flag: str | None, embedded_host: str | None, source: str) -> None:
    if host_flag and embedded_host and host_flag.strip().lower() != embedded_host:
        import shlex

        raise ValueError(
            "Conflicting host: --host does not match the host embedded in SOURCE.\n"
            f"To fix: drop --host and run: apm marketplace add {shlex.quote(source)}"
        )


def _looks_like_local_marketplace_source(raw: str) -> bool:
    """Return whether *raw* is a local filesystem source."""
    if raw.lower().startswith("file://"):
        return True
    if (
        raw.startswith(("/", "./", "../", "~/", ".\\", "..\\", "~\\"))
        or raw == "~"
        or (raw.startswith(".") and ("/" in raw or "\\" in raw))
    ):
        return True
    return len(raw) >= 3 and raw[0].isalpha() and raw[1] == ":" and raw[2] in ("\\", "/")


def _expand_local_path(raw: str) -> str:
    """Expand a local path without resolving symlinks."""
    return os.path.abspath(os.path.expanduser(raw))
