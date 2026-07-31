"""VS Code target-availability detection for MCP integration.

Extracted from ``mcp_integrator`` to keep that orchestrator within its
source file-length budget.  ``mcp_integrator`` re-exports
:func:`_is_vscode_available` so existing import and patch sites keep
resolving it at ``apm_cli.integration.mcp_integrator._is_vscode_available``.
"""

import shutil
from pathlib import Path


def _is_vscode_available(project_root: Path | str | None = None) -> bool:
    """Return True when VS Code can be targeted for MCP configuration.

    VS Code is considered available when either:
    - the ``code`` CLI command is on PATH (the standard case), or
    - a ``.vscode/`` directory exists in the resolved project root
      (common on macOS where the user hasn't run "Install 'code' command
      in PATH" from the VS Code command palette).

    Args:
        project_root: Project root to inspect for a `.vscode/` directory when
            explicit project context is provided. Falls back to CWD when unset.
    """
    root = Path(project_root) if project_root is not None else Path.cwd()
    return shutil.which("code") is not None or (root / ".vscode").is_dir()
