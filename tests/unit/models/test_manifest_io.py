"""Unit tests for the apm.yml read owner (duplicate keys #3691, anchors #3858)."""

from __future__ import annotations

from pathlib import Path

import pytest

from apm_cli.models.apm_package import APMPackage, clear_apm_yml_cache
from apm_cli.models.manifest_io import (
    DuplicateManifestKeyError,
    ManifestAnchorError,
    load_manifest,
    load_manifest_roundtrip,
    reject_duplicate_manifest_keys,
    reject_manifest_anchors,
)

pytestmark = pytest.mark.component

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
