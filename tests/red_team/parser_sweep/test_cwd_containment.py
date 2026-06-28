"""RED-TEAM round-2: command-script cwd containment + path chaos.

``_resolve_cwd`` must keep a RELATIVE cwd inside the project root (no
lateral movement via ``..``), while passing ABSOLUTE cwd values through
unchanged (documented: they are explicit and visible in apm.yml). This
probe attacks the containment check with traversal, sibling-prefix, NUL,
and missing-dir inputs, and verifies a hostile cwd never escapes ``fire``.
"""

from __future__ import annotations

import pytest

from .conftest import command_entry, fire, run_guarded


def _resolve(cwd, root):
    from apm_cli.core.script_executors import _resolve_cwd

    return _resolve_cwd(command_entry(cwd=cwd), str(root))


@pytest.mark.parametrize(
    "evil",
    ["..", "../..", "../../../etc", "subdir/../../x", "a/b/../../../../etc"],
)
def test_relative_traversal_is_clamped_to_root(tmp_path, evil):
    """A relative cwd that climbs out of root is clamped back to root."""
    root = tmp_path / "repo"
    root.mkdir()
    resolved = _resolve(evil, root)
    assert resolved == str(root.resolve())


def test_sibling_prefix_attack_is_clamped(tmp_path):
    """`../repo-evil` (string-prefix sibling of `repo`) must be clamped.

    Guards against a containment check that used ``startswith(root)``
    without a trailing separator -- ``/x/repo-evil`` must NOT count as
    inside ``/x/repo``.
    """
    root = tmp_path / "repo"
    root.mkdir()
    (tmp_path / "repo-evil").mkdir()
    resolved = _resolve("../repo-evil", root)
    assert resolved == str(root.resolve())


def test_relative_inside_root_is_preserved(tmp_path):
    """A legitimate relative subdir resolves inside root."""
    root = tmp_path / "repo"
    (root / "sub").mkdir(parents=True)
    resolved = _resolve("sub", root)
    assert resolved == str((root / "sub").resolve())


def test_absolute_cwd_is_passthrough(tmp_path):
    """Absolute cwd is documented passthrough -- returned verbatim."""
    resolved = _resolve("/etc", tmp_path / "repo")
    assert resolved == "/etc"


def test_nul_byte_cwd_is_isolated_at_fire(tmp_path, fire_event):
    """A NUL byte in a relative cwd raises in resolve() but fire() isolates it."""
    entry = command_entry(cwd="a\x00b")
    finished, _result, exc = run_guarded(lambda: fire(entry, fire_event, tmp_path), timeout=6.0)
    assert finished, "fire() hung on NUL-byte cwd"
    assert exc is None, f"NUL-byte cwd leaked from fire(): {exc!r}"


def test_missing_cwd_dir_is_isolated_at_fire(tmp_path, fire_event):
    """A cwd pointing at a non-existent dir -> Popen FileNotFoundError -> isolated."""
    entry = command_entry(cwd="does/not/exist")
    finished, _result, exc = run_guarded(lambda: fire(entry, fire_event, tmp_path), timeout=6.0)
    assert finished
    assert exc is None, f"missing cwd leaked from fire(): {exc!r}"


def test_very_long_relative_cwd_is_isolated(tmp_path, fire_event):
    """A pathologically long relative cwd must not crash or hang fire()."""
    entry = command_entry(cwd="/".join(["seg"] * 400))
    finished, _result, exc = run_guarded(lambda: fire(entry, fire_event, tmp_path), timeout=6.0)
    assert finished
    assert exc is None, f"long cwd leaked from fire(): {exc!r}"
