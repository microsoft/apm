"""`apm install` stops with the symlink capability diagnostic before hashing (#3457)."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import yaml
from click.testing import CliRunner

from apm_cli.cli import cli
from apm_cli.models.apm_package import clear_apm_yml_cache
from apm_cli.utils.git_env import GitSymlinkCapabilityError

pytestmark = [pytest.mark.unit]


@pytest.fixture(autouse=True)
def _clear_package_cache() -> None:
    clear_apm_yml_cache()
    yield
    clear_apm_yml_cache()


def test_install_fails_with_named_diagnostic_and_no_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / ".github").mkdir()
    (project / ".github" / "copilot-instructions.md").write_text("# Project\n", encoding="utf-8")
    (project / "apm.yml").write_text(
        yaml.safe_dump(
            {
                "name": "symlink-capability",
                "version": "1.0.0",
                "target": "copilot",
                "dependencies": {"apm": ["acme/linked-pkg#v1.0.0"], "mcp": []},
            }
        ),
        encoding="utf-8",
    )
    hashed: list[Path] = []

    def refuse(self: object, repo_ref: object, target_path: Path, *a: Any, **k: Any) -> None:
        raise GitSymlinkCapabilityError("acme/linked-pkg")

    from apm_cli.deps import github_downloader as _ghd
    from apm_cli.utils import content_hash

    real_hash = content_hash.compute_package_hash

    def record_hash(path: Path) -> str:
        hashed.append(Path(path))
        return real_hash(path)

    monkeypatch.setattr(_ghd.GitHubPackageDownloader, "download_package", refuse)
    monkeypatch.setattr(content_hash, "compute_package_hash", record_hash)
    monkeypatch.chdir(project)

    with patch("apm_cli.commands._helpers.check_for_updates", return_value=None):
        result = CliRunner().invoke(cli, ["install"], catch_exceptions=False)

    assert result.exit_code != 0, result.output
    output = " ".join(result.output.split())
    assert "acme/linked-pkg contains symbolic links" in output
    assert "Developer Mode" in output
    assert not (project / "apm.lock.yaml").exists()
    assert hashed == []
