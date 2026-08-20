"""RED-TEAM: malformed shape / type-confusion matrix for the parser.

These assert the ROBUST contract that holds on head: when ``lifecycle:``,
an event value, an entry, or an individual field has the wrong type, the
parser must (a) never raise and (b) drop the malformed unit rather than
emit a half-built entry. The crash surfaces for non-string ``url`` /
non-dict ``top-level`` live in their own modules; everything below is a
regression trap proving the surrounding shapes stay safe.
"""

from __future__ import annotations

import pytest

from .conftest import write_apm_yml


def _parse(tmp_path, content):
    from apm_cli.core.lifecycle_scripts import parse_apm_yml_lifecycle

    doc = write_apm_yml(tmp_path, content)
    return parse_apm_yml_lifecycle(doc, "project")


@pytest.mark.parametrize(
    "content",
    [
        'lifecycle: "not a mapping"\n',
        "lifecycle:\n  - one\n  - two\n",
        "lifecycle: null\n",
        "lifecycle: 42\n",
        "lifecycle: 3.14\n",
        "lifecycle: true\n",
    ],
)
def test_lifecycle_not_a_mapping_yields_empty(tmp_path, content):
    assert _parse(tmp_path, content) == []


@pytest.mark.parametrize(
    "event_value",
    [
        '"a bare string"',
        "42",
        "3.14",
        "true",
        "null",
        "{a: dict, not: a, list: here}",
    ],
)
def test_event_value_not_a_list_is_skipped(tmp_path, event_value):
    content = f"lifecycle:\n  post-install: {event_value}\n"
    assert _parse(tmp_path, content) == []


def test_mixed_event_values_only_list_events_survive(tmp_path):
    content = (
        "lifecycle:\n"
        "  pre-install: 42\n"
        '  post-install: "string"\n'
        "  pre-update:\n"
        "    - type: command\n"
        "      bash: echo ok\n"
    )
    entries = _parse(tmp_path, content)
    assert len(entries) == 1
    assert entries[0].event == "pre-update"


def test_non_dict_entries_are_dropped_not_half_built(tmp_path):
    content = (
        "lifecycle:\n"
        "  post-install:\n"
        '    - "a string entry"\n'
        "    - 123\n"
        "    - null\n"
        "    - 3.14\n"
        "    - [nested, list]\n"
        "    - type: command\n"
        "      bash: echo survivor\n"
    )
    entries = _parse(tmp_path, content)
    assert len(entries) == 1
    assert entries[0].bash == "echo survivor"


def test_unknown_event_names_ignored(tmp_path):
    content = (
        "lifecycle:\n"
        "  not-a-real-event:\n"
        "    - type: command\n"
        "      bash: echo nope\n"
        "  post-install:\n"
        "    - type: command\n"
        "      bash: echo yes\n"
    )
    entries = _parse(tmp_path, content)
    assert len(entries) == 1
    assert entries[0].event == "post-install"


def test_unknown_type_entry_dropped(tmp_path):
    content = (
        "lifecycle:\n"
        "  post-install:\n"
        "    - type: telepathy\n"
        "      bash: echo weird\n"
        "    - type: command\n"
        "      bash: echo ok\n"
    )
    entries = _parse(tmp_path, content)
    assert len(entries) == 1
    assert entries[0].script_type == "command"


def test_field_type_confusion_does_not_crash(tmp_path):
    """headers-as-list, env-as-list, allowedEnvVars-as-dict: no crash at parse."""
    content = (
        "lifecycle:\n"
        "  post-install:\n"
        "    - type: command\n"
        "      bash: echo hi\n"
        "      headers:\n"
        "        - not\n"
        "        - a\n"
        "        - map\n"
        "      env:\n"
        "        - also\n"
        "        - a\n"
        "        - list\n"
        "      allowedEnvVars:\n"
        "        a: dict-not-list\n"
    )
    entries = _parse(tmp_path, content)
    assert len(entries) == 1
    entry = entries[0]
    # allowedEnvVars must normalise a non-list to None (fail-safe), never raise.
    assert entry.allowed_env_vars is None
    # The malformed headers/env are stored raw; they are only acted on at
    # execution time, where fire() isolates any resulting error.
    assert isinstance(entry.headers, list)
    assert isinstance(entry.env, list)


@pytest.mark.parametrize(
    "raw_timeout, expected",
    [
        ('"abc"', "abc"),  # string survives via `timeoutSec or timeout`
        ("-5", -5),  # negative survives (truthy)
        ("true", True),  # bool survives (truthy)
        ("1.5", 1.5),  # float survives
        ("99999999999", 99999999999),  # huge int survives
    ],
)
def test_bad_timeout_reaches_entry_without_crash(tmp_path, raw_timeout, expected):
    content = (
        "lifecycle:\n"
        "  post-install:\n"
        "    - type: command\n"
        "      bash: echo hi\n"
        f"      timeoutSec: {raw_timeout}\n"
    )
    entries = _parse(tmp_path, content)
    assert len(entries) == 1
    assert entries[0].timeout_sec == expected
    # effective_timeout returns it as-is (no coercion) -- documented behavior.
    assert entries[0].effective_timeout == expected


def test_timeout_zero_silently_falls_through_to_default(tmp_path):
    """CHARACTERISATION: timeoutSec:0 is falsy, so `0 or timeout` -> default 30.

    A low-severity silent mis-parse: an author asking for a 0s timeout
    instead gets the 30s default. Pinned here so any future change to the
    `or` coalescing is caught.
    """
    content = (
        "lifecycle:\n"
        "  post-install:\n"
        "    - type: command\n"
        "      bash: echo hi\n"
        "      timeoutSec: 0\n"
    )
    entries = _parse(tmp_path, content)
    assert len(entries) == 1
    assert entries[0].timeout_sec is None
    assert entries[0].effective_timeout == 30


def test_non_string_cwd_stored_without_crash(tmp_path):
    content = (
        "lifecycle:\n  post-install:\n    - type: command\n      bash: echo hi\n      cwd: 12345\n"
    )
    entries = _parse(tmp_path, content)
    assert len(entries) == 1
    assert entries[0].cwd == 12345


def test_run_alias_maps_to_bash_and_command(tmp_path):
    content = "lifecycle:\n  post-install:\n    - run: echo via-run\n"
    entries = _parse(tmp_path, content)
    assert len(entries) == 1
    assert entries[0].script_type == "command"
    assert entries[0].bash == "echo via-run"
    assert entries[0].command == "echo via-run"
