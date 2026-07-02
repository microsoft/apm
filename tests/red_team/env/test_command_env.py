"""Vector 5 + 8: command subprocess env exfiltration and allowlist normalisation.

Runs the REAL ``_execute_command`` so a script can dump its environment
to a file and we can read back exactly what the subprocess saw. Proves
the suffix-gap families remain readable to lifecycle command scripts.

Also documents the intentional (non-break) ``script.env`` re-add path and
checks that whitespace-padded allowlist entries cannot smuggle a never-expand
auth var past the filter.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from apm_cli.core.lifecycle_scripts import ScriptEntry
from apm_cli.core.script_executors import _build_script_env, _execute_command

from .fixtures.helpers import make_event


def _dump_env_script(out_path: Path) -> ScriptEntry:
    """A command script that writes os.environ to *out_path* as KEY=VALUE."""
    py = sys.executable
    inline = (
        "import os;"
        f"open(r'{out_path}','w',encoding='utf-8').write("
        "'\\n'.join(f'{k}={v}' for k,v in os.environ.items()))"
    )
    return ScriptEntry(
        script_type="command",
        event="post-install",
        bash=f'{py} -c "{inline}"',
        command=f'{py} -c "{inline}"',
    )


class TestCommandEnvExfiltration:
    def test_suffix_gap_secret_readable_by_subprocess(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/keys/exfil-target.json")
        out_path = tmp_path / "env_dump.txt"
        script = _dump_env_script(out_path)
        _execute_command(script, make_event())
        dumped = out_path.read_text(encoding="utf-8")
        # Secure expectation: the credential-bearing var never reaches the child.
        assert "GOOGLE_APPLICATION_CREDENTIALS=/keys/exfil-target.json" not in dumped, (
            "suffix-gap secret was readable by the lifecycle command subprocess"
        )

    def test_blocked_secret_not_readable_by_subprocess(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "wJalrSecretExampleKey")
        out_path = tmp_path / "env_dump.txt"
        script = _dump_env_script(out_path)
        _execute_command(script, make_event())
        dumped = out_path.read_text(encoding="utf-8")
        assert "wJalrSecretExampleKey" not in dumped


class TestScriptEnvReAdd:
    """script.env re-adding a denylisted name is intentional convenience, not a break."""

    def test_script_env_reintroduces_denylisted_name(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ANALYTICS_TOKEN", raising=False)
        script = ScriptEntry(
            script_type="command",
            event="post-install",
            bash="env",
            env={"ANALYTICS_TOKEN": "explicit-author-value"},
        )
        env = _build_script_env(script)
        # Documented intended behaviour (is_genuine_break=false): the author
        # explicitly set this in apm.yml, so it is honoured.
        assert env.get("ANALYTICS_TOKEN") == "explicit-author-value"


class TestAllowlistNormalisation:
    """Whitespace-padded allowlist entries must not unblock a never-expand var."""

    def test_padded_allowlist_entry_does_not_unblock(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_padded_bypass")
        # " GITHUB_TOKEN " does not match the exact set membership, and even an
        # exact match is overridden by _NEVER_EXPAND -- either way it stays out.
        script = ScriptEntry(
            script_type="command",
            event="post-install",
            bash="env",
            allowed_env_vars=[" GITHUB_TOKEN "],
        )
        env = _build_script_env(script)
        assert "GITHUB_TOKEN" not in env
