"""Contract tests for the package manifest target declaration owner (#3732)."""

from __future__ import annotations

from pathlib import Path

import pytest

from apm_cli.core import apm_yml
from apm_cli.core.errors import ConflictingTargetsError, UnknownTargetError
from apm_cli.core.target_detection import EffectiveTargetDecision
from apm_cli.models.apm_package import (
    APMPackage,
    canonical_package_target_config,
    package_target_declaration,
    package_target_selection,
)
from apm_cli.models.manifest_targets import (
    ManifestTargetDeclaration,
    manifest_target_restriction,
    read_manifest_target_declaration,
)
from apm_cli.utils.yaml_io import dump_yaml


def _declaration(**fields) -> ManifestTargetDeclaration:
    return ManifestTargetDeclaration.from_mapping(fields)


@pytest.fixture(autouse=True)
def _rearm_legacy_all_warning():
    apm_yml._reset_legacy_all_warning()
    yield
    apm_yml._reset_legacy_all_warning()


@pytest.mark.parametrize(
    ("singular", "plural"),
    [
        ("claude,vscode", ["claude", "vscode"]),
        (["claude", "vscode"], ["claude", "vscode"]),
        ("agy", ["antigravity"]),
        ("agents", ["copilot"]),
    ],
)
def test_spellings_project_to_the_same_policy_targets(singular, plural):
    """``target:`` CSV/list and ``targets:`` are one declaration for policy."""
    singular_decl = _declaration(target=singular)
    plural_decl = _declaration(targets=plural)

    assert singular_decl.spelling == "target"
    assert plural_decl.spelling == "targets"
    assert (
        EffectiveTargetDecision(singular_decl.selection, "apm.yml").canonical_targets
        == EffectiveTargetDecision(plural_decl.selection, "apm.yml").canonical_targets
    )


def test_vscode_spelling_keeps_its_mcp_runtime():
    decision = EffectiveTargetDecision(_declaration(targets=["vscode"]).selection, "apm.yml")
    assert decision.canonical_targets == ("copilot",)
    assert decision.runtime_targets == ("vscode",)


@pytest.mark.parametrize(
    ("fields", "selection", "legacy_all", "explicit"),
    [
        ({}, None, False, False),
        ({"target": None}, None, False, False),
        ({"target": "all"}, "all", True, False),
        ({"targets": ["all"]}, None, True, False),
        ({"targets": ["claude"]}, ["claude"], False, True),
    ],
)
def test_declaration_views(fields, selection, legacy_all, explicit):
    declaration = ManifestTargetDeclaration.from_mapping(fields)
    assert declaration.selection == selection
    assert declaration.legacy_all is legacy_all
    assert declaration.explicit is explicit


def test_conflicting_keys_are_rejected():
    with pytest.raises(ConflictingTargetsError):
        _ = _declaration(target="claude", targets=["cursor"]).selection


def test_singular_all_restriction_folds_without_the_omitted_warning(capsys):
    """Install expands ``target: all``; it must not be reported as omitted."""
    declaration = _declaration(target="all")

    assert declaration.restriction() is None
    declaration.validate_for_install()
    assert canonical_package_target_config(APMPackage("p", "1", target="all")) == {}

    assert "treating the field as omitted" not in capsys.readouterr().out


def test_plural_all_still_warns_that_it_is_omitted(capsys):
    assert _declaration(targets=["all"]).restriction() is None
    assert "treating the field as omitted" in capsys.readouterr().out


@pytest.mark.parametrize("token", ["intellij", "copilot-app"])
def test_install_rejects_tokens_it_cannot_deploy(token):
    """The manifest parser accepts these singular tokens; install must not."""
    declaration = _declaration(target=token)
    assert declaration.selection == token

    with pytest.raises(UnknownTargetError):
        declaration.validate_for_install()


def test_install_accepts_deployable_declarations():
    for fields in ({"target": "claude,copilot"}, {"targets": ["vscode"]}, {}):
        ManifestTargetDeclaration.from_mapping(fields).validate_for_install()


def test_parsed_package_exposes_its_declaration(tmp_path: Path):
    manifest = tmp_path / "apm.yml"
    dump_yaml({"name": "p", "version": "1.0.0", "targets": ["claude", "vscode"]}, manifest)

    package = APMPackage.from_apm_yml(manifest)

    assert package.target_declaration is not None
    assert package_target_declaration(package) is package.target_declaration
    assert package_target_selection(package) == ["claude", "vscode"]
    assert read_manifest_target_declaration(manifest).restriction() == ["claude", "vscode"]


def test_path_reader_is_lenient_only_on_request(tmp_path: Path):
    manifest = tmp_path / "apm.yml"
    dump_yaml({"name": "p", "version": "1.0.0", "targets": ["nope"]}, manifest)

    assert read_manifest_target_declaration(tmp_path / "missing.yml") is None
    assert read_manifest_target_declaration(manifest, lenient=True) is None
    with pytest.raises(UnknownTargetError):
        read_manifest_target_declaration(manifest)


def test_stand_in_packages_route_through_the_owner():
    class _StandIn:
        target = "claude,cursor"

    assert package_target_selection(_StandIn()) == ["claude", "cursor"]
    assert manifest_target_restriction({"target": "claude,cursor"}) == ["claude", "cursor"]
    assert manifest_target_restriction(None) == []
