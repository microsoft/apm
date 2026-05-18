"""Centralized token management for different AI runtimes and git platforms.

This module handles the complex token environment setup required by different
AI CLI tools, each of which expects different environment variable names for
authentication and API access.

Token Architecture:
- GITHUB_COPILOT_PAT: User-scoped PAT specifically for Copilot
- GITHUB_APM_PAT: Fine-grained PAT for APM module access (GitHub)
- ADO_APM_PAT: PAT for APM module access (Azure DevOps)
- GITHUB_TOKEN: User-scoped PAT for GitHub Models API access

Platform Token Selection:
- GitHub-class: GITHUB_APM_PAT -> GITHUB_TOKEN -> GH_TOKEN -> gh auth token -> git credential helpers
- GitLab-class: GITLAB_APM_PAT -> GITLAB_TOKEN -> git credential helpers
- Generic FQDN hosts: git credential helpers only (no GitHub/GitLab platform env vars)
- Azure DevOps: ADO_APM_PAT

Runtime Requirements:
- Codex CLI: Uses GITHUB_TOKEN (must be user-scoped for GitHub Models)
"""

import logging
import os
import subprocess
import sys
from urllib.parse import urlparse

from apm_cli.utils.github_host import (
    default_host,
    is_azure_devops_hostname,
    is_github_hostname,
    is_valid_fqdn,
)

from . import _credential_helpers as _ch
from ._credential_helpers import (
    _format_credential_host,
    _sanitize_credential_path,
)

logger = logging.getLogger(__name__)


