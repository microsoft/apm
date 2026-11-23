"""The hook bundle owner recognizes only the module metadata it deployed (#4921)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from apm_cli.integration.hook_bundle import is_deployed_hook_module_metadata
from apm_cli.utils.content_hash import compute_file_hash

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-LAW-outcome.status_matches_state")]

_COMMONJS = json.dumps({"type": "commonjs"}, indent=2, sort_keys=True) + "\n"
_MODULE = json.dumps({"type": "module"}, indent=2, sort_keys=True) + "\n"


@pytest.mark.parametrize(
    ("name", "deployed", "current", "target", "recorded", "expected"),
    [
        ("package.json", _COMMONJS, _COMMONJS, "claude", True, True),
        ("package.json", '{ "type": "module" }\n', None, "claude", True, True),
        ("package.json", _COMMONJS, _COMMONJS, "claude", False, False),
        ("package.json", _COMMONJS, _MODULE, "claude", True, False),
        ("package.json", _COMMONJS, _COMMONJS, "copilot", True, False),
        ("extra.json", _COMMONJS, _COMMONJS, "claude", True, False),
        ("package.json", '{"name": "x", "type": "module"}\n', None, "claude", True, False),
    ],
    ids=[
        "sidecar",
        "nested-metadata",
        "unrecorded",
        "type-only-edit",
        "json-excluding-target",
        "other-filename",
        "extra-keys",
    ],
)
def test_only_recorded_unedited_module_metadata_on_eligible_targets_is_recognized(
    tmp_path: Path,
    name: str,
    deployed: str,
    current: str | None,
    target: str,
    recorded: bool,
    expected: bool,
) -> None:
    path = tmp_path / name
    path.write_text(deployed, encoding="utf-8")
    recorded_hash = compute_file_hash(path) if recorded else None
    if current is not None:
        path.write_text(current, encoding="utf-8")

    document = json.loads(path.read_text(encoding="utf-8"))

    assert (
        is_deployed_hook_module_metadata(
            path, document, target_name=target, recorded_hash=recorded_hash
        )
        is expected
    )
