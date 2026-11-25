"""Helper tools found only inside the project name the skipped path and root (#5473)."""

from __future__ import annotations

import os
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
    from apm_cli.utils import executable_exclusion, git_env

    _project, bindir, _tool = _project_with_tool(tmp_path, monkeypatch, "pwsh")

    with patch("os.get_exec_path", return_value=[str(bindir)]):
        found = git_env.trusted_executable("powershell") or git_env.trusted_executable("pwsh")

    message = executable_exclusion.not_found_message("generic", "powershell", "pwsh")
    assert found is None
    assert "pwsh at" in message
    assert "is inside the project" in message


def test_outside_entry_linking_into_project_is_not_called_inside(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "az").write_bytes(b"")
    link = tmp_path / "tools" / "az"
    link.parent.mkdir()
    try:
        link.symlink_to(project / "az")
    except OSError:
        pytest.skip("symlinks unavailable")

    message = str(ProjectShadowedExecutableError("az", str(link), project))

    assert f"az at {link.parent.resolve() / 'az'} links to {(project / 'az').resolve()}" in message
    assert f"which is inside the project {project}" in message
    assert "not a link into it" in message


def test_llm_version_failure_ignores_a_stale_shadow_note(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from apm_cli.runtime.llm_runtime import LLMRuntime
    from apm_cli.utils.git_env import trusted_executable

    if os.name == "nt":
        pytest.skip("POSIX shell stand-in")
    _project, bindir, _tool = _project_with_tool(tmp_path, monkeypatch, "llm")
    managed = Path.home() / ".apm" / "runtimes" / "llm"
    managed.parent.mkdir(parents=True)
    managed.write_text("#!/bin/sh\nexit 1\n")
    managed.chmod(0o755)

    with patch("os.get_exec_path", return_value=[str(bindir)]):
        assert trusted_executable("llm") is None
        with pytest.raises(RuntimeError) as excinfo:
            LLMRuntime()

    assert "pip install llm" in str(excinfo.value)
    assert "inside the project" not in str(excinfo.value)


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
