"""Schema-conformance tests for the ``plugin.json`` produced by
:func:`apm_cli.bundle.plugin_exporter.export_plugin_bundle`.

These tests guard the canonical ``apm pack`` output (default ``--format
plugin``) against drift from the official Claude Code plugin manifest
schema. The vendored schema lives at
``tests/fixtures/schemas/claude-code-plugin.schema.json`` and matches
the published source at https://json.schemastore.org/claude-code-plugin.json.

Each test exercises a different plugin shape (synthesized vs. authored,
with/without optional fields) and asserts ``Draft7Validator.iter_errors``
returns no errors against the on-disk ``plugin.json``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft7Validator

from apm_cli.bundle.plugin_exporter import export_plugin_bundle

# Re-use the rich project fixture from the main exporter test module so we
# don't duplicate the apm.yml + lockfile + .apm/ scaffolding.
from tests.unit.test_plugin_exporter import _setup_plugin_project

_PLUGIN_SCHEMA_PATH = (
    Path(__file__).parent.parent / "fixtures" / "schemas" / "claude-code-plugin.schema.json"
)


@pytest.fixture(scope="module")
def plugin_validator() -> Draft7Validator:
    schema = json.loads(_PLUGIN_SCHEMA_PATH.read_text(encoding="utf-8"))
    return Draft7Validator(schema)


def _validate(pj_path: Path, validator: Draft7Validator) -> None:
    """Read ``plugin.json`` from disk and assert schema-clean."""
    assert pj_path.exists(), f"plugin.json missing at {pj_path}"
    doc = json.loads(pj_path.read_text(encoding="utf-8"))
    errors = sorted(
        validator.iter_errors(doc),
        key=lambda e: list(e.absolute_path),
    )
    assert errors == [], "plugin.json failed official schema:\n" + "\n".join(
        f"  {list(e.absolute_path)}: {e.message}" for e in errors
    )


class TestSynthesizedPluginJsonSchema:
    """``plugin.json`` synthesized from ``apm.yml`` (no authored manifest)."""

    def test_minimal_synthesis_validates(self, tmp_path, plugin_validator):
        project = _setup_plugin_project(tmp_path, agents=["bot.agent.md"])
        out = tmp_path / "build"
        result = export_plugin_bundle(project, out)
        _validate(result.bundle_path / "plugin.json", plugin_validator)

    def test_synthesis_with_full_apm_yml_metadata_validates(self, tmp_path, plugin_validator):
        project = _setup_plugin_project(
            tmp_path,
            agents=["a.agent.md"],
            skills={"s": ["SKILL.md"]},
            apm_yml_extra={
                "description": "A demo package",
                "author": "Test Author",
                "license": "MIT",
            },
        )
        out = tmp_path / "build"
        result = export_plugin_bundle(project, out)
        _validate(result.bundle_path / "plugin.json", plugin_validator)


class TestAuthoredPluginJsonSchema:
    """Authored ``plugin.json`` shipped at project root must round-trip clean."""

    def test_authored_minimal_validates(self, tmp_path, plugin_validator):
        project = _setup_plugin_project(
            tmp_path,
            agents=["a.agent.md"],
            plugin_json={"name": "authored-min", "version": "1.0.0"},
        )
        out = tmp_path / "build"
        result = export_plugin_bundle(project, out)
        _validate(result.bundle_path / "plugin.json", plugin_validator)

    def test_authored_with_author_object_validates(self, tmp_path, plugin_validator):
        project = _setup_plugin_project(
            tmp_path,
            agents=["a.agent.md"],
            plugin_json={
                "name": "authored-rich",
                "version": "2.1.0",
                "description": "Rich authored manifest",
                "author": {
                    "name": "Acme Inc",
                    "email": "team@acme.example",
                    "url": "https://acme.example",
                },
                "homepage": "https://acme.example/plugin",
                "repository": "https://github.com/acme/plugin",
                "license": "Apache-2.0",
                "keywords": ["demo", "ci"],
            },
        )
        out = tmp_path / "build"
        result = export_plugin_bundle(project, out)
        _validate(result.bundle_path / "plugin.json", plugin_validator)

    def test_authored_legacy_invalid_keys_are_stripped_to_validate(
        self, tmp_path, plugin_validator
    ):
        """Pre-existing invalid convention-dir entries are scrubbed at pack
        time so the published ``plugin.json`` is schema-clean even if the
        author shipped a stale manifest from before the schema clarified
        these fields are auto-discovered."""
        project = _setup_plugin_project(
            tmp_path,
            agents=["a.agent.md"],
            skills={"s1": ["SKILL.md"]},
            plugin_json={
                "name": "legacy",
                "version": "1.0.0",
                # These are invalid per the official schema (no leading ./
                # and not pointing at .md files). The exporter must scrub
                # them before write so the published manifest validates.
                "agents": ["agents/"],
                "skills": ["skills/"],
            },
        )
        out = tmp_path / "build"
        result = export_plugin_bundle(project, out)
        _validate(result.bundle_path / "plugin.json", plugin_validator)


class TestExportedComponentsStillReachable:
    """Stripping convention-dir keys from ``plugin.json`` must not affect
    file placement -- Claude Code auto-discovers the convention dirs."""

    def test_convention_dirs_present_on_disk(self, tmp_path, plugin_validator):
        project = _setup_plugin_project(
            tmp_path,
            agents=["bot.agent.md"],
            skills={"writer": ["SKILL.md"]},
            commands=["build.md"],
            instructions=["style.md"],
        )
        out = tmp_path / "build"
        result = export_plugin_bundle(project, out)

        bundle = result.bundle_path
        assert (bundle / "agents" / "bot.agent.md").exists()
        assert (bundle / "skills" / "writer" / "SKILL.md").exists()
        assert (bundle / "commands" / "build.md").exists()
        assert (bundle / "instructions" / "style.md").exists()
        # And the manifest validates (no broken references).
        _validate(bundle / "plugin.json", plugin_validator)
