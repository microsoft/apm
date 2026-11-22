"""Root ``apm --help`` stays byte-identical while the boot path gets lazier (#4810).

Regenerate the golden only for an intended help change:
``NO_COLOR=1 python -c "from click.testing import CliRunner; from apm_cli.cli import cli;
print(CliRunner().invoke(cli, ['--help'], terminal_width=100, color=False).output, end='')"
> tests/fixtures/cli/root-help.golden.txt``
"""

from pathlib import Path

import pytest
from click.testing import CliRunner

from apm_cli.cli import cli

pytestmark = [pytest.mark.component, pytest.mark.promise("P-OWN-cli-startup-import-budget")]

_GOLDEN = Path(__file__).resolve().parents[1] / "fixtures" / "cli" / "root-help.golden.txt"


def test_root_help_matches_the_golden(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    result = CliRunner().invoke(cli, ["--help"], terminal_width=100, color=False)

    assert result.exit_code == 0, result.output
    assert result.output == _GOLDEN.read_text(encoding="utf-8")
