"""RED-TEAM: `apm lifecycle validate` against malformed manifests.

``validate`` is the user's safety net -- it must REPORT problems, never
crash on them. The robust cases below confirm it reports errors and exits
1 (or 0 when clean) without leaking an exception. The final group is a
GENUINE BREAK (D4-2): a non-string ``url`` reaches an unguarded
``urlparse(url)`` inside ``_validate_script_file`` and raises an uncaught
``AttributeError`` ('int'/'list' object has no attribute 'decode'),
tracebacking the command instead of reporting a validation error. Those
tests assert the robust expectation and therefore FAIL on head.
"""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from .conftest import write_apm_yml


def _run_validate(tmp_path, monkeypatch, content):
    from apm_cli.commands.lifecycle import lifecycle

    monkeypatch.chdir(tmp_path)
    write_apm_yml(tmp_path, content)
    return CliRunner().invoke(lifecycle, ["validate"], catch_exceptions=True)


def _crashed(result) -> bool:
    """True if validate raised an *unexpected* exception.

    A clean ``sys.exit(1)`` surfaces through CliRunner as ``SystemExit``,
    which is the command's intended error-exit path -- not a crash. Any
    other exception type (e.g. AttributeError from an unguarded urlparse)
    is a genuine traceback.
    """
    exc = result.exception
    return exc is not None and not isinstance(exc, SystemExit)


# -- Robust cases: errors must be reported gracefully (exit 1, no traceback).


@pytest.mark.parametrize(
    "content",
    [
        # entry not a mapping
        "lifecycle:\n  post-install:\n    - just-a-string\n",
        # unknown event name
        "lifecycle:\n  not-an-event:\n    - type: command\n      bash: echo hi\n",
        # command entry missing all of bash/command/run
        "lifecycle:\n  post-install:\n    - type: command\n",
        # http entry missing url
        "lifecycle:\n  post-install:\n    - type: http\n",
        # lifecycle not a mapping
        "lifecycle: 42\n",
        # top-level not a mapping (validate handles this, unlike discovery)
        "- a\n- b\n",
        # invalid YAML
        "lifecycle: {unterminated\n",
    ],
    ids=[
        "entry-not-mapping",
        "unknown-event",
        "command-missing-fields",
        "http-missing-url",
        "lifecycle-scalar",
        "toplevel-list",
        "invalid-yaml",
    ],
)
def test_validate_reports_errors_gracefully(
    tmp_path, monkeypatch, policy_dir, isolated_home, content
):
    result = _run_validate(tmp_path, monkeypatch, content)
    assert not _crashed(result), f"validate crashed instead of reporting: {result.exception!r}"
    assert result.exit_code == 1


def test_validate_passes_on_clean_manifest(tmp_path, monkeypatch, policy_dir, isolated_home):
    content = (
        "lifecycle:\n"
        "  post-install:\n"
        "    - type: command\n"
        "      bash: echo hi\n"
        "    - type: http\n"
        "      url: https://example.test/hook\n"
    )
    result = _run_validate(tmp_path, monkeypatch, content)
    assert not _crashed(result)
    assert result.exit_code == 0


def test_validate_flags_http_scheme_and_credentials(
    tmp_path, monkeypatch, policy_dir, isolated_home
):
    content = (
        "lifecycle:\n"
        "  post-install:\n"
        "    - type: http\n"
        "      url: http://user:pass@example.test/hook\n"
    )
    result = _run_validate(tmp_path, monkeypatch, content)
    assert not _crashed(result)
    assert result.exit_code == 1
    # Wording assertions only -- avoid asserting on raw URL substrings.
    assert "https" in result.output
    assert "credentials" in result.output


# -- GENUINE BREAK (D4-2): non-string url crashes validate via urlparse().


@pytest.mark.parametrize(
    "url_literal",
    ["5", "3.14", "true", "[a, b]", "{k: v}"],
    ids=["int", "float", "bool", "list", "dict"],
)
def test_validate_must_not_crash_on_non_string_url(
    tmp_path, monkeypatch, policy_dir, isolated_home, url_literal
):
    content = f"lifecycle:\n  post-install:\n    - type: http\n      url: {url_literal}\n"
    result = _run_validate(tmp_path, monkeypatch, content)
    assert not _crashed(result), (
        f"apm lifecycle validate crashed on a non-string url ({url_literal}): {result.exception!r}"
    )
    assert result.exit_code in (0, 1)
