"""RT3 v0.39 red team: hostile Windows layouts against the RC's fast paths.

Oracle: stock ``Path.resolve()`` for every operand the fast resolver answers
(#5374), containment verdicts that must hold through junctions, 8.3 aliases
and case variants, and a tampered cache that must heal, never serve (#5410).
Every cell lists all violations it finds instead of stopping at the first.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import zlib
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-LAW-portability.byte_deterministic")]

_WINDOWS = pytest.mark.skipif(sys.platform != "win32", reason="Win32 layouts")


def _junction(link: Path, target: Path) -> bool:
    import _winapi

    try:
        _winapi.CreateJunction(str(target), str(link))
    except OSError:
        return False
    return True


def _short(path: Path) -> str | None:
    import ctypes

    buffer = ctypes.create_unicode_buffer(1024)
    length = ctypes.windll.kernel32.GetShortPathNameW(str(path), buffer, 1024)
    return buffer.value if 0 < length < 1024 and "~" in buffer.value else None


def _swapcase_tail(text: str, keep: str) -> str:
    return keep + text[len(keep) :].swapcase()


def _layout(root: Path) -> tuple[list[str], dict[str, Path]]:
    """A hostile tree under *root*; returns operand strings and named nodes."""
    a = root / "a"
    (a / "inner").mkdir(parents=True)
    (a / "f.txt").write_text("f", encoding="ascii")
    longname = root / "LongDirectoryNameForShort"
    (longname / "deep").mkdir(parents=True)
    outside = root.parent / f"{root.name}-outside"
    outside.mkdir(exist_ok=True)
    (outside / "secret.txt").write_text("s", encoding="ascii")
    nodes = {"a": a, "long": longname, "outside": outside}
    links = {
        "J": a,
        "Jout": outside,
        "Jdangling": root / "gone-target",
        "Jinner": a / "inner",
    }
    for name, target in links.items():
        if _junction(root / name, target):
            nodes[name] = root / name
    if _junction(root / "L1", root / "L2") and _junction(root / "L2", root / "L1"):
        nodes["L1"] = root / "L1"
    # A junction to a directory whose own name is reached through a junction.
    if "J" in nodes and _junction(root / "JJ", root / "J" / "inner"):
        nodes["JJ"] = root / "JJ"
    nest = root
    while len(str(nest)) < 230:
        nest = nest / "nested-component"
    nest.mkdir(parents=True)
    nodes["nest"] = nest
    keep = str(root)
    base = [
        str(root),
        str(a),
        str(a / "f.txt"),
        str(a / "inner"),
        str(longname),
        str(longname / "deep"),
        str(nest),
        *(str(p) for k, p in nodes.items() if k[0] in "JL"),
    ]
    operands: list[str] = []
    for text in base:
        for tail in ("", "\\x", "\\x\\y", "\\f.txt", "\\F.TXT", "\\..\\a", "\\.\\x"):
            operands.append(text + tail)
            operands.append(_swapcase_tail(text + tail, keep))
    operands += [
        str(a / "f.txt") + "\\x",
        str(a / "f.txt") + "\\x\\y",
        str(nest) + "\\" + "m" * 40,
        str(nest) + "\\" + "m" * 40 + "\\n",
        str(root).lower() + "\\a",
        str(root)[0].lower() + str(root)[1:] + "\\a\\x",
    ]
    short = _short(longname)
    if short:
        operands += [short, short + "\\deep", short + "\\x", short + "\\deep\\x"]
        nodes["short"] = Path(short)
    return operands, nodes


def _mismatches(operands: list[str]) -> list[str]:
    from pathlib import WindowsPath

    from apm_cli.utils.realpath_win import resolve_windows

    bad = []
    for text in operands:
        path = WindowsPath(text)
        try:
            fast = resolve_windows(path)
        except Exception as exc:  # the fast form must never raise where stock answers
            try:
                stock = path.resolve()
            except Exception:
                continue
            bad.append(f"{text!r}: fast raised {exc!r}, stock {stock}")
            continue
        if fast is None:
            continue
        stock = path.resolve()
        if str(fast) != str(stock):
            bad.append(f"{text!r}: fast {fast} != stock {stock}")
    return bad


@_WINDOWS
def test_rt3_fast_resolve_matches_stock_on_hostile_operands(tmp_path: Path) -> None:
    operands, _nodes = _layout(tmp_path / "proj")

    assert _mismatches(operands) == []


@_WINDOWS
def test_rt3_held_walk_matches_stock_and_after_swaps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Form A (held walk) inside an invocation, then every unheld dir swapped for a junction."""
    from apm_cli.core.invocation_memo import invocation_scope
    from apm_cli.utils.realpath_win import held_count, register_held_root

    project = tmp_path / "proj"
    operands, nodes = _layout(project)
    package = project / "apm_modules" / "o" / "r"
    (package / "sub").mkdir(parents=True)
    (package / "sub" / "b.txt").write_text("b", encoding="ascii")
    operands += [str(package), str(package / "sub"), str(package / "sub" / "b.txt")]
    operands += [str(package / "sub" / "x"), _swapcase_tail(str(package / "sub"), str(project))]
    monkeypatch.chdir(project)
    bad: list[str] = []
    swap_errors: list[str] = []
    with invocation_scope():
        register_held_root(package, project / "apm_modules")
        bad += [f"held: {m}" for m in _mismatches(operands)]
        # Swap: rename each plain dir away and put a junction to the outside
        # dir in its place, then compare again (no stale held answer allowed).
        for name in ("a", "LongDirectoryNameForShort"):
            victim = project / name
            try:
                victim.rename(project / f"{name}-moved")
            except OSError as exc:
                swap_errors.append(f"{name}: {exc!r}")
                continue
            _junction(victim, nodes["outside"])
        sub = package / "sub"
        try:
            sub.rename(package / "sub-moved")
            _junction(sub, nodes["outside"])
        except OSError as exc:
            swap_errors.append(f"package/sub: {exc!r}")
        bad += [f"after swap: {m}" for m in _mismatches(operands)]
    assert held_count() == 0, "holds leaked past the invocation"
    print("swap refusals (held dirs pin their names):", swap_errors)
    assert bad == []


