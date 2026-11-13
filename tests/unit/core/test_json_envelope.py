"""Contract tests for marketplace and view JSON envelopes."""

import json
import os
from pathlib import Path

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


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("user:PASS'WORD@host:path", "ssh://host:path"),
        ("user:PASS@host:path", "ssh://host:path"),
        ("git@github.com:user/repo.git", "ssh://github.com:user/repo.git"),
        ("https://user:PASS'WORD@github.com/repo", "https://github.com/repo"),
    ],
)
def test_safe_text_handles_special_chars_in_credentials(url: str, expected: str) -> None:
    """Credentials with special chars like apostrophes must be fully redacted."""
    from apm_cli.core.json_envelope import safe_text

    result = safe_text(url)

    assert result == expected


def test_safe_text_redacts_cache_root_prefixes_with_spaces(tmp_path: Path) -> None:
    """Complete cache-root prefixes with spaces must be redacted before tokenizing."""
    old_cache_dir = os.environ.get("APM_CACHE_DIR")
    cache_dir = tmp_path / "apm cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    os.environ["APM_CACHE_DIR"] = str(cache_dir)

    try:
        from apm_cli.core.json_envelope import safe_text

        # Test that cache paths with spaces are redacted, not left visible
        description = f"See {cache_dir}/checkout for details"
        result = safe_text(description)

        # Verify the cache path is redacted and the surrounding text is preserved
        assert "[internal-cache]" in result
        assert "apm cache" not in result
    finally:
        if old_cache_dir is None:
            os.environ.pop("APM_CACHE_DIR", None)
        else:
            os.environ["APM_CACHE_DIR"] = old_cache_dir
