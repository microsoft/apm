"""Turn an ``apm marketplace add`` source form into a validated registration.

Single owner of the source-to-registration step shared by
``apm marketplace add`` and the user-manifest ``marketplaces:`` reconcile
run by ``apm install -g``. Both paths parse the same SOURCE grammar, probe
the same ``marketplace.json`` locations and fetch the manifest before
anything is written to the registry.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from .errors import MarketplaceError
from .models import MarketplaceManifest, MarketplaceSource, url_names_remote_manifest

# A marketplace alias must satisfy this pattern so it can appear on the right
# of ``@`` in ``apm install <plugin>@<marketplace>`` syntax.
ALIAS_PATTERN = re.compile(r"^[a-zA-Z0-9._-]+$")

PROBED_MANIFEST_PATHS = (
    "marketplace.json",
    ".github/plugin/marketplace.json",
    ".claude-plugin/marketplace.json",
)


# Lives beside PROBED_MANIFEST_PATHS, which its message lists; errors.py would
# have to import this module back to build it.
class MarketplaceManifestNotFoundError(MarketplaceError):
    """Raised when no ``marketplace.json`` exists at any probed location."""

    def __init__(self, display_source: str):
        self.display_source = display_source
        super().__init__(
            f"No marketplace.json found in '{display_source}'. "
            f"Checked: {', '.join(PROBED_MANIFEST_PATHS)}"
        )


@dataclass(frozen=True)
class ParsedSource:
    """A SOURCE argument parsed through the canonical marketplace parser."""

    url: str
    kind: str
    host: str | None
    ref: str
    fragment_ref: str
    is_direct_url: bool


def is_valid_alias(value: str) -> bool:
    """Return True when ``value`` is a legal marketplace alias."""
    return bool(value) and ALIAS_PATTERN.match(value) is not None


def split_source_fragment_ref(source: str) -> tuple[str, str]:
    """Split an HTTPS git URL #ref fragment from the URL stored in the registry."""
    raw = (source or "").strip()
    if raw.lower().startswith("ssh://") and "#" in raw:
        raise ValueError("SSH URL fragments are not supported; use --ref REF instead.")
    if not raw.lower().startswith("https://"):
        return raw, ""
    parsed = urlsplit(raw)
    if not parsed.fragment:
        return raw, ""
    clean_url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))
    return clean_url, parsed.fragment


def parse_registration_source(
    source: str, *, ref: str | None = None, host: str | None = None
) -> ParsedSource:
    """Parse SOURCE plus an optional explicit ref exactly like ``marketplace add``.

    Raises:
        ValueError: The source is malformed or combines a ``#ref`` fragment
            with an explicit ref.
        PathTraversalError: The source contains a traversal sequence.
    """
    from .source_identity import parse_marketplace_source

    source_arg, fragment_ref = split_source_fragment_ref(source)
    if fragment_ref and ref is not None:
        raise ValueError(
            "Do not combine a git URL #ref with --ref or --branch. Use one ref source."
        )
    identity = parse_marketplace_source(source_arg, host)
    is_direct_url = url_names_remote_manifest(identity.url)
    return ParsedSource(
        url=identity.url,
        kind=identity.kind,
        host=identity.host,
        ref="" if is_direct_url else (fragment_ref or ref or "main"),
        fragment_ref=fragment_ref,
        is_direct_url=is_direct_url,
    )


def local_source_points_to_file(source: MarketplaceSource) -> bool:
    """Return True when a local marketplace source points directly to a file."""
    if source.kind != "local":
        return False
    try:
        return Path(source.local_path).expanduser().is_file()
    except OSError:
        return False


def probe_registration(
    name: str, url: str, ref: str, *, is_direct_url: bool
) -> tuple[MarketplaceSource, MarketplaceManifest]:
    """Locate and fetch the marketplace manifest behind a parsed source.

    Returns the source to register (with the detected manifest path) and
    the fetched manifest.

    Raises:
        MarketplaceManifestNotFoundError: No manifest at any probed path.
        MarketplaceError: Fetching or parsing the manifest failed.
    """
    from . import client

    effective_ref = "" if is_direct_url else ref
    probe_source = MarketplaceSource(
        name=name,
        url=url,
        ref=effective_ref,
        path="" if is_direct_url else "marketplace.json",
    )
    if is_direct_url or local_source_points_to_file(probe_source):
        detected_path: str | None = ""
        discovered_manifest = None
    else:
        detected_path, discovered_manifest = client._auto_detect_path_with_payload(probe_source)
    if detected_path is None:
        raise MarketplaceManifestNotFoundError(probe_source.display_source)

    fetch_source = MarketplaceSource(name=name, url=url, ref=effective_ref, path=detected_path)
    manifest = client.fetch_marketplace(
        fetch_source,
        force_refresh=True,
        # Reuse the catalog response the probe just fetched from the same
        # url/ref/path instead of re-fetching it (apm#3283).
        discovery_payload=discovered_manifest,
    )
    return fetch_source, manifest
