# pylint: disable=duplicate-code
"""Deprecated per-target API mixin for :class:`~.agent_integrator.AgentIntegrator`.

Extracted from :mod:`agent_integrator` to keep that module under 400 lines.
:class:`AgentIntegrator` gains these methods via multiple inheritance:
``class AgentIntegrator(BaseIntegrator, _AgentDeprecatedMixin)``.

All methods in this mixin are deprecated; new callers should use the
target-driven API (``*_for_target``) with profiles from
:func:`~.base_integrator.BaseIntegrator.resolve_targets` instead.
"""

from __future__ import annotations

from pathlib import Path

from . import _agent_legacy
from ._opts import IntegrateOpts
from .base_integrator import IntegrationResult


class _AgentDeprecatedMixin:
    """Deprecated per-target methods for :class:`AgentIntegrator`.

    Do NOT add new methods here.  Kept for backward compatibility with
    external consumers only.
    """

    # DEPRECATED: use get_target_filename_for_target(KNOWN_TARGETS["copilot"], ...) instead.
    def get_target_filename(self, source_file: Path, package_name: str) -> str:
        """Generate target filename for copilot (always .agent.md)."""
        from apm_cli.integration.targets import KNOWN_TARGETS

        return self.get_target_filename_for_target(
            source_file,
            package_name,
            KNOWN_TARGETS["copilot"],
        )

    # DEPRECATED: use integrate_agents_for_target(KNOWN_TARGETS["copilot"], ...) instead.
    def integrate_package_agents(
        self,
        package_info,
        project_root: Path,
        force: bool = False,
        managed_files: set | None = None,
        diagnostics=None,
    ) -> IntegrationResult:
        """Integrate agents into .github/agents/ + auto-copy to claude/cursor.

        Legacy entry point that preserves the multi-target auto-copy
        behaviour. New callers should use ``integrate_agents_for_target``
        directly.

        Implementation delegates to
        :func:`._agent_legacy.run_legacy_multi_target_integration`.
        """
        self.init_link_resolver(package_info, project_root)
        if not self.find_agent_files(package_info.install_path):
            return IntegrationResult(0, 0, 0, [])
        return _agent_legacy.run_legacy_multi_target_integration(
            self,
            package_info,
            project_root,
            _agent_legacy.LegacyIntegrationOpts(
                force=force,
                managed_files=managed_files,
                diagnostics=diagnostics,
            ),
        )

    # DEPRECATED: use get_target_filename_for_target(KNOWN_TARGETS["claude"], ...) instead.
    def get_target_filename_claude(self, source_file: Path, package_name: str) -> str:
        """Generate target filename for Claude agents (plain .md)."""
        from apm_cli.integration.targets import KNOWN_TARGETS

        return self.get_target_filename_for_target(
            source_file,
            package_name,
            KNOWN_TARGETS["claude"],
        )

    # DEPRECATED: use integrate_agents_for_target(KNOWN_TARGETS["claude"], ...) instead.
    def integrate_package_agents_claude(
        self,
        package_info,
        project_root: Path,
        force: bool = False,
        managed_files: set | None = None,
        diagnostics=None,
    ) -> IntegrationResult:
        """Integrate agents into .claude/agents/.

        Legacy compat: ensures ``.claude/`` exists so the target-driven
        method does not skip (the old method did not guard on root-dir
        existence).
        """
        from apm_cli.integration.targets import KNOWN_TARGETS

        (project_root / ".claude").mkdir(parents=True, exist_ok=True)
        return self.integrate_agents_for_target(
            KNOWN_TARGETS["claude"],
            package_info,
            project_root,
            IntegrateOpts(
                force=force,
                managed_files=managed_files,
                diagnostics=diagnostics,
            ),
        )

    # DEPRECATED: use sync_for_target(KNOWN_TARGETS["copilot"], ...) instead.
    def sync_integration(
        self,
        apm_package,
        project_root: Path,
        managed_files: set | None = None,
    ) -> dict[str, int]:
        """Remove APM-managed agent files from .github/agents/."""
        from apm_cli.integration.targets import KNOWN_TARGETS

        return self.sync_for_target(
            KNOWN_TARGETS["copilot"],
            apm_package,
            project_root,
            managed_files=managed_files,
        )

    # DEPRECATED: use sync_for_target(KNOWN_TARGETS["claude"], ...) instead.
    def sync_integration_claude(
        self,
        apm_package,
        project_root: Path,
        managed_files: set | None = None,
    ) -> dict[str, int]:
        """Remove APM-managed agent files from .claude/agents/."""
        from apm_cli.integration.targets import KNOWN_TARGETS

        return self.sync_for_target(
            KNOWN_TARGETS["claude"],
            apm_package,
            project_root,
            managed_files=managed_files,
        )

    # DEPRECATED: use get_target_filename_for_target(KNOWN_TARGETS["cursor"], ...) instead.
    def get_target_filename_cursor(self, source_file: Path, package_name: str) -> str:
        """Generate target filename for Cursor agents (plain .md)."""
        from apm_cli.integration.targets import KNOWN_TARGETS

        return self.get_target_filename_for_target(
            source_file,
            package_name,
            KNOWN_TARGETS["cursor"],
        )

    # DEPRECATED: use integrate_agents_for_target(KNOWN_TARGETS["cursor"], ...) instead.
    def integrate_package_agents_cursor(
        self,
        package_info,
        project_root: Path,
        force: bool = False,
        managed_files: set | None = None,
        diagnostics=None,
    ) -> IntegrationResult:
        """Integrate agents into .cursor/agents/."""
        from apm_cli.integration.targets import KNOWN_TARGETS

        return self.integrate_agents_for_target(
            KNOWN_TARGETS["cursor"],
            package_info,
            project_root,
            IntegrateOpts(
                force=force,
                managed_files=managed_files,
                diagnostics=diagnostics,
            ),
        )

    # DEPRECATED: use sync_for_target(KNOWN_TARGETS["cursor"], ...) instead.
    def sync_integration_cursor(
        self,
        apm_package,
        project_root: Path,
        managed_files: set | None = None,
    ) -> dict[str, int]:
        """Remove APM-managed agent files from .cursor/agents/."""
        from apm_cli.integration.targets import KNOWN_TARGETS

        return self.sync_for_target(
            KNOWN_TARGETS["cursor"],
            apm_package,
            project_root,
            managed_files=managed_files,
        )

    # DEPRECATED: use integrate_agents_for_target(KNOWN_TARGETS["opencode"], ...) instead.
    def integrate_package_agents_opencode(
        self,
        package_info,
        project_root: Path,
        force: bool = False,
        managed_files: set | None = None,
        diagnostics=None,
    ) -> IntegrationResult:
        """Integrate agents into .opencode/agents/."""
        from apm_cli.integration.targets import KNOWN_TARGETS

        return self.integrate_agents_for_target(
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
    ) -> dict[str, int]:
        """Remove APM-managed agent files from .opencode/agents/."""
        from apm_cli.integration.targets import KNOWN_TARGETS

        return self.sync_for_target(
            KNOWN_TARGETS["opencode"],
            apm_package,
            project_root,
            managed_files=managed_files,
        )
