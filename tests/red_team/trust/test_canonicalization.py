"""Vector 4 -- canonicalization stability vs forgery.

Two properties must both hold for the SHA-256-of-canonical-JSON scheme:

  STABILITY  -- semantically identical YAML (key reorder, quoting style,
                anchors/aliases, comments/whitespace) keeps the same
                fingerprint, so trust is never spuriously revoked.
  NO FORGERY -- semantically different lifecycle blocks (different command,
                reordered list entries, int-vs-string type coercion) MUST
                produce different fingerprints, so a malicious block can
                never collide onto a trusted hash.

All assertions describe secure behavior and pass on head.
"""

from __future__ import annotations

from pathlib import Path

from apm_cli.core.script_trust import script_file_fingerprint


def _fp(tmp_path: Path, name: str, text: str) -> str | None:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return script_file_fingerprint(p)


def test_key_reorder_keeps_same_fingerprint(tmp_path: Path) -> None:
    a = "lifecycle:\n  post-install:\n    - type: command\n      bash: echo hi\n"
    b = "lifecycle:\n  post-install:\n    - bash: echo hi\n      type: command\n"
    assert _fp(tmp_path, "a.yml", a) == _fp(tmp_path, "b.yml", b)


def test_quoted_vs_unquoted_scalar_keeps_fingerprint(tmp_path: Path) -> None:
    a = "lifecycle:\n  post-install:\n    - type: command\n      bash: echo hi\n"
    b = 'lifecycle:\n  post-install:\n    - type: "command"\n      bash: "echo hi"\n'
    assert _fp(tmp_path, "a.yml", a) == _fp(tmp_path, "b.yml", b)


def test_comment_and_whitespace_noise_keeps_fingerprint(tmp_path: Path) -> None:
    a = "lifecycle:\n  post-install:\n    - type: command\n      bash: echo hi\n"
    b = (
        "# a leading comment\n"
        "lifecycle:\n  post-install:\n    - type: command   # inline\n"
        "      bash: echo hi\n\n"
    )
    assert _fp(tmp_path, "a.yml", a) == _fp(tmp_path, "b.yml", b)


def test_anchor_alias_expands_to_same_fingerprint(tmp_path: Path) -> None:
    anchored = (
        "lifecycle:\n"
        "  post-install: &steps\n"
        "    - type: command\n      bash: echo hi\n"
        "  post-update: *steps\n"
    )
    expanded = (
        "lifecycle:\n"
        "  post-install:\n    - type: command\n      bash: echo hi\n"
        "  post-update:\n    - type: command\n      bash: echo hi\n"
    )
    assert _fp(tmp_path, "a.yml", anchored) == _fp(tmp_path, "b.yml", expanded)


def test_different_command_does_not_collide(tmp_path: Path) -> None:
    a = "lifecycle:\n  post-install:\n    - type: command\n      bash: echo hi\n"
    b = "lifecycle:\n  post-install:\n    - type: command\n      bash: rm -rf /\n"
    assert _fp(tmp_path, "a.yml", a) != _fp(tmp_path, "b.yml", b)


def test_list_reorder_changes_fingerprint(tmp_path: Path) -> None:
    """Execution order is security-relevant, so a list reorder MUST revoke."""
    a = (
        "lifecycle:\n  post-install:\n"
        "    - type: command\n      bash: echo A\n"
        "    - type: command\n      bash: echo B\n"
    )
    b = (
        "lifecycle:\n  post-install:\n"
        "    - type: command\n      bash: echo B\n"
        "    - type: command\n      bash: echo A\n"
    )
    assert _fp(tmp_path, "a.yml", a) != _fp(tmp_path, "b.yml", b)


def test_int_vs_string_timeout_coercion_differs(tmp_path: Path) -> None:
    """timeoutSec: 30 (int) and "30" (str) parse differently and must differ."""
    a = "lifecycle:\n  post-install:\n    - type: command\n      bash: x\n      timeoutSec: 30\n"
    b = 'lifecycle:\n  post-install:\n    - type: command\n      bash: x\n      timeoutSec: "30"\n'
    assert _fp(tmp_path, "a.yml", a) != _fp(tmp_path, "b.yml", b)
