"""Unit tests for per-dependency target filtering (filesystem, process and CLI boundary cases)."""

from __future__ import annotations

from pathlib import Path

import pytest

from apm_cli.core.errors import ConflictingTargetsError, EmptyTargetsListError
from apm_cli.models.apm_package import APMPackage
from apm_cli.utils.yaml_io import dump_yaml

pytestmark = pytest.mark.component


@pytest.mark.parametrize(
    ("target_fields", "expected"),
    (
        ({"target": "claude"}, ("claude",)),
        ({"target": "claude,copilot"}, ("claude", "copilot")),
        ({"target": ["claude", "cursor"]}, ("claude", "cursor")),
        ({"target": "vscode"}, ("copilot",)),
        ({"target": "agents"}, ("copilot",)),
        ({"target": None}, ()),
        ({"targets": "claude"}, ("claude",)),
        ({"targets": ["claude", "cursor"]}, ("claude", "cursor")),
        ({"target": "all"}, ("all",)),
        ({"targets": ["all"]}, ()),
        ({"targets": ["all", "claude"]}, ()),
        ({}, ()),
    ),
    ids=(
        "scalar",
        "csv-scalar",
        "list-under-scalar",
        "scalar-alias",
        "deprecated-scalar-alias",
        "null-scalar-is-omitted",
        "scalar-under-plural",
        "canonical-list",
        "scalar-legacy-all",
        "legacy-all",
        "plural-legacy-all-mixed",
        "omitted",
    ),
)
def test_manifest_package_target_spellings_are_deterministic(
    tmp_path: Path,
    target_fields: dict[str, object],
    expected: tuple[str, ...],
) -> None:
    """Author-facing spellings produce one canonical restriction tuple."""
    manifest = {"name": "targeted-hooks", "version": "1.0.0", **target_fields}
    manifest_path = tmp_path / "apm.yml"
    dump_yaml(manifest, manifest_path)

    assert APMPackage.from_apm_yml(manifest_path).canonical_targets == expected


@pytest.mark.parametrize(
    ("target_fields", "error"),
    (
        ({"target": "unknown-editor"}, ValueError),
        ({"target": ""}, ValueError),
        ({"target": []}, ValueError),
        ({"target": [""]}, ValueError),
        ({"targets": []}, EmptyTargetsListError),
        ({"targets": ""}, EmptyTargetsListError),
        ({"targets": None}, EmptyTargetsListError),
        ({"targets": [""]}, EmptyTargetsListError),
        ({"target": ["all", "claude"]}, ValueError),
        ({"target": None, "targets": ["cursor"]}, ConflictingTargetsError),
        ({"target": "claude", "targets": None}, ConflictingTargetsError),
    ),
    ids=(
        "unknown",
        "empty-scalar",
        "empty-list-under-scalar",
        "blank-item-under-scalar",
        "empty-plural",
        "empty-scalar-under-plural",
        "null-plural",
        "blank-item-under-plural",
        "mixed-scalar-all",
        "null-scalar-conflict",
        "null-plural-conflict",
    ),
)
def test_invalid_manifest_package_target_spellings_fail_closed(
    tmp_path: Path,
    target_fields: dict[str, object],
    error: type[Exception],
) -> None:
    """Invalid package declarations never degrade to an unrestricted install."""
    manifest_path = tmp_path / "apm.yml"
    dump_yaml(
        {"name": "targeted-hooks", "version": "1.0.0", **target_fields},
        manifest_path,
    )

    with pytest.raises(error):
        APMPackage.from_apm_yml(manifest_path)


def test_conflicting_manifest_target_keys_fail_parsing(tmp_path: Path) -> None:
    """The model preserves the schema's key-presence mutex."""
    manifest_path = tmp_path / "apm.yml"
    dump_yaml(
        {
            "name": "targeted-hooks",
            "version": "1.0.0",
            "target": "claude",
            "targets": ["cursor"],
        },
        manifest_path,
    )
    with pytest.raises(ConflictingTargetsError):
        APMPackage.from_apm_yml(manifest_path)
