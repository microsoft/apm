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
