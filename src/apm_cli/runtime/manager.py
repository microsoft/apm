"""
Runtime management functionality for APM CLI.
Handles installation, configuration, and management of AI runtimes.
"""

import os
import sys
import subprocess
import tempfile
import shutil
from pathlib import Path
from typing import Dict, List, Optional
import click
from colorama import Fore, Style

from ..core.token_manager import setup_runtime_environment


class RuntimeManager:
    """Manages AI runtime installation and configuration via embedded scripts."""
    
    @property
    def _is_windows(self) -> bool:
        return sys.platform == "win32"

    def __init__(self):
        self.runtime_dir = Path.home() / ".apm" / "runtimes"
        ext = ".ps1" if sys.platform == "win32" else ".sh"
        self.supported_runtimes = {
            "copilot": {
                "script": f"setup-copilot{ext}",
                "description": "GitHub Copilot CLI with native MCP integration",
                "binary": "copilot"
            },
            "codex": {
                "script": f"setup-codex{ext}",
                "description": "OpenAI Codex CLI with GitHub Models support",
                "binary": "codex"
            },
            "llm": {
                "script": f"setup-llm{ext}",
                "description": "Simon Willison's LLM library with multiple providers",
                "binary": "llm"
            },
            "gemini": {
                "script": f"setup-gemini{ext}",
                "description": "Google Gemini CLI with MCP integration",
                "binary": "gemini"
            }
        }
    
    def get_embedded_script(self, script_name: str) -> str:
        """Get embedded setup script content."""
        try:
            # Try PyInstaller bundle first
            if getattr(sys, 'frozen', False):
                # Running in PyInstaller bundle
                bundle_dir = Path(sys._MEIPASS)
                script_path = bundle_dir / "scripts" / "runtime" / script_name
                if script_path.exists():
                    return script_path.read_text(encoding="utf-8")
            
            # Fall back to direct file access for development
            # Look for scripts relative to the repo structure
            current_file = Path(__file__)
            repo_root = current_file.parent.parent.parent.parent  # Go up to repo root
            script_path = repo_root / "scripts" / "runtime" / script_name
            if script_path.exists():
                return script_path.read_text(encoding="utf-8")
            
            raise FileNotFoundError(f"Script not found: {script_name}")
        except Exception as e:
            click.echo(f"{Fore.RED}[x] Failed to load embedded script {script_name}: {e}{Style.RESET_ALL}", err=True)
            raise RuntimeError(f"Could not load setup script: {script_name}")
    
    def get_common_script(self) -> str:
        """Get the common utilities script."""
        script_name = "setup-common.ps1" if self._is_windows else "setup-common.sh"
        return self.get_embedded_script(script_name)
    
    def get_token_helper_script(self) -> str:
        """Get the GitHub token helper script."""
        if self._is_windows:
            # On Windows, tokens are passed via environment variables directly
            return ""
        try:
            # Try PyInstaller bundle first
            if getattr(sys, 'frozen', False):
                # Running in PyInstaller bundle
                bundle_dir = Path(sys._MEIPASS)
                script_path = bundle_dir / "scripts" / "github-token-helper.sh"
                if script_path.exists():
                    return script_path.read_text(encoding="utf-8")

            # Fall back to direct file access for development
            # Look for scripts relative to the repo structure
            current_file = Path(__file__)
            repo_root = current_file.parent.parent.parent.parent  # Go up to repo root
            script_path = repo_root / "scripts" / "github-token-helper.sh"
            if script_path.exists():
                return script_path.read_text(encoding="utf-8")
            
            raise FileNotFoundError("github-token-helper.sh not found")
        except Exception as e:
            click.echo(f"{Fore.RED}[x] Failed to load github-token-helper.sh: {e}{Style.RESET_ALL}", err=True)
            raise RuntimeError("Could not load token helper script")
    
    def run_embedded_script(self, script_content: str, common_content: str, 
                           script_args: Optional[List[str]] = None) -> bool:
        """Execute an embedded setup script with common utilities."""
        script_args = script_args or []
        
        from ..config import get_apm_temp_dir
        with tempfile.TemporaryDirectory(dir=get_apm_temp_dir()) as temp_dir:
            temp_path = Path(temp_dir)
            
            if self._is_windows:
                # Write common utilities as PowerShell
                common_script = temp_path / "setup-common.ps1"
                common_script.write_text(common_content, encoding="utf-8")
                
                # Write GitHub token helper (empty on Windows)
                token_helper_content = self.get_token_helper_script()
                if token_helper_content:
                    token_helper_script = temp_path / "github-token-helper.ps1"
                    token_helper_script.write_text(token_helper_content, encoding="utf-8")
                
                # Write main script as PowerShell
                main_script = temp_path / "setup-script.ps1"
                main_script.write_text(script_content, encoding="utf-8")
            else:
                # Write common utilities as bash
                common_script = temp_path / "setup-common.sh"
                common_script.write_text(common_content, encoding="utf-8")
                common_script.chmod(0o755)
                
                # Write GitHub token helper
                try:
                    token_helper_content = self.get_token_helper_script()
                    token_helper_script = temp_path / "github-token-helper.sh"
                    token_helper_script.write_text(token_helper_content, encoding="utf-8")
                    token_helper_script.chmod(0o755)
                except Exception as e:
                    click.echo(f"{Fore.YELLOW}[!]  Token helper not available, scripts may use fallback authentication: {e}{Style.RESET_ALL}")
                
                # Write main script as bash
                main_script = temp_path / "setup-script.sh"
                main_script.write_text(script_content, encoding="utf-8")
                main_script.chmod(0o755)
            
            # Execute script with environment that includes npm authentication
            try:
                if self._is_windows:
                    ps_cmd = shutil.which("pwsh") or shutil.which("powershell") or "powershell"
                    cmd = [ps_cmd, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(main_script)] + script_args
                else:
                    cmd = ["bash", str(main_script)] + script_args
                
                # Prepare environment with GitHub tokens for all authentication needs
                env = os.environ.copy()
                
                # Set up comprehensive GitHub token environment for all runtimes
                # New token precedence:
                # - GITHUB_APM_PAT: APM module access
                # - GITHUB_TOKEN: User authentication for GitHub Models (Codex CLI)
                
                # Setup GitHub tokens using centralized manager
                env = setup_runtime_environment(env)  # Pass env to preserve CI tokens
                
                result = subprocess.run(
                    cmd,
                    cwd=temp_dir,
                    capture_output=False,  # Show output to user
                    text=True,
                    encoding="utf-8",
                    env=env
                )
                return result.returncode == 0
            except Exception as e:
                click.echo(f"{Fore.RED}[x] Failed to execute setup script: {e}{Style.RESET_ALL}", err=True)
                return False
    
    def setup_runtime(self, runtime_name: str, version: Optional[str] = None, vanilla: bool = False) -> bool:
        """Set up a specific runtime."""
        if runtime_name not in self.supported_runtimes:
            click.echo(f"{Fore.RED}[x] Unsupported runtime: {runtime_name}{Style.RESET_ALL}", err=True)
            click.echo(f"{Fore.BLUE}[i]  Supported runtimes: {', '.join(self.supported_runtimes.keys())}{Style.RESET_ALL}")
            return False
        
        runtime_info = self.supported_runtimes[runtime_name]
        script_name = runtime_info["script"]
        description = runtime_info["description"]
        
        click.echo(f"{Fore.BLUE} Setting up {runtime_name} runtime: {description}{Style.RESET_ALL}")
        
        if vanilla:
            click.echo(f"{Fore.YELLOW}[!]  Installing in vanilla mode - no APM configuration will be applied{Style.RESET_ALL}")
        else:
            click.echo(f"{Fore.BLUE}[i]  Installing with APM defaults (GitHub Models for free access){Style.RESET_ALL}")
        
        try:
            # Get scripts
            script_content = self.get_embedded_script(script_name)
            common_content = self.get_common_script()
            
            # Prepare arguments (PowerShell scripts use named params like -Version/-Vanilla)
            script_args = []
            if version:
                if self._is_windows:
                    script_args.extend(["-Version", version])
                else:
                    script_args.append(version)
            if vanilla:
                if self._is_windows:
                    script_args.append("-Vanilla")
                else:
                    script_args.append("--vanilla")
            
            # Run setup script
            success = self.run_embedded_script(script_content, common_content, script_args)
            
            if success:
                click.echo(f"{Fore.GREEN}[+] Successfully set up {runtime_name} runtime{Style.RESET_ALL}")
                return True
            else:
                click.echo(f"{Fore.RED}[x] Failed to set up {runtime_name} runtime{Style.RESET_ALL}", err=True)
                return False
                
        except Exception as e:
            click.echo(f"{Fore.RED}[x] Error setting up {runtime_name}: {e}{Style.RESET_ALL}", err=True)
            return False
    
    def list_runtimes(self) -> Dict[str, Dict[str, str]]:
        """List available and installed runtimes."""
        runtimes = {}
        
        for name, info in self.supported_runtimes.items():
            binary_name = info["binary"]
            
            # Check different installation paths based on runtime type
            # For all runtimes, check APM runtime directory first, then system PATH
            binary_path = self.runtime_dir / binary_name
            if binary_path.exists():
                installed = True
                path = str(binary_path)
            else:
                # Fallback to system PATH
                installed = shutil.which(binary_name) is not None
                path = shutil.which(binary_name) if installed else None
            
            runtime_status = {
                "description": info["description"],
                "installed": installed,
                "path": path
            }
            
            # Try to get version if installed
            if runtime_status["installed"]:
                try:
                    version_cmd = [binary_name, "--version"]
                    result = subprocess.run(
                        version_cmd,
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        timeout=5
                    )
                    if result.returncode == 0:
                        runtime_status["version"] = result.stdout.strip()
                except Exception:
                    runtime_status["version"] = "unknown"
            
            runtimes[name] = runtime_status
        
        return runtimes
    
    def is_runtime_available(self, runtime_name: str) -> bool:
        """Check if a runtime is installed and available."""
        if runtime_name not in self.supported_runtimes:
            return False
        
        binary_name = self.supported_runtimes[runtime_name]["binary"]
        
        # For all runtimes, check APM runtime directory first, then system PATH
        apm_binary = self.runtime_dir / binary_name
        if apm_binary.exists() and apm_binary.is_file():
            return True
        
        # Check in system PATH as fallback
        return shutil.which(binary_name) is not None
    
    def remove_runtime(self, runtime_name: str) -> bool:
        """Remove an installed runtime."""
        if runtime_name not in self.supported_runtimes:
            click.echo(f"{Fore.RED}[x] Unknown runtime: {runtime_name}{Style.RESET_ALL}", err=True)
            return False
        
        # Handle npm-based runtimes (copilot, gemini)
        _npm_packages = {
            "copilot": "@github/copilot",
            "gemini": "@google/gemini-cli",
        }
        if runtime_name in _npm_packages:
            try:
                result = subprocess.run(
                    ["npm", "uninstall", "-g", _npm_packages[runtime_name]],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                )
                if result.returncode == 0:
                    click.echo(f"{Fore.GREEN}[+] Successfully removed {runtime_name} runtime{Style.RESET_ALL}")
                    return True
                else:
                    click.echo(f"{Fore.RED}[x] Failed to remove {runtime_name}: {result.stderr}{Style.RESET_ALL}", err=True)
                    return False
            except Exception as e:
                click.echo(f"{Fore.RED}[x] Failed to remove {runtime_name}: {e}{Style.RESET_ALL}", err=True)
                return False
        
        # Handle other runtimes (installed in APM runtime directory)
        binary_name = self.supported_runtimes[runtime_name]["binary"]
        binary_path = self.runtime_dir / binary_name
        
        if not binary_path.exists():
            click.echo(f"{Fore.YELLOW}[!]  Runtime {runtime_name} is not installed in APM runtime directory{Style.RESET_ALL}")
            return False
        
        try:
            if binary_path.is_file():
                binary_path.unlink()
            elif binary_path.is_dir():
                shutil.rmtree(binary_path)
            
            # Also remove virtual environment for LLM
            if runtime_name == "llm":
                venv_path = self.runtime_dir / "llm-venv"
                if venv_path.exists():
                    shutil.rmtree(venv_path)
            
            click.echo(f"{Fore.GREEN}[+] Successfully removed {runtime_name} runtime{Style.RESET_ALL}")
            return True
            
        except Exception as e:
            click.echo(f"{Fore.RED}[x] Failed to remove {runtime_name}: {e}{Style.RESET_ALL}", err=True)
            return False
    
    def get_runtime_preference(self) -> List[str]:
        """Get the runtime preference order."""
        return ["copilot", "codex", "gemini", "llm"]
    
    def get_available_runtime(self) -> Optional[str]:
        """Get the first available runtime based on preference."""
        for runtime in self.get_runtime_preference():
            if self.is_runtime_available(runtime):
                return runtime
        return None
