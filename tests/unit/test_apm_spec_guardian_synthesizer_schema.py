"""Regression tests for the apm-spec-guardian synthesizer-return schema.

Background
----------
``.apm/skills/apm-spec-guardian/assets/synthesizer-return-schema.json`` is
the canonical (hand-authored) Draft 7 shape the ``spec-editor-synthesizer``
persona MUST return when invoked by the apm-spec-guardian skill. The
generated mirror at
``.agents/skills/apm-spec-guardian/assets/synthesizer-return-schema.json``
is produced by ``apm install`` and must stay byte-identical to the
canonical copy.

The original schema composed ``defer_v0_2.items`` via::

    "allOf": [{"$ref": "#/definitions/fold_item"}, {"required": [...]}]

Each ``allOf`` branch validates independently under Draft 7 (no
``unevaluatedProperties``, which is 2019-09+ only), so ``fold_item``'s own
``additionalProperties: false`` (closed over its own 5 properties) rejected
the sibling branch's ``reserved_slot_anchor`` key. This made every
genuinely valid ``defer_v0_2`` entry -- including the real PR #3150
synthesis return used as a fixture here -- fail schema validation.

The fix inlines a new definition, ``fold_item_with_reserved_slot``, used
directly (no ``allOf``) by ``defer_v0_2.items``. ``fold_item`` itself
(used bare by ``fold_now`` / ``defer_v0_1_1``) is unchanged.

Fixture provenance
-------------------
``tests/fixtures/spec-guardian/pr3150-synthesizer-return-original.json`` is
an unedited, verbatim copy of the genuine ``spec-editor-synthesizer``
return recorded for PR #3150 / issue #3126 (round 1, 2026-10-03,
coordinator session evidence
``spec3150-runtime-evidence/spec-editor-synthesizer-parsed.json``). Its
single ``defer_v0_2`` entry (id ``F9``) cites
``reserved_slot_anchor: "Section 9.2 ..."`` -- Section 9.2 is the
breaking-vs-non-breaking change-classification policy, not an actual
reserved-for-v0.2 slot (contrast the genuinely reserved anchors in
``docs/src/content/docs/specs/openapm-v0.1.md``: Section 4.8 workspaces,
Section 7.9 version withdrawal, Section 10.12 publisher attestations,
Appendix B registry HTTP API). This fixture file intentionally does not
embed any provenance text; it is parsed JSON only.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import jsonschema
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CANONICAL_SCHEMA_PATH = (
    REPO_ROOT / ".apm/skills/apm-spec-guardian/assets/synthesizer-return-schema.json"
)
GENERATED_MIRROR_PATH = (
    REPO_ROOT / ".agents/skills/apm-spec-guardian/assets/synthesizer-return-schema.json"
)
ORIGINAL_RETURN_FIXTURE = (
    REPO_ROOT / "tests/fixtures/spec-guardian/pr3150-synthesizer-return-original.json"
)


def _load_canonical_schema() -> dict:
    with CANONICAL_SCHEMA_PATH.open(encoding="utf-8") as handle:
        return json.load(handle)


def _load_original_return() -> dict:
    with ORIGINAL_RETURN_FIXTURE.open(encoding="utf-8") as handle:
        return json.load(handle)


def _reconstruct_pre_fix_schema(fixed_schema: dict) -> dict:
    """Build a disposable in-memory copy reproducing the ORIGINAL allOf bug.

    Never touches the committed schema file. Used only to prove the
    before/after behavior of the fix and that the negative guards remain
    load-bearing. Starts from a deep copy of the fixed schema and replaces
    ``defer_v0_2.items`` with the original closed-allOf composition plus a
    bare ``reserved_slot_anchor`` requirement, mirroring exactly what was
    on disk before this fix.
    """
    broken = copy.deepcopy(fixed_schema)
    broken["properties"]["defer_v0_2"]["items"] = {
        "allOf": [
            {"$ref": "#/definitions/fold_item"},
            {
                "type": "object",
                "required": ["reserved_slot_anchor"],
                "properties": {
                    "reserved_slot_anchor": {
                        "type": "string",
                        "description": (
                            "Section / anchor in the current artifact that "
                            "already reserves space for this work."
                        ),
                    }
                },
            },
        ]
    }
    return broken


def _minimal_valid_report(defer_v0_2_entry: dict | None = None) -> dict:
    """A synthetic, minimally-valid full report for isolated-field tests."""
    fold_item = {
        "id": "F1",
        "theme": "standalone",
        "spec_section": "req-tg-000",
        "patch_instruction": "example instruction",
        "success_criterion": "example criterion",
    }
    report = {
        "round": 1,
        "shocked_meter_avg": 8.0,
        "convergence_table": [
            {
                "panel": panel,
                "verdict": "ship_with_followups",
                "shocked_meter": 8,
                "new_blockers": 0,
                "new_recommended": 0,
                "new_nits": 0,
            }
            for panel in (
                "spec-swagger-editor",
                "spec-oci-editor",
                "spec-pkgmgr-editor",
                "spec-tag-architect",
            )
        ],
        "convergent_themes": [],
        "fold_now": [fold_item],
        "defer_v0_1_1": [fold_item],
        "defer_v0_2": [defer_v0_2_entry] if defer_v0_2_entry is not None else [],
        "reject": [],
        "ship_decision": "fold_and_ship",
        "ship_prose": "example ship prose",
        "linter_handoff_notes": "",
    }
    return report


def _valid_defer_v0_2_entry() -> dict:
    return {
        "id": "F9",
        "theme": "standalone",
        "spec_section": "sec.4.8",
        "patch_instruction": "example instruction",
        "success_criterion": "example criterion",
        "reserved_slot_anchor": "sec.4.8 workspaces reserved",
    }


def test_schema_document_is_valid_draft7() -> None:
    schema = _load_canonical_schema()
    assert schema["$schema"] == "http://json-schema.org/draft-07/schema#"
    jsonschema.Draft7Validator.check_schema(schema)


def test_valid_full_report_and_original_return_fail_broken_schema_pass_fixed() -> None:
    fixed_schema = _load_canonical_schema()
    broken_schema = _reconstruct_pre_fix_schema(fixed_schema)

    synthetic_report = _minimal_valid_report(_valid_defer_v0_2_entry())
    original_return = _load_original_return()

    for instance in (synthetic_report, original_return):
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(instance, broken_schema)
        jsonschema.validate(instance, fixed_schema)


def test_defer_v0_2_missing_reserved_slot_anchor_rejected() -> None:
    fixed_schema = _load_canonical_schema()
    entry = _valid_defer_v0_2_entry()
    del entry["reserved_slot_anchor"]
    report = _minimal_valid_report(entry)

    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(report, fixed_schema)

    # Load-bearing proof: a disposable in-memory copy with the `required`
    # constraint dropped from fold_item_with_reserved_slot would WRONGLY
    # accept this same instance -- confirming the real schema's `required`
    # clause is what makes the rejection above happen, not an unrelated
    # failure.
    weakened = copy.deepcopy(fixed_schema)
    weakened["definitions"]["fold_item_with_reserved_slot"]["required"] = [
        "id",
        "theme",
        "spec_section",
        "patch_instruction",
        "success_criterion",
    ]
    jsonschema.validate(report, weakened)


def test_defer_v0_2_invalid_reserved_slot_anchor_type_rejected() -> None:
    fixed_schema = _load_canonical_schema()
    entry = _valid_defer_v0_2_entry()
    entry["reserved_slot_anchor"] = 42
    report = _minimal_valid_report(entry)

    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(report, fixed_schema)


@pytest.mark.parametrize("section", ["fold_now", "defer_v0_1_1", "defer_v0_2"])
def test_unknown_property_rejected_in_each_section(section: str) -> None:
    fixed_schema = _load_canonical_schema()
    entry = (
        _valid_defer_v0_2_entry()
        if section == "defer_v0_2"
        else {
            "id": "F1",
            "theme": "standalone",
            "spec_section": "req-tg-000",
            "patch_instruction": "example instruction",
            "success_criterion": "example criterion",
        }
    )
    entry["unexpected_key"] = "should not be allowed"
    report = _minimal_valid_report()
    report[section] = [entry]

    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(report, fixed_schema)

    if section == "defer_v0_2":
        # Load-bearing proof for the closure guard specifically: flipping
        # additionalProperties to true on the disposable in-memory copy
        # would wrongly accept the same instance.
        weakened = copy.deepcopy(fixed_schema)
        weakened["definitions"]["fold_item_with_reserved_slot"]["additionalProperties"] = True
        jsonschema.validate(report, weakened)


def test_unknown_property_rejected_at_top_level() -> None:
    fixed_schema = _load_canonical_schema()
    report = _minimal_valid_report()
    report["unexpected_top_level_key"] = "nope"

    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(report, fixed_schema)


def test_malformed_id_pattern_and_shocked_meter_type_rejected() -> None:
    fixed_schema = _load_canonical_schema()

    bad_id_entry = _valid_defer_v0_2_entry()
    bad_id_entry["id"] = "not-an-f-id"
    bad_id_report = _minimal_valid_report(bad_id_entry)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad_id_report, fixed_schema)

    bad_meter_report = _minimal_valid_report(_valid_defer_v0_2_entry())
    bad_meter_report["shocked_meter_avg"] = "eight"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad_meter_report, fixed_schema)


def test_fold_now_and_defer_v0_1_1_unaffected_by_fix() -> None:
    """Bare fold_item entries (no reserved_slot_anchor) still validate."""
    fixed_schema = _load_canonical_schema()
    report = _minimal_valid_report()
    jsonschema.validate(report, fixed_schema)


def test_shared_fold_item_properties_structurally_identical() -> None:
    """fold_item and fold_item_with_reserved_slot must not silently drift.

    The five base properties are intentionally duplicated (not $ref-shared)
    to keep fold_item_with_reserved_slot a single closed object expressible
    in Draft 7. This test is the explicit parity guard the duplication
    requires.
    """
    schema = _load_canonical_schema()
    base = schema["definitions"]["fold_item"]
    extended = schema["definitions"]["fold_item_with_reserved_slot"]

    shared_keys = ["id", "theme", "spec_section", "patch_instruction", "success_criterion"]
    assert set(base["required"]) == set(shared_keys)
    assert set(shared_keys).issubset(set(extended["required"]))

    for key in shared_keys:
        assert base["properties"][key] == extended["properties"][key], (
            f"fold_item.properties.{key} drifted from fold_item_with_reserved_slot.properties.{key}"
        )


def test_canonical_and_generated_copy_are_byte_identical() -> None:
    """The .agents/ mirror must match the canonical .apm/ source exactly.

    This fails until `apm install` has regenerated the mirror. It is a
    deliberate, reported gate -- not something this test silently skips.
    """
    if not GENERATED_MIRROR_PATH.exists():
        pytest.fail(
            f"generated mirror missing at {GENERATED_MIRROR_PATH}; "
            "run `apm install` to regenerate it before this test can pass"
        )
    canonical_bytes = CANONICAL_SCHEMA_PATH.read_bytes()
    mirror_bytes = GENERATED_MIRROR_PATH.read_bytes()
    assert canonical_bytes == mirror_bytes, (
        "canonical schema and generated .agents/ mirror have diverged; "
        "run `apm install` to regenerate the mirror"
    )
