"""Contract tests for marketplace and view JSON envelopes."""

import json

import pytest

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "data",
    [
        [],
        [{"name": "example", "description": None}],
        {"name": "example", "tags": []},
    ],
)
def test_success_envelope_preserves_json_data(data: object) -> None:
    from apm_cli.core.json_envelope import success_envelope

    document = json.loads(json.dumps(success_envelope(data)))

    assert document == {"schema_version": 1, "data": data}


def test_error_envelope_contains_only_version_and_code() -> None:
    from apm_cli.core.json_envelope import error_envelope

    document = json.loads(json.dumps(error_envelope("not_found")))

    assert document == {"schema_version": 1, "error": "not_found"}
