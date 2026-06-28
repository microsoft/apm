"""Round-10 parser break r10-parser-1: trust-store bare-ValueError crash.

``_load_trust_store`` was the lone JSON reader that caught only
``(OSError, json.JSONDecodeError)``. A JSON integer literal with more than
4300 digits trips the CPython ``int_max_str_digits`` limit, which raises a
*bare* ``ValueError`` ("Exceeds the limit (4300 digits) ...") -- NOT a
``json.JSONDecodeError`` -- so it escaped the handler and crashed every
trust-gated entrypoint (``apm lifecycle test`` dry-run trust label,
``apm lifecycle trust`` / ``untrust``, and the ``is_fingerprint_trusted``
fire gate on ``apm install``) when the user-tier
``$APM_HOME/scripts-trust.json`` was corrupted or externally planted.

Every sibling loader (``parse_script_file``, ``_validate_script_file``)
already fails closed on this exact case. The fix widens the handler to
``(OSError, ValueError, RecursionError)`` so a malformed/oversized/non-UTF-8
trust store degrades to ``{}`` (nothing trusted -- the SECURE direction)
instead of crashing.

These traps drive the real ``_load_trust_store`` and the user-facing
``apm lifecycle trust`` / ``untrust`` / ``test`` subcommands against a
hostile store and assert no uncaught exception plus a fail-closed result.
"""

from __future__ import annotations

from click.testing import CliRunner

from apm_cli.commands.lifecycle import (
    lifecycle_test,
    lifecycle_trust,
    lifecycle_untrust,
)
from apm_cli.core import script_trust as st

_OVERSIZED_INT_STORE = '{"projects":{"/p/apm.yml":' + "1" * 5000 + "}}"

_VALID_APM_YML = """\
name: demo
version: 1.0.0
lifecycle:
  post-install:
    - command: echo hi
"""


def _write_store(monkeypatch, tmp_path, payload):
    """Point APM_HOME at tmp_path and plant a hostile trust store."""
    monkeypatch.setenv("APM_HOME", str(tmp_path))
    store = st._trust_store_path()
    store.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(payload, bytes):
        store.write_bytes(payload)
    else:
        store.write_text(payload, encoding="utf-8")
    return store


def test_load_trust_store_oversized_int_fails_closed(monkeypatch, tmp_path):
    """A >4300-digit int value must not crash; load returns {}."""
    _write_store(monkeypatch, tmp_path, _OVERSIZED_INT_STORE)
    assert st._load_trust_store() == {}


def test_load_trust_store_non_utf8_fails_closed(monkeypatch, tmp_path):
    """Non-UTF-8 bytes raise a (bare) UnicodeDecodeError -> caught -> {}."""
    _write_store(monkeypatch, tmp_path, b"\xff\xfe not valid utf8")
    assert st._load_trust_store() == {}


def test_load_trust_store_valid_still_loads(monkeypatch, tmp_path):
    """The widened handler must not swallow a well-formed store."""
    _write_store(
        monkeypatch,
        tmp_path,
        '{"projects":{"/p/apm.yml":"deadbeef"}}',
    )
    assert st._load_trust_store() == {"/p/apm.yml": "deadbeef"}


def test_is_fingerprint_trusted_survives_oversized_store(monkeypatch, tmp_path):
    """The install fire gate must fail closed (untrusted), never crash."""
    _write_store(monkeypatch, tmp_path, _OVERSIZED_INT_STORE)
    project = tmp_path / "proj"
    project.mkdir()
    apm_yml = project / "apm.yml"
    apm_yml.write_text(_VALID_APM_YML, encoding="utf-8")
    # Must not raise; a corrupted store means nothing is trusted.
    assert st.is_fingerprint_trusted(apm_yml, "a" * 64) is False


def test_lifecycle_trust_subcommand_survives_oversized_store(monkeypatch, tmp_path):
    """`apm lifecycle trust` must not crash on a hostile trust store."""
    _write_store(monkeypatch, tmp_path, _OVERSIZED_INT_STORE)
    project = tmp_path / "proj"
    project.mkdir()
    (project / "apm.yml").write_text(_VALID_APM_YML, encoding="utf-8")
    monkeypatch.chdir(project)

    result = CliRunner().invoke(lifecycle_trust, [])
    assert not isinstance(result.exception, ValueError), result.exception


def test_lifecycle_untrust_subcommand_survives_oversized_store(monkeypatch, tmp_path):
    """`apm lifecycle untrust` must not crash on a hostile trust store."""
    _write_store(monkeypatch, tmp_path, _OVERSIZED_INT_STORE)
    project = tmp_path / "proj"
    project.mkdir()
    (project / "apm.yml").write_text(_VALID_APM_YML, encoding="utf-8")
    monkeypatch.chdir(project)

    result = CliRunner().invoke(lifecycle_untrust, [])
    assert not isinstance(result.exception, ValueError), result.exception


def test_lifecycle_test_subcommand_survives_oversized_store(monkeypatch, tmp_path):
    """`apm lifecycle test` (dry-run trust label) must not crash."""
    _write_store(monkeypatch, tmp_path, _OVERSIZED_INT_STORE)
    project = tmp_path / "proj"
    project.mkdir()
    (project / "apm.yml").write_text(_VALID_APM_YML, encoding="utf-8")
    monkeypatch.chdir(project)

    result = CliRunner().invoke(lifecycle_test, ["post-install"])
    assert not isinstance(result.exception, ValueError), result.exception
