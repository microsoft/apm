"""`apm install PKG` resolves shorthand against the manifest ``default_host`` (#3843).

Under ``default_host: gitlab.com`` a host-less ``acme/x`` must be validated
against ``gitlab.com`` and written back as the shorthand, while an explicit
``github.com/acme/y`` keeps its host.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml
from click.testing import CliRunner

from apm_cli.cli import cli
from apm_cli.models.results import InstallResult

pytestmark = pytest.mark.component


@pytest.fixture
def project(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "apm.yml").write_text(
        "name: project\nversion: 1.0.0\ndefault_host: gitlab.com\n"
        "targets: [copilot]\ndependencies:\n  apm: []\n",
        encoding="utf-8",
    )
    return tmp_path


def _invoke(args: list[str]) -> tuple[object, dict[str, str | None]]:
    seen: dict[str, str | None] = {}

    def _record(package, *_args, dep_ref=None, **_kwargs):
        seen[package] = dep_ref.host
        return True

    install_result = InstallResult(
        diagnostics=MagicMock(has_diagnostics=False, has_critical_security=False, error_count=0)
    )
    with (
        patch("apm_cli.commands.install._validate_package_exists", side_effect=_record),
        patch("apm_cli.commands.install._install_apm_dependencies", return_value=install_result),
    ):
        result = CliRunner().invoke(cli, ["install", *args])
    return result, seen


def _declared(project: Path) -> list[str]:
    data = yaml.safe_load((project / "apm.yml").read_text(encoding="utf-8"))
    return data["dependencies"]["apm"]


def test_dry_run_validates_shorthand_against_default_host(project: Path) -> None:
    result, seen = _invoke(["acme/x", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert seen == {"acme/x": "gitlab.com"}
    assert _declared(project) == []


def test_add_writes_shorthand_and_keeps_explicit_github_host(project: Path) -> None:
    result, seen = _invoke(["acme/x", "github.com/acme/y"])

    assert result.exit_code == 0, result.output
    assert seen == {"acme/x": "gitlab.com", "github.com/acme/y": "github.com"}
    assert _declared(project) == ["acme/x", "github.com/acme/y"]
