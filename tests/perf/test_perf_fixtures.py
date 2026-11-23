"""Opt-in proof that the perf fixtures and the journey harness work end to end.

Run with ``PYTEST_PERF=1 pytest tests/perf/test_perf_fixtures.py -m benchmark -s``
(never part of default CI: ``tests/perf`` is skipped without PYTEST_PERF=1 and
the ``benchmark`` marker is deselected by default). The install tests run the
real ``apm`` CLI (``APM_BINARY_PATH`` or ``.venv/bin/apm``) against the
offline mirror and print the request counts.
"""

from __future__ import annotations

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
    publish_repositories,
    serve_offline,
)

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.benchmark,
    pytest.mark.promise("P-LAW-lockfile.frozen_preserves_bytes"),
]

_REPO = Path(__file__).resolve().parents[2]


def _apm() -> str:
    candidate = os.environ.get("APM_BINARY_PATH") or str(_REPO / ".venv" / "bin" / "apm")
    if not Path(candidate).is_file():
        candidate = shutil.which("apm") or ""
    if not candidate:
        pytest.skip("no apm CLI: set APM_BINARY_PATH")
    return candidate


@pytest.mark.parametrize("name", sorted(TREE_RECIPES))
def test_tree_recipes_write_the_pinned_shape(name: str, tmp_path: Path) -> None:
    recipe = TREE_RECIPES[name]
    fixture = build_tree(recipe, tmp_path / name)

    def name_of(spec: str) -> str:
        return spec.split("#")[0].rsplit("/", 1)[-1]

    edges: dict[str, list[str]] = {}
    for manifest in sorted((fixture.root / "packages").glob("*/apm.yml")):
        document = yaml.safe_load(manifest.read_text(encoding="utf-8"))
        deps = document.get("dependencies", {}).get("apm", [])
        edges[document["name"]] = [name_of(d) for d in deps]
    project = yaml.safe_load((fixture.project / "apm.yml").read_text(encoding="utf-8"))
    roots = [name_of(d) for d in project["dependencies"]["apm"]]
    parents: dict[str, int] = {}
    for children in edges.values():
        for child in children:
            parents[child] = parents.get(child, 0) + 1
    memo: dict[str, int] = {}

    def height(node: str) -> int:
        if node not in memo:
            memo[node] = 1 + max((height(c) for c in edges[node]), default=0)
        return memo[node]

    assert len(edges) == recipe.packages
    assert max(height(root) for root in roots) == recipe.depth >= 5
    assert set(parents) | set(roots) == set(edges)
    assert sum(1 for count in parents.values() if count >= 2) >= recipe.min_diamonds
    specs = {
        d.split("#")[0]
        for m in (fixture.root / "packages").glob("*/apm.yml")
        for d in (
            yaml.safe_load(m.read_text(encoding="utf-8")).get("dependencies", {}).get("apm", [])
        )
    } | {d.split("#")[0] for d in project["dependencies"]["apm"]}
    # One host per package: every parent references it the same way (no forked identity).
    assert len(specs) == len({name_of(s) for s in specs}) == recipe.packages
    assert {fixture.spec(name_of(s)).split("#")[0] for s in specs} == specs
    skill_files = list((fixture.root / "packages").rglob("SKILL.md"))
    assert len(skill_files) == recipe.packages * recipe.primitives.skills


def test_generation_is_deterministic(tmp_path: Path) -> None:
    recipe = TREE_RECIPES["tree-10"]
    first = build_tree(recipe, tmp_path / "a")
    second = build_tree(recipe, tmp_path / "b")

    first_files = sorted(p for p in first.root.rglob("*") if p.is_file())
    second_files = sorted(p for p in second.root.rglob("*") if p.is_file())
    assert [p.relative_to(first.root) for p in first_files] == [
        p.relative_to(second.root) for p in second_files
    ]
    assert [p.read_bytes() for p in first_files] == [p.read_bytes() for p in second_files]
    shas = []
    for fixture, label in ((first, "ra"), (second, "rb")):
        bares = publish_repositories(fixture, tmp_path / label)
        tags = [
            subprocess.run(
                ["git", "--git-dir", str(bare), "rev-parse", "v1.0.0"],
                capture_output=True,
                text=True,
                check=True,
            )
            for bare in bares.values()
        ]
        shas.append(sorted(result.stdout.strip() for result in tags))
    assert shas[0] == shas[1]



def _rev(bare: Path, ref: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "--git-dir", str(bare), "rev-parse", ref],
        capture_output=True,
        text=True,
        check=True,
    )


def test_pin_shapes_reference_the_tip_or_a_non_tip_commit(tmp_path: Path) -> None:
    tip = build_tree(TREE_RECIPES["tree-10-tip"], tmp_path / "tip")
    tip_project = yaml.safe_load((tip.project / "apm.yml").read_text(encoding="utf-8"))
    sha = build_tree(TREE_RECIPES["tree-10-sha"], tmp_path / "sha")
    sha_project = yaml.safe_load((sha.project / "apm.yml").read_text(encoding="utf-8"))
    bares = publish_repositories(sha, tmp_path / "repos")

    assert all("#" not in spec for spec in tip_project["dependencies"]["apm"])
    for spec in sha_project["dependencies"]["apm"]:
        path, _, pinned = spec.partition("#")
        bare = bares[sha.slug(path.rsplit("/", 1)[-1])]
        tagged, main = _rev(bare, "v1.0.0"), _rev(bare, "main")
        assert len(pinned) == 40
        assert tagged.stdout.strip() == pinned != main.stdout.strip()

def _install(apm: str, cwd: Path, env: dict[str, str], *flags: str) -> None:
    result = subprocess.run(
        [apm, "install", *flags],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_tree_10_offline_install_reports_requests(tmp_path: Path) -> None:
    apm = _apm()
    fixture = build_tree(TREE_RECIPES["tree-10"], tmp_path / "fixture")
    lockfile = fixture.project / "apm.lock.yaml"
    with serve_offline(fixture, tmp_path / "world") as world:
        _install(apm, fixture.project, world.env)
        traffic = world.traffic().as_dict()
        committed = lockfile.read_bytes()
        shutil.rmtree(fixture.project / "apm_modules")
        world.clear_cache()
        world.reset_traffic()
        _install(apm, fixture.project, world.env, "--frozen")
        frozen_traffic = world.traffic().as_dict()
    print(f"\n[tree-10 offline] install={traffic} install --frozen={frozen_traffic}")

    lock = yaml.safe_load(committed.decode("utf-8"))
    locked = {entry["repo_url"] for entry in lock["dependencies"]}
    assert locked == {fixture.slug(name) for name in fixture.packages}
    assert lockfile.read_bytes() == committed
    assert traffic["requests"] > 0
    assert traffic["round_trips"] > traffic["requests"]
    assert frozen_traffic["requests"] > 0


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


def test_serving_two_trees_with_shared_slugs_is_refused(tmp_path: Path) -> None:
    small = build_tree(TREE_RECIPES["tree-10"], tmp_path / "small")
    large = build_tree(TREE_RECIPES["tree-50"], tmp_path / "large")

    with (
        pytest.raises(ValueError, match="reuses slugs"),
        serve_offline((small, large), tmp_path / "world"),
    ):
        pass
    assert (tmp_path / "world" / "repositories" / "tree-10").is_dir()
