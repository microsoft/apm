"""sourceBase validation and URL-shape helpers for marketplace YAML parsing.

Extracted from _yml_parsers.py to keep that module under 800 lines.
All public names are re-exported from _yml_parsers.py so callers see no change.

Leaf module -- no imports from _yml_parsers (no circular risk).
"""

from __future__ import annotations

import re
import urllib.parse as _urlparse
from typing import Any

from ..utils.path_security import PathTraversalError, validate_path_segments
from .errors import MarketplaceYmlError

# ---------------------------------------------------------------------------
# Pattern fragments (duplicated here to keep this a leaf module)
# ---------------------------------------------------------------------------

_HOST_PAT = r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?\.)+[A-Za-z][A-Za-z0-9-]*"
_SEGMENT_PAT = r"[A-Za-z0-9._-]+"
_OWNER_REPO_PAT = rf"{_SEGMENT_PAT}/{_SEGMENT_PAT}"
_RELATIVE_SOURCE_PAT = rf"{_SEGMENT_PAT}(?:/{_SEGMENT_PAT})*"

# Compiled patterns exposed to _yml_parsers and yml_schema
SOURCE_BASE_RE = re.compile(rf"^https://{_HOST_PAT}/{_RELATIVE_SOURCE_PAT}$")
_RELATIVE_SOURCE_RE = re.compile(rf"^{_RELATIVE_SOURCE_PAT}$")

# Duplicated from _yml_parsers to keep this module self-contained
_SOURCE_RE = re.compile(
    r"^(?:"
    rf"https://{_HOST_PAT}/{_OWNER_REPO_PAT}(?:\.git)?"
    rf"|{_HOST_PAT}/{_OWNER_REPO_PAT}"
    rf"|{_OWNER_REPO_PAT}"
    r"|\./.*"
    r")$"
)
_LOCAL_SOURCE_RE = re.compile(r"^\./")


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def split_source_base(source_base: str) -> tuple[str, str]:
    """Split a ``parse_source_base``-validated value into host and path."""
    without_scheme = source_base.removeprefix("https://")
    host, path_prefix = without_scheme.split("/", 1)
    return host, path_prefix


def _source_error(ctx: str, source: str, *, source_base: str | None) -> MarketplaceYmlError:
    forms = [
        "'<owner>/<repo>'",
        "'<host.tld>/<owner>/<repo>'",
        "'https://<host.tld>/<owner>/<repo>[.git]'",
        "'./<path>'",
    ]
    if source_base is not None:
        forms.append("'<relative-path>' when sourceBase is set")
    return MarketplaceYmlError(f"'{ctx}' must be one of {', '.join(forms)}, got '{source}'")


def validate_source_value(
    source: str,
    *,
    context: str,
    source_base: str | None = None,
) -> None:
    """Validate a package ``source`` field shape and path safety."""
    matches_existing_shape = bool(_SOURCE_RE.match(source))
    if not matches_existing_shape:
        first_segment = source.split("/", 1)[0]
        looks_like_unsupported_host_override = "/" in source and bool(
            re.fullmatch(_HOST_PAT, first_segment)
        )
        matches_relative_source = bool(_RELATIVE_SOURCE_RE.match(source))
        if looks_like_unsupported_host_override:
            raise MarketplaceYmlError(
                f"'{context}' looks like a host-prefixed source but does not match "
                f"'<host.tld>/<owner>/<repo>'. Use a full HTTPS URL override "
                f"('https://...') or remove the host to compose onto sourceBase."
            )
        if source_base is None or not matches_relative_source:
            raise _source_error(context, source, source_base=source_base)
    is_local = bool(_LOCAL_SOURCE_RE.match(source))
    try:
        validate_path_segments(source, context=context, allow_current_dir=is_local)
    except PathTraversalError as exc:
        raise MarketplaceYmlError(str(exc)) from exc


def parse_source_base(raw: Any) -> str | None:
    """Parse and validate marketplace-level ``sourceBase``."""
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw.strip():
        raise MarketplaceYmlError("'sourceBase' must be a non-empty string")

    raw_source_base = raw.strip()
    if not raw_source_base.startswith("https://"):
        raise MarketplaceYmlError("'sourceBase' must start with https://")

    parsed = _urlparse.urlparse(raw_source_base)
    source_base = raw_source_base.rstrip("/")
    if parsed.username or parsed.password or "@" in parsed.netloc:
        raise MarketplaceYmlError("'sourceBase' must not include userinfo")
    if ":" in parsed.netloc:
        raise MarketplaceYmlError("'sourceBase' must not include a port")
    if parsed.query:
        raise MarketplaceYmlError("'sourceBase' must not include a query string")
    if parsed.fragment:
        raise MarketplaceYmlError("'sourceBase' must not include a fragment")
    if not parsed.hostname or not re.fullmatch(_HOST_PAT, parsed.hostname):
        raise MarketplaceYmlError("'sourceBase' host must be a FQDN")
    if source_base.endswith(".git"):
        raise MarketplaceYmlError("'sourceBase' must not end with .git")

    path = parsed.path.lstrip("/")
    if path.endswith("/"):
        path = path[:-1]
    if not path:
        raise MarketplaceYmlError("'sourceBase' must include at least one path segment")
    try:
        validate_path_segments(path, context="sourceBase", reject_empty=True)
    except PathTraversalError as exc:
        raise MarketplaceYmlError(str(exc)) from exc
    if not SOURCE_BASE_RE.match(source_base):
        raise MarketplaceYmlError(
            "'sourceBase' path segments may only contain letters, digits, dot, underscore, or hyphen"
        )
    return source_base