class GitHubTokenManager:
    """Manages GitHub token environment setup for different AI runtimes."""

    # Diagnostic source label for bearer-resolved tokens (AAD via az CLI).
    # Used by AuthResolver and downstream diagnostics to identify bearer auth.
    ADO_BEARER_SOURCE = "AAD_BEARER_AZ_CLI"

    # Define token precedence for different use cases
    TOKEN_PRECEDENCE = {  # noqa: RUF012
        "copilot": ["GITHUB_COPILOT_PAT", "GITHUB_TOKEN", "GITHUB_APM_PAT"],
        "models": [
            "GITHUB_TOKEN",
            "GITHUB_APM_PAT",
        ],  # GitHub Models prefers user-scoped PAT, falls back to APM PAT
        "modules": ["GITHUB_APM_PAT", "GITHUB_TOKEN", "GH_TOKEN"],  # GitHub-class module access
        "gitlab_modules": [
            "GITLAB_APM_PAT",
            "GITLAB_TOKEN",
        ],  # GitLab SaaS / self-managed API + git HTTPS
        "generic_modules": [],  # Non-GitHub / non-GitLab FQDN: env PATs deferred to credential fill only
        "ado_modules": ["ADO_APM_PAT"],  # APM module access (Azure DevOps)
        "artifactory_modules": ["ARTIFACTORY_APM_TOKEN"],  # APM module access (JFrog Artifactory)
    }

    # Runtime-specific environment variable mappings
    RUNTIME_ENV_VARS = {  # noqa: RUF012
        "copilot": ["GH_TOKEN", "GITHUB_PERSONAL_ACCESS_TOKEN"],
        "codex": ["GITHUB_TOKEN"],  # Uses GITHUB_TOKEN directly
        "llm": ["GITHUB_MODELS_KEY"],  # LLM-specific variable for GitHub Models
    }

    def __init__(self, preserve_existing: bool = True):
        """Initialize token manager.

        Args:
            preserve_existing: If True, never overwrite existing environment variables
        """
        self.preserve_existing = preserve_existing
        # Keyed by (host, port): same hostname on different ports may have
        # distinct credentials, so a host-only key would cross-contaminate.
        self._credential_cache: dict[tuple[str, int | None], str | None] = {}

    @staticmethod
    def _is_valid_credential_token(token: str) -> bool:
        """Delegate to _credential_helpers."""
        return _ch._is_valid_credential_token(token)

    @staticmethod
    def _supports_gh_cli_host(host: str | None) -> bool:
        """Delegate to _credential_helpers."""
        return _ch._supports_gh_cli_host(host)

    DEFAULT_CREDENTIAL_TIMEOUT = _ch.DEFAULT_CREDENTIAL_TIMEOUT
    MAX_CREDENTIAL_TIMEOUT = _ch.MAX_CREDENTIAL_TIMEOUT

    @classmethod
    def _get_credential_timeout(cls) -> int:
        """Delegate to _credential_helpers."""
        return _ch._get_credential_timeout()

    @staticmethod
    def resolve_credential_from_git(
        host: str, port: int | None = None, path: str | None = None
    ) -> str | None:
        """Delegate to _credential_helpers."""
        return _ch.resolve_credential_from_git(host, port, path)

    @staticmethod
    def resolve_credential_from_gh_cli(host: str | None) -> str | None:
        """Delegate to _credential_helpers."""
        return _ch.resolve_credential_from_gh_cli(host)

    def setup_environment(self, env: dict[str, str] | None = None) -> dict[str, str]:
        """Set up complete token environment for all runtimes.

        Args:
            env: Environment dictionary to modify (defaults to os.environ.copy())

        Returns:
            Updated environment dictionary with all required tokens set
        """
        if env is None:
            env = os.environ.copy()

        # Get available tokens
        available_tokens = self._get_available_tokens(env)

        # Set up tokens for each runtime without overwriting existing values
        self._setup_copilot_tokens(env, available_tokens)
        self._setup_codex_tokens(env, available_tokens)
        self._setup_llm_tokens(env, available_tokens)

        return env

    def get_token_for_purpose(self, purpose: str, env: dict[str, str] | None = None) -> str | None:
        """Get the best available token for a specific purpose.

        Args:
            purpose: Token purpose ('copilot', 'models', 'modules')
            env: Environment to check (defaults to os.environ)

        Returns:
            Best available token for the purpose, or None if not available
        """
        if env is None:
            env = os.environ

        if purpose not in self.TOKEN_PRECEDENCE:
            raise ValueError(f"Unknown purpose: {purpose}")

        for token_var in self.TOKEN_PRECEDENCE[purpose]:
            token = env.get(token_var)
            if token:
                return token
        return None

    def get_token_with_credential_fallback(
        self,
        purpose: str,
        host: str,
        env: dict[str, str] | None = None,
        *,
        port: int | None = None,
    ) -> str | None:
        """Get token for a purpose, falling back to git credential helpers.

        Tries environment variables first (via get_token_for_purpose), then
        checks the active gh CLI account, then queries the git credential
        store as a last resort. Results are cached per ``(host, port)`` to
        avoid repeated subprocess calls while keeping same-host-different-port
        credentials separate.

        Args:
            purpose: Token purpose ('modules', etc.)
            host: Git host to resolve credentials for (e.g., "github.com")
            env: Environment to check (defaults to os.environ)
            port: Optional non-standard git port. Flows through to
                ``resolve_credential_from_git`` so credential helpers can
                return port-specific credentials.

        Returns:
            Best available token, or None if not available from any source
        """
        token = self.get_token_for_purpose(purpose, env)
        if token:
            return token

        cache_key = (host, port)
        if cache_key in self._credential_cache:
            return self._credential_cache[cache_key]

        gh_token = None
        if self._supports_gh_cli_host(host):
            gh_token = self.resolve_credential_from_gh_cli(host)
        if gh_token:
            self._credential_cache[cache_key] = gh_token
            return gh_token

        credential = self.resolve_credential_from_git(host, port=port)
        self._credential_cache[cache_key] = credential
        return credential

    def validate_tokens(self, env: dict[str, str] | None = None) -> tuple[bool, str]:
        """Validate that required tokens are available.

        Args:
            env: Environment to check (defaults to os.environ)

        Returns:
            Tuple of (is_valid, error_message)
        """
        if env is None:
            env = os.environ

        # Check for at least one valid token
        has_any_token = any(
            self.get_token_for_purpose(purpose, env) for purpose in ["copilot", "models", "modules"]
        )

        if not has_any_token:
            return False, (
                "No tokens found. Set one of:\n"
                "- GITHUB_TOKEN (user-scoped PAT for GitHub Models)\n"
                "- GITHUB_APM_PAT (fine-grained PAT for APM modules on GitHub)\n"
                "- ADO_APM_PAT (PAT for APM modules on Azure DevOps)"
            )

        # Warn about GitHub Models access if only fine-grained PAT is available
        models_token = self.get_token_for_purpose("models", env)
        if not models_token:
            has_fine_grained = env.get("GITHUB_APM_PAT")
            if has_fine_grained:
                return True, (
                    "Warning: Only fine-grained PAT available. "
                    "GitHub Models requires GITHUB_TOKEN (user-scoped PAT)"
                )

        return True, "Token validation passed"

    def _get_available_tokens(self, env: dict[str, str]) -> dict[str, str]:
        """Get all available GitHub tokens from environment."""
        tokens = {}
        for _purpose, token_vars in self.TOKEN_PRECEDENCE.items():
            for token_var in token_vars:
                if env.get(token_var):
                    tokens[token_var] = env[token_var]
        return tokens

    def _setup_copilot_tokens(self, env: dict[str, str], available_tokens: dict[str, str]):
        """Set up tokens for Copilot."""
        copilot_token = self.get_token_for_purpose("copilot", available_tokens)
        if not copilot_token:
            return

        for env_var in self.RUNTIME_ENV_VARS["copilot"]:
            if self.preserve_existing and env_var in env:
                continue
            env[env_var] = copilot_token

    def _setup_codex_tokens(self, env: dict[str, str], available_tokens: dict[str, str]):
        """Set up tokens for Codex CLI (preserve existing tokens)."""
        # Codex script checks for both GITHUB_TOKEN and GITHUB_APM_PAT
        # Set up GITHUB_TOKEN if not present
        if not (self.preserve_existing and "GITHUB_TOKEN" in env):
            models_token = self.get_token_for_purpose("models", available_tokens)
            if models_token and "GITHUB_TOKEN" not in env:
                env["GITHUB_TOKEN"] = models_token

        # Ensure GITHUB_APM_PAT is available if we have it
        if not (self.preserve_existing and "GITHUB_APM_PAT" in env):
            apm_token = available_tokens.get("GITHUB_APM_PAT")
            if apm_token and "GITHUB_APM_PAT" not in env:
                env["GITHUB_APM_PAT"] = apm_token

    def _setup_llm_tokens(self, env: dict[str, str], available_tokens: dict[str, str]):
        """Set up tokens for LLM CLI."""
        # LLM uses GITHUB_MODELS_KEY, prefer GITHUB_TOKEN if available
        if self.preserve_existing and "GITHUB_MODELS_KEY" in env:
            return

        models_token = self.get_token_for_purpose("models", available_tokens)
        if models_token:
            env["GITHUB_MODELS_KEY"] = models_token


# Convenience functions for common use cases
def setup_runtime_environment(env: dict[str, str] | None = None) -> dict[str, str]:
    """Set up complete runtime environment for all AI CLIs."""
    manager = GitHubTokenManager()
    return manager.setup_environment(env)


def validate_github_tokens(env: dict[str, str] | None = None) -> tuple[bool, str]:
    """Validate GitHub token setup."""
    manager = GitHubTokenManager()
    return manager.validate_tokens(env)


def get_github_token_for_runtime(runtime: str, env: dict[str, str] | None = None) -> str | None:
    """Get the appropriate GitHub token for a specific runtime."""
    manager = GitHubTokenManager()

    # Map runtime names to purposes
    runtime_to_purpose = {
        "copilot": "copilot",
        "codex": "models",
        "llm": "models",
    }

    purpose = runtime_to_purpose.get(runtime)
    if not purpose:
        raise ValueError(f"Unknown runtime: {runtime}")

    return manager.get_token_for_purpose(purpose, env)
