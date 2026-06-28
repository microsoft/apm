"""Round-2 environment / log-hardening regression sweep.

Targets the five genuine breaks the env agent surfaced against the
round-1 redaction + logging fixes:

* e-1  ``PWD``/``PASSWD`` denylist gap (real password vars leaked while
       the bare shell ``$PWD``/``$OLDPWD`` must stay usable).
* e-2  credential *blob* names whose benign suffix the token regex
       cannot express (``DOCKER_AUTH_CONFIG``, ``BASIC_AUTH``,
       ``*_CONNECTION_STRING``).
* e-3  word-boundary under-redaction (a secret VALUE glued to adjacent
       characters slipped past the boundary-aware round-1 masker).
* e-4  log file world-readable / symlink-followed (``0644`` + no
       ``O_NOFOLLOW``).
* e-5  unbounded log growth + unbounded per-entry fields.

Every assertion encodes the SECURE expected behaviour, so a regression
in any of the five fixes fails here.
"""

from __future__ import annotations

import os
import stat

from apm_cli.core import script_executors
from apm_cli.core.script_executors import (
    _append_to_script_log,
    _is_denylisted,
    _matches_credential,
    _redact_secrets,
)

_NONE: frozenset[str] = frozenset()


# -- e-1: PWD / PASSWD denylist gap ---------------------------------------


class TestPwdPasswdDenylist:
    def test_real_password_vars_are_denylisted(self) -> None:
        for name in ("MYSQL_PWD", "DB_PWD", "PASSWD", "POSTGRES_PASSWD"):
            assert _is_denylisted(name, _NONE), f"{name} must be treated as a credential"

    def test_bare_working_directory_vars_are_exempt(self) -> None:
        # $PWD / $OLDPWD end in the PWD token but hold a path, not a secret.
        assert not _is_denylisted("PWD", _NONE)
        assert not _is_denylisted("OLDPWD", _NONE)
        assert not _matches_credential("PWD")
        assert not _matches_credential("OLDPWD")


# -- e-2: credential-blob names -------------------------------------------


class TestCredentialBlobNames:
    def test_docker_and_basic_auth_blobs_match(self) -> None:
        assert _matches_credential("DOCKER_AUTH_CONFIG")
        assert _matches_credential("BASIC_AUTH")

    def test_connection_string_suffix_matches(self) -> None:
        for name in ("DATABASE_CONNECTION_STRING", "PGCONNECTIONSTRING"):
            assert _matches_credential(name), f"{name} embeds a password"

    def test_auth_config_suffix_matches(self) -> None:
        assert _matches_credential("REGISTRY_AUTH_CONFIG")

    def test_benign_names_are_not_flagged(self) -> None:
        for name in ("PATH", "HOME", "LANG", "TRACE_ID", "EDITOR"):
            assert not _matches_credential(name), f"{name} is not a credential"


# -- e-3: glued-value redaction -------------------------------------------


class TestGluedValueRedaction:
    def test_long_secret_glued_to_text_is_masked(self, monkeypatch) -> None:
        secret = "tok_blocked_value_xyz123"  # 24 chars, well over the floor
        monkeypatch.setenv("API_TOKEN", secret)
        out = _redact_secrets(f"echo{secret}NOW and again {secret}.")
        assert secret not in out
        assert out.count("[REDACTED]") == 2

    def test_short_value_does_not_corrupt_unrelated_text(self, monkeypatch) -> None:
        # A 4-char value is a common substring; masking it would corrupt
        # ordinary words. The length floor must skip it.
        monkeypatch.setenv("APP_TOKEN", "test")
        out = _redact_secrets("running tests in the latest build")
        assert out == "running tests in the latest build"

    def test_blob_value_is_masked_even_with_benign_name_suffix(self, monkeypatch) -> None:
        secret = "dGhpcy1pcy1hLXJlZ2lzdHJ5LXNlY3JldA=="
        monkeypatch.setenv("DOCKER_AUTH_CONFIG", secret)
        out = _redact_secrets(f"pushing with {secret}")
        assert secret not in out
        assert "[REDACTED]" in out


# -- e-4 / e-5: log file hardening ----------------------------------------


