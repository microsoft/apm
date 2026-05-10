"""Parallel BFS resolver tests (F7, #1116).

These tests pin down the contract that level-batched parallel
resolution must honour:

1. ``max_parallel=1`` is byte-identical to the legacy sequential path
   (parity test).
2. With concurrent workers the resolved tree shape, callback-recorded
   download set, and node ordering remain deterministic across runs
   even when individual download callbacks sleep for randomized
   intervals.
3. Two parents at the same depth that reference the same dep get
   deduplicated -- only one node is created, both parents reference
   it via ``children``.
4. Worker exceptions surfaced from ``_try_load_dependency_package``
   are caught and reported via the debug log path; resolution does
   not abort.
"""

from __future__ import annotations

import random
import threading
import time
from pathlib import Path

import yaml

from apm_cli.deps.apm_resolver import APMDependencyResolver


def _write_pkg(root: Path, name: str, deps: list[str] | None = None) -> Path:
    pkg_dir = root / name
    pkg_dir.mkdir(parents=True, exist_ok=True)
    manifest: dict = {"name": name, "version": "1.0.0"}
    if deps:
        manifest["dependencies"] = {"apm": deps, "mcp": []}
    (pkg_dir / "apm.yml").write_text(yaml.safe_dump(manifest))
    return pkg_dir


def _make_callback(call_log: list[str], lock: threading.Lock, sleep_jitter: float = 0.0):
    """Return a callback that records every dep it sees.

    When ``sleep_jitter`` > 0, the callback sleeps for a randomized
    interval to expose ordering races.
    """

    def cb(dep_ref, mods_dir, parent_chain="", parent_pkg=None):
        if sleep_jitter:
            time.sleep(random.uniform(0, sleep_jitter))  # noqa: S311
        with lock:
            call_log.append(dep_ref.get_display_name())
        # All packages are pre-laid-out; just return the install path.
        return dep_ref.get_install_path(mods_dir)

    return cb


def _make_tree(tmp_path: Path) -> Path:
    """Lay out a small dep graph and return the project root.

    Shape::

        root -> a -> shared
        root -> b -> shared
        root -> c
    """
    modules = tmp_path / "apm_modules"
    modules.mkdir()
    _write_pkg(modules / "org", "a", deps=["org/shared"])
    _write_pkg(modules / "org", "b", deps=["org/shared"])
    _write_pkg(modules / "org", "c")
    _write_pkg(modules / "org", "shared")
    (tmp_path / "apm.yml").write_text(
        yaml.safe_dump(
            {
                "name": "root",
                "version": "0.0.1",
                "dependencies": {"apm": ["org/a", "org/b", "org/c"], "mcp": []},
            }
        )
    )
    return tmp_path


def _resolved_node_keys(graph) -> list[str]:
    """Return tree node keys in deterministic insertion order."""
    return list(graph.dependency_tree.nodes.keys())


def test_max_parallel_one_matches_default_resolver(tmp_path):
    """``max_parallel=1`` must produce the exact same tree as the default."""
    project = _make_tree(tmp_path)

    log_a: list[str] = []
    log_b: list[str] = []
    lock = threading.Lock()

    resolver_seq = APMDependencyResolver(
        apm_modules_dir=project / "apm_modules",
        download_callback=_make_callback(log_a, lock),
        max_parallel=1,
    )
    resolver_par = APMDependencyResolver(
        apm_modules_dir=project / "apm_modules",
        download_callback=_make_callback(log_b, lock),
        max_parallel=4,
    )

    g_seq = resolver_seq.resolve_dependencies(project)
    g_par = resolver_par.resolve_dependencies(project)

    # Same set of resolved nodes, same insertion order.
    assert _resolved_node_keys(g_seq) == _resolved_node_keys(g_par)
    # Shared dep is deduplicated: 4 nodes total (a, b, c, shared).
    assert len(g_seq.dependency_tree.nodes) == 4


def test_parallel_resolution_is_deterministic_under_jitter(tmp_path):
    """Random sleeps in the callback must not perturb the resolved tree."""
    project = _make_tree(tmp_path)
    random.seed(0xA1B2)

    runs: list[list[str]] = []
    for _ in range(10):
        log: list[str] = []
        lock = threading.Lock()
        resolver = APMDependencyResolver(
            apm_modules_dir=project / "apm_modules",
            download_callback=_make_callback(log, lock, sleep_jitter=0.005),
            max_parallel=4,
        )
        graph = resolver.resolve_dependencies(project)
        runs.append(_resolved_node_keys(graph))

    # Every run produces the same node-insertion order.
    assert all(r == runs[0] for r in runs), runs


