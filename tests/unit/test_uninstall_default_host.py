"""`apm uninstall` resolves shorthand against the manifest ``default_host`` (#3843).

`apm install acme/x` under ``default_host: gitlab.com`` records the
``gitlab.com/acme/x`` lock entry; uninstall must select that same entry
instead of removing only the declaration and leaving GitLab content behind.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from apm_cli.deps.lockfile import LockedDependency, LockFile

pytestmark = pytest.mark.component


@pytest.mark.parametrize("requested", ["acme/x", "gitlab.com/acme/x"])
def test_uninstall_selects_the_default_host_lock_entry(
    tmp_path: Path, monkeypatch, requested: str
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "apm.yml").write_text(
        "name: project\nversion: 1.0.0\ndefault_host: gitlab.com\n"
        "targets: [copilot]\ndependencies:\n  apm:\n    - acme/x\n",
        encoding="utf-8",
    )
    lock = LockFile()
    lock.add_dependency(LockedDependency(repo_url="acme/x", host="gitlab.com"))
    lock.write(tmp_path / "apm.lock.yaml")

    from apm_cli.cli import cli

    result = CliRunner().invoke(cli, ["uninstall", requested])

    assert result.exit_code == 0, result.output
    assert "acme/x" not in (tmp_path / "apm.yml").read_text(encoding="utf-8")
    remaining = LockFile.read(tmp_path / "apm.lock.yaml")
    assert remaining is None or not remaining.dependencies, result.output
