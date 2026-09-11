"""A real PTY and installed command boundary, with a deterministic fake harness.

This is cancellation/terminal coverage, not the required live Copilot demo.
"""

import errno
import json
import os
import re
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


@pytest.mark.parametrize(
    "animate,interrupt,fail",
    [
        (False, True, False),
        (True, True, False),
        (True, False, False),
        (False, False, False),
        (True, False, True),
    ],
)
def test_pty_streams_live_output_and_restores_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, animate: bool, interrupt: bool, fail: bool
) -> None:
    import pty
    import termios

    source_root = Path(__file__).resolve().parents[3]
    project = tmp_path / "fixture"
    shutil.copytree(source_root / "examples/contracts/first-contract", project)
    (project / "handoff.contract.md").write_text(
        "---\nproduces: handoff.json\nverify:\n  handoff: 'test -s handoff.json'\n---\n"
        "Write handoff.json.\n",
        encoding="ascii",
    )
    tools = tmp_path / "bin"
    tools.mkdir()
    actor = tools / "copilot"
    actor.write_text(
        f"#!{sys.executable}\n"
        "import json,sys,time\n"
        "from pathlib import Path\n"
        "if 'mcp' in sys.argv:\n"
        "    print(json.dumps({'mcpServers':{}}),flush=True)\n"
        "    raise SystemExit(0)\n"
        "def emit(kind, data):\n"
        "    print(json.dumps({'type':kind,'data':data}),flush=True)\n"
        "emit('assistant.message_start', {'messageId':'pty-ready','phase':'final_answer'})\n"
        "emit('assistant.message_delta', {'messageId':'pty-ready',"
        "'deltaContent':'PTY actor ready\\n'})\n"
        "emit('tool.execution_start', {'toolName':'view','arguments':'PRIVATE_ARGUMENTS'})\n"
        "emit('assistant.message', {'messageId':'private','phase':'analysis',"
        "'content':'PRIVATE_ANALYSIS'})\n"
        "print('Native stderr ready',file=sys.stderr,flush=True)\n"
        "time.sleep(2)\n"
        "Path('handoff.json').write_text('[]\\n')\n"
        "emit('assistant.message', {'messageId':'pty-ready','content':'PTY actor ready\\n',"
        "'model':'gpt-6-astra'})\n"
        f"print(json.dumps({{'type':'result','exitCode':{int(fail)},'sessionId':'pty','usage':{{}}}}),flush=True)\n"
        f"raise SystemExit({int(fail)})\n",
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
        "TERM": "xterm-256color",
        "CI": "false",
        "APM_PROGRESS": "auto",
    }
    if animate:
        env.pop("NO_COLOR")
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
            "--allow-host-access",
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
    streamed_at: float | None = None
    spinner_seen_while_running = False
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
            if (
                streamed_at is None
                and b"PTY actor ready" in output
                and b"Native stderr ready" in output
                and b"Tool started: view" in output
            ):
                assert child.poll() is None
                streamed_at = time.monotonic()
            if b"Running Copilot" in output and child.poll() is None:
                spinner_seen_while_running = True
            if interrupt and not interrupted and streamed_at is not None:
                if not animate or (
                    spinner_seen_while_running and time.monotonic() - streamed_at >= 0.5
                ):
                    os.kill(child.pid, signal.SIGINT)
                    interrupted = True
            if child.poll() is not None:
                break
        assert streamed_at is not None, output.decode("ascii", errors="replace")
        assert interrupted is interrupt, output.decode("ascii", errors="replace")
        assert child.wait(timeout=2) == (22 if interrupt or fail else 21), output.decode(
            "ascii", errors="replace"
        )
        while select.select([master], [], [], 0.1)[0]:
            output.extend(os.read(master, 65536))
        assert termios.tcgetattr(slave) == settings
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=2)
        os.close(master)
        os.close(slave)
    text = re.sub(rb"\x1b\[[0-?]*[ -/]*[@-~]", b"", bytes(output))
    (tmp_path / "terminal.ansi").write_bytes(output)
    (tmp_path / "terminal.txt").write_bytes(text)
    records = list((project / ".apm" / "runs").glob("*/record.json"))
    assert len(records) == 1
    record = json.loads(records[0].read_text(encoding="utf-8"))
    assert record["complete"] is True
    assert record["result"]["outcome"]["name"] == ("HALTED" if interrupt or fail else "UNPROVEN")
    expected_reason = "cancelled" if interrupt else "producer_failed" if fail else None
    assert record["result"]["stop_reason"] == expected_reason
    assert record["producer"]["cleanup_confirmed"] is True
    assert record["producer"]["returncode"] is not None
    assert text.count(b"PTY actor ready") == 1
    assert b"PRIVATE_" not in output
    assert b"VERIFIED" not in text
    assert (b"UNPROVEN" in text) is not (interrupt or fail)
    assert b"Copilot > PTY actor ready" in text
    assert b"Copilot stderr > Native stderr ready" in text
    assert b"(untrusted)" not in text
    assert b"native-advisory" not in text
    assert b"raw exit" not in text
    assert b"[i]" not in text
    if not interrupt and not fail:
        assert b"Contract checks passed; this run was not sandboxed." in text
        assert b"Output: .apm/runs/" in text
        assert b"Record: .apm/runs/" in text
    if fail:
        assert b"Copilot did not complete successfully." in text
        assert b"Review Copilot diagnostics and logs before retrying." in text
    if animate:
        assert spinner_seen_while_running
        assert b"\x1b[?25l" in output
        assert b"\x1b[?25h" in output
        assert output.count(b"Running Copilot") >= 2
    else:
        assert b"\x1b" not in output
    assert all(byte < 128 for byte in output)
    transcript = (records[0].parent / "transcript.log").read_bytes()
    assert b"\x1b" not in transcript
    assert transcript.count(b"PTY actor ready") == 1
    assert b"Native stderr ready" in transcript
    assert b"PRIVATE_" not in transcript
