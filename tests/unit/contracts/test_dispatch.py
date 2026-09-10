"""Contract mode is explicit and cannot inherit legacy success fallbacks."""

from unittest.mock import Mock

import pytest
from click.testing import CliRunner

from apm_cli.commands import contracts as commands
from apm_cli.commands.plan import plan
from apm_cli.commands.run import run

pytestmark = pytest.mark.component


@pytest.mark.parametrize(
    "args",
    [
        ["--on", "copilot"],
        ["job.contract.md", "--on", "copilot", "--param", "name=value"],
        ["script", "--model", "gpt-6-astra"],
        ["script", "--allow-advisory"],
    ],
)
def test_invalid_mode_options_never_launch(
    args: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    invoke = Mock()
    monkeypatch.setattr(commands, "invoke_contract", invoke)
    result = CliRunner().invoke(run, args)
    assert result.exit_code == 2
    invoke.assert_not_called()


def test_contract_mode_forwards_explicit_selection_without_script_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invoke = Mock()
    monkeypatch.setattr(commands, "invoke_contract", invoke)
    script = Mock(side_effect=AssertionError("legacy runner must not be constructed"))
    monkeypatch.setattr("apm_cli.core.script_runner.ScriptRunner", script)
    result = CliRunner().invoke(
        run,
        [
            "job.contract.md",
            "--on",
            "copilot",
            "--model",
            "gpt-6-astra",
            "--allow-advisory",
        ],
    )
    assert result.exit_code == 0
    assert invoke.call_args.args[1] == "job.contract.md"
    assert invoke.call_args.kwargs == {
        "harness": "copilot",
        "model": "gpt-6-astra",
        "verbose": False,
        "planning": False,
        "allow_advisory": True,
    }
    script.assert_not_called()
    assert "Script executed successfully" not in result.output


def test_contract_named_script_is_still_a_script_without_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = Mock()
    instance.run_script.return_value = True
    constructor = Mock(return_value=instance)
    monkeypatch.setattr("apm_cli.core.script_runner.ScriptRunner", constructor)
    invoke = Mock()
    monkeypatch.setattr(commands, "invoke_contract", invoke)
    result = CliRunner().invoke(run, ["job.contract.md"])
    assert result.exit_code == 0
    instance.run_script.assert_called_once_with("job.contract.md", {})
    invoke.assert_not_called()


def test_plan_requires_explicit_harness() -> None:
    result = CliRunner().invoke(plan, ["job.contract.md"])
    assert result.exit_code == 2
    assert "--on" in result.output


@pytest.mark.parametrize("command", ["plan", "run"])
def test_contract_commands_do_not_probe_updates(
    command: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from apm_cli import cli as cli_module
    from apm_cli.commands import plan as plan_module

    invoke = Mock()
    monkeypatch.setattr(commands, "invoke_contract", invoke)
    monkeypatch.setattr(plan_module, "invoke_contract", invoke)
    update = Mock(side_effect=AssertionError("contract commands must not check updates"))
    monkeypatch.setattr(cli_module, "_check_and_notify_updates", update)
    result = CliRunner().invoke(cli_module.cli, [command, "job.contract.md", "--on", "copilot"])
    assert result.exit_code == 0, result.output
    update.assert_not_called()
    invoke.assert_called_once()
