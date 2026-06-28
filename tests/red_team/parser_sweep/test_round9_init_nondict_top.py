"""Round-9 parser regression trap: init guard on non-dict apm.yml.

r9-parser-1 (MED) -- ``apm lifecycle init`` did
``data = load_yaml(target_file) or {}`` then ``if "lifecycle" in data`` /
``data["lifecycle"] = ...`` with no ``isinstance(data, dict)`` guard. A
project apm.yml whose top-level YAML node is a list or a scalar
(``- a``, ``5``, ``hello``, ``3.14``) therefore raised an uncaught
``TypeError`` (membership-test / item-assign on a non-mapping), crashing
the command with a traceback. Every sibling consumer (parse / validate /
fingerprint) already fails closed on this guard; init was the lone gap.
The ``null`` document (-> None -> {}) must still scaffold normally.
"""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from apm_cli.commands.lifecycle import lifecycle_init

_NON_DICT_TOPS = ["- a\n- b\n", "5\n", "hello\n", "3.14\n", "[1, 2, 3]\n"]


@pytest.mark.parametrize("content", _NON_DICT_TOPS)
def test_init_non_dict_top_fails_clean(content, tmp_path, monkeypatch):
    """A non-mapping apm.yml must exit(1) cleanly, never raise TypeError."""
    (tmp_path / "apm.yml").write_text(content)
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(lifecycle_init, [])

    assert result.exit_code == 1
    assert not isinstance(result.exception, TypeError), repr(result.exception)
    assert "mapping" in result.output


def test_init_null_top_still_scaffolds(tmp_path, monkeypatch):
    """A ``null`` document (-> {}) must still scaffold a lifecycle block."""
    (tmp_path / "apm.yml").write_text("null\n")
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(lifecycle_init, [])

    assert result.exit_code == 0, result.output
    assert "lifecycle" in (tmp_path / "apm.yml").read_text()


def test_init_mapping_top_unaffected(tmp_path, monkeypatch):
    """A normal mapping apm.yml must keep scaffolding as before."""
    (tmp_path / "apm.yml").write_text("name: demo\n")
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(lifecycle_init, [])

    assert result.exit_code == 0, result.output
    assert "lifecycle" in (tmp_path / "apm.yml").read_text()
