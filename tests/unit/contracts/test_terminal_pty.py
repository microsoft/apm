"""Real POSIX PTY/pipe actors, not real-model or artifact-assessment demos."""

import errno
import json
import os
import select
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.component,
    pytest.mark.skipif(os.name != "posix", reason="The native leaf supervisor requires POSIX."),
]

_ACTOR = r"""
import os
import sys
from dataclasses import replace
from pathlib import Path
from apm_cli.contracts.events import EventEmitter
from apm_cli.contracts.models import ContractLimits, ProcessRequest, RunResult
from apm_cli.contracts.process import supervise_process
from apm_cli.contracts.records import AttemptStore, reduce_outcome
from apm_cli.contracts.stream import ContractStreamDecoder
from apm_cli.core.contract_logger import ContractLogger

directory = Path(sys.argv[1])
pause = float(sys.argv[2])
logger = ContractLogger()
logger.attach_run("pty-run", directory)
events = EventEmitter("pty-run", logger.on_event)
decoder = ContractStreamDecoder(events)
events.emit(
    "selected",
    contract=str(directory / "long-source-name.contract.md"),
    harness="copilot",
    model="gpt-6-astra",
    run_directory=str(directory),
)
events.emit("phase", name="execution")
native = '''
import json, time
def emit(kind, data):
    print(json.dumps({"type": kind, "data": data}), flush=True)
emit("assistant.intent", {"intent": "Creating the handoff"})
emit("assistant.message_start", {"messageId": "first", "phase": "final_answer"})
emit("assistant.message_delta", {"messageId": "first", "deltaContent": "Useful stream before completion\\n"})
time.sleep(PAUSE)
emit("assistant.message", {"messageId": "first", "phase": "final_answer", "content": "Useful stream before completion\\n"})
emit("assistant.message", {"messageId": "second", "phase": "final_answer", "content": "Native work ended"})
emit("assistant.idle", {})
print(json.dumps({"type": "result", "exitCode": 0, "sessionId": "pty-fixture", "usage": {}}), flush=True)
'''.replace("PAUSE", str(pause))
observed = supervise_process(
    ProcessRequest((sys.executable, "-c", native), directory, 8),
    on_bytes=decoder.feed,
    events=events,
    limits=replace(ContractLimits(), cleanup_seconds=1),
)
decoder.finish()
events.emit("phase", name="record")
result = RunResult(
    "pty-run", directory, reduce_outcome(None, (), observed.stop_reason),
    None, (), stop_reason=observed.stop_reason,
)
logger.close()
AttemptStore("pty-run", directory, {"test_actor": True}).finish(result)
events.emit("finished", result=result)
logger.close()
if os.isatty(0):
    # Keep the terminal session alive for the parent to inspect attributes.
    # Darwin detaches the slave when its controlling session leader exits.
    os.read(0, 1)
"""


def _read(fd: int, timeout: float = 0.1) -> bytes:
    if not select.select([fd], [], [], timeout)[0]:
        return b""
    try:
        return os.read(fd, 65536)
    except OSError as error:
        if error.errno != errno.EIO:
            raise
        return b""


@pytest.mark.parametrize("cancel", [False, True])
@pytest.mark.parametrize("width", [30, 40, 80])
def test_real_pty_streams_before_completion_and_restores_terminal(
    tmp_path: Path, monkeypatch, cancel: bool, width: int
) -> None:
    import fcntl
    import pty
    import struct
    import termios

    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("COLUMNS", str(width))
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, width, 0, 0))
    before = termios.tcgetattr(slave)
    pid = os.fork()
    if pid == 0:
        try:
            os.close(master)
            os.setsid()
            fcntl.ioctl(slave, termios.TIOCSCTTY, 0)
            for descriptor in (0, 1, 2):
                os.dup2(slave, descriptor)
            if slave > 2:
                os.close(slave)
            # Fixed local interpreter and test-owned source, never shell input.
            os.execv(  # noqa: S606
                sys.executable,
                [sys.executable, "-c", _ACTOR, str(tmp_path), "5" if cancel else "1"],
            )
        finally:
            os._exit(127)
    output = bytearray()
    exited = False
    streamed = False
    restored = False
    deadline = time.monotonic() + 12
    try:
        while time.monotonic() < deadline:
            output.extend(_read(master))
            # PTYs preserve logical lines; terminal emulators wrap visually.
            text = output.decode("ascii", errors="strict").replace("\r", "")
            if not streamed and "Useful stream before completion" in text:
                assert "UNPROVEN" not in text
                assert "HALTED" not in text
                assert os.waitpid(pid, os.WNOHANG) == (0, 0)
                streamed = True
                if cancel:
                    os.write(master, b"\x03")
            if not restored and ("UNPROVEN" in text or "HALTED" in text):
                assert termios.tcgetattr(slave) == before
                restored = True
                os.write(master, b"\n")
            found, status = os.waitpid(pid, os.WNOHANG)
            if found:
                exited = True
                output.extend(_read(master))
                assert os.waitstatus_to_exitcode(status) == 0, output.decode()
                break
        assert exited, output.decode()
        assert streamed, output.decode()
        assert restored, output.decode()
        text = output.decode("ascii").replace("\r", "")
        assert "\x1b" not in text
        lines = text.splitlines()
        assert f"[>] Contract: {tmp_path / 'long-source-name.contract.md'}" in lines
        assert f"[i] Record and logs: {tmp_path}" in lines
        assert "[i] copilot (untrusted): Useful stream before completion" in lines
        if cancel:
            words = " ".join(line[4:].strip() for line in text.splitlines())
            assert (
                words.index("Stop requested")
                < words.index("stop confirmed")
                < words.index("HALTED")
            )
            assert "UNPROVEN" not in text
        else:
            assert text.count("UNPROVEN") == 1
        record = json.loads((tmp_path / "record.json").read_bytes())
        assert record["complete"]
        assert (tmp_path / "transcript.log").is_file()
    finally:
        if not exited:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
        os.close(master)
        os.close(slave)


def test_real_closed_stdout_pipe_keeps_draining_and_finishes_record(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    child = subprocess.Popen(
        [sys.executable, "-c", _ACTOR, str(tmp_path), "0.5"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert child.stdout is not None
    assert child.stderr is not None
    try:
        assert b"Contract:" in child.stdout.readline()
        child.stdout.close()
        child.wait(timeout=12)
        errors = child.stderr.read()
        assert child.returncode == 0, errors.decode()
        record = json.loads((tmp_path / "record.json").read_bytes())
        assert record["complete"]
        transcript = (tmp_path / "transcript.log").read_text()
        assert "Native work ended" in transcript
        assert "Traceback" not in errors.decode()
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=3)
        child.stderr.close()
