"""RED-TEAM round-2: non-UTF8 / wrong-encoding manifests crash the parser.

Round 1 hardened type-confusion and non-dict tops. This probe attacks the
ENCODING layer, which round 1 did not. Two read sites decode bytes as
UTF-8 but only guard a SUBSET of the exceptions that decode can raise:

* ``commands.lifecycle._validate_script_file`` does
  ``path.read_text(encoding="utf-8")`` guarded ONLY by ``except OSError``.
  ``UnicodeDecodeError`` is a ``ValueError``, NOT an ``OSError`` -- it
  escapes and crashes ``apm lifecycle validate`` with an unhandled
  traceback on any non-UTF8 apm.yml or policy JSON.

* ``core.lifecycle_scripts.parse_script_file`` opens the policy JSON with
  ``encoding="utf-8"`` and calls ``json.load`` guarded by
  ``except (OSError, json.JSONDecodeError)``. The decode happens inside
  ``json.load``; a ``UnicodeDecodeError`` is in neither class and escapes
  -- crashing DISCOVERY (and therefore ``apm lifecycle`` list / test and
  the install-time firing path) on any non-UTF8 file under policy.d.

Expected SAFE behaviour: a wrong-encoding manifest is a malformed input,
so it must degrade to a structured error (validate) or an empty list
(discovery), never an unhandled traceback. These tests assert the SAFE
contract and therefore FAIL against the current code -- proving the break.
"""

from __future__ import annotations

import pytest

from .conftest import write_apm_yml_bytes, write_bytes

# A latin-1 byte sequence that is not valid UTF-8 (0xff 0xfe are invalid
# UTF-8 start bytes). Embedded inside an otherwise valid apm.yml/JSON so
# the ONLY defect is the encoding.
_LATIN1_TAIL = b"\xff\xfe non-utf8 byte"

_APM_YML_BAD = (
    b'lifecycle:\n  post-install:\n    - {type: command, command: "echo ' + _LATIN1_TAIL + b'"}\n'
)

_POLICY_JSON_BAD = (
    b'{"version":1,"scripts":{"post-install":'
    b'[{"type":"command","command":"echo ' + _LATIN1_TAIL + b'"}]}}'
)

# UTF-16-LE with BOM -- a common "I saved it in Notepad" failure mode.
_UTF16_YML = "lifecycle:\n  post-install: []\n".encode("utf-16")


def test_validate_non_utf8_project_yml_returns_structured_error(tmp_path):
    """_validate_script_file must report a structured error, not raise."""
    from apm_cli.commands.lifecycle import _validate_script_file

    path = write_apm_yml_bytes(tmp_path, _APM_YML_BAD)

    try:
        errors = _validate_script_file(path, "project")
    except UnicodeDecodeError as exc:  # pragma: no cover - documents the break
        pytest.fail(
            "BREAK: _validate_script_file leaked UnicodeDecodeError on a "
            f"non-UTF8 apm.yml instead of returning a structured error: {exc}"
        )

    assert isinstance(errors, list)
    assert errors, "a non-UTF8 manifest must produce at least one error message"


def test_validate_non_utf8_policy_json_returns_structured_error(tmp_path):
    """The JSON (policy) branch of validate must also degrade gracefully."""
    from apm_cli.commands.lifecycle import _validate_script_file

    path = tmp_path / "00-bad.json"
    write_bytes(path, _POLICY_JSON_BAD)

    try:
        errors = _validate_script_file(path, "policy")
    except UnicodeDecodeError as exc:  # pragma: no cover - documents the break
        pytest.fail(
            "BREAK: _validate_script_file leaked UnicodeDecodeError on a "
            f"non-UTF8 policy JSON instead of a structured error: {exc}"
        )

    assert isinstance(errors, list)
    assert errors


def test_validate_utf16_bom_project_yml_returns_structured_error(tmp_path):
    """A UTF-16-BOM apm.yml is not valid UTF-8 and must not crash validate."""
    from apm_cli.commands.lifecycle import _validate_script_file

    path = write_apm_yml_bytes(tmp_path, _UTF16_YML)

    try:
        errors = _validate_script_file(path, "project")
    except UnicodeDecodeError as exc:  # pragma: no cover - documents the break
        pytest.fail(f"BREAK: validate leaked UnicodeDecodeError on UTF-16 apm.yml: {exc}")

    assert isinstance(errors, list)


def test_discovery_parse_script_file_non_utf8_returns_empty(tmp_path):
    """parse_script_file must swallow a non-UTF8 JSON and return []."""
    from apm_cli.core.lifecycle_scripts import parse_script_file

    path = tmp_path / "00-bad.json"
    write_bytes(path, _POLICY_JSON_BAD)

    try:
        entries = parse_script_file(path, "policy")
    except UnicodeDecodeError as exc:  # pragma: no cover - documents the break
        pytest.fail(
            "BREAK: parse_script_file leaked UnicodeDecodeError on a non-UTF8 "
            f"policy JSON -- discovery / install firing crashes: {exc}"
        )

    assert entries == []


def test_discovery_dir_with_non_utf8_json_does_not_crash(tmp_path, policy_dir, isolated_home):
    """discover_scripts over a policy.d holding one non-UTF8 file must not crash.

    This is the end-to-end install-firing surface: a single bad admin
    drop-in must not take down every lifecycle operation.
    """
    from apm_cli.core.lifecycle_scripts import discover_scripts

    write_bytes(policy_dir / "00-bad.json", _POLICY_JSON_BAD)

    try:
        scripts = discover_scripts(project_root=str(tmp_path / "noproj"))
    except UnicodeDecodeError as exc:  # pragma: no cover - documents the break
        pytest.fail(f"BREAK: discover_scripts crashed on a non-UTF8 policy.d file: {exc}")

    assert isinstance(scripts, list)
