"""Regression coverage for shared deployed-path ownership on uninstall."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import yaml
from click.testing import CliRunner

from apm_cli.cli import cli
from apm_cli.deps.lockfile import LockFile


def _write_local_package(root: Path, name: str) -> None:
    """Create a local package that provides the shared instruction."""
    instructions = root / ".apm" / "instructions"
    instructions.mkdir(parents=True)
    (root / "apm.yml").write_text(
        f"name: {name}\nversion: 1.0.0\n",
        encoding="ascii",
    )
    (instructions / "shared.instructions.md").write_text(
        "---\ndescription: shared ownership regression\napplyTo: '**'\n---\n# Shared\n",
        encoding="ascii",
    )


def test_uninstall_transfers_shared_deployed_path_ownership(tmp_path: Path, monkeypatch) -> None:
    """The surviving package owns and audits a shared path after uninstall."""
    package_a = tmp_path / "a"
    package_b = tmp_path / "b"
    _write_local_package(package_a, "package-a")
    _write_local_package(package_b, "package-b")

    project = tmp_path / "app"
    project.mkdir()
    (project / "apm.yml").write_text(
        "name: app\n"
        "version: 1.0.0\n"
        "targets: [copilot]\n"
        "dependencies:\n"
        "  apm:\n"
        "    - path: ../a\n"
        "    - path: ../b\n",
        encoding="ascii",
    )
    monkeypatch.chdir(project)
    runner = CliRunner()

    install = runner.invoke(cli, ["install", "--target", "copilot"])
    assert install.exit_code == 0, install.output

    lockfile_writes = []
    original_write = LockFile.write

    def _record_write(lockfile: LockFile, path: Path) -> None:
        lockfile_writes.append(path)
        original_write(lockfile, path)

    with patch.object(LockFile, "write", _record_write):
        uninstall = runner.invoke(cli, ["uninstall", "../b"])
    assert uninstall.exit_code == 0, uninstall.output
    assert lockfile_writes == [project / "apm.lock.yaml"]

    deployed_path = ".github/instructions/shared.instructions.md"
    deployed_file = project / deployed_path
    assert deployed_file.is_file()

    lock_data = yaml.safe_load((project / "apm.lock.yaml").read_text(encoding="ascii"))
    assert len(lock_data["dependencies"]) == 1
    surviving = lock_data["dependencies"][0]
    assert surviving["name"] == "package-a"
    assert deployed_path in surviving["deployed_files"]
    assert surviving["deployed_file_hashes"][deployed_path].startswith("sha256:")

    deployed_file.write_text("# tampered\n", encoding="ascii")
    audit = runner.invoke(cli, ["audit", "--ci", "--no-policy", "--no-drift"])
    assert audit.exit_code == 1, audit.output
