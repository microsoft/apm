"""Deprecated per-target command integration methods.

These methods are kept for backward compatibility with external consumers.
Do NOT add new methods here.  Use the target-driven API
(``integrate_commands_for_target`` / ``sync_for_target``) with profiles
from ``resolve_targets()`` instead.

The mixin is applied to ``CommandIntegrator`` in ``_integrator.py`` via
multiple inheritance so callers that hold a ``CommandIntegrator`` instance
gain these methods without any change to their call sites.
"""

from __future__ import annotations

from pathlib import Path

from .._opts import IntegrateOpts


class _LegacyCommandsMixin:
    """Mixin carrying deprecated per-target command methods.

    Methods here delegate to the target-driven API (``integrate_commands_for_target``
    / ``sync_for_target``) which is defined on ``CommandIntegrator``.  Python's
    MRO ensures those names are resolved correctly when ``CommandIntegrator``
    inherits from both ``BaseIntegrator`` and this mixin.
    """

    # DEPRECATED: use integrate_commands_for_target(KNOWN_TARGETS["claude"], ...) instead.
    def integrate_package_commands(
        self,
        package_info,
        project_root: Path,
        force: bool = False,
        managed_files: set | None = None,
        diagnostics=None,
    ):
        """Integrate prompt files as Claude commands (.claude/commands/).

        Legacy compat: ensures ``.claude/`` exists so the target-driven
        method does not skip.
        """
        from apm_cli.integration.targets import KNOWN_TARGETS

        (project_root / ".claude").mkdir(parents=True, exist_ok=True)
        return self.integrate_commands_for_target(  # type: ignore[attr-defined]
            KNOWN_TARGETS["claude"],
            package_info,
            project_root,
            IntegrateOpts(
                force=force,
                managed_files=managed_files,
                diagnostics=diagnostics,
            ),
        )

    # DEPRECATED: use sync_for_target(KNOWN_TARGETS["claude"], ...) instead.
    def sync_integration(
        self, apm_package, project_root: Path, managed_files: set | None = None
    ) -> dict:
        """Remove APM-managed command files from .claude/commands/."""
        from apm_cli.integration.targets import KNOWN_TARGETS

        return self.sync_for_target(  # type: ignore[attr-defined]
            KNOWN_TARGETS["claude"],
            apm_package,
            project_root,
            managed_files=managed_files,
        )

    # DEPRECATED: use sync_for_target(KNOWN_TARGETS["claude"], ...) instead.
    def remove_package_commands(
        self,
        package_name: str,
        project_root: Path,
        managed_files: set | None = None,
    ) -> int:
        """Remove APM-managed command files."""
        stats = self.sync_integration(None, project_root, managed_files=managed_files)
        return stats["files_removed"]

    # DEPRECATED: use integrate_commands_for_target(KNOWN_TARGETS["opencode"], ...) instead.
    def integrate_package_commands_opencode(
        self,
        package_info,
        project_root: Path,
        force: bool = False,
        managed_files: set | None = None,
        diagnostics=None,
    ):
        """Integrate prompt files as OpenCode commands (.opencode/commands/)."""
        from apm_cli.integration.targets import KNOWN_TARGETS

        return self.integrate_commands_for_target(  # type: ignore[attr-defined]
            KNOWN_TARGETS["opencode"],
            package_info,
            project_root,
            IntegrateOpts(
                force=force,
                managed_files=managed_files,
                diagnostics=diagnostics,
            ),
        )

    # DEPRECATED: use sync_for_target(KNOWN_TARGETS["opencode"], ...) instead.
    def sync_integration_opencode(
        self,
        apm_package,
        project_root: Path,
        managed_files: set | None = None,
    ) -> dict:
        """Remove APM-managed command files from .opencode/commands/."""
        from apm_cli.integration.targets import KNOWN_TARGETS

        return self.sync_for_target(  # type: ignore[attr-defined]
            KNOWN_TARGETS["opencode"],
            apm_package,
            project_root,
            managed_files=managed_files,
        )
