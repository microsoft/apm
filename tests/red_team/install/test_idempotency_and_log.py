"""Vectors 8 + 9 -- idempotency, log creation, and secret redaction.

8. Firing post-install twice runs the scripts twice (no dedupe) -- this
   is the documented/expected behaviour; we pin it so a silent change is
   caught.
9. ~/.apm/logs/scripts.log is created on first write, and denylisted
   secret VALUES that appear in script output are redacted before they
   land in the log.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from .conftest import PYEXE, fire_via_context, trust, write_project


def test_post_install_fires_twice_runs_twice(apm_home: Path, tmp_path: Path) -> None:
    """No idempotency guard: two fires == two executions."""
    project = tmp_path / "proj"
    counter = tmp_path / "runs.log"
    cmd = f'{PYEXE} -c "import sys; open(sys.argv[1], chr(97)).write(chr(120)+chr(10))" "{counter}"'
    apm_yml = write_project(project, "post-install", [cmd])
    trust(apm_yml)

    fire_via_context(project, "post-install")
    fire_via_context(project, "post-install")

    runs = counter.read_text(encoding="utf-8").split()
    assert len(runs) == 2, f"expected 2 executions (no dedupe), got {len(runs)}"


def test_scripts_log_created_on_firing(apm_home: Path, tmp_path: Path) -> None:
    """The first command execution creates $APM_HOME/logs/scripts.log."""
    project = tmp_path / "proj"
    apm_yml = write_project(project, "post-install", ["echo hello-from-script"])
    trust(apm_yml)

    log = apm_home / "logs" / "scripts.log"
    assert not log.exists()

    fire_via_context(project, "post-install")

    assert log.is_file(), "scripts.log must be created on first write"
    assert "hello-from-script" in log.read_text(encoding="utf-8")


def test_secret_value_redacted_in_log(
    apm_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A denylisted env-var VALUE echoed by a script is redacted in the log.

    NOVEL FINDING: stdout/stderr ARE redacted, but the command string is
    logged verbatim in the `target=` header field with NO redaction. A
    secret that appears in the command itself leaks to scripts.log even
    though the same value is masked in stdout.
    """
    secret = "supersecretvalue123"
    monkeypatch.setenv("MY_SECRET", secret)

    project = tmp_path / "proj"
    apm_yml = write_project(project, "post-install", [f"echo {secret}"])
    trust(apm_yml)

    fire_via_context(project, "post-install")

    log_text = (apm_home / "logs" / "scripts.log").read_text(encoding="utf-8")
    # The stdout-redaction path works as designed:
    assert "[REDACTED]" in log_text, "expected the secret value to be redacted"
    # ...but the secure invariant is that the secret appears NOWHERE in the log.
    assert secret not in log_text, (
        "SECRET LEAK: cleartext secret persisted to scripts.log via the "
        "unredacted command 'target=' header field (only stdout/stderr are "
        "passed through _redact_secrets)."
    )
