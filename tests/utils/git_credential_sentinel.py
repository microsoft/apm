"""Real Git credential-helper trap used by auth-boundary tests."""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

from apm_cli.utils.git_env import get_git_executable


def credential_helper_trap_env(tmp_path: Path) -> tuple[dict[str, str], Path]:
    """Return an environment with a helper that records any execution."""
    marker = tmp_path / "credential-helper-ran"
    helper = tmp_path / "credential-helper.py"
    helper.write_text(
        f"#!{sys.executable}\n"
        "import os\n"
        "from pathlib import Path\n"
        "Path(os.environ['APM_TEST_HELPER_MARKER']).write_text('ran', encoding='ascii')\n",
        encoding="ascii",
        newline="\n",
    )
    helper.chmod(helper.stat().st_mode | stat.S_IXUSR)
    config = tmp_path / "gitconfig"
    config.write_text(
        f"[credential]\n\thelper = !{helper}\n",
        encoding="ascii",
        newline="\n",
    )
    env = {
        **os.environ,
        "APM_TEST_HELPER_MARKER": str(marker),
        "GIT_CONFIG_GLOBAL": str(config),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
    }
    return env, marker


def exercise_credential_helper(env: dict[str, str], *, host: str, path: str) -> None:
    """Ask real Git for a credential so an unsuppressed helper trips its marker."""
    subprocess.run(
        (get_git_executable(), "credential", "fill"),
        input=f"protocol=https\nhost={host}\npath={path}\n\n",
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
