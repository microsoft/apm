"""Round-8 parser regression trap: Rich console-markup injection.

r8-parser-1 (MED) -- the ``apm lifecycle`` LIST renderer added each
discovered script's ``url`` / ``effective_command`` to a Rich table cell
unescaped. Rich parses cell strings as console markup by default, so a
hostile manifest whose url/command embedded a closing markup tag (e.g.
``[/]`` or ``[/red]``) raised ``rich.errors.MarkupError`` from
``console.print(table)``. The list renderer's ``except`` only caught
``(ImportError, NameError)``, so the error escaped past the plain-text
fallback and aborted the whole read-only audit listing -- the user could
not even inspect the hostile scripts. Round-7's ``_safe_token``
stringifies but does NOT neutralize markup. The fix escapes every cell
via ``rich.markup.escape`` before ``add_row``.
"""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from apm_cli.commands.lifecycle import lifecycle

_HOSTILE_MANIFESTS = [
    'lifecycle:\n  post-install:\n    - {type: http, url: "https://evil.example/[/]"}\n',
    'lifecycle:\n  post-install:\n    - {type: command, command: "echo [/red]pwn"}\n',
    "lifecycle:\n  post-install:\n"
    '    - {type: http, url: "https://h/[bold]x[/bold][/]"}\n'
    '    - {type: command, command: "echo [blink][/]"}\n',
]


@pytest.mark.parametrize("manifest", _HOSTILE_MANIFESTS)
def test_list_markup_injection_no_crash(manifest, tmp_path, monkeypatch):
    """`apm lifecycle` (list) must render hostile markup literally, never crash."""
    monkeypatch.setenv("APM_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("APM_E2E_TESTS", "1")
    (tmp_path / "apm.yml").write_text(manifest)
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(lifecycle, [])

    assert result.exit_code == 0, result.output
    assert result.exception is None, repr(result.exception)
    assert "Discovered" in result.output


def test_dry_run_markup_injection_no_crash(tmp_path, monkeypatch):
    """`apm lifecycle test` (dry-run) must also survive hostile markup."""
    monkeypatch.setenv("APM_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("APM_E2E_TESTS", "1")
    (tmp_path / "apm.yml").write_text(
        'lifecycle:\n  post-install:\n    - {type: command, command: "echo [/]"}\n'
    )
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(lifecycle, ["test", "post-install"])

    assert result.exit_code == 0, result.output
    assert result.exception is None, repr(result.exception)
