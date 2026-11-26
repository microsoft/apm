"""Primitive discovery walks and classifies a package once for every type (#4939).

Car 1 (design doc section 7): one inventory walk and ONE classification pass
feed every primitive type. The per-type result must equal what the per-type
pass returned, including under planted links (K9).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from apm_cli.compilation.inventory import CompileInventory
from apm_cli.primitives import discovery
from tests.unit.deps.test_lockfile_read_cost import _count_calls

pytestmark = [pytest.mark.component, pytest.mark.promise("P-LAW-ownership.preserve_unowned")]


def _package(root: Path) -> Path:
    files = {
        ".apm/agents/a.agent.md": "# a\n",
        ".apm/instructions/i.instructions.md": "---\napplyTo: '**'\n---\nI\n",
        ".apm/context/c.context.md": "C\n",
        ".apm/memory/m.memory.md": "M\n",
        ".github/agents/g.agent.md": "# g\n",
        "nested/deep/x.instructions.md": "---\napplyTo: '**'\n---\nX\n",
        "node_modules/skip/z.agent.md": "# z\n",
        "both.agent.instructions.md": "---\napplyTo: '**'\n---\nB\n",
        "README.md": "readme\n",
    }
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    if sys.platform != "win32":
        outside = root.parent / "outside"
        outside.mkdir(exist_ok=True)
        (outside / "o.agent.md").write_text("# o\n", encoding="utf-8")
        os.symlink(outside / "o.agent.md", root / ".apm" / "agents" / "leaf.agent.md")
        os.symlink(outside, root / ".apm" / "linked")
    return root


def _per_type(root: Path, exclude: list[str] | None) -> dict[str, list[Path]]:
    """Today's shape: one pass per type, each over its own inventory."""
    result = {}
    for primitive_type, patterns in discovery.LOCAL_PRIMITIVE_PATTERNS.items():
        inventory = CompileInventory.collect(root.resolve(), exclude_patterns=exclude)
        base = root.resolve()
        tuples = [tuple(p for p in pat.split("/") if p) for pat in patterns]
        files = []
        for path in inventory.files_within(base):
            rel = path.relative_to(base)
            if any(part in discovery.DEFAULT_SKIP_DIRS for part in rel.parts[:-1]):
                continue
            if not any(discovery._glob_match_parts(rel.parts, t) for t in tuples):
                continue
            if exclude and discovery.should_exclude(path, base, exclude):
                continue
            if path.is_file() and not path.is_symlink():
                files.append(path)
        result[primitive_type] = files
    return result


@pytest.mark.parametrize("exclude", [None, ["nested/**"]], ids=["all", "excluded"])
def test_one_classification_returns_each_types_files(tmp_path: Path, exclude) -> None:
    root = _package(tmp_path / "pkg")

    classified = discovery.classify_primitive_files(
        root, discovery.LOCAL_PRIMITIVE_PATTERNS, exclude_patterns=exclude
    )

    assert classified == _per_type(root, exclude)
    assert all(not path.is_symlink() for files in classified.values() for path in files)
    names = {path.name for files in classified.values() for path in files}
    assert "leaf.agent.md" not in names and "o.agent.md" not in names
    assert "both.agent.instructions.md" in {p.name for p in classified["instruction"]}


def test_discovery_walks_a_package_once_for_every_type(tmp_path: Path) -> None:
    root = _package(tmp_path / "pkg")
    discovery.clear_discovery_cache()

    walks = _count_calls(
        CompileInventory.collect.__func__.__code__,
        lambda: discovery.discover_primitives(str(root)),
    )
    discovery.clear_discovery_cache()
    collection = discovery.discover_primitives(str(root))

    assert walks == 1
    assert {Path(p.file_path).name for p in collection.all_primitives()} >= {
        "a.agent.md",
        "i.instructions.md",
        "c.context.md",
        "m.memory.md",
        "g.agent.md",
    }
