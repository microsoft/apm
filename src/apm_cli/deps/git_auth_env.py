"""Git environment construction for APM dependency operations.

Centralizes the three flavours of git env the downloader needs:

1. ``setup_environment`` -- the auth-bearing env used for the
   downloader's primary git ops (clone, fetch, ls-remote with token).
   Sets up GIT_TERMINAL_PROMPT, GIT_ASKPASS, GIT_CONFIG_NOSYSTEM,
   GIT_SSH_COMMAND (with ConnectTimeout), and GIT_CONFIG_GLOBAL
   to a sentinel empty file.

2. ``noninteractive_env`` -- a non-auth env for fallback attempts
   (HTTPS/SSH without a token, plain HTTP). Implements the canonical
   pop-then-conditionally-restore credential-helper fence.

3. ``subprocess_env_dict`` -- the env handed to cache-layer subprocess
   git calls; merges the auth env over a sanitized base so the
   subprocess never inherits a stray ``GIT_DIR`` / ``GIT_CEILING_DIRECTORIES``.

Design pattern: **Builder** -- each public method takes flags and
returns a fully-populated env dict. The builder owns no state apart
from a reference to the surrounding downloader's token manager and
``git_env`` snapshot.
"""

from __future__ import annotations

import os
import sys
from typing import Any


class GitAuthEnvBuilder:
    """Build the various git env dicts the downloader needs."""

    def __init__(self, token_manager) -> None:
        self._token_manager = token_manager

    # -- primary env ----------------------------------------------------

    def setup_environment(self) -> dict[str, Any]:
        """Build the auth-bearing primary git env.

        Mirrors :meth:`GitHubPackageDownloader._setup_git_environment`
        but does not write to the downloader's token-state attributes;
        the caller is responsible for those assignments.
        """
        env = self._token_manager.setup_environment()

        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GIT_ASKPASS"] = "echo"
        env["GIT_CONFIG_NOSYSTEM"] = "1"

        # Ensure SSH connections fail fast instead of hanging indefinitely
        # when a firewall silently drops packets.
        ssh_timeout = "-o ConnectTimeout=30"
        existing_ssh_cmd = os.environ.get("GIT_SSH_COMMAND", "").strip()
        if existing_ssh_cmd:
            if "connecttimeout" not in existing_ssh_cmd.lower():
                env["GIT_SSH_COMMAND"] = f"{existing_ssh_cmd} {ssh_timeout}"
            else:
                env["GIT_SSH_COMMAND"] = existing_ssh_cmd
        else:
            env["GIT_SSH_COMMAND"] = f"ssh {ssh_timeout}"

        if sys.platform == "win32":
            import tempfile

            from ..config import get_apm_temp_dir

            temp_base = get_apm_temp_dir() or tempfile.gettempdir()
            empty_cfg = os.path.join(temp_base, ".apm_empty_gitconfig")
            with open(empty_cfg, "w") as f:  # noqa: F841
                pass
            env["GIT_CONFIG_GLOBAL"] = empty_cfg
        else:
            env["GIT_CONFIG_GLOBAL"] = "/dev/null"

        return env

    # -- noninteractive (fallback) env ----------------------------------

    @staticmethod
    def noninteractive_env(
        base_git_env: dict[str, str],
        *,
        preserve_config_isolation: bool = False,
        suppress_credential_helpers: bool = False,
    ) -> dict[str, str]:
        """Build a non-interactive git env for unauthenticated git operations.

        Credential-helper policy (intentional two-stage design):

        1. Start by clearing ``GIT_ASKPASS`` unconditionally. The default
           APM env sets ``GIT_ASKPASS=echo`` for all authenticated ops; for
           unauthenticated fallback attempts (HTTPS/SSH without a token), we
           want the user's system credential helpers (e.g. macOS Keychain,
           Windows credential manager, SSH agent) to resolve naturally.
        2. Then re-set the full credential-helper *suppression* fence ONLY
           when ``suppress_credential_helpers=True`` (HTTP transport). This
           blocks all four credential channels: ``GIT_ASKPASS``,
           ``GIT_TERMINAL_PROMPT``, ``GIT_CONFIG_NOSYSTEM``, and
           ``credential.helper=`` (via ``GIT_CONFIG_COUNT/KEY/VALUE``).

        Do NOT invert or flatten this pop-then-conditionally-restore pattern
        without re-auditing every caller: removing step 1 would leak
        credentials through user helpers on HTTPS/SSH fallbacks; removing
        step 2 would leak them over plaintext HTTP.
        """
        env = dict(base_git_env)
        env["GIT_TERMINAL_PROMPT"] = "0"
        env.pop("GIT_ASKPASS", None)

        if preserve_config_isolation or suppress_credential_helpers:
            env["GIT_CONFIG_NOSYSTEM"] = "1"
            if "GIT_CONFIG_GLOBAL" in base_git_env:
                env["GIT_CONFIG_GLOBAL"] = base_git_env["GIT_CONFIG_GLOBAL"]
        else:
            env.pop("GIT_CONFIG_GLOBAL", None)
            env.pop("GIT_CONFIG_NOSYSTEM", None)

        if suppress_credential_helpers:
            env["GIT_ASKPASS"] = "echo"
            env["GIT_CONFIG_COUNT"] = "1"
            env["GIT_CONFIG_KEY_0"] = "credential.helper"
            env["GIT_CONFIG_VALUE_0"] = ""
        else:
            env.pop("GIT_CONFIG_COUNT", None)
            env.pop("GIT_CONFIG_KEY_0", None)
            env.pop("GIT_CONFIG_VALUE_0", None)

        return env

    # -- subprocess env dict --------------------------------------------

    @staticmethod
    def subprocess_env_dict(base_git_env: dict[str, str]) -> dict[str, str]:
        """Return a sanitized git env dict for cache-layer subprocess calls.

        Combines the auth-aware ``base_git_env`` with the ambient-state
        sanitization performed by ``git_subprocess_env``. Required for
        every ``GitCache.get_checkout`` call so that private repos
        receive credentials AND the subprocess never inherits a stray
        ``GIT_DIR`` / ``GIT_CEILING_DIRECTORIES`` that would bias the
        cache fetch / integrity verification.
        """
        from ..utils.git_env import git_subprocess_env

        env: dict[str, str] = git_subprocess_env()
        for key, value in base_git_env.items():
            if isinstance(value, str):
                env[key] = value
        return env
