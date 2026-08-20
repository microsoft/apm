"""RED-TEAM: oversized / deeply nested manifests must stay bounded.

A malformed or hostile apm.yml can carry tens of thousands of entries or
deeply nested structures. The parser must complete in bounded wall-clock
time, build exactly the well-formed entries, and never recurse without
limit. Each parse is daemon-thread guarded so a runaway never stalls CI.
"""

from __future__ import annotations

from .conftest import run_guarded, write_apm_yml


def test_thousands_of_entries_parse_bounded(tmp_path):
    n = 5000
    lines = ["lifecycle:", "  post-install:"]
    for i in range(n):
        lines.append("    - type: command")
        lines.append(f"      bash: echo {i}")
    write_apm_yml(tmp_path, "\n".join(lines) + "\n")

    from apm_cli.core.lifecycle_scripts import parse_apm_yml_lifecycle

    doc = tmp_path / "apm.yml"
    finished, result, exc = run_guarded(
        lambda: parse_apm_yml_lifecycle(doc, "project"), timeout=20.0
    )
    assert finished, "parser did not finish on a 5000-entry manifest"
    assert exc is None, f"parser raised on large manifest: {exc!r}"
    assert len(result) == n


def test_many_events_each_with_entries(tmp_path):
    events = (
        "pre-install",
        "post-install",
        "pre-update",
        "post-update",
        "pre-uninstall",
        "post-uninstall",
    )
    per = 500
    lines = ["lifecycle:"]
    for ev in events:
        lines.append(f"  {ev}:")
        for i in range(per):
            lines.append("    - type: command")
            lines.append(f"      bash: echo {ev}-{i}")
    write_apm_yml(tmp_path, "\n".join(lines) + "\n")

    from apm_cli.core.lifecycle_scripts import parse_apm_yml_lifecycle

    doc = tmp_path / "apm.yml"
    finished, result, exc = run_guarded(
        lambda: parse_apm_yml_lifecycle(doc, "project"), timeout=20.0
    )
    assert finished, "parser did not finish on a multi-event manifest"
    assert exc is None, f"parser raised: {exc!r}"
    assert len(result) == len(events) * per


def test_deeply_nested_value_does_not_blow_recursion(tmp_path):
    """A deep (non-alias) nested list under a real event -> entries dropped,
    no RecursionError, bounded time."""
    depth = 400
    nested = "x"
    for _ in range(depth):
        nested = "[" + nested + "]"
    content = "lifecycle:\n  post-install:\n    - " + nested + "\n"
    write_apm_yml(tmp_path, content)

    from apm_cli.core.lifecycle_scripts import parse_apm_yml_lifecycle

    doc = tmp_path / "apm.yml"
    finished, result, exc = run_guarded(
        lambda: parse_apm_yml_lifecycle(doc, "project"), timeout=10.0
    )
    assert finished, "parser hung on a deeply nested entry"
    assert exc is None, f"parser raised on deep nesting: {exc!r}"
    # The single entry is a deep list, not a dict, so it is dropped.
    assert result == []
