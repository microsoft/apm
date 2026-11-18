"""Contract tests for the package manifest target declaration owner (#3732)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from apm_cli.core import apm_yml
from apm_cli.core.errors import (
    ConflictingTargetsError,
    EmptyTargetsListError,
    TargetResolutionError,
    UnknownTargetError,
)
from apm_cli.core.target_detection import EffectiveTargetDecision
from apm_cli.models.apm_package import (
    APMPackage,
    canonical_package_target_config,
    canonical_package_targets,
    package_target_declaration,
    package_target_selection,
)
from apm_cli.models.manifest_targets import (
    ManifestTargetDeclaration,
    TargetRewriteError,
    bootstrap_target_names,
    manifest_target_restriction,
    read_manifest_target_declaration,
    rewrite_target_names,
    verify_rewritten_manifest,
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
    # Tokens are validated when a view is read, not when the file is loaded.
    declaration = read_manifest_target_declaration(manifest)
    with pytest.raises(UnknownTargetError):
        declaration.restriction()

    manifest.write_text("- not\n- a mapping\n", encoding="utf-8")
    assert read_manifest_target_declaration(manifest, lenient=True) is None
    with pytest.raises(yaml.YAMLError, match="must contain a YAML object"):
        read_manifest_target_declaration(manifest)

    manifest.write_text("targets: [claude\n", encoding="utf-8")
    assert read_manifest_target_declaration(manifest, lenient=True) is None
    with pytest.raises(yaml.YAMLError):
        read_manifest_target_declaration(manifest)


def test_strict_path_reader_refuses_symlinked_manifest(tmp_path: Path):
    real = tmp_path / "real.yml"
    dump_yaml({"name": "p", "version": "1.0.0", "targets": ["claude"]}, real)
    link = tmp_path / "apm.yml"
    try:
        link.symlink_to(real)
    except OSError:
        pytest.skip("symlinks unavailable")

    with pytest.raises(OSError, match="non-symlink"):
        read_manifest_target_declaration(link)
    assert read_manifest_target_declaration(link, lenient=True).restriction() == ["claude"]


def test_strict_restriction_rejects_an_empty_singular_target():
    declaration = _declaration(target=None)
    assert declaration.restriction() is None
    with pytest.raises(EmptyTargetsListError):
        declaration.restriction(strict=True)


def test_strict_restriction_rejects_singular_all_combined_with_targets():
    """compile -g agrees with install: ``all`` cannot be combined with other targets."""
    declaration = _declaration(target="all,claude")
    assert declaration.restriction() is None
    with pytest.raises(TargetResolutionError, match="all"):
        declaration.restriction(strict=True)
    assert _declaration(target="all").restriction(strict=True) is None


def test_stand_in_packages_route_through_the_owner():
    class _StandIn:
        target = "claude,cursor"

    assert package_target_selection(_StandIn()) == ["claude", "cursor"]
    assert manifest_target_restriction({"target": "claude,cursor"}) == ["claude", "cursor"]
    assert manifest_target_restriction(None) == []


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        pytest.param({}, None, id="nothing-declared"),
        pytest.param({"target": None}, None, id="empty-singular"),
        pytest.param({"target": "agents"}, ("copilot",), id="agents-to-copilot"),
        pytest.param({"target": "agents,claude"}, ("copilot", "claude"), id="csv-to-list"),
        pytest.param({"target": "agy"}, ("antigravity",), id="agy-to-antigravity"),
        pytest.param({"targets": ["claude", "cursor"]}, ("claude", "cursor"), id="plural-kept"),
        pytest.param({"targets": ["all"]}, ("all",), id="plural-all-kept"),
    ],
)
def test_rewrite_target_names_keeps_what_deploys(fields, expected):
    """The one apm.yml target rewrite mapping (#3857)."""
    assert rewrite_target_names(ManifestTargetDeclaration.from_mapping(fields)) == expected


def test_rewrite_of_singular_all_is_the_install_expansion():
    """Re-init keeps writing the list ``apm install`` deploys for ``target: all``."""
    names = rewrite_target_names(_declaration(target="all"))
    assert names == EffectiveTargetDecision("all", "apm.yml").runtime_targets
    assert len(names) > 1


@pytest.mark.parametrize("token", ["intellij", "copilot-app"])
def test_rewrite_refuses_a_token_with_no_targets_equivalent(token):
    with pytest.raises(TargetRewriteError) as excinfo:
        rewrite_target_names(_declaration(target=f"claude,{token}"))
    assert str(excinfo.value).startswith(f"Target '{token}' has no 'targets:' equivalent.")


@pytest.mark.parametrize(
    ("fields", "error", "message"),
    [
        pytest.param({"target": "foo"}, ValueError, "'foo' is not a valid target", id="unknown"),
        pytest.param(
            {"targets": ["agents"]}, UnknownTargetError, "Unknown target 'agents'", id="plural"
        ),
        pytest.param(
            {"target": "claude", "targets": ["claude"]},
            ConflictingTargetsError,
            "both 'target:' and 'targets:'",
            id="both",
        ),
    ],
)
def test_rewrite_refuses_a_declaration_install_rejects(fields, error, message):
    with pytest.raises(error, match=message):
        rewrite_target_names(ManifestTargetDeclaration.from_mapping(fields))


def test_verify_refuses_a_rewrite_apm_would_reject(tmp_path):
    manifest = tmp_path / "apm.yml"
    with pytest.raises(TargetRewriteError, match=r"Refusing to write apm.yml.*Unknown target"):
        verify_rewritten_manifest(manifest, "name: demo\ntargets:\n- agents\n")
    with pytest.raises(TargetRewriteError, match=r"YAML anchor '&t' on line 1"):
        verify_rewritten_manifest(manifest, "x: &t [claude]\ntargets: *t\n")
    verify_rewritten_manifest(manifest, "name: demo\ntargets:\n- copilot\n")
    assert not manifest.exists()


@pytest.mark.parametrize(
    "fields",
    [{"target": "claude,vscode"}, {"targets": ["claude", "vscode"]}],
    ids=["target", "targets"],
)
def test_both_spellings_resolve_vscode_to_copilot_for_dependency_intersection(tmp_path, fields):
    """A package migrated to ``targets: [vscode]`` still deploys to copilot (#3762)."""
    manifest = tmp_path / "apm.yml"
    dump_yaml({"name": "p", "version": "1.0.0", **fields}, manifest)

    assert ManifestTargetDeclaration.from_mapping(fields).canonical == ("claude", "copilot")
    assert canonical_package_targets(APMPackage.from_apm_yml(manifest)) == ("claude", "copilot")
    assert canonical_package_target_config(APMPackage.from_apm_yml(manifest)) == {
        "targets": ["claude", "copilot"]
    }


@pytest.mark.parametrize(
    ("names", "expected"),
    [([], ()), (["claude"], ("claude",)), (["claude", "copilot"], ("claude", "copilot"))],
)
def test_bootstrap_target_names_spells_readable_targets(names, expected):
    """#3870: a new manifest records only tokens the reader accepts."""
    assert bootstrap_target_names(names) == expected


@pytest.mark.parametrize("names", [["intellij"], ["claude", "intellij"]])
def test_bootstrap_target_names_refuses_an_unspellable_target(names):
    with pytest.raises(TargetRewriteError) as excinfo:
        bootstrap_target_names(names)
    assert excinfo.value.token == "intellij"