class TestLogFileHardening:
    def _log_path(self, tmp_path, monkeypatch):
        monkeypatch.setenv("APM_HOME", str(tmp_path))
        return script_executors._get_scripts_log_path()

    def test_log_file_is_owner_only_0600(self, tmp_path, monkeypatch) -> None:
        log_path = self._log_path(tmp_path, monkeypatch)
        _append_to_script_log("post-install", "command", "echo hi", status="ok")
        assert log_path.exists()
        mode = stat.S_IMODE(log_path.stat().st_mode)
        assert mode == 0o600, f"log mode {oct(mode)} exposes secrets to other users"
        dir_mode = stat.S_IMODE(log_path.parent.stat().st_mode)
        assert dir_mode == 0o700, f"log dir mode {oct(dir_mode)} is too permissive"

    def test_preplanted_symlink_is_not_followed(self, tmp_path, monkeypatch) -> None:
        log_path = self._log_path(tmp_path, monkeypatch)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        evil = tmp_path / "evil_target.txt"
        evil.write_text("original")
        os.symlink(evil, log_path)

        # O_NOFOLLOW: the open must refuse the symlink; the write is
        # swallowed and the attacker-controlled target stays untouched.
        _append_to_script_log("post-install", "command", "echo hi", status="ok")
        assert evil.read_text() == "original"

    def test_per_entry_output_is_truncated(self, tmp_path, monkeypatch) -> None:
        log_path = self._log_path(tmp_path, monkeypatch)
        huge = "A" * (200 * 1024)
        _append_to_script_log("post-install", "command", "noisy", stdout=huge, status="ok")
        contents = log_path.read_text()
        assert "...[truncated]" in contents
        assert len(contents) < 64 * 1024, "a single entry should not bloat the log"

    def test_oversized_log_is_rotated(self, tmp_path, monkeypatch) -> None:
        log_path = self._log_path(tmp_path, monkeypatch)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        # Seed an over-cap log (>5 MiB) then trigger one more append.
        log_path.write_bytes(b"x" * (5 * 1024 * 1024 + 16))
        _append_to_script_log("post-install", "command", "echo hi", status="ok")
        rotated = log_path.with_name(log_path.name + ".1")
        assert rotated.exists(), "log past the size cap must rotate to .1"
        # The fresh log holds only the new entry, not the seeded bytes.
        assert log_path.stat().st_size < 5 * 1024 * 1024


# -- r3-env-1: prefix-fragmentation under-redaction ------------------------


class TestPrefixFragmentationRedaction:
    """A shorter secret that is a PREFIX of a longer one must not split it.

    Left-to-right substring replacement is order-sensitive: if the short
    credential is masked first it fragments the longer value, leaking the
    longer value's tail to scripts.log. _redact_secrets must mask
    longest-first so the longer value is fully redacted.
    """

    def test_longer_secret_not_fragmented_by_prefix(self, monkeypatch) -> None:
        short = "abcd1234"
        longer = short + "efgh5678ijkl9012"
        monkeypatch.setenv("SHORT_TOKEN", short)
        monkeypatch.setenv("LONG_TOKEN", longer)

        out = _redact_secrets(f"see {longer} and {short} in output")

        # No part of either secret may survive in cleartext.
        assert short not in out
        assert longer not in out
        assert "efgh5678ijkl9012" not in out, "longer secret tail leaked (prefix fragmentation)"
        assert "[REDACTED]" in out


# -- r4-env-1: audit-log line injection via stdout/stderr newlines ----------


class TestLogLineInjection:
    """Newlines in a script's stdout/stderr must not forge new log entries.

    scripts.log is the audit trail that catches malicious scripts, so a field
    derived from attacker-controlled output must never contain a raw CR/LF --
    otherwise the script can emit a line that, at column 0, is byte-identical
    to a genuine ``[ts] event=... status=ok`` entry and forge or bury records.
    """

    def _log_path(self, tmp_path, monkeypatch):
        monkeypatch.setenv("APM_HOME", str(tmp_path))
        return script_executors._get_scripts_log_path()

    def test_stdout_newline_cannot_forge_entry(self, tmp_path, monkeypatch) -> None:
        log_path = self._log_path(tmp_path, monkeypatch)
        forged = (
            "step done\r\n"
            "[2030-05-05T00:00:00Z] event=preinstall type=command "
            "target=clean status=ok exit_code=0\r\n"
            "  stdout: nothing to see\r\n"
        )
        _append_to_script_log(
            "preinstall", "command", "attacker.sh", stdout=forged, status="error", exit_code=1
        )
        contents = log_path.read_text()
        # No line may begin with the forged timestamp at column 0.
        forged_at_col0 = [ln for ln in contents.splitlines() if ln.startswith("[2030-05-05")]
        assert forged_at_col0 == [], "script forged a column-0 audit entry via stdout newlines"
        # The real entry's header is the only bracketed line at column 0.
        headers = [ln for ln in contents.splitlines() if ln.startswith("[") and "event=" in ln]
        assert len(headers) == 1
        assert "status=error" in headers[0]
        # The injected payload survives as escaped text on the stdout field line.
        assert "\\r\\n" in contents or "\\n" in contents

    def test_target_newline_cannot_forge_entry(self, tmp_path, monkeypatch) -> None:
        log_path = self._log_path(tmp_path, monkeypatch)
        evil_cmd = (
            "echo hi\n[2030-01-01T00:00:00Z] event=postinstall type=command target=x status=ok"
        )
        _append_to_script_log("post-install", "command", evil_cmd, status="ok")
        contents = log_path.read_text()
        assert [ln for ln in contents.splitlines() if ln.startswith("[2030-01-01")] == []
