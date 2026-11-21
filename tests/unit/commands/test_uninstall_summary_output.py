"""Uninstall summary wording: one count per package, manifest names, relative paths (#4184)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from apm_cli.cli import cli

pytestmark = [
    pytest.mark.unit,
    pytest.mark.promise("P-OWN-user-facing-output-diagnostics"),
]


def _uninstall(project: Path, deps: list[str], *args: str) -> str:
    (project / "apm.yml").write_text(
        yaml.dump({"name": "project", "version": "1.0.0", "dependencies": {"apm": deps}}),
        encoding="utf-8",
    )
    for dep in deps:
        (project / "apm_modules" / Path(dep.removesuffix(".git").split("github.com/")[-1])).mkdir(
            parents=True
        )
    result = CliRunner().invoke(cli, ["uninstall", *args])
    assert result.exit_code == 0, result.output
    return " ".join(result.output.split())


@pytest.mark.parametrize(
    ("deps", "args", "count"),
    (
        (["acme/tool"], ("acme/tool",), "1 package"),
        (["https://github.com/acme/tool.git"], ("acme/tool",), "1 package"),
        (["acme/one", "acme/two"], ("acme/one", "acme/two"), "2 packages"),
    ),
)
def test_remote_uninstall_summary_counts_each_package_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    deps: list[str],
    args: tuple[str, ...],
    count: str,
) -> None:
    monkeypatch.chdir(tmp_path)

    output = _uninstall(tmp_path, deps, *args)

    assert f"Uninstalling {count}..." in output
    assert output.count("Uninstall complete") == 1
    assert output.endswith(f"[*] Uninstall complete: removed {count}")
    assert "package(s)" not in output
    assert "[*] Updated apm.yml [*]" in output
    assert str(tmp_path) not in output
    assert "github.com" not in output
    for package in args:
        assert f"Removed {package} from apm_modules/" in output
        assert f"Removed {package} from dependencies.apm in apm.yml" in output


@pytest.mark.parametrize(
    ("logged", "reintegrated"),
    ((1, 0), (0, 1)),
    ids=["cleanup-failure", "survivor-failure"],
)
def test_reported_package_failure_makes_uninstall_incomplete(
    logged: int, reintegrated: int
) -> None:
    """A package failure is never followed by a success line or exit 0 (#4703)."""
    from unittest.mock import MagicMock

    from apm_cli.commands.uninstall.cli import _report_uninstall_outcome
    from apm_cli.commands.uninstall.engine import IntegrationCleanupOutcome
    from apm_cli.utils.diagnostics import DiagnosticCollector

    logger = MagicMock()
    logger.diagnostics = DiagnosticCollector()
    for _ in range(logged):
        logger.diagnostics.error("Materialization has no ownership metadata", package="pkg")
    cleanup = IntegrationCleanupOutcome({}, {}, [], 0, package_failures=reintegrated)

    incomplete = _report_uninstall_outcome(
        logger, "removed 1 package", cleanup, None, None, None, False
    )

    assert incomplete is True
    logger.success.assert_not_called()
    logger.error.assert_called_once_with("Uninstall incomplete: 1 package failure reported above.")
