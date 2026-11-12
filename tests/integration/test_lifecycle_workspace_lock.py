"""Cross-command regression coverage for the workspace mutation lock."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.windows_compat

_BLOCKING_INSTALL = textwrap.dedent(
    """
    import importlib
    import json
    import os
    import sys
    import time
    from pathlib import Path

    from click.testing import CliRunner
    from apm_cli.cli import cli
    from apm_cli.install.transaction import InstallTransaction as BaseTransaction

    ready = Path(os.environ["APM_TEST_LOCK_READY"])
    release = Path(os.environ["APM_TEST_LOCK_RELEASE"])

    class BlockingTransaction(BaseTransaction):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            ready.write_text("ready", encoding="ascii")
            deadline = time.monotonic() + 10
            while not release.exists():
                if time.monotonic() >= deadline:
                    raise TimeoutError("test did not release install")
                time.sleep(0.01)

    install_module = importlib.import_module("apm_cli.commands.install")
    install_module.InstallTransaction = BlockingTransaction
    install_args = json.loads(os.environ.get("APM_TEST_INSTALL_ARGS", '["install"]'))
    result = CliRunner().invoke(cli, install_args)
    sys.stdout.write(result.output)
    if result.exception is not None and result.exit_code != 0:
        sys.stderr.write(repr(result.exception))
    raise SystemExit(result.exit_code)
    """
)

_WATCHING_COMPILE = textwrap.dedent(
    """
    import os
    from pathlib import Path
    from types import SimpleNamespace
    from unittest.mock import patch

    from apm_cli.commands.compile.watcher import _watch_mode

    marker = Path(os.environ["APM_TEST_WATCH_COMPILED"])

    class MarkerCompiler:
        def __init__(self, project_root):
            self.project_root = project_root

        def compile(self, config):
            marker.write_text("compiled", encoding="ascii")
            return SimpleNamespace(success=True, output_path="AGENTS.md", errors=[])

    with (
        patch(
            "apm_cli.commands.compile.watcher.CompilationConfig.from_apm_yml",
            return_value=object(),
        ),
        patch("apm_cli.commands.compile.watcher.AgentsCompiler", MarkerCompiler),
    ):
        _watch_mode("AGENTS.md", None, False, False)
    """
)


def _wait_for_path(path: Path, process: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + 10
    while not path.exists():
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            pytest.fail(f"install exited before taking lock: {stdout}\n{stderr}")
        if time.monotonic() >= deadline:
            process.kill()
            pytest.fail("install did not take workspace lock")
        time.sleep(0.01)


@pytest.mark.parametrize(
    ("install_args", "contender_args", "global_scope", "cross_project"),
    [
        (("install",), ("update", "--yes"), False, False),
        (("install",), ("prune",), False, False),
        (("install",), ("uninstall", "missing"), False, False),
        (("install",), ("prune",), False, True),
        (("install",), ("deps", "clean", "--yes"), False, False),
        (("install",), ("lock",), False, False),
        (("install",), ("approve", "missing"), False, False),
        (("install",), ("deny", "missing"), False, False),
        (("install",), ("compile", "--clean"), False, False),
        (("install",), ("config", "set", "auto-integrate", "false"), False, False),
        (("install",), ("config", "unset", "auto-integrate"), False, False),
        (("install",), ("experimental", "enable", "canvas"), False, False),
        (("install",), ("experimental", "disable", "canvas"), False, False),
        (("install",), ("experimental", "reset", "canvas", "--yes"), False, False),
        (("install",), ("lifecycle", "init", "--force"), False, False),
        (
            ("install",),
            ("audit", "--file", "__AUDIT_FILE__", "--strip"),
            False,
            False,
        ),
        (("install",), ("init", "new-project", "--yes"), False, False),
        (("install",), ("plugin", "init", "new-plugin", "--yes"), False, False),
        (
            ("install",),
            ("marketplace", "init", "--force", "--no-gitignore-check"),
            False,
            False,
        ),
        (("install",), ("deps", "update"), False, False),
        (
            ("install",),
            ("marketplace", "add", "__MARKETPLACE_SOURCE__", "--name", "test-market"),
            False,
            False,
        ),
        (("install",), ("marketplace", "update"), False, False),
        (("install",), ("marketplace", "remove", "missing", "--yes"), False, False),
        (
            ("install",),
            (
                "marketplace",
                "package",
                "add",
                "acme/new-tool",
                "--version",
                ">=1.0.0",
                "--no-verify",
            ),
            False,
            False,
        ),
        (
            ("install",),
            (
                "marketplace",
                "package",
                "set",
                "existing-package",
                "--version",
                ">=2.0.0",
            ),
            False,
            False,
        ),
        (
            ("install",),
            ("marketplace", "package", "remove", "existing-package", "--yes"),
            False,
            False,
        ),
        (
            ("install", "--global", "--frozen"),
            ("uninstall", "--global", "missing"),
            True,
            False,
        ),
    ],
)
def test_install_serializes_other_lifecycle_commands(
    tmp_path: Path,
    apm_binary_path: Path,
    install_args: tuple[str, ...],
    contender_args: tuple[str, ...],
    global_scope: bool,
    cross_project: bool,
) -> None:
    """Update, uninstall, and prune wait for an active install workspace."""
    home = tmp_path / "home"
    workspace = home / ".apm" if global_scope else tmp_path
    workspace.mkdir(parents=True, exist_ok=True)
    manifest = workspace / "apm.yml"
    original_manifest = "name: fixture\nversion: 1.0.0\ndependencies:\n  apm: []\n"
    manifest.write_text(original_manifest, encoding="ascii")
    (tmp_path / "marketplace.yml").write_text(
        "name: test-marketplace\ndescription: Test marketplace\nversion: 1.0.0\n"
        "owner:\n  name: Test Owner\npackages:\n"
        "  - name: existing-package\n    source: acme/existing-package\n    version: '>=1.0.0'\n",
        encoding="ascii",
    )
    marketplace_source = tmp_path / "marketplace-source"
    marketplace_source.mkdir()
    (marketplace_source / "marketplace.json").write_text(
        '{"name":"test-market","owner":"test","plugins":[]}',
        encoding="ascii",
    )
    contender_workspace = tmp_path / "other-project" if cross_project else tmp_path
    contender_workspace.mkdir(exist_ok=True)
    if cross_project:
        (contender_workspace / "apm.yml").write_text(original_manifest, encoding="ascii")
    ready = tmp_path / "install-ready"
    release = tmp_path / "install-release"
    environment = {
        **os.environ,
        "HOME": str(home),
        "USERPROFILE": str(home),
        "HOMEDRIVE": home.drive,
        "HOMEPATH": str(home)[len(home.drive) :],
        "APM_TEST_LOCK_READY": str(ready),
        "APM_TEST_LOCK_RELEASE": str(release),
        "APM_TEST_INSTALL_ARGS": json.dumps(install_args),
    }
    install = subprocess.Popen(
        [sys.executable, "-c", _BLOCKING_INSTALL],
        cwd=tmp_path,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    contender = None
    try:
        _wait_for_path(ready, install)
        resolved_contender_args = tuple(
            (
                str(marketplace_source)
                if arg == "__MARKETPLACE_SOURCE__"
                else str(manifest)
                if arg == "__AUDIT_FILE__"
                else arg
            )
            for arg in contender_args
        )
        contender = subprocess.Popen(
            [str(apm_binary_path), *resolved_contender_args],
            cwd=contender_workspace,
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        startup_sensitive = contender_args[0] in {"audit", "lifecycle"}
        with pytest.raises(subprocess.TimeoutExpired):
            contender.wait(timeout=2.0 if startup_sensitive else 0.5)

        release.write_text("release", encoding="ascii")
        install_stdout, install_stderr = install.communicate(timeout=30)
        contender_stdout, contender_stderr = contender.communicate(timeout=30)
    finally:
        for process in (install, contender):
            if process is not None and process.poll() is None:
                process.kill()
                process.wait()

    expected_install_exit = 1 if "--frozen" in install_args else 0
    assert install.returncode == expected_install_exit, (install_stdout, install_stderr)
    assert contender.returncode in {0, 1}, (contender_stdout, contender_stderr)
    if contender_args[0] == "deny":
        assert "missing:" in manifest.read_text(encoding="ascii")
    elif contender_args[:2] == ("lifecycle", "init"):
        assert "lifecycle:" in manifest.read_text(encoding="ascii")
    elif contender_args[:2] == ("marketplace", "init"):
        assert "marketplace:" in manifest.read_text(encoding="ascii")
    else:
        assert manifest.read_text(encoding="ascii") == original_manifest
    if cross_project:
        assert (contender_workspace / "apm.yml").read_text(encoding="ascii") == original_manifest


def test_compile_watch_startup_waits_for_active_lifecycle_operation(
    tmp_path: Path,
) -> None:
    """Watch startup must not compile until the active install releases its lock."""
    home = tmp_path / "home"
    home.mkdir()
    manifest = tmp_path / "apm.yml"
    manifest.write_text(
        "name: fixture\nversion: 1.0.0\ntargets:\n  - copilot\n",
        encoding="ascii",
    )
    ready = tmp_path / "install-ready"
    release = tmp_path / "install-release"
    output = tmp_path / "watch-compiled"
    environment = {
        **os.environ,
        "HOME": str(home),
        "USERPROFILE": str(home),
        "HOMEDRIVE": home.drive,
        "HOMEPATH": str(home)[len(home.drive) :],
        "APM_TEST_LOCK_READY": str(ready),
        "APM_TEST_LOCK_RELEASE": str(release),
        "APM_TEST_WATCH_COMPILED": str(output),
    }
    install = subprocess.Popen(
        [sys.executable, "-c", _BLOCKING_INSTALL],
        cwd=tmp_path,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    watcher = None
    try:
        _wait_for_path(ready, install)
        watcher = subprocess.Popen(
            [sys.executable, "-c", _WATCHING_COMPILE],
            cwd=tmp_path,
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        time.sleep(0.5)
        assert watcher.poll() is None
        assert not output.exists()

        release.write_text("release", encoding="ascii")
        install_stdout, install_stderr = install.communicate(timeout=30)
        deadline = time.monotonic() + 30
        while not output.exists() and time.monotonic() < deadline:
            if watcher.poll() is not None:
                stdout, stderr = watcher.communicate()
                pytest.fail(f"watch exited before compile: {stdout}\n{stderr}")
            time.sleep(0.05)
        assert output.is_file()
    finally:
        for process in (install, watcher):
            if process is not None and process.poll() is None:
                process.kill()
                process.wait()

    assert install.returncode == 0, (install_stdout, install_stderr)
