"""Characterization tests for target capability metadata (filesystem, process and CLI boundary cases)."""

import pytest
from click.testing import CliRunner

from apm_cli.cli import cli
from apm_cli.core.target_catalog import (
    accepted_target_values,
)
from apm_cli.core.target_detection import (
    parse_target_field,
)

pytestmark = [
    pytest.mark.component,
    pytest.mark.promise(
        "P-OWN-effective-install-target-selection", "P-OWN-accepted-target-vocabulary"
    ),
]

COMMANDS = ("compile", "install", "update")


@pytest.mark.parametrize("command", COMMANDS)
def test_command_help_and_errors_advertise_every_accepted_value(command: str) -> None:
    """Real Click surfaces advertise the same values accepted by parsing."""
    runner = CliRunner()
    help_result = runner.invoke(cli, [command, "--help"], terminal_width=1000)
    error_result = runner.invoke(
        cli,
        [command, "--target", "definitely-bogus"],
        terminal_width=1000,
    )

    assert help_result.exit_code == 0
    assert error_result.exit_code == 2
    valid_line = next(
        line for line in error_result.output.splitlines() if line.startswith("Valid targets:")
    )
    labels = [target.strip() for target in valid_line.partition(":")[2].split(",")]
    error_values = {label.split(" (")[0] for label in labels}
    assert error_values == set(accepted_target_values(command))
    # RA-037: every alias names its target; a deprecated one says so.
    assert "vscode (deprecated alias of copilot)" in labels
    assert "agents (alias of copilot)" in labels
    assert "agy (alias of antigravity)" in labels
    assert [label for label in labels if "(" not in label] == sorted(
        value
        for value in accepted_target_values(command)
        if value not in {"vscode", "agents", "agy"}
    )
    for value in accepted_target_values(command):
        assert value in help_result.output


def test_deprecated_aliases_are_accepted_but_never_advertised(tmp_path) -> None:
    """vscode still parses with a warning, but generated manifests and errors omit it."""
    from apm_cli.core.errors import UnknownTargetError
    from apm_cli.core.target_catalog import (
        advertised_manifest_target_names,
        manifest_target_names,
    )
    from apm_cli.core.target_detection import _validate_canonical_v2

    assert "vscode" in manifest_target_names()
    assert "vscode" not in advertised_manifest_target_names()
    with pytest.raises(UnknownTargetError) as excinfo:
        _validate_canonical_v2(["bogus"])
    assert "vscode" not in str(excinfo.value)


def test_manifest_agents_value_is_not_reported_as_a_flag(tmp_path) -> None:
    """`target: agents` warns once, from the manifest owner, never as `--target agents`."""
    import warnings as _warnings
    from pathlib import Path

    from apm_cli.core.target_detection import (
        AgentsTargetDeprecationWarning,
        agents_alias_was_detected,
    )

    with _warnings.catch_warnings(record=True) as caught:
        _warnings.simplefilter("always")
        parse_target_field("agents", source_path=Path(tmp_path) / "apm.yml")
    assert not agents_alias_was_detected()
    assert not [w for w in caught if issubclass(w.category, AgentsTargetDeprecationWarning)]

    with _warnings.catch_warnings(record=True) as caught:
        _warnings.simplefilter("always")
        parse_target_field("agents")
    assert agents_alias_was_detected()
    assert [w for w in caught if issubclass(w.category, AgentsTargetDeprecationWarning)]


def test_unknown_target_error_marks_aliases_exactly() -> None:
    """RA-037: the listing equals the documented targets, each alias marked."""
    result = CliRunner().invoke(cli, ["install", "--target", "bogus"], terminal_width=1000)

    assert result.exit_code == 2
    assert (
        "Valid targets: agent-skills, agents (alias of copilot), agy (alias of antigravity), "
        "all, antigravity, claude, codex, copilot, copilot-app, copilot-cowork, cursor, "
        "gemini, grok-bot, grok-build, grok-cloud, hermes, intellij, kiro, openclaw, "
        "opencode, vscode (deprecated alias of copilot), windsurf"
    ) in result.output.splitlines()
