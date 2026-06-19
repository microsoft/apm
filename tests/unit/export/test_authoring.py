"""Tests for the authoring-path license warning (issue #1777, U6 / D3b).

ASYMMETRY (npm-faithful): WARN only on the AUTHORING path (apm pack/publish on
the author's OWN apm.yml) when no ``license:`` is declared. The CONSUMING path
(install/export of others' deps) stays SILENT -- never nag per-install about
transitive deps. The warning must be actionable and ASCII-only.
"""

from __future__ import annotations

from apm_cli.export.authoring import warn_if_license_undeclared


def _capture():
    messages: list[str] = []
    return messages, messages.append


def test_warns_when_apm_yml_has_no_license(tmp_path):
    apm_yml = tmp_path / "apm.yml"
    apm_yml.write_text("name: pkg\nversion: 1.0.0\n")
    messages, emit = _capture()

    warned = warn_if_license_undeclared(apm_yml, emit)

    assert warned is True
    assert len(messages) == 1
    assert "license:" in messages[0]
    assert "apm.yml" in messages[0]


def test_silent_when_license_declared(tmp_path):
    apm_yml = tmp_path / "apm.yml"
    apm_yml.write_text("name: pkg\nversion: 1.0.0\nlicense: MIT\n")
    messages, emit = _capture()

    warned = warn_if_license_undeclared(apm_yml, emit)

    assert warned is False
    assert messages == []


def test_silent_when_special_token_declared(tmp_path):
    apm_yml = tmp_path / "apm.yml"
    apm_yml.write_text("name: pkg\nlicense: UNLICENSED\n")
    messages, emit = _capture()

    warned = warn_if_license_undeclared(apm_yml, emit)

    assert warned is False
    assert messages == []


def test_warns_on_blank_license(tmp_path):
    apm_yml = tmp_path / "apm.yml"
    apm_yml.write_text('name: pkg\nlicense: "  "\n')
    messages, emit = _capture()

    assert warn_if_license_undeclared(apm_yml, emit) is True
    assert len(messages) == 1


def test_message_is_ascii_only(tmp_path):
    apm_yml = tmp_path / "apm.yml"
    apm_yml.write_text("name: pkg\n")
    messages, emit = _capture()

    warn_if_license_undeclared(apm_yml, emit)

    assert messages
    assert messages[0].isascii()


def test_missing_apm_yml_does_not_raise(tmp_path):
    messages, emit = _capture()
    # Never blocks: a missing manifest just yields no warning.
    assert warn_if_license_undeclared(tmp_path / "apm.yml", emit) is False
    assert messages == []
