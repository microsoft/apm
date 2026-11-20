"""Name both remedies for a lockfile version this APM cannot read (#4233, req-lk-004)."""

from __future__ import annotations

import re
from pathlib import Path

_VERSION = re.compile(r"Unsupported lockfile version (.+?);")


def with_version_remedy(exc: Exception, path: Path) -> Exception:
    """Return *exc* naming both remedies for *path*: regenerate it, or upgrade APM.

    Any other lockfile error, or one that already names its remedies, is
    returned unchanged. The new error carries ``version`` and the structured
    ``remediations`` for machine-readable reports.
    """
    from ..core import remediation
    from ..core.scope import is_user_scope_root
    from .lockfile import UnsupportedLockfileVersionError

    if not isinstance(exc, UnsupportedLockfileVersionError) or getattr(exc, "remediations", ()):
        return exc
    match = _VERSION.search(str(exc))
    version = match.group(1) if match else "'unknown'"
    regen = remediation.LOCKFILE_VERSION_UNSUPPORTED.bind(lockfile=path.name)
    if is_user_scope_root(path.parent):
        regen = regen.with_command("apm lock -g")
    upgrade = remediation.LOCKFILE_VERSION_UPGRADE_APM.bind(version=version)
    error = type(exc)(f"{exc}. {remediation.either_sentence(regen, upgrade)}")
    error.version = version.strip("'")
    error.remediations = (regen, upgrade)
    error.__cause__ = exc
    return error
