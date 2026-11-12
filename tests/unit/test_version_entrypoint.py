"""The lightweight entrypoint must not reinterpret ordinary CLI arguments."""

import sys
from unittest.mock import patch

import pytest

from apm_cli.entrypoint import main

pytestmark = pytest.mark.component


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["--help"],
        ["--verbose", "--version"],
        ["--version", "--help"],
        ["--help", "--version"],
        ["--version=unexpected"],
        ["--unknown", "--version"],
        ["--", "--version"],
        ["init", "--version"],
        ["run", "echo", "--version"],
    ],
)
def test_non_standalone_version_delegates_unchanged(args: list[str]) -> None:
    with (
        patch.object(sys, "argv", ["apm", *args]),
        patch("apm_cli.cli.main") as normal_main,
    ):
        main()
    normal_main.assert_called_once_with()


def test_standalone_version_uses_shared_callback(capsys: pytest.CaptureFixture[str]) -> None:
    with (
        patch.object(sys, "argv", ["apm", "--version"]),
        patch("apm_cli.cli.main") as normal_main,
        patch("apm_cli.version_output.get_version", return_value="1.2.3"),
        patch("apm_cli.version_output.get_build_sha", return_value=""),
        pytest.raises(SystemExit) as exit_info,
    ):
        main()
    assert exit_info.value.code == 0
    assert capsys.readouterr().out.strip() == "Agent Package Manager (APM) CLI version 1.2.3"
    normal_main.assert_not_called()


def test_shared_callback_is_the_existing_cli_callback() -> None:
    from apm_cli.commands._helpers import print_version as legacy_callback
    from apm_cli.version_output import print_version

    assert legacy_callback is print_version


@pytest.mark.parametrize(
    ("args", "exit_code", "message"),
    [
        (["--help", "--version"], 0, "Usage:"),
        (["--version", "--help"], 0, "Agent Package Manager (APM) CLI version"),
        (["--version=unexpected"], 2, "does not take a value"),
        (["--unknown", "--version"], 2, "No such option"),
        (["--", "--version"], 0, "Agent Package Manager (APM) CLI version"),
        (["init", "--version"], 2, "No such option"),
    ],
)
def test_argument_placement_preserves_click_behavior(
    args: list[str], exit_code: int, message: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with (
        patch.object(sys, "argv", ["apm", *args]),
        patch("apm_cli.cli._check_and_notify_updates"),
        pytest.raises(SystemExit) as exit_info,
    ):
        main()
    captured = capsys.readouterr()
    assert exit_info.value.code == exit_code
    assert message in captured.out + captured.err
