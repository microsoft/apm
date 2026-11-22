"""``--policy`` refuses undefined flag combinations at the CLI boundary (#1925)."""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

pytestmark = [pytest.mark.component, pytest.mark.promise("req-pl-001")]


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "apm.yml").write_text("name: p\nversion: 1.0.0\n", encoding="utf-8")
    return tmp_path


@pytest.mark.parametrize(
    ("command", "args", "message"),
    [
        ("install", ["--policy", "p.yml", "--global"], "--global"),
        ("install", ["--policy", "p.yml", "--no-policy"], "mutually exclusive"),
        ("update", ["--policy", "p.yml", "--global"], "--global"),
    ],
)
def test_policy_flag_rejects_undefined_combinations(
    project: Path, command: str, args: list[str], message: str
) -> None:
    from apm_cli.cli import cli

    result = CliRunner().invoke(cli, [command, *args])
    assert result.exit_code == 2, result.output
    assert message in result.output
