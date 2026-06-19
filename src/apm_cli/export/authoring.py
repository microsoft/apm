"""Authoring-path license nudge (issue #1777, U6 / design D3b).

When an author packs or publishes their OWN package and the ``apm.yml`` declares
no ``license:``, APM emits a single actionable WARNING: the SBOM will record
NOASSERTION for this package until a license is declared. This mirrors npm's
"No license field" nudge.

ASYMMETRY (mandatory): this fires ONLY on the authoring path (pack/publish on
the author's own manifest). The consuming path (install/export of other people's
dependencies) stays SILENT -- APM never nags per-install about transitive deps.

This NEVER blocks: a missing or malformed manifest simply yields no warning.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from apm_cli.utils.yaml_io import load_yaml

# Actionable, ASCII-only. ``[!]`` is the warning status symbol; callers that
# route through CommandLogger.warning already prefix their own symbol, so the
# message text itself stays symbol-free and the logger owns presentation.
_WARN_MESSAGE = (
    "No 'license:' field in apm.yml; the SBOM will record NOASSERTION for this "
    "package. Add a 'license:' field to apm.yml (an SPDX expression such as "
    "MIT or Apache-2.0, or UNLICENSED) to declare it."
)


def warn_if_license_undeclared(
    apm_yml_path: Path | str,
    emit_warning: Callable[[str], None],
) -> bool:
    """Emit an authoring nudge when *apm_yml_path* declares no license.

    Returns ``True`` when a warning was emitted, ``False`` otherwise. Never
    raises and never blocks -- a missing/unreadable manifest yields no warning.
    """
    path = Path(apm_yml_path)
    if not path.is_file():
        return False
    try:
        data = load_yaml(path)
    except Exception:
        return False
    declared = data.get("license") if isinstance(data, dict) else None
    if isinstance(declared, str) and declared.strip():
        return False
    emit_warning(_WARN_MESSAGE)
    return True


__all__ = ["warn_if_license_undeclared"]
