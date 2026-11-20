"""Bootstrap ``--target`` preflight for ``apm install <pkg>`` (#3878)."""

from __future__ import annotations

from pathlib import Path

import click
import pytest

from apm_cli.install import manifest_helpers
from apm_cli.install.manifest_helpers import preflight_bootstrap_targets

pytestmark = pytest.mark.component


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_refuses_an_ide_before_bootstrapping(project: Path) -> None:
    with pytest.raises(click.ClickException) as excinfo:
        preflight_bootstrap_targets(("acme/x",), "claude,intellij", mcp_name=None, global_=False)
    assert excinfo.value.exit_code == 1
    assert excinfo.value.message.startswith("Cannot create apm.yml for --target intellij: ")
    assert excinfo.value.message.endswith(" Nothing was written.")
    assert list(project.iterdir()) == []


def test_maps_aliases_through_the_owner(project: Path) -> None:
    targets = preflight_bootstrap_targets(("acme/x",), "agents", mcp_name=None, global_=False)
    assert targets == ["copilot"]


@pytest.mark.parametrize(
    ("packages", "mcp_name"),
    [
        pytest.param((), None, id="no-packages"),
        pytest.param(("npx",), "server", id="mcp-install"),
    ],
)
def test_skips_paths_that_do_not_bootstrap(
    project: Path, packages: tuple[str, ...], mcp_name: str | None
) -> None:
    assert (
        preflight_bootstrap_targets(packages, "intellij", mcp_name=mcp_name, global_=False) is None
    )


def test_existing_manifest_is_never_bootstrapped(project: Path) -> None:
    (project / "apm.yml").write_text("name: p\nversion: 1.0.0\n", encoding="utf-8")
    assert (
        preflight_bootstrap_targets(("acme/x",), "intellij", mcp_name=None, global_=False) is None
    )


@pytest.mark.parametrize(
    "detect",
    [
        pytest.param(lambda _path: object(), id="bundle"),
        pytest.param(lambda _path: (_ for _ in ()).throw(ValueError("unsafe")), id="bad-bundle"),
    ],
)
def test_local_bundles_never_edit_apm_yml(
    project: Path, monkeypatch: pytest.MonkeyPatch, detect
) -> None:
    (project / "bundle").mkdir()
    monkeypatch.setattr("apm_cli.bundle.local_bundle.detect_local_bundle", detect)
    assert (
        preflight_bootstrap_targets(("bundle",), "intellij", mcp_name=None, global_=False) is None
    )


def test_local_package_directory_is_still_refused(project: Path) -> None:
    (project / "pkg").mkdir()
    with pytest.raises(click.ClickException):
        manifest_helpers.preflight_bootstrap_targets(
            ("pkg",), "intellij", mcp_name=None, global_=False
        )
