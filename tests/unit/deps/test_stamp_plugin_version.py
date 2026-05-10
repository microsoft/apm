"""Tests for ``stamp_plugin_version`` helper.

Extracted from the duplicate inline blocks that used to live in
``download_package`` and ``download_subdirectory_package``.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from apm_cli.deps.package_validator import stamp_plugin_version
from apm_cli.models.validation import PackageType


def _pkg(version="0.0.0"):
    return SimpleNamespace(version=version)


def test_stamps_short_sha_when_marketplace_plugin_and_zero_version(tmp_path):
    pkg = _pkg("0.0.0")
    apm_yml = tmp_path / "apm.yml"
    apm_yml.write_text("name: foo\nversion: 0.0.0\n", encoding="utf-8")

    stamp_plugin_version(
        pkg,
        PackageType.MARKETPLACE_PLUGIN,
        "abcdef1234567890aabbccddeeff00112233abcd",
        tmp_path,
    )

    assert pkg.version == "abcdef1"
    assert "version: abcdef1" in apm_yml.read_text(encoding="utf-8")


def test_no_op_when_package_type_is_not_marketplace_plugin(tmp_path):
    pkg = _pkg("0.0.0")
    apm_yml = tmp_path / "apm.yml"
    apm_yml.write_text("name: foo\nversion: 0.0.0\n", encoding="utf-8")

    stamp_plugin_version(
        pkg,
        PackageType.APM_PACKAGE,
        "abcdef1234567890aabbccddeeff00112233abcd",
        tmp_path,
    )

    assert pkg.version == "0.0.0"
    assert "version: 0.0.0" in apm_yml.read_text(encoding="utf-8")


def test_no_op_when_version_is_already_set(tmp_path):
    pkg = _pkg("1.2.3")
    apm_yml = tmp_path / "apm.yml"
    apm_yml.write_text("name: foo\nversion: 1.2.3\n", encoding="utf-8")

    stamp_plugin_version(
        pkg,
        PackageType.MARKETPLACE_PLUGIN,
        "abcdef1234567890aabbccddeeff00112233abcd",
        tmp_path,
    )

    assert pkg.version == "1.2.3"


@pytest.mark.parametrize("commit", ["", None, "unknown"])
def test_no_op_when_commit_is_unusable(tmp_path, commit):
    pkg = _pkg("0.0.0")
    apm_yml = tmp_path / "apm.yml"
    apm_yml.write_text("name: foo\nversion: 0.0.0\n", encoding="utf-8")

    stamp_plugin_version(pkg, PackageType.MARKETPLACE_PLUGIN, commit, tmp_path)

    assert pkg.version == "0.0.0"


def test_no_op_when_apm_yml_is_missing(tmp_path):
    pkg = _pkg("0.0.0")
    # The in-memory package version is still updated for the lockfile.
    stamp_plugin_version(
        pkg,
        PackageType.MARKETPLACE_PLUGIN,
        "abcdef1234567890aabbccddeeff00112233abcd",
        tmp_path,
    )
    assert pkg.version == "abcdef1"
    assert not (tmp_path / "apm.yml").exists()


def test_no_op_when_package_is_none(tmp_path):
    apm_yml = tmp_path / "apm.yml"
    apm_yml.write_text("name: foo\nversion: 0.0.0\n", encoding="utf-8")

    # Should not raise.
    stamp_plugin_version(
        None,
        PackageType.MARKETPLACE_PLUGIN,
        "abcdef1234567890aabbccddeeff00112233abcd",
        tmp_path,
    )

    assert "version: 0.0.0" in apm_yml.read_text(encoding="utf-8")
