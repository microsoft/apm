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
- GitHub: GITHUB_APM_PAT -> GITHUB_TOKEN -> GH_TOKEN -> git credential helpers
- Azure DevOps: ADO_APM_PAT

Runtime Requirements:
- Codex CLI: Uses GITHUB_TOKEN (must be user-scoped for GitHub Models)
"""

import os
import subprocess
import sys
from typing import Dict, Optional, Tuple  # noqa: F401, UP035


def _format_credential_host(host: str, port: int | None) -> str:
    """Embed a custom port into the git credential ``host`` field.

    Per ``gitcredentials(7)``, there is no standalone ``port=`` attribute in
    the credential protocol -- port must be embedded into the host field as
    ``host:port``. Sending a separate ``port=`` line is silently ignored by
    helpers, collapsing two different services into one credential entry.

    Uses ``is not None`` (not truthy) so that ``None`` is the only sentinel
    for "no port", matching the rest of the port-handling logic.
    """
    return f"{host}:{port}" if port is not None else host


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
        "modules": ["GITHUB_APM_PAT", "GITHUB_TOKEN", "GH_TOKEN"],  # APM module access (GitHub)
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
        """Validate that a credential-fill token looks like a real credential.

        Rejects garbage values that can appear when GIT_ASKPASS or credential
        helpers return prompt text instead of actual tokens.
        """
        if not token:
            return False
        if len(token) > 1024:
            return False
        if any(c in token for c in (" ", "\t", "\n", "\r")):
            return False
        prompt_fragments = ("Password for", "Username for", "password for", "username for")
        if any(fragment in token for fragment in prompt_fragments):  # noqa: SIM103
            return False
        return True

    # `git credential fill` may invoke OS credential helpers that show
    # interactive dialogs (e.g. Windows Credential Manager account picker).
    # The 60s default prevents false negatives on slow helpers.
    DEFAULT_CREDENTIAL_TIMEOUT = 60
    MAX_CREDENTIAL_TIMEOUT = 180

    @classmethod
    def _get_credential_timeout(cls) -> int:
        """Return timeout (seconds) for ``git credential fill``.

        Configurable via ``APM_GIT_CREDENTIAL_TIMEOUT`` (1–180).
        """
        raw = os.environ.get("APM_GIT_CREDENTIAL_TIMEOUT", "").strip()
        if not raw:
            return cls.DEFAULT_CREDENTIAL_TIMEOUT
        try:
            val = int(raw)
        except ValueError:
            return cls.DEFAULT_CREDENTIAL_TIMEOUT
        return max(1, min(val, cls.MAX_CREDENTIAL_TIMEOUT))

    @staticmethod
    def resolve_credential_from_git(host: str, port: int | None = None) -> str | None:
        """Resolve a credential from the git credential store.

        Uses `git credential fill` to query the user's configured credential
        helpers (macOS Keychain, Windows Credential Manager, gh CLI, etc.).
        This is the same mechanism git clone uses internally.

        Args:
            host: The git host to resolve credentials for (e.g., "github.com")
            port: Optional non-standard git port (e.g. 7999 for Bitbucket DC).
                Embedded into the ``host`` field per ``gitcredentials(7)`` --
                a standalone ``port=`` line is not part of the protocol.

        Returns:
            The password/token from the credential store, or None if unavailable
        """
        host_field = _format_credential_host(host, port)
        try:
            result = subprocess.run(
                ["git", "credential", "fill"],
                input=f"protocol=https\nhost={host_field}\n\n",
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=GitHubTokenManager._get_credential_timeout(),
                env={
                    **os.environ,
                    "GIT_TERMINAL_PROMPT": "0",
                    "GIT_ASKPASS": "" if sys.platform != "win32" else "echo",
                },
            )
            if result.returncode != 0:
                return None

            for line in result.stdout.splitlines():
                if line.startswith("password="):
                    token = line[len("password=") :]
                    if token and GitHubTokenManager._is_valid_credential_token(token):
                        return token
                    return None
            return None
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return None

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
        queries the git credential store as a last resort. Results are cached
        per ``(host, port)`` to avoid repeated subprocess calls while keeping
        same-host-different-port credentials separate.

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
        for purpose, token_vars in self.TOKEN_PRECEDENCE.items():  # noqa: B007
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
