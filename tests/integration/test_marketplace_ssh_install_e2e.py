"""End-to-end coverage for SSH marketplace dependency persistence."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from apm_cli.cli import cli
from apm_cli.marketplace.models import (
    MarketplaceManifest,
    MarketplacePlugin,
    MarketplaceSource,
)
from apm_cli.models.apm_package import clear_apm_yml_cache

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _clear_manifest_cache() -> None:
    """Keep manifest parsing isolated around the CLI invocation."""
    clear_apm_yml_cache()
    yield
    clear_apm_yml_cache()


def test_ssh_marketplace_url_reaches_dependency_install_unchanged(tmp_path, monkeypatch) -> None:
    """An SSH registration survives persistence, reload, and install dispatch."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("APM_PROGRESS", "never")
    (tmp_path / "apm.yml").write_text(
        "name: ssh-marketplace-consumer\nversion: 1.0.0\n",
        encoding="utf-8",
    )
    source = MarketplaceSource(
        name="apm-reg",
        url="git@gitlab.mycompany.com:team/packages/toolkit.git",
    )
    manifest = MarketplaceManifest(
        name="apm-reg",
        plugins=(
            MarketplacePlugin(
                name="some-skill",
                source="skills/some-skill",
            ),
        ),
    )
    install_result = SimpleNamespace(installed_count=1, diagnostics=None)

    with (
        patch("apm_cli.commands._helpers.check_for_updates", return_value=None),
        patch(
            "apm_cli.marketplace.resolver.get_marketplace_by_name",
            return_value=source,
        ),
        patch(
            "apm_cli.marketplace.resolver.fetch_or_cache",
            return_value=manifest,
        ),
        patch("apm_cli.commands.install._validate_package_exists", return_value=True),
        patch(
            "apm_cli.commands.install._install_apm_dependencies",
            return_value=install_result,
        ) as install_dependencies,
    ):
        result = CliRunner().invoke(
            cli,
            ["install", "some-skill@apm-reg", "--only=apm", "--no-policy"],
            catch_exceptions=False,
        )

    assert result.exit_code == 0, result.output
    install_dependencies.assert_called_once()
    loaded_package = install_dependencies.call_args.args[0]
    loaded_dependencies = loaded_package.get_apm_dependencies()
    assert len(loaded_dependencies) == 1
    dependency = loaded_dependencies[0]
    assert dependency.explicit_scheme == "ssh"
    assert dependency.ssh_user == "git"
    assert dependency.host == "gitlab.mycompany.com"
    assert dependency.repo_url == "team/packages/toolkit"
    assert dependency.virtual_path == "skills/some-skill"
