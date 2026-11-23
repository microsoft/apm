"""The string containment helpers in utils/paths agree with pathlib on every input.

``relative_to``, ``relative_posix`` and ``is_within`` decide the common shape
(absolute POSIX ``/`` or Windows ``X:\\`` paths of one class) by comparing
normalized strings, and leave every other shape to pathlib. A seeded property
test checks result, result type and exception against pathlib on both
flavours, including UNC, ``\\\\?\\``, drive-relative, ``..`` and unicode case.
"""

from __future__ import annotations

import os
import random
from pathlib import PurePath, PurePosixPath, PureWindowsPath

import pytest

from apm_cli.utils import paths

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-LAW-portability.byte_deterministic")]

_PARTS = [
    "a",
    "A",
    "b",
    "B",
    "abc",
    "ABC",
    "x.md",
    "..",
    ".",
    "",
    "d:foo",
    "C:",
    "\u0130",
    "\u00df",
    "ss",
    "SS",
    "e\u0301",
    "\u00e9",
    " ",
    "a b",
    "~1",
    "RUNNER~1",
]
_POSIX_ROOTS = ["/", "//", "///", "", "./"]
_WINDOWS_ROOTS = [
    "C:\\",
    "c:\\",
    "C:/",
    "D:\\",
    "\\\\srv\\share\\",
    "//srv/share/",
    "\\",
    "C:",
    "",
    "\\\\?\\C:\\",
]
_CASES = 60000


def _outcome(call) -> tuple:
    try:
        result = call()
    except Exception as exc:
        return ("raise", type(exc), str(exc))
    return ("ok", type(result), str(result))


def _random_path(rng: random.Random, flavour: type[PurePath], roots: list[str]) -> PurePath:
    parts = [rng.choice(_PARTS) for _ in range(rng.randrange(5))]
    sep = "/" if flavour is PurePosixPath or rng.random() < 0.3 else "\\"
    return flavour(rng.choice(roots) + sep.join(parts))


def _pair(rng: random.Random, flavour: type[PurePath], roots: list[str]) -> tuple:
    path = _random_path(rng, flavour, roots)
    if rng.random() < 0.6:
        base = _random_path(rng, flavour, roots)
    else:
        base = flavour(*path.parts[: rng.randrange(len(path.parts) + 1)] or ["."])
    if rng.random() < 0.2:
        base = flavour(str(base).upper())
    return path, base


def _relative_posix_by_pathlib(path: PurePath, base: PurePath) -> str | None:
    try:
        return path.relative_to(base).as_posix()
    except ValueError:
        return None


@pytest.mark.parametrize(
    ("flavour", "roots"),
    [(PurePosixPath, _POSIX_ROOTS), (PureWindowsPath, _WINDOWS_ROOTS)],
    ids=["posix", "windows"],
)
def test_every_helper_matches_pathlib(flavour: type[PurePath], roots: list[str]) -> None:
    rng = random.Random(1805)  # noqa: S311 -- seeded test data
    decided = 0
    for _ in range(_CASES):
        path, base = _pair(rng, flavour, roots)
        fast = _outcome(lambda: paths.relative_to(path, base))  # noqa: B023
        slow = _outcome(lambda: path.relative_to(base))  # noqa: B023
        assert fast == slow, (path, base)
        fast = _outcome(lambda: paths.is_within(path, base))  # noqa: B023
        slow = _outcome(lambda: path.is_relative_to(base))  # noqa: B023
        assert fast == slow, (path, base)
        expected = _relative_posix_by_pathlib(path, base)
        assert paths.relative_posix(path, base) == expected, (path, base)
        decided += paths._remainder(path, base) is not paths._UNDECIDED
    assert decided > _CASES // 8, decided


@pytest.fixture
def no_pathlib_containment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make pathlib's own containment methods fail, so only the string route can answer."""

    def refuse(*_args, **_kwargs):
        raise AssertionError("pathlib containment was used for a common-shape input")

    monkeypatch.setattr(PurePath, "relative_to", refuse)
    monkeypatch.setattr(PurePath, "is_relative_to", refuse)


@pytest.mark.parametrize(
    ("flavour", "path", "base", "rest"),
    [
        (PurePosixPath, "/w/apm_modules/o/r/x.md", "/w/apm_modules", "o/r/x.md"),
        (PurePosixPath, "/w", "/", "w"),
        (PureWindowsPath, "C:\\W\\Apm_Modules\\o\\x.md", "c:\\w\\apm_modules", "o/x.md"),
        (PureWindowsPath, "D:\\w", "d:\\", "w"),
    ],
)
def test_common_shapes_are_decided_without_pathlib(
    no_pathlib_containment: None, flavour: type[PurePath], path: str, base: str, rest: str
) -> None:
    inner, outer = flavour(path), flavour(base)
    relative = paths.relative_to(inner, outer)
    assert type(relative) is flavour
    assert relative == flavour(rest)
    assert paths.relative_posix(inner, outer) == rest
    assert paths.is_within(inner, outer) is True
    assert paths.is_within(outer, inner) is False
    assert paths.relative_posix(outer, inner) is None


def test_a_sibling_sharing_a_name_prefix_is_not_within(no_pathlib_containment: None) -> None:
    sibling = PurePosixPath("/w/apm_modules2/x")
    assert paths.is_within(sibling, PurePosixPath("/w/apm_modules")) is False
    assert paths.relative_posix(PurePosixPath("/w/ab"), PurePosixPath("/w/a")) is None


class _StrictPath(PurePosixPath):
    """A subclass with its own containment rules, which the helpers must honour."""

    def relative_to(self, other, *args, **kwargs):
        raise ValueError("strict containment")

    def is_relative_to(self, other, *args, **kwargs):
        return False


def test_a_path_subclass_keeps_its_own_containment() -> None:
    inner, outer = _StrictPath("/w/a/b"), _StrictPath("/w/a")
    with pytest.raises(ValueError, match="strict containment"):
        paths.relative_to(inner, outer)
    assert paths.relative_posix(inner, outer) is None
    assert paths.is_within(inner, outer) is False


def test_the_same_path_is_dot() -> None:
    assert paths.relative_posix(PurePosixPath("/w/a"), PurePosixPath("/w/a")) == "."
    same = paths.relative_to(PureWindowsPath("C:\\A"), PureWindowsPath("c:\\a"))
    assert same == PureWindowsPath(".")


@pytest.mark.skipif(os.name == "nt", reason="the property test covers Windows path shapes")
def test_owner_containment_checks_take_the_string_route(
    tmp_path, no_pathlib_containment: None
) -> None:
    """Resolved, same-class absolute paths never reach pathlib's containment walk."""
    from apm_cli.utils import content_hash, path_security

    root = tmp_path.resolve()
    inner = root / "pkg" / "x.md"
    inner.parent.mkdir()
    inner.write_text("x\n", encoding="utf-8")
    assert path_security.resolved_relative_within(inner.resolve(), root) == "pkg/x.md"
    assert path_security.ensure_path_within_resolved(inner, root) == inner.resolve()
    assert path_security.has_symlink_component(root, inner) is False
    assert paths.portable_relpath(inner, root) == "pkg/x.md"
    assert content_hash.compute_package_hash(root / "pkg").startswith("sha256:")
