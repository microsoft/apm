"""Name both remedies for a lockfile version this APM cannot read (#4233, req-lk-004)."""

from __future__ import annotations

import re
from pathlib import Path

_VERSION = re.compile(r"Unsupported lockfile version (.+?);")
_UNPARSEABLE = "Invalid lockfile YAML:"


def with_version_remedy(exc: Exception, path: Path) -> Exception:
    """Return *exc* naming both remedies for *path*: regenerate it, or upgrade APM.

    Any other lockfile error, or one that already names its remedies, is
    returned unchanged. The new error carries ``version`` and the structured
    ``remediations`` for machine-readable reports.
    """
    from ..core import remediation
    from ..core.scope import is_user_scope_root
    from .lockfile import UnsupportedLockfileVersionError

    if str(exc).startswith(_UNPARSEABLE):
        return _with_regenerate_remedy(exc, path)
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


def _with_regenerate_remedy(exc: Exception, path: Path) -> Exception:
    """Return *exc* ending with how to regenerate *path*, whose YAML did not parse."""
    from ..core import remediation
    from ..core.scope import get_source_root_override, is_user_scope_root

    if get_source_root_override() is not None:
        # Under `apm install --root` a bare `apm install` would act on the source repo.
        deploy_dir = path.resolve().parent
        regen = remediation.LOCKFILE_UNREADABLE.bind(lockfile=path.resolve())
        regen = regen.with_command(f"apm install --root {deploy_dir}")
    else:
        regen = remediation.LOCKFILE_UNREADABLE.bind(lockfile=path.name)
        if is_user_scope_root(path.parent):
            regen = regen.with_command("apm install -g")
    error = type(exc)(f"{exc}\n{regen.sentence()}")
    error.__cause__ = exc
    return error
