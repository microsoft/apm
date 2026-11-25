"""Helper tools found only inside the project name the skipped path and root (#5473)."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from apm_cli.core.azure_cli import AzureCliBearerError, AzureCliBearerProvider
from apm_cli.utils.executable_exclusion import ProjectShadowedExecutableError

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-OWN-remediation-advice")]


def _project_with_tool(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str):
    home = (tmp_path / "home").resolve()
    project = home / "code" / "p"
    (project / ".git").mkdir(parents=True)
    bindir = project / "bin"
    bindir.mkdir()
    tool = bindir / (f"{name}.exe" if os.name == "nt" else name)
    tool.write_bytes(b"")
    tool.chmod(0o755)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.chdir(project)
    return project, bindir, tool


def test_az_inside_project_names_skipped_path_and_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, bindir, tool = _project_with_tool(tmp_path, monkeypatch, "az")

    with patch("os.get_exec_path", return_value=[str(bindir)]):
        provider = AzureCliBearerProvider()
        with pytest.raises(AzureCliBearerError) as excinfo:
            provider.get_bearer_token()

    message = str(excinfo.value)
    assert excinfo.value.kind == "az_not_found"
    assert str(tool.resolve()) in message
    assert f"inside the project {project.resolve()}" in message
    assert "not installed" not in message


def test_az_missing_everywhere_keeps_not_installed_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _project_with_tool(tmp_path, monkeypatch, "other")

    with patch("os.get_exec_path", return_value=[]):
        provider = AzureCliBearerProvider()
        with pytest.raises(AzureCliBearerError, match="not installed"):
            provider.get_bearer_token()


def test_shadow_message_reports_stored_spelling(tmp_path: Path) -> None:
    (tmp_path / "git.exe").write_bytes(b"")

    error = ProjectShadowedExecutableError("git", str(tmp_path / "git.EXE"), tmp_path)

    assert f"git at {tmp_path.resolve() / 'git.exe'} is inside" in str(error)
    assert "git.EXE" not in str(error)


def test_symlinked_entry_is_the_skipped_tool_and_target_is_info(tmp_path: Path) -> None:
    target = tmp_path / "outside" / "real-az"
    target.parent.mkdir()
    target.write_bytes(b"")
    link = tmp_path / "project" / "az"
    link.parent.mkdir()
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlinks unavailable")

    message = str(ProjectShadowedExecutableError("az", str(link), tmp_path / "project"))

    assert (
        f"az at {link.parent.resolve() / 'az'} (symlink to {target.resolve()}) is inside" in message
    )


def test_powershell_message_names_whichever_candidate_was_shadowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from apm_cli.commands import self_update

    _project, bindir, _tool = _project_with_tool(tmp_path, monkeypatch, "pwsh")
    monkeypatch.setattr(sys, "platform", "win32")

    with (
        patch("os.get_exec_path", return_value=[str(bindir)]),
        pytest.raises(FileNotFoundError) as excinfo,
    ):
        self_update._get_installer_run_command("install.ps1")

    assert "pwsh at" in str(excinfo.value)
    assert "is inside the project" in str(excinfo.value)


def test_runtime_helper_names_later_candidate_shadow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from apm_cli.runtime.manager import _required_helper

    _project, bindir, _tool = _project_with_tool(tmp_path, monkeypatch, "powershell")

    with (
        patch("os.get_exec_path", return_value=[str(bindir)]),
        pytest.raises(FileNotFoundError, match="is inside the project"),
    ):
        _required_helper("pwsh", "powershell")


def test_llm_runtime_surfaces_the_shadow_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from apm_cli.runtime.llm_runtime import LLMRuntime

    _project, bindir, _tool = _project_with_tool(tmp_path, monkeypatch, "llm")

    with (
        patch("os.get_exec_path", return_value=[str(bindir)]),
        pytest.raises(RuntimeError, match="is inside the project"),
    ):
        LLMRuntime()


def test_ado_auth_failure_context_names_shadowed_az(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from apm_cli.core.azure_cli import AzureCliBearerProvider

    _project, bindir, tool = _project_with_tool(tmp_path, monkeypatch, "az")

    with patch("os.get_exec_path", return_value=[str(bindir)]):
        provider = AzureCliBearerProvider()

    assert provider.is_available() is False
    assert str(tool.resolve()) in (provider.shadow_notice or "")


@pytest.mark.skipif(os.name != "nt", reason="PATHEXT spelling is a Windows behavior")
def test_windows_pathext_reports_stored_spelling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from apm_cli.utils.git_env import _resolve_trusted_executable

    _project, bindir, tool = _project_with_tool(tmp_path, monkeypatch, "git")
    monkeypatch.setenv("PATHEXT", ".EXE")

    with (
        patch("os.get_exec_path", return_value=[str(bindir)]),
        pytest.raises(FileNotFoundError) as excinfo,
    ):
        _resolve_trusted_executable("git")

    assert str(tool.resolve()) in str(excinfo.value)
    assert "git.EXE" not in str(excinfo.value)
