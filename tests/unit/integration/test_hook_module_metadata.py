"""The hook bundle owner recognizes only the module metadata it deployed (#4921)."""

from __future__ import annotations

import json
from pathlib import PurePosixPath

import pytest

from apm_cli.integration.hook_bundle import is_deployed_hook_module_metadata
from apm_cli.utils.content_hash import content_hash_of_bytes

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-LAW-outcome.status_matches_state")]

_COMMONJS = json.dumps({"type": "commonjs"}, indent=2, sort_keys=True) + "\n"
_MODULE = json.dumps({"type": "module"}, indent=2, sort_keys=True) + "\n"
_NESTED = '{ "type": "module" }\n'
_EXTRA_KEYS = '{"name": "x", "type": "module"}\n'


@pytest.mark.parametrize(
    ("name", "deployed", "current", "target", "expected"),
    [
        ("package.json", _COMMONJS, _COMMONJS, "claude", True),
        ("package.json", _NESTED, _NESTED, "claude", True),
        ("package.json", _COMMONJS.replace("\n", "\r\n"), _COMMONJS, "claude", True),
        ("package.json", None, _COMMONJS, "claude", False),
        ("package.json", _COMMONJS, _MODULE, "claude", False),
        ("package.json", _COMMONJS, _COMMONJS, "copilot", False),
        ("extra.json", _COMMONJS, _COMMONJS, "claude", False),
        ("package.json", _EXTRA_KEYS, _EXTRA_KEYS, "claude", False),
        ("package.json", "{", "{", "claude", False),
    ],
    ids=[
        "sidecar",
        "nested-metadata",
        "recorded-with-crlf",
        "unrecorded",
        "type-only-edit",
        "json-excluding-target",
        "other-filename",
        "extra-keys",
        "malformed",
    ],
)
def test_only_recorded_unedited_module_metadata_on_eligible_targets_is_recognized(
    name: str,
    deployed: str | None,
    current: str,
    target: str,
    expected: bool,
) -> None:
    recorded_hash = None if deployed is None else content_hash_of_bytes(deployed.encode("utf-8"))

    assert (
        is_deployed_hook_module_metadata(
            PurePosixPath(".claude/hooks/pkg/scripts") / name,
            current,
            target_name=target,
            recorded_hash=recorded_hash,
        )
        is expected
    )
