"""Unit tests for the apm.yml read owner (duplicate top-level keys, #3691)."""

from __future__ import annotations

from pathlib import Path

import pytest

from apm_cli.models.apm_package import APMPackage, clear_apm_yml_cache
from apm_cli.models.manifest_io import (
    DuplicateManifestKeyError,
    load_manifest_roundtrip,
    reject_duplicate_manifest_keys,
)

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