@_WINDOWS
def test_rt3_containment_through_junctions_aliases_and_case(tmp_path: Path) -> None:
    from apm_cli.utils.path_security import PathTraversalError, ensure_path_within

    project = tmp_path / "proj"
    _operands, nodes = _layout(project)
    escapes = [project / "Jout" / "secret.txt", project / "Jout" / "new.md"]
    if "JJ" in nodes:
        escapes.append(project / "JJ" / ".." / ".." / "Jout" / "x")
    inside = [project / "a" / "x", project / "J" / "x", project / "JJ" / "x"]
    if "short" in nodes:
        inside += [nodes["short"] / "deep", nodes["short"] / "x"]
    bases = [project, Path(_swapcase_tail(str(project), str(tmp_path)))]
    if _short(project):
        bases.append(Path(_short(project)))
    bad = []
    for base in bases:
        for path in escapes:
            try:
                ensure_path_within(path, base)
            except PathTraversalError:
                continue
            bad.append(f"escape accepted: {path} within {base}")
        for path in inside:
            try:
                ensure_path_within(path, base)
            except PathTraversalError as exc:
                bad.append(f"inside refused: {path} within {base}: {exc}")
    assert bad == []


# --------------------------------------------------------------------------
# #5410: a tampered or corrupt cache must heal, never serve
# --------------------------------------------------------------------------

_BODY = b"---\nname: demo\n---\nhello\n"


def _git_env() -> dict[str, str]:
    env = os.environ.copy()
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_SYSTEM"] = os.devnull
    for key in list(env):
        if key.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_", "GIT_CONFIG_COUNT")):
            env.pop(key, None)
    return env


