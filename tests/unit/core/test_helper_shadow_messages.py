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


def test_shadow_message_reports_stored_spelling() -> None:
    root = Path("/proj")
    with patch("os.path.realpath", return_value="/proj/bin/git.exe"):
        error = ProjectShadowedExecutableError("git", "/proj/bin/git.EXE", root)

    assert "git at /proj/bin/git.exe is inside" in str(error)
    assert "git.EXE" not in str(error)


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
