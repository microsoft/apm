"""The retry advice of an interrupted add is a command the platform shell runs as typed (#5454)."""

from __future__ import annotations

import pytest

from apm_cli.install.manifest_journal import shell_words

pytestmark = [
    pytest.mark.unit,
    pytest.mark.promise("P-LAW-transaction.failed_command_preserves_state"),
]


@pytest.mark.parametrize(
    ("words", "windows", "rendered"),
    [
        (["--root", "C:\\Users\\me\\deploy", "./x"], True, "--root C:\\Users\\me\\deploy ./x"),
        (["--root", "C:\\a&b\\deploy"], True, '--root "C:\\a&b\\deploy"'),
        (["--root", "C:\\My Docs|x^y"], True, '--root "C:\\My Docs|x^y"'),
        (["--skill", 'say "hi"'], True, '--skill "say ""hi"""'),
        (["--root", "/a&b/deploy"], False, "--root '/a&b/deploy'"),
    ],
    ids=["windows-plain", "windows-ampersand", "windows-pipe-caret", "windows-quote", "posix"],
)
def test_a_word_the_shell_would_split_or_interpret_is_quoted(
    words: list[str], windows: bool, rendered: str
) -> None:
    assert shell_words(words, windows=windows) == rendered
