"""Registry reporting crosses the CLI boundary without changing project state."""

import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner
from rich.console import Console

from apm_cli.cli import cli
from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.deps.registry.client import VersionEntry
from apm_cli.models.apm_package import clear_apm_yml_cache

pytestmark = pytest.mark.component


@pytest.mark.parametrize("lock_name", ["apm.lock.yaml", "apm.lock"])
@pytest.mark.parametrize("plain", [False, True])
@pytest.mark.parametrize("current", ["1.7.0", "2.0.0"])
@pytest.mark.parametrize(
    ("selector", "wanted"),
    [("1.7.0", "1.7.0"), ("=1.7.0", "1.7.0"), ("^1.7.0", "1.8.0")],
)
def test_exact_registry_pin_reports_latest_without_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    lock_name: str,
    plain: bool,
    selector: str,
    wanted: str,
    current: str,
) -> None:
    """Real manifest/lock reads expose the pin and preserve legacy filenames."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("apm_cli.config._config_cache", {"experimental": {"registries": True}})
    clear_apm_yml_cache()
    (tmp_path / "apm.yml").write_text(
        "name: consumer\nversion: 1.0.0\n"
        "registries:\n  corp:\n    url: https://reg.example.com/apm\n  default: corp\n"
        f"dependencies:\n  apm:\n    - org/pkg#{selector}\n",
        encoding="utf-8",
    )
    lock = LockFile()
    lock.dependencies = {
        "org/pkg": LockedDependency(
            repo_url="org/pkg", source="registry", version=current, resolved_ref=current
        ),
        "local/pkg": LockedDependency(repo_url="local/pkg", source="local"),
    }
    lock.write(tmp_path / lock_name)
    (tmp_path / "apm_modules").mkdir()
    (tmp_path / "apm_modules" / "sentinel").write_bytes(b"installed content\n")
    before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    client = MagicMock()
    client.list_versions.return_value = [
        VersionEntry(version=v, digest="sha256:test", published_at="")
        for v in ["1.7.0", "1.8.0", "2.0.0"]
    ]
    with (
        patch("apm_cli.deps.registry.outdated.RegistryClient", return_value=client),
        patch("apm_cli.deps.registry.outdated.make_auth_context", return_value=None),
        patch("apm_cli.deps.github_downloader.GitHubPackageDownloader") as downloader,
        patch("apm_cli.core.auth.AuthResolver"),
        patch(
            "apm_cli.commands._helpers._get_console",
            side_effect=lambda: None if plain else Console(width=200),
        ),
    ):
        result = CliRunner().invoke(cli, ["outdated", "-j", "0"])

    assert result.exit_code == 0, result.output
    assert "Wanted" in result.output
    package_line = next(line for line in result.output.splitlines() if "org/pkg" in line)
    assert re.findall(r"\d+\.\d+\.\d+", package_line) == [current, wanted, "2.0.0"]
    assert "outside constraint" in result.output
    if current == "1.7.0":
        assert "1 outdated dependency found" in result.output
    else:
        assert "up-to-date" in result.output
    assert "All dependencies are up-to-date" not in result.output
    assert "local/pkg" not in result.output
    downloader.return_value.list_remote_refs.assert_not_called()
    client.download_archive.assert_not_called()
    after = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert after == before
    clear_apm_yml_cache()
