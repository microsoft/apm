"""Copilot project-scope MCP destination: workspace-root ``.mcp.json`` (#3866).

Single owner of where the ``copilot`` target writes project MCP servers and in
which shape. User scope is unchanged (``$COPILOT_HOME/mcp-config.json`` through
:class:`~apm_cli.adapters.client.copilot.CopilotClientAdapter`).

Evidence:

* VS Code (https://code.visualstudio.com/docs/agents/reference/mcp-configuration):
  the Copilot harness reads ``$COPILOT_HOME/mcp-config.json`` and ``.mcp.json``
  at the workspace root (top-level ``mcpServers``); ``.vscode/mcp.json`` is the
  deprecated Local-harness ``servers`` format the harness does not read.
* Copilot CLI (https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/add-mcp-servers):
  reads project ``.mcp.json`` (taking precedence) and ``.github/mcp.json``,
  accepts ``type`` ``stdio`` / ``http`` / ``sse``, defaults ``tools`` to all,
  and does not read ``.vscode/mcp.json``.

``.mcp.json`` is also Claude Code's project file, so both targets render one
entry per server through the same shape: this adapter reuses
:class:`~apm_cli.adapters.client.claude.ClaudeClientAdapter`'s render, merge
and fingerprint rules (``type: stdio``, no default ``tools`` or empty ``id``,
``${VAR}`` references kept). Entries are merged per server and user-authored
servers are never overwritten (the shared ownership rules in
:mod:`apm_cli.install.mcp.ownership`). The one place the two harnesses can
disagree is a self-defined launcher under ``.agents/skills/``: Claude Code
deploys skills to ``.claude/skills/``, so when that copy exists the Claude path
wins for both targets. Both adapters resolve it through
``ClaudeClientAdapter._rewrite_self_defined_skill_command``, so the two renders
stay identical.

``.github/mcp.json`` (APM <= 0.36 ``copilot``) and ``.vscode/mcp.json`` (the
removed ``vscode`` target) are retired destinations; APM-managed entries there
are healed by :mod:`apm_cli.install.mcp.retired_destinations`.
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from ._remote_shapes import remote_shape_row
from .claude import ClaudeClientAdapter

PROJECT_MCP_JSON = ".mcp.json"


class CopilotProjectClientAdapter(ClaudeClientAdapter):
    """Copilot project MCP configuration in workspace-root ``.mcp.json``."""

    remote_shape = remote_shape_row("copilot")

    _client_label: str = "Copilot (.mcp.json)"
    target_name: str = "copilot"
    _runtime_config_label: ClassVar[str] = "the project .mcp.json"
    _runtime_launch_command: ClassVar[str] = "copilot"

    # Own post-install aggregation buckets (see ClaudeClientAdapter).
    _legacy_angle_offenders_by_server: ClassVar[dict] = {}
    _security_upgraded_keys: ClassVar[set] = set()
    _unset_env_keys_by_server: ClassVar[dict] = {}
    _install_run_summary_emitted: ClassVar[bool] = False

    def __init__(
        self,
        registry_url=None,
        project_root: Path | str | None = None,
        user_scope: bool = False,
    ):
        if user_scope:
            raise ValueError(
                "CopilotProjectClientAdapter is project-scope only; "
                "user scope uses CopilotClientAdapter"
            )
        super().__init__(registry_url=registry_url, project_root=project_root, user_scope=False)

    def _should_write_project(self) -> bool:
        """The copilot target itself opts in; no marker directory is required."""
        return True

    def _project_mcp_path(self) -> Path:
        return self.project_root / PROJECT_MCP_JSON

    def legacy_managed_renders(
        self,
        server_info: dict,
        *,
        env_overrides: dict | None = None,
        runtime_vars: dict | None = None,
    ) -> tuple[dict, ...]:
        """Add the renders APM <= 0.36 wrote to ``.github/mcp.json`` (any remote shape).

        A ``--frozen`` replay keeps that release's lockfile fingerprint, so the
        retired-shape owner recognizes the healed ``.mcp.json`` entry by it.
        """
        from .copilot import CopilotClientAdapter

        kwargs = {"env_overrides": env_overrides, "runtime_vars": runtime_vars}
        renders = list(super().legacy_managed_renders(server_info, **kwargs))
        legacy = CopilotClientAdapter(project_root=self.project_root, user_scope=False)
        try:
            current = self.render_server_config(server_info, **kwargs)
            retired = [
                legacy.render_server_config(server_info, **kwargs),
                *legacy.legacy_managed_renders(server_info, **kwargs),
            ]
        except ValueError:
            return tuple(renders)
        renders.extend(r for r in retired if r != current and r not in renders)
        return tuple(renders)
