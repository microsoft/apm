"""Package integration services.

The functions in this module own the *integration template* for a single
package -- looping over the resolved targets, dispatching primitives to their
integrators, accumulating counters, and recording deployed file paths.

Moved here from ``apm_cli.commands.install`` so that the install engine
package owns its own integration logic.  ``commands/install`` keeps thin
underscore-prefixed re-exports for backward compatibility with existing
``@patch`` sites and direct imports.

Design notes
------------
``integrate_local_content()`` calls ``integrate_package_primitives()`` via a
bare-name lookup so that ``@patch`` of either symbol on this module's
namespace intercepts both call paths consistently.  Both functions must
remain defined in this module for the mock.patch seam to work correctly.

Real implementations live in the sub-modules:

* ``deployed_path.py``  -- ``_deployed_path_entry``
* ``primitives.py``     -- ``integrate_package_primitives``
* ``local_bundle.py``   -- ``integrate_local_bundle``

This file is a thin orchestrator that re-exports those symbols and defines
``integrate_local_content`` inline so the bare-name call to
``integrate_package_primitives`` resolves in this module's namespace.
"""

from __future__ import annotations

import builtins
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .deployed_path import _deployed_path_entry
from .local_bundle import LocalBundleOpts, integrate_local_bundle
from .primitives import _IntegratorSet, integrate_package_primitives

if TYPE_CHECKING:
    from apm_cli.core.command_logger import InstallLogger
    from apm_cli.core.scope import InstallScope
    from apm_cli.install.context import InstallContext
    from apm_cli.utils.diagnostics import DiagnosticCollector


# CRITICAL: Shadow Python builtins that share names with Click commands so
# ``set()`` / ``list()`` / ``dict()`` resolve to the builtins, not Click
# subcommand objects.  ``commands/install`` and ``install/pipeline`` do the
# same dance for the same reason.
set = builtins.set
list = builtins.list
dict = builtins.dict


def integrate_local_content(
    project_root: Path,
    *,
    targets: Any,
    prompt_integrator: Any,
    agent_integrator: Any,
    skill_integrator: Any,
    instruction_integrator: Any,
    command_integrator: Any,
    hook_integrator: Any,
    force: bool,
    managed_files: Any,
    diagnostics: DiagnosticCollector,
    logger: InstallLogger | None = None,
    scope: InstallScope | None = None,
    ctx: InstallContext | None = None,
) -> dict:
    """Integrate primitives from the project's own .apm/ directory.

    This treats the project root as a synthetic package so that local
    skills, instructions, agents, prompts, hooks, and commands in .apm/
    are deployed to target directories exactly like dependency primitives.

    Only .apm/ sub-directories are processed.  A root-level SKILL.md is
    intentionally ignored (it describes the project itself, not a
    deployable skill).

    Returns a dict with integration counters and deployed file paths,
    same shape as ``integrate_package_primitives()``.
    """
    from apm_cli.models.apm_package import APMPackage, PackageInfo, PackageType

    local_pkg = APMPackage(
        name="_local",
        version="0.0.0",
        package_path=project_root,
        source="local",
    )
    local_info = PackageInfo(
        package=local_pkg,
        install_path=project_root,
        package_type=PackageType.APM_PACKAGE,
    )

    return integrate_package_primitives(
        local_info,
        project_root,
        targets=targets,
        integrators=_IntegratorSet(
            prompt_integrator=prompt_integrator,
            agent_integrator=agent_integrator,
            skill_integrator=skill_integrator,
            instruction_integrator=instruction_integrator,
        ),
        command_integrator=command_integrator,
        hook_integrator=hook_integrator,
        force=force,
        managed_files=managed_files,
        diagnostics=diagnostics,
        package_name="_local",
        logger=logger,
        scope=scope,
        ctx=ctx,
    )


# Underscore-prefixed aliases for backward compatibility with existing
# imports/patches in tests and elsewhere that use the old names.
_integrate_package_primitives = integrate_package_primitives
_integrate_local_content = integrate_local_content

__all__ = [
    "LocalBundleOpts",
    "_deployed_path_entry",
    "_integrate_local_content",
    "_integrate_package_primitives",
    "integrate_local_bundle",
    "integrate_local_content",
    "integrate_package_primitives",
]
