"""End-to-end integration tests for `apm update` and `apm install --frozen`.

Issue: https://github.com/microsoft/apm/issues/1203 (P0).

Validates the full pipeline against a real GitHub package:

* `apm update --dry-run` resolves, renders a plan, and writes nothing.
* `apm update --yes` after install with no manifest changes is a no-op.
* `apm install --frozen` succeeds against an in-sync lockfile.
* `apm install --frozen` exits non-zero when lockfile is missing.
* `apm install --frozen` exits non-zero when manifest declares a dep
  not present in the lockfile.
* `apm install --frozen --update` is rejected as a usage error.

Uses the real `microsoft/apm-sample-package`. Requires GITHUB_APM_PAT
or GITHUB_TOKEN.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.skipif(
    not os.environ.get("GITHUB_APM_PAT") and not os.environ.get("GITHUB_TOKEN"),
    reason="GITHUB_APM_PAT or GITHUB_TOKEN required for GitHub API access",
)


@pytest.fixture
def apm_command():
    apm_on_path = shutil.which("apm")
    if apm_on_path:
        return apm_on_path
    venv_apm = Path(__file__).parent.parent.parent / ".venv" / "bin" / "apm"
    if venv_apm.exists():
        return str(venv_apm)
    return "apm"


@pytest.fixture
def temp_project(tmp_path):
    project_dir = tmp_path / "update-test"
    project_dir.mkdir()
    (project_dir / ".github").mkdir()
    # Per #1154, vscode/copilot target detection requires this signal file.
    (project_dir / ".github" / "copilot-instructions.md").write_text("# test\n")
    return project_dir


def _run_apm(apm_command, args, cwd, timeout=180, stdin_input=None):
    return subprocess.run(
        [apm_command] + args,  # noqa: RUF005
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        input=stdin_input,
    )


def _write_apm_yml(project_dir: Path, apm_packages: list[str]) -> None:
    config = {
        "name": "update-test",
        "version": "1.0.0",
        "dependencies": {"apm": apm_packages, "mcp": []},
    }
    (project_dir / "apm.yml").write_text(
        yaml.dump(config, default_flow_style=False), encoding="utf-8"
    )


class TestUpdateE2E:
    def test_update_dry_run_writes_nothing(self, temp_project, apm_command):
        """`apm update --dry-run` prints a plan and writes no artifacts."""
        _write_apm_yml(temp_project, ["microsoft/apm-sample-package"])

        result = _run_apm(apm_command, ["update", "--dry-run"], temp_project)

        assert result.returncode == 0, (
            f"Dry-run failed:\nSTDOUT: {result.stdout}\nSTDERR: {result.stderr}"
        )
        assert "Dry run" in result.stdout or "plan" in result.stdout.lower()
        assert not (temp_project / "apm.lock.yaml").exists()

    def test_update_after_install_no_changes_short_circuits(self, temp_project, apm_command):
        """After `apm install`, a follow-up `apm update --yes` with no
        manifest changes should report all-up-to-date and not fail."""
        _write_apm_yml(temp_project, ["microsoft/apm-sample-package"])

        first = _run_apm(apm_command, ["install"], temp_project)
        assert first.returncode == 0, (
            f"Initial install failed:\nSTDOUT: {first.stdout}\nSTDERR: {first.stderr}"
        )
        assert (temp_project / "apm.lock.yaml").exists()

        result = _run_apm(apm_command, ["update", "--yes"], temp_project)

        assert result.returncode == 0, (
            f"Update failed:\nSTDOUT: {result.stdout}\nSTDERR: {result.stderr}"
        )


class TestFrozenE2E:
    def test_frozen_succeeds_against_in_sync_lockfile(self, temp_project, apm_command):
        _write_apm_yml(temp_project, ["microsoft/apm-sample-package"])

        first = _run_apm(apm_command, ["install"], temp_project)
        assert first.returncode == 0, first.stderr

        # Re-run with --frozen on the same manifest+lockfile.
        result = _run_apm(apm_command, ["install", "--frozen"], temp_project)

        assert result.returncode == 0, (
            f"Frozen install failed on in-sync project:\n{result.stdout}\n{result.stderr}"
        )

    def test_frozen_fails_when_lockfile_missing(self, temp_project, apm_command):
        _write_apm_yml(temp_project, ["microsoft/apm-sample-package"])

        result = _run_apm(apm_command, ["install", "--frozen"], temp_project)

        assert result.returncode != 0
        combined = result.stdout + result.stderr
        assert "frozen" in combined.lower() or "lock" in combined.lower()

    def test_frozen_fails_when_manifest_adds_undeclared_dep(self, temp_project, apm_command):
        """Lockfile present but manifest gained a dep that isn't in lock -> fail."""
        _write_apm_yml(temp_project, ["microsoft/apm-sample-package"])
        first = _run_apm(apm_command, ["install"], temp_project)
        assert first.returncode == 0, first.stderr

        _write_apm_yml(
            temp_project,
            ["microsoft/apm-sample-package", "microsoft/some-other-not-in-lock"],
        )

        result = _run_apm(apm_command, ["install", "--frozen"], temp_project)

        assert result.returncode != 0
        combined = result.stdout + result.stderr
        assert "out of sync" in combined.lower() or "missing" in combined.lower()

    def test_frozen_with_update_is_rejected(self, temp_project, apm_command):
        _write_apm_yml(temp_project, ["microsoft/apm-sample-package"])

        result = _run_apm(apm_command, ["install", "--frozen", "--update"], temp_project)

        assert result.returncode != 0
        combined = result.stdout + result.stderr
        assert "frozen" in combined.lower() and "update" in combined.lower()


