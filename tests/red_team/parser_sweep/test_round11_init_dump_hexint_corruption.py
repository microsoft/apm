"""Round-11 parser break r11-parser-1: init dump-path data destruction.

``apm lifecycle init`` on a project ``apm.yml`` carrying an integer in a
power-of-two base (hex ``0xff...``, leading-zero octal ``0777...``) used to
(a) crash with an uncaught ``ValueError`` and (b) TRUNCATE the victim
``apm.yml`` to zero bytes -- total content destruction, not the benign
comment-loss non-goal.

Mechanism: ``safe_load`` materialises a hex/octal literal losslessly
(base-16/8 parsing has no ``int_max_str_digits`` cap), but
``yaml.safe_dump``'s ``represent_int`` does ``str(int)``, which trips the
4300-digit decimal-conversion limit and raises a bare ``ValueError``. The
old ``dump_yaml`` opened the file with ``open(path, "w")`` -- truncating it
-- BEFORE ``safe_dump`` ran, so the failure left the file empty.

The fix serialises to a string FIRST (so the represent error is raised
before the file is opened, leaving it untouched) and guards the
``lifecycle_init`` call site to exit cleanly instead of crashing.

These traps fire the real ``apm lifecycle init`` against hostile manifests
and assert the original content is PRESERVED, the exit is clean (code 1, no
uncaught ``ValueError``), and a normal init still injects the block.
"""

from __future__ import annotations

import yaml
from click.testing import CliRunner

from apm_cli.commands.lifecycle import lifecycle_init

_HEX_BOMB = "name: victim-project\nport: 0x" + "f" * 6000 + "\n"
_OCTAL_BOMB = "name: victim-project\nperms: 0" + "7" * 6000 + "\n"


def _uncaught(result):
    """The non-SystemExit exception a CliRunner surfaced, if any."""
    exc = result.exception
    if exc is None or isinstance(exc, SystemExit):
        return None
    return type(exc).__name__


def test_hex_huge_int_preserves_apm_yml(monkeypatch, tmp_path):
    """A hex huge-int field must not crash or wipe apm.yml."""
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "apm.yml"
    target.write_text(_HEX_BOMB, encoding="utf-8")

    result = CliRunner().invoke(lifecycle_init, [])

    assert _uncaught(result) is None, f"uncaught {result.exception!r}"
    assert result.exit_code == 1
    assert target.read_text(encoding="utf-8") == _HEX_BOMB, "apm.yml mutated/wiped"


def test_octal_huge_int_preserves_apm_yml(monkeypatch, tmp_path):
    """A leading-zero (YAML 1.1) octal huge-int must not crash or wipe apm.yml."""
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "apm.yml"
    target.write_text(_OCTAL_BOMB, encoding="utf-8")

    result = CliRunner().invoke(lifecycle_init, [])

    assert _uncaught(result) is None, f"uncaught {result.exception!r}"
    assert result.exit_code == 1
    assert target.read_text(encoding="utf-8") == _OCTAL_BOMB, "apm.yml mutated/wiped"


def test_hex_bomb_never_truncates_to_zero(monkeypatch, tmp_path):
    """Explicit regression: the file must never reach zero bytes."""
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "apm.yml"
    target.write_text(_HEX_BOMB, encoding="utf-8")

    CliRunner().invoke(lifecycle_init, [])

    assert target.read_text(encoding="utf-8") != "", "apm.yml truncated to 0 bytes"


def test_normal_init_still_injects(monkeypatch, tmp_path):
    """The serialize-first dump path must not regress a normal init."""
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "apm.yml"
    target.write_text("name: demo\nversion: 1.0.0\n", encoding="utf-8")

    result = CliRunner().invoke(lifecycle_init, [])

    assert result.exit_code == 0, result.output
    parsed = yaml.safe_load(target.read_text(encoding="utf-8"))
    assert parsed["name"] == "demo"
    assert "post-install" in parsed["lifecycle"]


def test_dump_yaml_serialize_first_leaves_file_intact(tmp_path):
    """Unit-level: dump_yaml must not truncate on an unserialisable int."""
    from apm_cli.utils.yaml_io import dump_yaml

    target = tmp_path / "manifest.yml"
    sentinel = "original: content\n"
    target.write_text(sentinel, encoding="utf-8")

    huge = int("f" * 6000, 16)  # representable in memory, not as decimal str
    try:
        dump_yaml({"value": huge}, target)
    except ValueError:
        pass
    else:  # pragma: no cover - the represent error is expected
        raise AssertionError("expected ValueError from int_max_str_digits")

    assert target.read_text(encoding="utf-8") == sentinel, "file truncated before failure"
