"""Round-6 env regression traps: intra-line audit forgery + URL-cred leak.

r6-env-1 (MED) -- intra-line ``key=value`` smuggling. The scripts.log header
is a single space-delimited ``key=value`` line and ``target`` (the effective
command, attacker-controlled for a dependency-supplied entry) sits MID-LINE,
BEFORE the real ``status=``. A command embedding ``status=ok event=deploy``
forges those tokens, so a first-match ``status=(\\S+)`` regex, a
whitespace-tokenized key=value parser, or a logfmt consumer reads the
attacker's value. The fix escapes ``=`` in the target so no ``<word>=``
lookalike can appear, while spaces (benign multi-word commands) stay readable.

r6-env-2 (LOW) -- a tokenized URL printed by a script
(``https://user:token@host``) must not persist its credential to the 0600 log
in cleartext. ``_redact_secrets`` only masks credential-NAMED env values; a URL
credential in stdout/stderr/target slipped through. The fix applies
``_redact_embedded_url_credentials`` to those fields.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from apm_cli.core import script_executors as se


def _header_line(log: Path) -> str:
    lines = [ln for ln in log.read_text().splitlines() if ln.startswith("[") and "event=" in ln]
    assert lines, "no header line written"
    return lines[0]


def _logfmt_dict(line: str) -> dict[str, str]:
    """Parse the line the way a whitespace-tokenized logfmt consumer would."""
    out: dict[str, str] = {}
    for tok in line.split():
        if "=" in tok and not tok.startswith("["):
            key, _, val = tok.partition("=")
            out[key] = val  # last-wins, the more dangerous variant
    return out


@pytest.mark.parametrize(
    "malicious_command",
    [
        "evil status=ok event=deploy exit_code=0",
        "deploy.sh status=ok",
        "x event=pre-install status=ok",
    ],
)
def test_target_cannot_forge_header_fields(tmp_path, monkeypatch, malicious_command):
    """A command smuggling status=/event= tokens must not fool log parsers."""
    monkeypatch.setenv("APM_HOME", str(tmp_path))
    se._append_to_script_log(
        "post-install", "command", malicious_command, status="error", exit_code=7
    )
    line = _header_line(tmp_path / "logs" / "scripts.log")

    # First-match regex parser reads the REAL status, not the smuggled one.
    first = re.search(r"(?:^| )status=(\S+)", line)
    assert first is not None and first.group(1) == "error", line

    # Last-wins logfmt dict parser also reads the real fixed fields.
    parsed = _logfmt_dict(line)
    assert parsed.get("status") == "error", line
    assert parsed.get("event") == "post-install", line
    assert parsed.get("exit_code") == "7", line


def test_benign_multiword_command_stays_readable(tmp_path, monkeypatch):
    """Escaping must not mangle ordinary spaced commands like 'echo hi'."""
    monkeypatch.setenv("APM_HOME", str(tmp_path))
    se._append_to_script_log("pre-install", "command", "echo hi there")
    content = (tmp_path / "logs" / "scripts.log").read_text()
    assert "echo hi there" in content, content


def test_bare_email_in_output_is_not_over_redacted(tmp_path, monkeypatch):
    """No scheme:// -> a bare user@host (e.g. an email) must be left intact."""
    monkeypatch.setenv("APM_HOME", str(tmp_path))
    se._append_to_script_log(
        "post-install", "command", "send", stdout="notifying maintainer@example.com done"
    )
    content = (tmp_path / "logs" / "scripts.log").read_text()
    assert "maintainer@example.com" in content, content
