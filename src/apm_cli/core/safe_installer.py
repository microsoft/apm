"""Safe MCP server installation with conflict detection."""

from typing import List, Dict, Any, Optional
from dataclasses import dataclass
from pathlib import Path
from ..factory import ClientFactory
from .conflict_detector import MCPConflictDetector
from ..utils.console import _rich_warning, _rich_success, _rich_error


@dataclass
class InstallationSummary:
    """Summary of MCP server installation results."""
    
    def __init__(self):
        self.installed = []
        self.skipped = []
        self.failed = []
    
    def add_installed(self, server_ref: str):
        """Add a server to the installed list."""
        self.installed.append(server_ref)
    
    def add_skipped(self, server_ref: str, reason: str):
        """Add a server to the skipped list."""
        self.skipped.append({"server": server_ref, "reason": reason})
    
    def add_failed(self, server_ref: str, reason: str):
        """Add a server to the failed list."""
        self.failed.append({"server": server_ref, "reason": reason})
    
    def has_any_changes(self) -> bool:
        """Check if any installations or failures occurred."""
        return len(self.installed) > 0 or len(self.failed) > 0
    
    def log_summary(self, logger=None):
        """Log a summary of installation results."""
        if self.installed:
            if logger:
                logger.success(f"[+] Installed: {', '.join(self.installed)}")
            else:
                _rich_success(f"[+] Installed: {', '.join(self.installed)}")
        
        if self.skipped:
            for item in self.skipped:
                if logger:
                    logger.warning(f"Skipped {item['server']}: {item['reason']}")
                else:
                    _rich_warning(f"[!]  Skipped {item['server']}: {item['reason']}")
        
        if self.failed:
            for item in self.failed:
                if logger:
                    logger.error(f"Failed {item['server']}: {item['reason']}")
                else:
                    _rich_error(f"[x] Failed {item['server']}: {item['reason']}")


class SafeMCPInstaller:
    """Safe MCP server installation with conflict detection."""
    
    def __init__(
        self,
        runtime: str,
        logger=None,
        project_root: Path | str | None = None,
        user_scope: bool = False,
    ):
        """Initialize the safe installer.
        
        Args:
            runtime: Target runtime (copilot, codex, vscode).
            logger: Optional CommandLogger for structured output.
            project_root: Optional project root for repo-local runtime configs.
            user_scope: Whether runtime config should resolve in user scope.
        """
        self.runtime = runtime
        self.adapter = ClientFactory.create_client(
            runtime,
            project_root=project_root,
            user_scope=user_scope,
        )
        self.conflict_detector = MCPConflictDetector(self.adapter)
        self.logger = logger
    
    def install_servers(self, server_references: List[str], env_overrides: Dict[str, str] = None, server_info_cache: Dict[str, Any] = None, runtime_vars: Dict[str, str] = None) -> InstallationSummary:
        """Install MCP servers with conflict detection.
        
        Args:
            server_references: List of server references to install.
            env_overrides: Optional dictionary of environment variable overrides.
            server_info_cache: Optional pre-fetched server info to avoid duplicate registry calls.
            runtime_vars: Optional dictionary of runtime variable values.
            
        Returns:
            InstallationSummary with detailed results.
        """
        summary = InstallationSummary()
        
        for server_ref in server_references:
            if self.conflict_detector.check_server_exists(server_ref):
                summary.add_skipped(server_ref, "already configured")
                self._log_skip(server_ref)
                continue
            
            try:
                # Pass environment overrides, server info cache, and runtime variables if provided
                kwargs = {}
                if env_overrides is not None:
                    kwargs['env_overrides'] = env_overrides
                if server_info_cache is not None:
                    kwargs['server_info_cache'] = server_info_cache
                if runtime_vars is not None:
                    kwargs['runtime_vars'] = runtime_vars
                
                result = self.adapter.configure_mcp_server(server_ref, **kwargs)
                    
                if result:
                    summary.add_installed(server_ref)
                    self._log_success(server_ref)
                else:
                    summary.add_failed(server_ref, "configuration failed")
                    self._log_failure(server_ref)
            except Exception as e:
                summary.add_failed(server_ref, str(e))
                self._log_error(server_ref, e)
        
        return summary
    
    def _log_skip(self, server_ref: str):
        """Log when a server is skipped due to existing configuration."""
        if self.logger:
            self.logger.warning(f"  {server_ref} already configured, skipping")
        else:
            _rich_warning(f"  {server_ref} already configured, skipping")
    
    def _log_success(self, server_ref: str):
        """Log successful server installation."""
        if self.logger:
            self.logger.success(f"  + {server_ref}")
        else:
            _rich_success(f"  + {server_ref}")
    
    def _log_failure(self, server_ref: str):
        """Log failed server installation."""
        if self.logger:
            self.logger.warning(f"  x {server_ref} installation failed")
        else:
            _rich_warning(f"  x {server_ref} installation failed")
    
    def _log_error(self, server_ref: str, error: Exception):
        """Log error during server installation."""
        if self.logger:
            self.logger.error(f"  x {server_ref}: {error}")
        else:
            _rich_error(f"  x {server_ref}: {error}")
    
    def check_conflicts_only(self, server_references: List[str]) -> Dict[str, Any]:
        """Check for conflicts without installing.
        
        Args:
            server_references: List of server references to check.
            
        Returns:
            Dictionary with conflict information for each server.
        """
        conflicts = {}
        
        for server_ref in server_references:
            conflicts[server_ref] = self.conflict_detector.get_conflict_summary(server_ref)
        
        return conflicts
