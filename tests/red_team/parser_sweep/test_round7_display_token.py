"""Round-7 parser trap: the DISPLAY path must not crash on a hostile manifest.

r7-parser-1 (MED) -- round-6 wired ``_safe_token`` into ``apm lifecycle
validate`` only. The SAME oversized-int / non-string ``url`` / ``command``
crash was still live in two render paths:

* ``apm lifecycle`` (list) -- ``table.add_row(..., target, ...)`` where
  ``target = entry.url`` is a non-string ``int``/``bool`` -> Rich
  ``NotRenderableError``; and an oversized YAML hex/octal int forces
  ``str(int)`` past the 4300-digit limit -> ``ValueError``.
* ``apm lifecycle test <event>`` (dry-run) -- the same interpolation.

discover_scripts stores ``url``/``command`` unvalidated, so a typo or hostile
project/user/policy manifest produced an uncaught traceback on a read-only
command. The fix routes every rendered ``target`` through ``_safe_token`` (which
never forces a decimal ``str(int)`` on a hostile value and always yields a str).

Run:
    APM_E2E_TESTS=1 uv run --extra dev pytest \
      tests/red_team/parser_sweep/test_round7_display_token.py -q
"""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from apm_cli.commands.lifecycle import lifecycle

# >4300 decimal digits once converted; the literal parses fine in YAML.
_HEX_BOMB = "0x" + "f" * 5000

_HOSTILE_MANIFESTS = [
    # oversized hex int as url -> str(int) ValueError if unguarded
    f"name: x\nversion: 1\nlifecycle:\n  post-install:\n    - type: http\n      url: {_HEX_BOMB}\n",
    # small non-string int as command -> Rich NotRenderableError if unguarded
    "name: x\nversion: 1\nlifecycle:\n  post-install:\n    - type: command\n      command: 123\n",
    # bool as url -> Rich NotRenderableError if unguarded
    "name: x\nversion: 1\nlifecycle:\n  post-install:\n    - type: http\n      url: true\n",
]


@pytest.fixture()
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("APM_E2E_TESTS", "1")
    monkeypatch.setenv("APM_HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.mark.parametrize("manifest", _HOSTILE_MANIFESTS)
def test_list_command_no_crash_on_hostile_target(_isolated, manifest):
    """`apm lifecycle` (list) must render, never raise, on a hostile target."""
    (_isolated / "apm.yml").write_text(manifest, encoding="utf-8")
    result = CliRunner().invoke(lifecycle, [])
    assert result.exception is None or isinstance(result.exception, SystemExit), (
        f"BREAK: list raised {result.exception!r}\n{result.output}"
    )
    assert result.exit_code == 0, result.output


@pytest.mark.parametrize("manifest", _HOSTILE_MANIFESTS)
def test_dryrun_command_no_crash_on_hostile_target(_isolated, manifest):
    """`apm lifecycle test` dry-run must render, never raise, on a hostile target."""
    (_isolated / "apm.yml").write_text(manifest, encoding="utf-8")
    result = CliRunner().invoke(lifecycle, ["test", "post-install"])
    assert result.exception is None or isinstance(result.exception, SystemExit), (
        f"BREAK: dry-run raised {result.exception!r}\n{result.output}"
    )
    assert result.exit_code == 0, result.output


def test_oversized_int_renders_as_degraded_token(_isolated):
    """The oversized int degrades to a type token, not a decimal blowup."""
    (_isolated / "apm.yml").write_text(_HOSTILE_MANIFESTS[0], encoding="utf-8")
    result = CliRunner().invoke(lifecycle, [])
    assert result.exit_code == 0, result.output
    assert "<int>" in result.output, result.output
