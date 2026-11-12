"""Reject nonregular additive inputs before the real CLI performs any work."""

import os
import subprocess
from pathlib import Path

import pytest

from tests.utils.isolated_apm_environment import IsolatedApmEnvironment

pytestmark = [pytest.mark.e2e, pytest.mark.lifecycle_smoke]


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX named pipes unavailable")
def test_cli_rejects_fifo_without_waiting_for_writer(
    tmp_path: Path, apm_engine_command: tuple[str, ...]
) -> None:
    isolated = IsolatedApmEnvironment.create(tmp_path / "isolated", base_env=os.environ)
    fifo = isolated.work_root / "corporate-ca.pem"
    os.mkfifo(fifo)
    env = isolated.subprocess_env(overrides={"APM_EXTRA_CA_BUNDLE": str(fifo)})
    for name in ("REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "APM_DISABLE_TRUSTSTORE"):
        env.pop(name, None)
    result = subprocess.run(
        [*apm_engine_command, "install"],
        cwd=isolated.work_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode != 0
    assert "APM_EXTRA_CA_BUNDLE must reference a regular file" in result.stderr
    assert "Check the APM_EXTRA_CA_BUNDLE setting, or unset it and retry" in result.stderr
    assert "Traceback" not in result.stderr
    assert not (isolated.work_root / "apm_modules").exists()
