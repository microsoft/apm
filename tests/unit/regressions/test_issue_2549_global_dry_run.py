from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from apm_cli.cli import cli
from apm_cli.commands.install import (
    _prepare_dry_run_manifest_path,
    _validate_and_add_packages_to_apm_yml,
)
from apm_cli.core.command_logger import _ValidationOutcome
from apm_cli.install.registry_wiring import get_effective_default_registry
from apm_cli.models.apm_package import APMPackage


def test_global_dry_run_command_leaves_absent_home_state_uncreated(tmp_path: Path) -> None:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    user_manifest = fake_home / ".apm" / "apm.yml"
    captured: dict[str, Any] = {}
    outcome = _ValidationOutcome(valid=[("test/pkg", False)], invalid=[])

    def fake_validate(
        packages: tuple[str, ...],
        dry_run: bool,
        *,
        manifest_path: Path,
        **_kwargs: object,
    ) -> tuple[list[str], _ValidationOutcome]:
        captured["validation_manifest_path"] = manifest_path
        captured["validation_manifest_display"] = _kwargs.get("manifest_display")
        assert packages == ("test/pkg",)
        assert dry_run is True
        return ["test/pkg"], outcome

    def fake_install(
        ctx: Any, validation_outcome: _ValidationOutcome
    ) -> tuple[int, int, int, None]:
        captured["install_manifest_path"] = ctx.manifest_path
        captured["install_manifest_display"] = ctx.manifest_display
        assert validation_outcome is outcome
        return 0, 0, 0, None

    with (
        patch.object(Path, "home", return_value=fake_home),
        patch("apm_cli.commands.install._validate_and_add_packages_to_apm_yml", fake_validate),
        patch("apm_cli.commands.install._install_apm_packages", fake_install),
    ):
        result = CliRunner().invoke(
            cli,
            ["install", "--dry-run", "-g", "test/pkg"],
            catch_exceptions=False,
        )

    assert result.exit_code == 0
    assert "Previewing user-scope install" in result.output
    assert captured["validation_manifest_path"] != user_manifest
    assert captured["install_manifest_path"] != user_manifest
    assert captured["install_manifest_display"] == str(user_manifest)
    assert captured["validation_manifest_display"] == str(user_manifest)
    assert not captured["validation_manifest_path"].parent.exists()
    assert not (fake_home / ".apm").exists()


def test_global_dry_run_redirects_absent_manifest_without_creating_user_state(
    tmp_path: Path,
) -> None:
    user_manifest = tmp_path / "home" / ".apm" / "apm.yml"

    preview_manifest, temp_dir = _prepare_dry_run_manifest_path(
        user_manifest,
        dry_run=True,
        user_scope=True,
        has_packages=True,
    )
    try:
        assert preview_manifest != user_manifest
        assert preview_manifest.name == "apm.yml"
        assert not user_manifest.parent.exists()
    finally:
        temp_dir.cleanup()


def test_existing_user_manifest_is_read_in_place(tmp_path: Path) -> None:
    user_manifest = tmp_path / ".apm" / "apm.yml"
    user_manifest.parent.mkdir(parents=True)
    user_manifest.write_text("name: test\n", encoding="utf-8")

    preview_manifest, temp_dir = _prepare_dry_run_manifest_path(
        user_manifest,
        dry_run=True,
        user_scope=True,
        has_packages=True,
    )

    assert preview_manifest == user_manifest
    assert temp_dir is None


def test_registry_default_read_does_not_create_missing_user_config(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_dir = tmp_path / "home" / ".apm"
    config_file = config_dir / "config.json"

    monkeypatch.setattr("apm_cli.config.CONFIG_DIR", str(config_dir))
    monkeypatch.setattr("apm_cli.config.CONFIG_FILE", str(config_file))
    monkeypatch.setattr("apm_cli.config._config_cache", None)

    default_registry = get_effective_default_registry({}, create_config=False)

    assert default_registry is None
    assert not config_dir.exists()


def test_apm_package_preview_parse_does_not_create_missing_user_config(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_dir = tmp_path / "home" / ".apm"
    config_file = config_dir / "config.json"

    monkeypatch.setattr("apm_cli.config.CONFIG_DIR", str(config_dir))
    monkeypatch.setattr("apm_cli.config.CONFIG_FILE", str(config_file))
    monkeypatch.setattr("apm_cli.config._config_cache", None)

    APMPackage.from_mapping(
        {
            "name": "preview",
            "version": "1.0.0",
            "dependencies": {"apm": ["example/pkg"]},
        },
        package_path=tmp_path,
        source_path=tmp_path,
        manifest_path=tmp_path / "apm.yml",
        create_config=False,
    )

    assert not config_dir.exists()


def test_global_dry_run_validation_summary_names_user_manifest(
    tmp_path: Path,
    monkeypatch,
) -> None:
    manifest = tmp_path / "preview" / "apm.yml"
    manifest.parent.mkdir()
    manifest.write_text("name: preview\nversion: 1.0.0\n", encoding="utf-8")
    user_manifest = tmp_path / "home" / ".apm" / "apm.yml"
    logger = MagicMock()
    logger.validation_summary.return_value = True

    def fake_resolve(*_args: object, **_kwargs: object) -> tuple:
        return (
            [("test/pkg", False)],
            [],
            ["test/pkg"],
            None,
            {"test/pkg": "test/pkg"},
            True,
        )

    monkeypatch.setattr("apm_cli.commands.install._resolve_package_references", fake_resolve)

    validated, _outcome = _validate_and_add_packages_to_apm_yml(
        ("test/pkg",),
        dry_run=True,
        logger=logger,
        manifest_path=manifest,
        manifest_display=str(user_manifest),
        create_config=False,
    )

    assert validated == ["test/pkg"]
    logger.progress.assert_any_call(f"Dry run: Would add 1 package to {user_manifest}")
