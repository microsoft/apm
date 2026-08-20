"""RED-TEAM: duplicate YAML keys must parse deterministically (last wins).

PyYAML's safe loader silently accepts duplicate mapping keys and keeps the
last occurrence. The parser must therefore be deterministic and never
crash on a manifest with a repeated event name or a repeated field inside
an entry -- an easy way for a malformed or adversarial apm.yml to try to
confuse the reader.
"""

from __future__ import annotations

from .conftest import write_apm_yml


def _parse(tmp_path, content):
    from apm_cli.core.lifecycle_scripts import parse_apm_yml_lifecycle

    doc = write_apm_yml(tmp_path, content)
    return parse_apm_yml_lifecycle(doc, "project")


def test_duplicate_event_keys_last_wins(tmp_path):
    content = (
        "lifecycle:\n"
        "  post-install:\n"
        "    - type: command\n"
        "      bash: echo first-block\n"
        "  post-install:\n"
        "    - type: command\n"
        "      bash: echo second-block\n"
    )
    entries = _parse(tmp_path, content)
    assert len(entries) == 1
    assert entries[0].bash == "echo second-block"


def test_duplicate_entry_fields_last_wins(tmp_path):
    content = (
        "lifecycle:\n"
        "  post-install:\n"
        "    - type: command\n"
        "      bash: first\n"
        "      bash: second\n"
        "      timeoutSec: 1\n"
        "      timeoutSec: 2\n"
    )
    entries = _parse(tmp_path, content)
    assert len(entries) == 1
    assert entries[0].bash == "second"
    assert entries[0].timeout_sec == 2


def test_duplicate_keys_are_deterministic_across_loads(tmp_path):
    content = (
        "lifecycle:\n"
        "  post-install:\n"
        "    - type: command\n"
        "      bash: a\n"
        "      bash: b\n"
        "      bash: c\n"
    )
    first = _parse(tmp_path, content)
    second = _parse(tmp_path, content)
    assert [e.bash for e in first] == [e.bash for e in second] == ["c"]