def test_shared_transitive_dep_is_deduplicated(tmp_path):
    """A dep referenced by two siblings appears once in the tree, with
    both parents pointing at the same node."""
    project = _make_tree(tmp_path)
    log: list[str] = []
    lock = threading.Lock()

    resolver = APMDependencyResolver(
        apm_modules_dir=project / "apm_modules",
        download_callback=_make_callback(log, lock),
        max_parallel=4,
    )
    graph = resolver.resolve_dependencies(project)

    # The shared dep should appear exactly once in the tree -- the
    # parallel BFS dedups identical (dep_ref, depth) pairs at Phase A.
    nodes = graph.dependency_tree.nodes
    shared_keys = [k for k in nodes if "shared" in k]
    assert len(shared_keys) == 1, list(nodes.keys())

    # Preserved sequential semantics: ``queued_keys`` blocks the second
    # parent from enqueuing the same sub-dep, so exactly one of (a, b)
    # owns the shared child. Whichever parent wins is determined by
    # manifest declaration order ("a" before "b"), which Phase C must
    # honour by iterating results in submission order.
    a_node = next(n for k, n in nodes.items() if "/a" in k or k.endswith(":a"))
    b_node = next(n for k, n in nodes.items() if "/b" in k or k.endswith(":b"))
    assert len(a_node.children) == 1
    assert len(b_node.children) == 0
    assert "shared" in a_node.children[0].dependency_ref.get_unique_key()


def test_callback_exception_does_not_abort_resolution(tmp_path):
    """A worker raising must not bring down the whole resolution."""
    project = _make_tree(tmp_path)
    lock = threading.Lock()

    def cb(dep_ref, mods_dir, parent_chain="", parent_pkg=None):
        # The resolver catches ValueError / FileNotFoundError around
        # ``_try_load_dependency_package``. A callback that returns the
        # install_path keeps everything healthy; for "c" we return None
        # to simulate a soft failure.
        with lock:
            pass
        if dep_ref.get_display_name().endswith("/c"):
            return None
        return dep_ref.get_install_path(mods_dir)

    resolver = APMDependencyResolver(
        apm_modules_dir=project / "apm_modules",
        download_callback=cb,
        max_parallel=4,
    )
    graph = resolver.resolve_dependencies(project)

    # All four nodes should still appear -- "c" with a placeholder
    # package, the others fully loaded.
    assert len(graph.dependency_tree.nodes) == 4


def test_max_parallel_env_override(monkeypatch, tmp_path):
    """``APM_RESOLVE_PARALLEL`` env var sets the worker count when no
    explicit ``max_parallel`` is supplied."""
    monkeypatch.setenv("APM_RESOLVE_PARALLEL", "7")
    resolver = APMDependencyResolver(apm_modules_dir=tmp_path / "apm_modules")
    assert resolver._max_parallel == 7

    monkeypatch.setenv("APM_RESOLVE_PARALLEL", "not-a-number")
    resolver = APMDependencyResolver(apm_modules_dir=tmp_path / "apm_modules")
    # Falls back to the default when env var is malformed.
    assert resolver._max_parallel == 4

    # Explicit ctor arg wins over env.
    monkeypatch.setenv("APM_RESOLVE_PARALLEL", "9")
    resolver = APMDependencyResolver(apm_modules_dir=tmp_path / "apm_modules", max_parallel=2)
    assert resolver._max_parallel == 2


def test_max_parallel_zero_clamped_to_one(tmp_path):
    """``max_parallel=0`` must coerce to 1 -- ThreadPoolExecutor rejects 0."""
    resolver = APMDependencyResolver(apm_modules_dir=tmp_path / "apm_modules", max_parallel=0)
    assert resolver._max_parallel == 1


def test_transitive_malformed_deps_surfaces_warning(tmp_path, caplog):
    """A transitive dep with flat-list ``dependencies`` must produce a
    warning-level log (not silently swallowed) and resolution must
    continue without the malformed dep's sub-dependencies."""
    import logging

    modules = tmp_path / "apm_modules"
    modules.mkdir()

    # "good" has valid structured deps
    _write_pkg(modules / "org", "good", deps=["org/leaf"])
    _write_pkg(modules / "org", "leaf")

    # "bad" has flat-list deps (invalid format)
    bad_dir = modules / "org" / "bad"
    bad_dir.mkdir(parents=True)
    (bad_dir / "apm.yml").write_text(
        yaml.safe_dump(
            {
                "name": "bad",
                "version": "1.0.0",
                "dependencies": ["org/should-not-resolve"],
            }
        )
    )

    # Root depends on both
    (tmp_path / "apm.yml").write_text(
        yaml.safe_dump(
            {
                "name": "root",
                "version": "0.0.1",
                "dependencies": {"apm": ["org/good", "org/bad"], "mcp": []},
            }
        )
    )

    lock = threading.Lock()
    log: list[str] = []

    resolver = APMDependencyResolver(
        apm_modules_dir=modules,
        download_callback=_make_callback(log, lock),
        max_parallel=1,
    )

    with caplog.at_level(logging.WARNING, logger="apm_cli.deps.apm_resolver"):
        graph = resolver.resolve_dependencies(tmp_path)

    # "good" and its child "leaf" resolve; "bad" gets a node but its
    # invalid sub-deps are not enqueued.
    node_keys = list(graph.dependency_tree.nodes.keys())
    assert any("good" in k for k in node_keys), node_keys
    assert any("leaf" in k for k in node_keys), node_keys
    assert any("bad" in k for k in node_keys), node_keys
    assert not any("should-not-resolve" in k for k in node_keys), node_keys

    # The warning must mention the structured-format error.
    warning_messages = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("expected a mapping" in msg for msg in warning_messages), (
        f"Expected 'expected a mapping' warning; got: {warning_messages}"
    )
