"""The indexed claimed-file surface lookup equals the linear filter it replaces (#4940)."""

from __future__ import annotations

import random
from pathlib import Path

import pytest

from apm_cli.security.file_scanner import surface_candidates
from apm_cli.security.primitive_discovery import PrimitiveSurface

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-OWN-audit-primitive-discovery")]

_ROOT = Path("/proj")
_DIRS = (".github", ".github/instructions", ".github/skills", ".agents/skills", ".claude/rules")
_NAMES = ("a.md", "b.instructions.md", "SKILL.md", "x.prompt.md", "deep/c.md")


def _linear(surfaces: list[PrimitiveSurface], path: Path) -> list[PrimitiveSurface]:
    return [
        s
        for s in surfaces
        if s.contains(path) or (s.pattern is not None and path.is_relative_to(s.path))
    ]


def _surface(path: Path, pattern: str | None, index: int) -> PrimitiveSurface:
    return PrimitiveSurface(_ROOT, path, pattern, f"t{index}", "kind", "fmt")


@pytest.mark.parametrize("seed", range(25))
def test_the_index_returns_the_linear_candidates_in_order(seed: int) -> None:
    rng = random.Random(seed)  # noqa: S311 -- seeded test data, not security
    surfaces = []
    for index in range(rng.randint(1, 30)):
        base = _ROOT / rng.choice(_DIRS)
        if rng.random() < 0.4:
            surfaces.append(_surface(base / rng.choice(_NAMES), None, index))
        else:
            surfaces.append(_surface(base, rng.choice(("*.md", "*", "SKILL.md")), index))
    lookup = surface_candidates(surfaces)

    for directory in (*_DIRS, "elsewhere"):
        for name in _NAMES:
            path = _ROOT / directory / name
            assert lookup(path) == _linear(surfaces, path)
        assert lookup(_ROOT / directory) == _linear(surfaces, _ROOT / directory)
