"""RED-TEAM round-2: ScriptEntry field type-confusion beyond `url`.

Round 1 type-guarded the http ``url`` field. This probe sweeps the OTHER
fields (``type``, ``bash`` / ``command`` / ``run``, ``timeoutSec``,
``env``, ``headers``, event names) with int / list / dict / str confusion.

Two contracts:

1. ``apm lifecycle validate`` must never raise -- it must either report a
   structured error or (for a gap it does not yet cover) return cleanly.
2. A type-confused entry that slips past validate must NOT produce a
   DANGEROUS runnable entry: firing it must be isolated by ``fire()``
   (no exception escape, no hang). A non-string command under
   ``shell=True`` either no-ops or raises TypeError that the runner eats.

Findings here are validation-completeness GAPS (validate accepts a few
malformed shapes), not security breaks: every such entry is inert or
isolated at fire time, and project entries are trust-gated.
"""

from __future__ import annotations

import pytest

from .conftest import command_entry, fire, run_guarded, write_apm_yml


def _validate(tmp_path, body):
    from apm_cli.commands.lifecycle import _validate_script_file

    path = write_apm_yml(tmp_path, body)
    return _validate_script_file(path, "project")


@pytest.mark.parametrize(
    "body",
    [
        "lifecycle:\n  post-install:\n    - {type: 5, command: echo hi}\n",
        "lifecycle:\n  post-install:\n    - {type: [a], command: echo hi}\n",
        "lifecycle:\n  post-install:\n    - {type: http, url: 5}\n",
        "lifecycle:\n  post-install:\n    - {type: http, url: [a]}\n",
        "lifecycle:\n  post-install:\n    - {type: http, url: true}\n",
        "lifecycle:\n  5:\n    - {type: command, command: echo}\n",
        "lifecycle:\n  post-install:\n    - {type: command, bash: [echo, hi]}\n",
        "lifecycle:\n  post-install:\n    - {type: command, command: {a: 1}}\n",
        "lifecycle:\n  post-install:\n    - {type: command, command: echo, timeoutSec: [1]}\n",
        "lifecycle:\n  post-install:\n    - {type: command, command: echo, env: [a, b]}\n",
        "lifecycle:\n  post-install:\n    - {type: command, command: echo, env: 'a=b'}\n",
        "lifecycle:\n  post-install:\n    - {type: http, url: 'https://x.example', headers: [a]}\n",
        "lifecycle:\n  post-install:\n    - [not, a, mapping]\n",
        "lifecycle:\n  post-install:\n    - null\n",
    ],
)
def test_validate_never_raises_on_type_confusion(tmp_path, body):
    """validate must degrade to a list[str], never an unhandled traceback."""
    try:
        errors = _validate(tmp_path, body)
    except Exception as exc:
        pytest.fail(f"BREAK: validate raised on type-confused entry: {type(exc).__name__}: {exc}")
    assert isinstance(errors, list)


def test_validate_flags_non_string_url_structured(tmp_path):
    """Round-1 guard still holds: a non-string url -> typed structured error."""
    errors = _validate(tmp_path, "lifecycle:\n  post-install:\n    - {type: http, url: 5}\n")
    assert any("must be a string" in e for e in errors)


@pytest.mark.parametrize("env_val", [["a", "b"], "a=b", {"OK": "1"}])
def test_typeconfused_env_isolated_at_fire(tmp_path, fire_event, env_val):
    """env as list/str/dict must be isolated by fire(), not crash the CLI."""
    entry = command_entry(env=env_val)
    finished, _result, exc = run_guarded(lambda: fire(entry, fire_event, tmp_path), timeout=6.0)
    assert finished, f"fire() hung with env={env_val!r}"
    assert exc is None, f"fire() leaked with env={env_val!r}: {exc!r}"


@pytest.mark.parametrize("cmd_val", [["echo", "hi"], {"a": 1}, 5])
def test_typeconfused_command_isolated_at_fire(tmp_path, fire_event, cmd_val):
    """Non-string bash/command must not escape or hang at fire time."""
    entry = command_entry(bash=cmd_val, command=cmd_val)
    finished, _result, exc = run_guarded(lambda: fire(entry, fire_event, tmp_path), timeout=6.0)
    assert finished, f"fire() hung with command={cmd_val!r}"
    assert exc is None, f"fire() leaked with command={cmd_val!r}: {exc!r}"
