"""Leaf helpers shared by :mod:`reference` and its parse/url/shorthand mixins.

These helpers depend on nothing inside ``reference.py``, so importing them
here (instead of from ``reference``) keeps the mixin modules free of a circular
import back to the composed :class:`DependencyReference`.  ``reference.py``
re-exports the public-ish names so existing
``apm_cli.models.dependency.reference.NAME`` references keep resolving.
"""

import re

# Default ports per URI scheme -- used to normalise away redundant
# explicit ports (e.g. https://host:443/...) so that lockfile keys
# and error messages stay consistent regardless of how the user
# spelled the URL.
_DEFAULT_SCHEME_PORTS: dict[str, int] = {"https": 443, "http": 80, "ssh": 22}

# Allowed character set for a single repository path segment.
#
# ADO accepts spaces (project / repo names can contain them) but NOT tilde --
# tilde has no meaning on Azure DevOps URLs and keeping it out preserves the
# asymmetry that protects the ADO surface from inadvertent regressions.
#
# Non-ADO hosts accept tilde because Bitbucket Data Center / Server (and
# Sourcehut) use ``~username`` path segments for personal repositories
# (e.g. ``/scm/~jdoe/repo.git``). ``~`` is RFC 3986 unreserved, has no
# POSIX path-traversal meaning, and all subprocess calls in APM use
# list-form ``argv`` so there is no shell-expansion vector.
_ADO_PATH_SEGMENT_RE = r"^[a-zA-Z0-9._\- ]+$"
_NON_ADO_PATH_SEGMENT_RE = r"^[a-zA-Z0-9._~-]+$"
_REF_VERSION_SUFFIX_RE = re.compile(r"^v?\d+(?:\.\d+)*(?:[-+][A-Za-z0-9][A-Za-z0-9._-]*)?$")

_RANGE_PREFIX_RE = re.compile(r"^(>=|<=|>|<|\^|~|=)")


def _path_segment_pattern(is_ado_host: bool) -> str:
    """Return the allowed-character regex for a single repo path segment."""
    return _ADO_PATH_SEGMENT_RE if is_ado_host else _NON_ADO_PATH_SEGMENT_RE


def _is_valid_registry_semver_range(spec: str) -> bool:
    """Defer importing ``deps.registry`` until call time (avoids import cycles)."""
    from ...deps.registry.semver import is_semver_range

    return is_semver_range(spec)


class InvalidSemverRangeError(ValueError):
    """Raised when a ref starts like a semver range but is invalid."""


def _looks_like_invalid_semver_range(spec: str) -> bool:
    """Return whether *spec* starts like a semver range but is invalid."""
    return bool(_RANGE_PREFIX_RE.match(spec.strip()))
