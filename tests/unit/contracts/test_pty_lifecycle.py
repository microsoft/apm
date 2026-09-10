"""A real PTY and installed command boundary, with a deterministic fake harness.

This is cancellation/terminal coverage, not the required live Copilot demo.
"""

import errno
import json
import os
import select
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.component,
    pytest.mark.skipif(os.name != "posix", reason="PTY and process-group exercise requires POSIX"),
]


def test_pty_interrupt_leaves_halted_record_and_no_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import pty
    import termios

    source_root = Path(__file__).resolve().parents[3]
    project = tmp_path / "fixture"
    shutil.copytree(source_root / "examples/contracts/first-contract", project)
    tools = tmp_path / "bin"
    tools.mkdir()
    actor = tools / "copilot"
    actor.write_text(
        f"#!{sys.executable}\n"
        "import json,sys,time\n"
        "if 'mcp' in sys.argv:\n"
        "    print(json.dumps({'mcpServers':{}}),flush=True)\n"
        "    raise SystemExit(0)\n"
        "print(json.dumps({'type':'assistant.message','data':"
        "{'messageId':'pty-ready','content':'PTY actor ready','model':'gpt-6-astra'}}),flush=True)\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    actor.chmod(0o755)
    home = tmp_path / "home"
    config_dir = home / ".apm"
    config_dir.mkdir(parents=True)
    (config_dir / "config.json").write_text(
        '{"experimental": {"contracts": true}}\n',
        encoding="ascii",
    )
    monkeypatch.delenv("APM_NO_SCRIPTS", raising=False)
    monkeypatch.delenv("APM_POLICY_DISABLE", raising=False)
    env = {
        **os.environ,
        "PATH": str(tools) + os.pathsep + os.environ.get("PATH", ""),
        "HOME": str(home),
        "PYTHONPATH": str(source_root / "src"),
        "NO_COLOR": "1",
        "COLUMNS": "48",
    }
    master, slave = pty.openpty()
    settings = termios.tcgetattr(slave)
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "from apm_cli.cli import cli; cli()",
            "run",
            "handoff.contract.md",
            "--on",
            "copilot",
            "--model",
            "gpt-6-astra",
            "--allow-advisory",
        ],
        cwd=project,
        env=env,
        stdin=slave,
        stdout=slave,
        stderr=slave,
        start_new_session=True,
    )
    output = bytearray()
    interrupted = False
    deadline = time.monotonic() + 15
    try:
        while time.monotonic() < deadline:
            if select.select([master], [], [], 0.1)[0]:
                try:
                    chunk = os.read(master, 65536)
                except OSError as exc:
                    if exc.errno == errno.EIO:
                        break
                    raise
                if not chunk:
                    break
                output.extend(chunk)
            if not interrupted and b"PTY actor ready" in output:
                os.kill(child.pid, signal.SIGINT)
                interrupted = True
            if child.poll() is not None:
                break
        assert interrupted, output.decode("ascii", errors="replace")
        assert child.wait(timeout=2) == 22, output.decode("ascii", errors="replace")
        assert termios.tcgetattr(slave) == settings
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=2)
        os.close(master)
        os.close(slave)
    records = list((project / ".apm" / "runs").glob("*/record.json"))
    assert len(records) == 1
    record = json.loads(records[0].read_text(encoding="utf-8"))
    assert record["complete"] is True
    assert record["result"]["outcome"]["name"] == "HALTED"
    assert record["result"]["stop_reason"] == "cancelled"
    assert record["producer"]["cleanup_confirmed"] is True
    assert record["producer"]["returncode"] is not None
    assert b"VERIFIED" not in output
    assert b"\x1b" not in output
    assert all(byte < 128 for byte in output)
