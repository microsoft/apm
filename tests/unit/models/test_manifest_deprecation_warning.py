"""Deprecated ``target:`` / ``compilation.target`` warning gating (#3762)."""

from __future__ import annotations

from pathlib import Path

import pytest

from apm_cli.models.apm_package import APMPackage
from apm_cli.models.manifest_targets import (
    read_manifest_target_declaration,
    reset_manifest_deprecation_warnings,
)

pytestmark = pytest.mark.component

_TARGET_DEPRECATION = (
    "uses the deprecated 'target:' key; 'target:' will be removed in a future "
    "release in favor of 'targets:'."
)
_TARGET_WARNING = (
    f"[!] apm.yml {_TARGET_DEPRECATION} Replace 'target: claude' with 'targets: [claude]'."
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


def test_unknown_key_handler_does_not_control_the_deprecation_warning(tmp_path, capsys):
    _parse(tmp_path / "apm.yml", "target: claude\n", warning_handler=None)

    assert _flat(capsys.readouterr().err) == _TARGET_WARNING


def test_user_manifest_warning_names_its_path_and_replacement(tmp_path, capsys):
    user_manifest = tmp_path / "home" / ".apm" / "apm.yml"
    _parse(user_manifest, "target: claude\n")

    err = "".join(capsys.readouterr().err.split())
    assert err.startswith(f"[!]{user_manifest.as_posix()}usesthedeprecated'target:'key;")
    assert err.endswith("Replace'target:claude'with'targets:[claude]'.")
    assert "v1.0" not in err


@pytest.mark.parametrize(
    ("body", "hint"),
    [
        (
            "target: [claude, cursor]\n",
            "Replace 'target: [claude, cursor]' with 'targets: [claude, cursor]'.",
        ),
        (
            "target: claude,agents\n",
            "Replace 'target: claude,agents' with 'targets: [claude, copilot]'.",
        ),
        ("target:\n", "Delete the empty 'target:' key."),
        (
            "target: intellij\n",
            "'targets:' has no exact equivalent of 'target: intellij' here; list the "
            "targets you use, e.g. 'targets: [claude, copilot]'.",
        ),
        (
            "target: claude,vscode\n",
            "Replace 'target: claude,vscode' with 'targets: [claude, copilot]'. "
            "(`vscode` is a deprecated alias: replace `vscode` with `copilot`.)",
        ),
    ],
    ids=["flow-list", "alias", "empty", "no-equivalent", "vscode-alias"],
)
def test_project_warning_names_the_behaviour_preserving_replacement(tmp_path, capsys, body, hint):
    _parse(tmp_path / "apm.yml", body)

    assert _flat(capsys.readouterr().err) == f"[!] apm.yml {_TARGET_DEPRECATION} {hint}"


def test_singular_all_expands_in_the_project_but_not_the_user_manifest(tmp_path, capsys):
    from apm_cli.core.target_catalog import expand_all

    _parse(tmp_path / "apm.yml", "target: all\n")
    expanded = ", ".join(expand_all("install"))
    assert _flat(capsys.readouterr().err).endswith(
        f"Replace 'target: all' with 'targets: [{expanded}]'. "
        "('targets: [all]' means auto-detect, not every target.)"
    )

    _parse(tmp_path / "home" / ".apm" / "apm.yml", "target: all\n")
    assert _flat(capsys.readouterr().err).endswith(
        "'targets:' has no exact equivalent of 'target: all' here; list the targets "
        "you use, e.g. 'targets: [claude, copilot]'."
    )


def test_deprecated_vscode_alias_warns_for_consumer_manifest_once(tmp_path, capsys):
    """vscode is read as copilot; only the consumer manifest warns (#3866)."""
    package = _parse(tmp_path / "apm.yml", "targets: [vscode]\n")
    _parse(tmp_path / "apm.yml", "targets: [vscode]\n")

    assert package.target_declaration.selection == ["copilot"]
    assert _flat(capsys.readouterr().err) == (
        "[!] apm.yml: replace `vscode` with `copilot` in 'targets:'; `vscode` is "
        "deprecated and will be removed in a future release."
    )

    dependency = _parse(
        tmp_path / "apm_modules" / "acme" / "pkg" / "apm.yml", "targets: [vscode]\n"
    )
    assert dependency.target_declaration.selection == ["copilot"]
    assert capsys.readouterr().err == ""


def test_ignored_compilation_target_warns_once(tmp_path, capsys):
    _parse(tmp_path / "apm.yml", "compilation:\n  target: cursor\n")
    _parse(tmp_path / "apm.yml", "compilation:\n  target: cursor\n")

    assert _flat(capsys.readouterr().err) == (
        "[!] apm.yml sets 'compilation.target', which APM ignores; targets come from "
        "'targets:' or auto-detection. Delete it; a future release will reject it."
    )


def test_invalid_target_fails_before_warning(tmp_path, capsys):
    with pytest.raises(ValueError, match="not a valid target"):
        _parse(tmp_path / "apm.yml", "target: nope\n")

    assert capsys.readouterr().err == ""


def test_path_only_reader_warns_for_consumer_manifest_once(tmp_path, capsys):
    manifest = tmp_path / "apm.yml"
    manifest.write_text("name: demo\nversion: 1.0.0\ntarget: claude\n", encoding="utf-8")

    read_manifest_target_declaration(manifest)
    read_manifest_target_declaration(manifest, lenient=True)
    _parse(manifest, "target: claude\n")

    assert _flat(capsys.readouterr().err) == _TARGET_WARNING


def test_path_only_reader_never_warns_for_dependency_manifest(tmp_path, capsys):
    manifest = tmp_path / "apm_modules" / "acme" / "pkg" / "apm.yml"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("name: dep\nversion: 1.0.0\ntarget: claude\n", encoding="utf-8")

    read_manifest_target_declaration(manifest)

    assert capsys.readouterr().err == ""


def test_user_and_project_manifests_each_warn_once(tmp_path, capsys):
    _parse(tmp_path / "home" / ".apm" / "apm.yml", "target: claude\n")
    _parse(tmp_path / "apm.yml", "target: claude\n")
    _parse(tmp_path / "apm.yml", "target: claude\n")

    err = "".join(capsys.readouterr().err.split())
    assert err.count("usesthedeprecated'target:'key") == 2
    assert err.count(f"{(tmp_path / 'home' / '.apm' / 'apm.yml').as_posix()}uses") == 1


def test_prospective_parse_can_opt_out_explicitly(tmp_path, capsys):
    _parse(tmp_path / "apm.yml", "target: claude\n", warn_deprecated=False)

    assert capsys.readouterr().err == ""
