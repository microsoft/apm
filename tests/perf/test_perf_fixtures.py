"""Opt-in proof that the perf fixtures and the journey harness work end to end.

Run with ``PYTEST_PERF=1 pytest tests/perf/test_perf_fixtures.py -m benchmark -s``
(never part of default CI: ``tests/perf`` is skipped without PYTEST_PERF=1 and
the ``benchmark`` marker is deselected by default). The install tests run the
real ``apm`` CLI (``APM_BINARY_PATH`` or ``.venv/bin/apm``) against the
offline mirror and print the request counts.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from tests.benchmarks import journeys
from tests.fixtures.perf import (
    MONOREPO_RECIPES,
    TREE_RECIPES,
    build_monorepo,
    build_tree,
    plan_tree,
    publish_repositories,
    serve_offline,
)

pytestmark = [pytest.mark.e2e, pytest.mark.benchmark]

_REPO = Path(__file__).resolve().parents[2]


def _apm() -> str:
    candidate = os.environ.get("APM_BINARY_PATH") or str(_REPO / ".venv" / "bin" / "apm")
    if not Path(candidate).is_file():
        candidate = shutil.which("apm") or ""
    if not candidate:
        pytest.skip("no apm CLI: set APM_BINARY_PATH")
    return candidate


def _digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


@pytest.mark.parametrize("name", sorted(TREE_RECIPES))
def test_tree_recipes_have_the_pinned_shape(name: str) -> None:
    recipe = TREE_RECIPES[name]
    levels, edges = plan_tree(recipe)
    parents: dict[str, int] = {}
    for children in edges.values():
        for child in children:
            parents[child] = parents.get(child, 0) + 1

    assert sum(len(level) for level in levels) == recipe.packages
    assert len(levels) == recipe.depth >= 5
    assert all(parents.get(name, 0) >= 1 for level in levels[1:] for name in level)
    assert sum(1 for count in parents.values() if count >= 2) >= recipe.min_diamonds
    assert any(len(children) >= 2 for children in edges.values())


def test_generation_is_deterministic(tmp_path: Path) -> None:
    recipe = TREE_RECIPES["tree-10"]
    first = build_tree(recipe, tmp_path / "a")
    second = build_tree(recipe, tmp_path / "b")

    assert _digest(first.root) == _digest(second.root)
    assert first.depth >= 5
    assert first.diamonds >= 2
    shas = []
    for fixture, label in ((first, "ra"), (second, "rb")):
        bares = publish_repositories(fixture, tmp_path / label)
        shas.append(
            sorted(
                subprocess.run(
                    ["git", "--git-dir", str(bare), "rev-parse", "v1.0.0"],
                    capture_output=True,
                    text=True,
                    check=True,
                ).stdout.strip()
                for bare in bares.values()
            )
        )
    assert shas[0] == shas[1]


def test_tree_10_offline_install_reports_requests(tmp_path: Path) -> None:
    apm = _apm()
    fixture = build_tree(TREE_RECIPES["tree-10"], tmp_path / "fixture")
    with serve_offline(fixture, tmp_path / "world") as world:
        result = subprocess.run(
            [apm, "install"],
            cwd=fixture.project,
            env=world.env,
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
        traffic = world.traffic().as_dict()
    print(f"\n[tree-10 offline install] traffic={traffic}")

    assert result.returncode == 0, result.stdout + result.stderr
    lock = yaml.safe_load((fixture.project / "apm.lock.yaml").read_text(encoding="utf-8"))
    locked = json.dumps(lock)
    assert all(f"perf-org/{name}" in locked for name in fixture.packages)
    assert traffic["requests"] > 0
    assert traffic["round_trips"] > traffic["requests"]


def test_monorepo_roots_install_offline(tmp_path: Path) -> None:
    apm = _apm()
    mono = build_monorepo(MONOREPO_RECIPES["monorepo-4"], tmp_path / "fixture")
    assert len(mono.project_roots) >= 3
    with serve_offline(mono.remote, tmp_path / "world") as world:
        for root in mono.project_roots[:2]:
            result = subprocess.run(
                [apm, "install"],
                cwd=root,
                env=world.env,
                capture_output=True,
                text=True,
                timeout=600,
                check=False,
            )
            assert result.returncode == 0, result.stdout + result.stderr
            assert (root / "apm.lock.yaml").is_file()


def test_harness_writes_json_and_markdown(tmp_path: Path) -> None:
    out = tmp_path / "out"
    code = journeys.main(
        [
            "run",
            "--apm",
            _apm(),
            "--fixtures",
            "tree-10",
            "--journeys",
            "boot-version,install-cold,install-frozen,compile",
            "--rtt",
            "0",
            "--runs",
            "1",
            "--max-load",
            "1000",
            "--work",
            str(tmp_path / "work"),
            "--out",
            str(out),
        ]
    )
    (report_path,) = out.glob("journeys-*.json")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    print("\n" + next(out.glob("journeys-*.md")).read_text(encoding="utf-8"))

    assert code == 0, [r["error"] for r in report["results"] if r["error"]]
    by_journey = {r["journey"]: r for r in report["results"]}
    assert set(by_journey) == {"boot-version", "install-cold", "install-frozen", "compile"}
    assert by_journey["install-cold"]["traffic"]["requests"] > 0
    assert by_journey["boot-version"]["traffic"]["requests"] == 0
