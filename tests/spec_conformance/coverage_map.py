"""Requirement coverage map for the OpenAPM conformance statement.

Every collected test anywhere under ``tests/`` that carries
``@pytest.mark.req("req-xx-NNN")`` binds to that requirement. Collection
validates each id against the requirements manifest, and a collect-only run
over the whole ``tests/`` tree writes ``build/conformance-coverage.json`` for
``gen_statement.py`` and ``orphan_check.py``; partial collections never
overwrite it. The ``req`` marker is registered in ``pyproject.toml``.
"""

from __future__ import annotations

import ast
import inspect
import json
import re
from collections import defaultdict
from pathlib import Path

import pytest

from tests.spec_conformance._manifest import (
    COVERAGE_PATH,
    REPO_ROOT,
    requirements_by_id,
)
from tests.spec_conformance._test_kind import TEXT_SHAPE, classify_function

ID_PATTERN = re.compile(r"^req-[a-z]{2,3}-[0-9]{3}$")


def _static_status(item: pytest.Item) -> str:
    """Determine status by static inspection of the test function body.

    A test that calls `waive(...)` unconditionally with no preceding
    `assert` is `skipped`. A test that contains at least one assertion
    (assert / raises / pytest.raises) and no top-level unconditional
    waive is `active`. A test marked @pytest.mark.xfail is `xfail`.
    """
    if item.get_closest_marker("xfail"):
        return "xfail"
    if item.get_closest_marker("skip") or item.get_closest_marker("skipif"):
        return "skipped"
    func = getattr(item, "function", None)
    if func is None:
        return "active"
    try:
        src = inspect.getsource(func)
    except (OSError, TypeError):
        return "active"
    src = inspect.cleandoc(src) if False else src
    try:
        tree = ast.parse(src.lstrip())
    except SyntaxError:
        return "active"
    has_assert = False
    has_unconditional_waive = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Assert):
            has_assert = True
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "waive"
        ):
            has_unconditional_waive = True
    if has_assert:
        return "active"
    if has_unconditional_waive:
        return "skipped"
    return "active"


def _collects_whole_suite(config: pytest.Config) -> bool:
    """Return True when this is a collect-only run over the whole tests/ tree."""
    if not config.getoption("collectonly", False):
        return False
    tests_root = (REPO_ROOT / "tests").resolve()
    for arg in config.args or [str(tests_root)]:
        path = (Path(config.invocation_params.dir) / arg.split("::", 1)[0]).resolve()
        if path in (tests_root, REPO_ROOT.resolve()):
            return True
    return False


def record(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Validate req markers on every collection; write the map on a full collect-only."""
    manifest = requirements_by_id()
    coverage: dict[str, list[dict[str, str]]] = defaultdict(list)
    errors: list[str] = []
    for item in items:
        markers = list(item.iter_markers("req"))
        if not markers:
            continue
        ids: list[str] = []
        for m in markers:
            ids.extend(str(a) for a in m.args)
        status = _static_status(item)
        func = getattr(item, "function", None)
        kind = classify_function(func) if func is not None else TEXT_SHAPE
        for req_id in ids:
            if not ID_PATTERN.match(req_id):
                errors.append(
                    f"{item.nodeid}: malformed req id '{req_id}' "
                    f"(must match ^req-[a-z]{{2,3}}-[0-9]{{3}}$)"
                )
                continue
            if req_id not in manifest:
                errors.append(f"{item.nodeid}: req '{req_id}' is not in the requirements manifest")
                continue
            coverage[req_id].append({"test_nodeid": item.nodeid, "status": status, "kind": kind})
    if errors:
        joined = "\n".join(f"  - {e}" for e in errors)
        raise pytest.UsageError("Spec-conformance marker validation failed:\n" + joined)
    if not _collects_whole_suite(config):
        return
    COVERAGE_PATH.parent.mkdir(parents=True, exist_ok=True)
    canonical = {
        rid: sorted(rows, key=lambda r: r["test_nodeid"]) for rid, rows in sorted(coverage.items())
    }
    with COVERAGE_PATH.open("w", encoding="ascii", newline="\n") as f:
        json.dump(canonical, f, indent=2, sort_keys=True)
        f.write("\n")
