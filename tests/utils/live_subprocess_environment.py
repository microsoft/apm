"""Sanitized subprocess environments for anonymous live HTTPS tests."""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from pathlib import Path

OUTPUT_TAIL_CHARS = 4000

# Live anonymous clones need proxy/CA settings, but never credentials, SSH
# transport controls, or caller-owned git configuration.
LIVE_SUBPROCESS_ENV_ALLOWLIST = frozenset(
    {
        "APM_E2E_TESTS",
        "APM_RUN_INTEGRATION_TESTS",
        "GIT_SSL_CAINFO",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "NO_PROXY",
        "PATH",
        "REQUESTS_CA_BUNDLE",
        "SSL_CERT_FILE",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "WINDIR",
    }
)
LIVE_SUBPROCESS_ENV_DENYLIST = frozenset(
    {
        "ACTIONS_RUNTIME_TOKEN",
        "ADO_APM_PAT",
        "COPILOT_GITHUB_TOKEN",
        "GH_ENTERPRISE_TOKEN",
        "GH_TOKEN",
        "GITHUB_APM_PAT",
        "GITHUB_COPILOT_PAT",
        "GITHUB_ENTERPRISE_TOKEN",
        "GITHUB_MODELS_KEY",
        "GITHUB_PERSONAL_ACCESS_TOKEN",
        "GITHUB_TOKEN",
        "GITLAB_APM_PAT",
        "GITLAB_TOKEN",
        "GIT_ASKPASS",
        "GIT_CONFIG_GLOBAL",
        "GIT_HTTP_EXTRAHEADER",
        "GIT_SSH_COMMAND",
        "NETRC",
        "SSH_AGENT_PID",
        "SSH_ASKPASS",
        "SSH_AUTH_SOCK",
    }
)
_LIVE_SUBPROCESS_ENV_DENY_PREFIXES = ("GITHUB_APM_PAT_",)


def isolated_live_subprocess_env(
    home: Path, *, base_env: Mapping[str, str] | None = None
) -> dict[str, str]:
    """Build a non-interactive environment for an anonymous HTTPS clone."""
    source = os.environ if base_env is None else base_env
    env = {
        key: value
        for key, value in source.items()
        if (normalized := key.upper()) in LIVE_SUBPROCESS_ENV_ALLOWLIST
        and normalized not in LIVE_SUBPROCESS_ENV_DENYLIST
        and not normalized.startswith(_LIVE_SUBPROCESS_ENV_DENY_PREFIXES)
    }
    env["HOME"] = str(home)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["NO_COLOR"] = "1"
    env["APM_E2E_TESTS"] = "1"
    if sys.platform == "win32":
        env["USERPROFILE"] = str(home)
    return env


def tail_output(text: str) -> str:
    """Bound captured command output while preserving its diagnostic tail."""
    if len(text) <= OUTPUT_TAIL_CHARS:
        return text
    return f"[truncated to last {OUTPUT_TAIL_CHARS} chars]\n{text[-OUTPUT_TAIL_CHARS:]}"