class TestUpdateInteractiveDecline:
    """Cover the interactive 'user declines the plan' path end-to-end.

    The unit-tier CliRunner test mocks ``click.confirm`` and exits
    before the install pipeline runs. This class exercises the full
    real-binary flow with a real TTY-attached subprocess so the test
    catches regressions in TTY detection, stdin plumbing, and the
    lockfile-untouched contract on decline.

    Skipped on Windows: ``pty.openpty`` is POSIX-only.
    """

    @pytest.mark.skipif(
        sys.platform.startswith("win"),
        reason="pty.openpty is POSIX-only; decline path is covered by unit tests on Windows",
    )
    def test_decline_at_prompt_leaves_lockfile_untouched(self, temp_project, apm_command):
        """``apm update`` with TTY stdin and 'n' answer mutates nothing.

        Sequence:

        1. Install the sample package -> writes ``apm.lock.yaml``
           pinning the resolved commit.
        2. Mutate ``apm.yml`` to pin a new ref so the next resolve
           sees a real planned change to confirm.
        3. Spawn ``apm update`` with a real PTY attached to stdin so
           the prompt actually fires.
        4. Send ``n\\n`` to decline.
        5. Assert: rc == 0, "No changes applied" surfaces, and the
           lockfile commit field is byte-identical to the pre-update
           state (the decline path performed no on-disk mutation).
        """
        _write_apm_yml(temp_project, ["microsoft/apm-sample-package"])
        first = _run_apm(apm_command, ["install"], temp_project)
        assert first.returncode == 0, first.stderr

        lockfile = temp_project / "apm.lock.yaml"
        assert lockfile.exists(), "install should have produced apm.lock.yaml"
        lockfile_before = lockfile.read_bytes()

        _write_apm_yml_with_ref(
            temp_project,
            "microsoft/apm-sample-package",
            ref="main",
        )

        rc, output = _run_apm_with_pty(
            apm_command,
            ["update"],
            cwd=temp_project,
            stdin_text="n\n",
            timeout=180,
        )

        assert rc == 0, f"apm update (declined) returned rc={rc}\noutput:\n{output}"
        assert "no changes applied" in output.lower(), (
            f"Decline path did not surface 'No changes applied':\n{output}"
        )
        lockfile_after = lockfile.read_bytes()
        assert lockfile_after == lockfile_before, (
            "Lockfile bytes changed after user declined the plan -- "
            "the decline path leaked an on-disk mutation."
        )


def _write_apm_yml_with_ref(project_dir: Path, package: str, *, ref: str) -> None:
    """Write apm.yml using the structured object form so a ref pin is honored."""
    config = {
        "name": "update-test",
        "version": "1.0.0",
        "dependencies": {
            "apm": [{"git": package, "ref": ref}],
            "mcp": [],
        },
    }
    (project_dir / "apm.yml").write_text(
        yaml.dump(config, default_flow_style=False), encoding="utf-8"
    )


def _run_apm_with_pty(
    apm_command: str,
    args: list[str],
    *,
    cwd: Path,
    stdin_text: str,
    timeout: int,
) -> tuple[int, str]:
    """Run apm with a real PTY attached so interactive prompts actually fire.

    Returns ``(returncode, combined_output)``. The PTY is allocated in
    the parent; stdout / stderr are merged into the master fd just like
    a real terminal would surface them. ``stdin_text`` is delivered as
    typed keystrokes (write to master).
    """
    import os
    import pty
    import select
    import time

    master_fd, slave_fd = pty.openpty()
    try:
        proc = subprocess.Popen(
            [apm_command, *args],
            cwd=str(cwd),
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            close_fds=True,
        )
    finally:
        os.close(slave_fd)

    os.write(master_fd, stdin_text.encode("utf-8"))

    deadline = time.monotonic() + timeout
    chunks: list[bytes] = []
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                proc.kill()
                raise TimeoutError(f"apm {' '.join(args)} did not exit within {timeout}s")
            ready, _, _ = select.select([master_fd], [], [], min(0.5, remaining))
            if ready:
                try:
                    chunk = os.read(master_fd, 4096)
                except OSError:
                    chunk = b""
                if not chunk:
                    break
                chunks.append(chunk)
            if proc.poll() is not None:
                # Drain anything still buffered before exiting the loop.
                while True:
                    ready, _, _ = select.select([master_fd], [], [], 0.1)
                    if not ready:
                        break
                    try:
                        chunk = os.read(master_fd, 4096)
                    except OSError:
                        chunk = b""
                    if not chunk:
                        break
                    chunks.append(chunk)
                break
    finally:
        os.close(master_fd)

    proc.wait(timeout=5)
    return proc.returncode, b"".join(chunks).decode("utf-8", errors="replace")
