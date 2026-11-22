"""Where a package's Copilot hook sources deploy: one clean file per source.

The single owner of the Copilot hook file name. :class:`HookIntegrator`
writes to these paths; the executables gate reads them, without writing, to
find files at a withheld package's hook path (:mod:`apm_cli.install.exec_gate`).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


def copilot_hook_target(hooks_dir: Path, package_name: str, hook_file: Any) -> Path:
    """The clean, package-namespaced file a Copilot hook source deploys to."""
    return hooks_dir / f"{package_name}-{Path(hook_file).stem}.json"


def copilot_hook_destinations(
    integrator: Any, package_info: Any, project_root: Path, target: Any = None
) -> list[Path]:
    """Every file ``integrate_package_hooks`` would write for *package_info*.

    Computed from the package's own hook sources through the read-only shared
    selector (no routing refusal); nothing is written.
    """
    package_name = integrator._get_package_name(package_info, project_root)
    hook_files = integrator.select_deployable_hook_sources(
        package_info.install_path, ("copilot",)
    ).descriptors_for("copilot")
    hooks_dir = project_root / (target.root_dir if target else ".github") / "hooks"
    return [copilot_hook_target(hooks_dir, package_name, f) for f in hook_files]