def _git(*args: str, cwd: Path | None = None) -> str:
    from apm_cli.utils.git_env import get_git_executable

    return subprocess.run(
        [get_git_executable(), *args],
        cwd=cwd,
        env=_git_env(),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _origin(tmp_path: Path) -> tuple[Path, str, str]:
    src = tmp_path / "origin"
    (src / "skills" / "demo").mkdir(parents=True)
    (src / "skills" / "demo" / "SKILL.md").write_bytes(_BODY)
    _git("init", "-q", "-b", "main", str(src))
    for key, value in (("user.email", "t@e.x"), ("user.name", "T"), ("core.autocrlf", "false")):
        _git("-C", str(src), "config", key, value)
    _git("-C", str(src), "add", ".")
    _git("-C", str(src), "commit", "-q", "-m", "x")
    sha = _git("-C", str(src), "rev-parse", "HEAD")
    blob = _git("-C", str(src), "rev-parse", "HEAD:skills/demo/SKILL.md")
    return src, sha, blob


def _explode_to_loose(checkout: Path, blob: str) -> Path:
    """Every object of the cached checkout as a loose object (the #5410 shape)."""
    from apm_cli.cache.pack_objects import _objects_dirs
    from apm_cli.utils.git_env import get_git_executable

    for objects in map(Path, _objects_dirs(checkout)):
        pack_dir = objects / "pack"
        for pack in sorted(pack_dir.glob("*.pack")) if pack_dir.is_dir() else []:
            moved = objects / pack.name
            shutil.move(str(pack), moved)
            for side in (pack.with_suffix(".idx"), pack.with_suffix(".rev")):
                side.unlink(missing_ok=True)
            env = {**_git_env(), "GIT_OBJECT_DIRECTORY": str(objects)}
            with moved.open("rb") as handle:
                subprocess.run(
                    [get_git_executable(), "-C", str(checkout), "unpack-objects", "-q"],
                    stdin=handle,
                    env=env,
                    check=True,
                )
            moved.unlink()
        loose = objects / blob[:2] / blob[2:]
        if loose.is_file():
            return loose
    raise AssertionError(f"fixture: blob {blob} is not loose in {_objects_dirs(checkout)}")


def _second_invocation(cache: Path, origin: Path, sha: str) -> subprocess.CompletedProcess:
    """A fresh process (no in-process verdicts), as the next ``apm install`` is."""
    code = (
        "import sys; from pathlib import Path\n"
        "from apm_cli.cache.git_cache import GitCache\n"
        "c = GitCache(Path(sys.argv[1])).get_checkout(sys.argv[2], sys.argv[3], locked_sha=sys.argv[3])\n"
        "sys.stdout.buffer.write((c / 'skills' / 'demo' / 'SKILL.md').read_bytes())\n"
    )
    return subprocess.run(
        [sys.executable, "-c", code, str(cache), str(origin), sha],
        capture_output=True,
        env={**_git_env(), "PYTHONPATH": os.pathsep.join(sys.path)},
        timeout=300,
        check=False,
    )


@pytest.mark.parametrize("damage", ["truncated", "garbage", "coherent-tamper", "growing"])
def test_rt3_damaged_loose_object_heals_and_never_serves(tmp_path: Path, damage: str) -> None:
    from apm_cli.cache.git_cache import GitCache

    origin, sha, blob = _origin(tmp_path)
    cache = tmp_path / "cache"
    checkout = GitCache(cache).get_checkout(str(origin), sha, locked_sha=sha)
    loose = _explode_to_loose(checkout, blob)
    # A local-path clone hardlinks origin objects: replace the name, never
    # write through the shared inode (that would poison the origin itself).
    original = loose.read_bytes()
    os.chmod(loose, 0o644)
    loose.unlink()
    evil = b"---\nname: demo\n---\nEVIL\n"
    if damage == "truncated":
        loose.write_bytes(original[:8])
    elif damage == "garbage":
        loose.write_bytes(b"\x00" * 64)
    elif damage == "coherent-tamper":
        # Worktree and loose blob both say EVIL; the blob keeps the honest name.
        loose.write_bytes(zlib.compress(b"blob %d\x00" % len(evil) + evil))
        (checkout / "skills" / "demo" / "SKILL.md").write_bytes(evil)
    else:
        loose.write_bytes(zlib.compress(b"blob 99999999\x00" + b"x" * 1024))

    result = _second_invocation(cache, origin, sha)

    assert result.returncode == 0, result.stderr.decode(errors="replace")[-2000:]
    assert result.stdout == _BODY, f"served {result.stdout[:80]!r}"
