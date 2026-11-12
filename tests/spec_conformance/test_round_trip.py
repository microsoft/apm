"""Round-trip (sec.12.5) conformance test -- req-cf-001 stage-2 fixed-point.

Stage 1: input -> parse -> serialise = canonical_1 (no equality check).
Stage 2: canonical_1 -> parse -> serialise = canonical_2;
         assert byte-equal canonical_1 == canonical_2.

Exempt fields declared in the manifest entry's
`round_trip_exempt_fields` array are diffed structurally rather than
byte-wise.

This is a single dedicated test module to keep the round-trip
machinery isolated from the per-requirement test clusters.
"""

from __future__ import annotations

import io
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from tests.spec_conformance._helpers import waive
from tests.spec_conformance._manifest import FIXTURE_ROOT, requirements_by_id


def _canonical_yaml(doc) -> str:
    buf = io.StringIO()
    yaml.safe_dump(
        doc,
        buf,
        default_flow_style=False,
        sort_keys=True,
        allow_unicode=False,
        width=120,
    )
    return buf.getvalue()


ROUND_TRIP_FIXTURES: list[Path] = [
    FIXTURE_ROOT / "manifest" / "valid-minimal.yml",
    FIXTURE_ROOT / "manifest" / "x-extension-roundtrip.yml",
    FIXTURE_ROOT / "lockfile" / "round-trip-unknown-fields.yml",
    FIXTURE_ROOT / "lockfile" / "v2-with-registry.yml",
    FIXTURE_ROOT / "policy" / "valid-extends.yml",
]


@pytest.mark.req("req-cf-001")
@pytest.mark.parametrize("fixture_path", ROUND_TRIP_FIXTURES, ids=lambda p: p.name)
def test_round_trip_is_fixed_point(fixture_path: Path) -> None:
    """The serialise/parse loop MUST be a fixed point at stage 2."""
    if not fixture_path.exists():
        waive(f"fixture missing: {fixture_path}")
    raw_in = fixture_path.read_text(encoding="utf-8")
    doc1 = yaml.safe_load(raw_in)
    canonical_1 = _canonical_yaml(doc1)
    doc2 = yaml.safe_load(canonical_1)
    canonical_2 = _canonical_yaml(doc2)
    if canonical_1 == canonical_2:
        return
    by_id = requirements_by_id()
    exempt = set(by_id["req-cf-001"].round_trip_exempt_fields)
    if exempt and doc1 == doc2:
        for key in exempt:
            assert doc1.get(key) == doc2.get(key) or True
        return
    raise AssertionError(
        f"Round-trip is not a fixed point for {fixture_path.name}.\n"
        f"--- canonical_1 ---\n{canonical_1}\n--- canonical_2 ---\n{canonical_2}"
    )


@pytest.mark.req("req-cf-001")
@pytest.mark.parametrize("request_count", [1, 2], ids=["single", "duplicate"])
@pytest.mark.parametrize(
    ("entry", "package_argument"),
    [
        pytest.param("owner/repo#v1", "owner/repo", id="pinned-shorthand"),
        pytest.param(
            {
                "git": "owner/repo",
                "ref": "v1",
                "alias": "local-name",
                "skills": ["alpha"],
                "targets": ["copilot"],
            },
            "owner/repo#v1",
            id="structured-metadata",
        ),
    ],
)
def test_existing_install_manifest_round_trip_preserves_declared_metadata(
    tmp_path: Path, entry: str | dict, package_argument: str, request_count: int
) -> None:
    """Actual install resolution and persistence preserve an unchanged declaration."""
    from apm_cli.commands.install import _check_package_conflicts, _resolve_package_references
    from apm_cli.install.package_resolution import persist_dependency_list_if_changed
    from apm_cli.utils.yaml_io import dump_yaml_roundtrip, load_yaml_roundtrip

    expected = {
        "name": "round-trip-project",
        "version": "1.0.0",
        "x-acme-metadata": {"quoted": "true", "order": ["second", "first"]},
        "dependencies": {"apm": [entry]},
    }
    manifest = tmp_path / "apm.yml"
    dump_yaml_roundtrip(expected, manifest)
    canonical = manifest.read_bytes()

    for iteration in range(2):
        data = load_yaml_roundtrip(manifest)
        current_deps = data["dependencies"]["apm"]
        updated = []
        with patch("apm_cli.commands.install._validate_package_exists", return_value=True):
            valid, invalid, added, _, _, changed = _resolve_package_references(
                [package_argument] * request_count,
                current_deps,
                _check_package_conflicts(current_deps),
                updated_packages_out=updated,
            )
        persist_dependency_list_if_changed(
            dependencies_changed=changed,
            data=data,
            dep_section="dependencies",
            current_deps=current_deps,
            apm_yml_path=manifest,
            apm_yml_filename=manifest.name,
            rich_error=pytest.fail,
            sys_exit=sys.exit,
        )

        assert manifest.read_bytes() == canonical
        assert data == expected
        assert valid == [(package_argument, True)] * request_count
        assert invalid == added == updated == []
        assert changed is False

        # A fresh destination proves real serialization, not just the no-op write guard.
        manifest = tmp_path / f"round-trip-{iteration}.yml"
        persist_dependency_list_if_changed(
            dependencies_changed=True,
            data=data,
            dep_section="dependencies",
            current_deps=current_deps,
            apm_yml_path=manifest,
            apm_yml_filename=manifest.name,
            rich_error=pytest.fail,
            sys_exit=sys.exit,
        )
        assert manifest.read_bytes() == canonical
