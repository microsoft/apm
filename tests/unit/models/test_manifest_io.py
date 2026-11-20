"""Tests for the apm.yml read owner (duplicate keys #3691, anchors #3858).

Also holds the on-disk cases of the manifest target declaration owner
(#3732); its pure cases live in test_manifest_targets.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from apm_cli.core import apm_yml
from apm_cli.core.errors import UnknownTargetError
from apm_cli.models.apm_package import (
    APMPackage,
    canonical_package_target_config,
    canonical_package_targets,
    clear_apm_yml_cache,
    package_target_declaration,
    package_target_selection,
)
from apm_cli.models.manifest_io import (
    DuplicateManifestKeyError,
    ManifestAnchorError,
    load_manifest,
    load_manifest_roundtrip,
    reject_duplicate_manifest_keys,
    reject_manifest_anchors,
)
from apm_cli.models.manifest_targets import (
    ManifestTargetDeclaration,
    TargetRewriteError,
    read_manifest_target_declaration,
    verify_rewritten_manifest,
)
from apm_cli.utils.yaml_io import dump_yaml

pytestmark = pytest.mark.component


def _declaration(**fields) -> ManifestTargetDeclaration:
    return ManifestTargetDeclaration.from_mapping(fields)


@pytest.fixture(autouse=True)
def _rearm_legacy_all_warning():
    apm_yml._reset_legacy_all_warning()
    yield
    apm_yml._reset_legacy_all_warning()


_DUPLICATE = (
    "name: demo\nversion: 1.0.0\n"
    "dependencies:\n  apm:\n    - owner/one\n"
    "dependencies:\n  apm:\n    - owner/two\n"
)


@pytest.fixture(autouse=True)
def _fresh_cache():
    clear_apm_yml_cache()
    yield
    clear_apm_yml_cache()


def test_duplicate_top_level_key_names_key_and_lines(tmp_path: Path) -> None:
    with pytest.raises(DuplicateManifestKeyError) as excinfo:
        reject_duplicate_manifest_keys(tmp_path / "apm.yml", _DUPLICATE)

    message = str(excinfo.value)
    assert "top-level key 'dependencies' twice (lines 3 and 6)" in message
    assert "Merge both 'dependencies:' blocks into one" in message
    assert excinfo.value.key == "dependencies"
    assert excinfo.value.lines == (3, 6)


def test_duplicate_keys_on_one_flow_line_are_refused(tmp_path: Path) -> None:
    text = "{name: demo, version: 1.0.0, dependencies: {apm: [a/one]}, dependencies: {apm: []}}\n"
    with pytest.raises(DuplicateManifestKeyError) as excinfo:
        reject_duplicate_manifest_keys(tmp_path / "apm.yml", text)

    message = str(excinfo.value)
    assert "top-level key 'dependencies' twice on line 1" in message
    assert "Merge both 'dependencies' entries into one" in message
    assert excinfo.value.lines == (1, 1)


def test_deeply_nested_text_is_left_to_the_bounded_loader(tmp_path: Path) -> None:
    reject_duplicate_manifest_keys(tmp_path / "apm.yml", "a: " + "[" * 5000 + "]" * 5000)
    assert not (tmp_path / "apm.yml").exists()


@pytest.mark.parametrize(
    "text",
    [
        "name: demo\nversion: 1.0.0\n",
        "base: &base {a: 1}\nother:\n  <<: *base\n  b: 2\n",
        "<<: {a: 1}\nb: 2\n",
        "- a\n- a\n",
        "dependencies:\n  apm: [x]\n  apm: [y]\n",
        "name: [unclosed\n",
        "",
    ],
    ids=["unique", "nested-merge", "top-merge", "list-root", "nested-duplicate", "bad", "empty"],
)
def test_non_duplicated_or_malformed_text_is_left_to_the_loader(tmp_path: Path, text: str) -> None:
    reject_duplicate_manifest_keys(tmp_path / "apm.yml", text)
    assert not (tmp_path / "apm.yml").exists()


def test_package_load_rejects_duplicate_keys(tmp_path: Path) -> None:
    manifest = tmp_path / "apm.yml"
    manifest.write_text(_DUPLICATE, encoding="utf-8")

    with pytest.raises(DuplicateManifestKeyError):
        APMPackage.from_apm_yml(manifest)


def test_roundtrip_load_rejects_duplicate_keys_without_writing(tmp_path: Path) -> None:
    manifest = tmp_path / "apm.yml"
    manifest.write_text(_DUPLICATE, encoding="utf-8")

    with pytest.raises(DuplicateManifestKeyError):
        load_manifest_roundtrip(manifest)
    assert manifest.read_text(encoding="utf-8") == _DUPLICATE


def test_bom_manifest_still_loads(tmp_path: Path) -> None:
    manifest = tmp_path / "apm.yml"
    manifest.write_bytes(b"\xef\xbb\xbfname: demo\nversion: 1.0.0\n")

    assert APMPackage.from_apm_yml(manifest).name == "demo"
    assert load_manifest_roundtrip(manifest)["name"] == "demo"
    assert manifest.read_bytes() == b"\xef\xbb\xbfname: demo\nversion: 1.0.0\n"


@pytest.mark.parametrize(
    ("text", "fragment"),
    [
        pytest.param(
            "name: demo\nx-shared: &rule owner/one\ndependencies:\n  apm:\n    - *rule\n",
            "uses the YAML anchor '&rule' on line 2",
            id="anchor-then-alias",
        ),
        pytest.param(
            "name: demo\ntargets: [claude]\nother: *missing\n",
            "uses the YAML alias '*missing' on line 3",
            id="alias-only",
        ),
        pytest.param(
            "{name: demo, targets: &t [claude], x-copy: *t}\n",
            "uses the YAML anchor '&t' on line 1",
            id="flow",
        ),
        pytest.param(
            "base: &base {a: 1}\nother:\n  <<: *base\n",
            "uses the YAML anchor '&base' on line 1",
            id="merge-key",
        ),
    ],
)
def test_anchor_or_alias_is_refused_with_file_and_line(tmp_path: Path, text: str, fragment: str):
    manifest = tmp_path / "apm.yml"

    with pytest.raises(ManifestAnchorError) as excinfo:
        reject_manifest_anchors(manifest, text)

    message = str(excinfo.value)
    assert message.startswith(str(manifest)), message
    assert fragment in message
    assert "must not use YAML anchors (&name) or aliases (*name)" in message


@pytest.mark.parametrize(
    "text",
    [
        'name: demo\ndescription: "R&D *notes* & more"\nversion: 1.0.0\n',
        "name: a&b\nscripts:\n  build: echo a && echo *\n",
        "name: [unclosed\n",
    ],
    ids=["quoted", "plain-scalars", "malformed"],
)
def test_ampersand_and_asterisk_inside_scalars_are_not_anchors(tmp_path: Path, text: str):
    reject_manifest_anchors(tmp_path / "apm.yml", text)
    assert not (tmp_path / "apm.yml").exists()


def test_every_manifest_read_refuses_anchors_without_writing(tmp_path: Path) -> None:
    from apm_cli.models.manifest_targets import read_manifest_target_declaration

    anchored = "name: demo\nversion: 1.0.0\nx: &t [claude]\ntargets: *t\n"
    manifest = tmp_path / "apm.yml"
    manifest.write_text(anchored, encoding="utf-8")

    for read in (
        APMPackage.from_apm_yml,
        load_manifest,
        load_manifest_roundtrip,
        read_manifest_target_declaration,
    ):
        with pytest.raises(ManifestAnchorError, match=r"anchor '&t' on line 3"):
            read(manifest)
    assert read_manifest_target_declaration(manifest, lenient=True) is None
    assert manifest.read_text(encoding="utf-8") == anchored


def test_parsed_package_exposes_its_declaration(tmp_path: Path):
    manifest = tmp_path / "apm.yml"
    dump_yaml({"name": "p", "version": "1.0.0", "targets": ["claude", "vscode"]}, manifest)

    package = APMPackage.from_apm_yml(manifest)

    assert package.target_declaration is not None
    assert package_target_declaration(package) is package.target_declaration
    assert package_target_selection(package) == ["claude", "copilot"]
    assert read_manifest_target_declaration(manifest).restriction() == ["claude", "copilot"]


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


def test_user_manifest_warning_names_the_resolved_path_from_a_relative_read(
    tmp_path, monkeypatch, capsys
) -> None:
    """`apm update -g` reads ``apm.yml`` relatively; the label still names APM_HOME (C5b)."""
    import apm_cli.models.manifest_targets as owner

    apm_home = tmp_path / "apm-home"
    apm_home.mkdir()
    (apm_home / "apm.yml").write_text("name: u\nversion: 1.0.0\ntarget: claude\n")
    monkeypatch.setenv("APM_HOME", str(apm_home))
    monkeypatch.chdir(apm_home)
    monkeypatch.setattr(owner, "_deprecated_manifest_keys_warned", set())

    owner._warn_deprecated_manifest_keys({"target": "claude"}, Path("apm.yml"))

    captured = capsys.readouterr()
    rendered = (captured.out + captured.err).replace("\n", "")
    assert (apm_home / "apm.yml").resolve().as_posix() in rendered
