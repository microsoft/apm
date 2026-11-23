"""Call ``unpack_bundle`` with an accepting admission.

Unpacker mechanics tests (extraction, verification, path safety) are not
about org policy; ``apm unpack`` itself always passes the shared bundle
admission owner (``install/bundle_admission.py``).
"""

from __future__ import annotations

from typing import Any

from apm_cli.bundle.unpacker import UnpackCandidate, UnpackResult
from apm_cli.bundle.unpacker import unpack_bundle as _unpack_bundle


def admit_any(candidate: UnpackCandidate) -> None:
    """Accept every bundle."""
    del candidate


def unpack_bundle(*args: Any, **kwargs: Any) -> UnpackResult:
    """``unpack_bundle`` with :func:`admit_any` unless a test passes ``admit``."""
    kwargs.setdefault("admit", admit_any)
    return _unpack_bundle(*args, **kwargs)
