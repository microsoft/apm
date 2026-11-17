"""Deprecated ``target:`` / ``compilation.target`` warning gating (#3762)."""

from __future__ import annotations

from pathlib import Path

import pytest

from apm_cli.models.apm_package import APMPackage, reset_manifest_deprecation_warnings

_TARGET_WARNING = (
    "[!] apm.yml uses the deprecated 'target:' key. Use 'targets:' (a YAML list). "
    "Run 'apm manifest migrate' to update it. 'target:' will be removed in v1.0."
)


@pytest.fixture(autouse=True)
def _rearm(monkeypatch, tmp_path):
    reset_manifest_deprecation_warnings()
    monkeypatch.setenv("APM_HOME", str(tmp_path / "home" / ".apm"))
    monkeypatch.chdir(tmp_path)
    yield
    reset_manifest_deprecation_warnings()


def _parse(manifest: Path, body: str, **kwargs) -> APMPackage:
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(f"name: demo\nversion: 1.0.0\n{body}", encoding="utf-8")
    data = {"name": "demo", "version": "1.0.0"}
    import yaml

    data.update(yaml.safe_load(body) or {})
    return APMPackage.from_mapping(
        data, package_path=manifest.parent, manifest_path=manifest, create_config=False, **kwargs
    )


def _flat(text: str) -> str:
    return " ".join(text.split())


def test_project_manifest_warns_once_on_stderr(tmp_path, capsys):
    _parse(tmp_path / "apm.yml", "target: claude\n")
    _parse(tmp_path / "apm.yml", "target: claude\n")

    captured = capsys.readouterr()
    assert _flat(captured.err) == _TARGET_WARNING
    assert captured.out == ""


def test_dependency_manifest_never_warns(tmp_path, capsys):
    _parse(tmp_path / "apm_modules" / "acme" / "pkg" / "apm.yml", "target: claude\n")

    assert capsys.readouterr().err == ""


def test_plural_targets_key_does_not_warn(tmp_path, capsys):
    _parse(tmp_path / "apm.yml", "targets: [claude]\n")

    assert capsys.readouterr().err == ""


def test_prospective_parse_without_warning_handler_does_not_warn(tmp_path, capsys):
    _parse(tmp_path / "apm.yml", "target: claude\n", warning_handler=None)

    assert capsys.readouterr().err == ""


def test_user_manifest_warning_points_at_global_migrate(tmp_path, capsys):
    user_manifest = tmp_path / "home" / ".apm" / "apm.yml"
    _parse(user_manifest, "target: claude\n")

    err = "".join(capsys.readouterr().err.split())
    assert err.startswith(f"[!]{user_manifest.as_posix()}usesthedeprecated'target:'key.")
    assert "Run'apmmanifestmigrate--global'toupdateit." in err


def test_ignored_compilation_target_warns_once(tmp_path, capsys):
    _parse(tmp_path / "apm.yml", "compilation:\n  target: cursor\n")
    _parse(tmp_path / "apm.yml", "compilation:\n  target: cursor\n")

    assert _flat(capsys.readouterr().err) == (
        "[!] apm.yml sets 'compilation.target', which APM ignores: targets come from "
        "'targets:' or auto-detection. Remove the key. 'compilation.target' will be "
        "removed in v1.0."
    )


def test_invalid_target_fails_before_warning(tmp_path, capsys):
    with pytest.raises(ValueError, match="not a valid target"):
        _parse(tmp_path / "apm.yml", "target: nope\n")

    assert capsys.readouterr().err == ""
