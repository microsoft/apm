"""Tests for read_declared_license -- the install-time backfill source (U6).

Reads the DECLARED license from a resolved dependency's manifest at its
install path. APM mirrors npm: it trusts the manifest's declared ``license``
and never reads the LICENSE file text. apm.yml wins over plugin.json when both
exist; absence yields ``None`` (omitted from the lockfile, == unknown).
"""

from __future__ import annotations

import json

from apm_cli.export.declared_license import read_declared_license


def test_reads_license_from_apm_yml(tmp_path):
    (tmp_path / "apm.yml").write_text("name: pkg\nversion: 1.0.0\nlicense: MIT\n")
    assert read_declared_license(tmp_path) == "MIT"


def test_reads_license_from_plugin_json(tmp_path):
    (tmp_path / "plugin.json").write_text(json.dumps({"name": "p", "license": "Apache-2.0"}))
    assert read_declared_license(tmp_path) == "Apache-2.0"


def test_apm_yml_license_wins_over_plugin_json(tmp_path):
    (tmp_path / "apm.yml").write_text("name: pkg\nlicense: MIT\n")
    (tmp_path / "plugin.json").write_text(json.dumps({"name": "p", "license": "Apache-2.0"}))
    assert read_declared_license(tmp_path) == "MIT"


def test_absent_license_returns_none(tmp_path):
    (tmp_path / "apm.yml").write_text("name: pkg\nversion: 1.0.0\n")
    assert read_declared_license(tmp_path) is None


def test_no_manifest_returns_none(tmp_path):
    assert read_declared_license(tmp_path) is None


def test_single_primitive_file_returns_none(tmp_path):
    # A lone primitive file (no manifest) cannot declare a license.
    (tmp_path / "skill.md").write_text("# a skill\n")
    assert read_declared_license(tmp_path) is None


def test_special_token_preserved_verbatim(tmp_path):
    (tmp_path / "apm.yml").write_text("name: pkg\nlicense: UNLICENSED\n")
    assert read_declared_license(tmp_path) == "UNLICENSED"


def test_blank_license_is_treated_as_absent(tmp_path):
    (tmp_path / "apm.yml").write_text('name: pkg\nlicense: "  "\n')
    assert read_declared_license(tmp_path) is None


def test_install_path_pointing_at_file_uses_parent(tmp_path):
    (tmp_path / "apm.yml").write_text("name: pkg\nlicense: BSD-3-Clause\n")
    primitive = tmp_path / "skill.md"
    primitive.write_text("# x\n")
    assert read_declared_license(primitive) == "BSD-3-Clause"
