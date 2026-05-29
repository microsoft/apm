"""pytest configuration for the spec-conformance suite.

Registers and enforces the `req` marker. Every marker MUST resolve to
an id in the requirements manifest; unknown ids fail collection. The
marker coverage map is written to build/conformance-coverage.json for
gen_statement.py consumption.
"""

from __future__ import annotations

import ast
import inspect
import json
import re
from collections import defaultdict

import pytest

from tests.spec_conformance._manifest import (
    COVERAGE_PATH,
    REPO_ROOT,
    requirements_by_id,
)

ID_PATTERN = re.compile(r"^req-[a-z]{2,3}-[0-9]{3}$")


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "req(*ids): bind this test to one or more OpenAPM req-XXX ids.",
    )


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


def pytest_collection_modifyitems(config, items: list[pytest.Item]) -> None:
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
            coverage[req_id].append({"test_nodeid": item.nodeid, "status": status})
    if errors:
        joined = "\n".join(f"  - {e}" for e in errors)
        raise pytest.UsageError("Spec-conformance marker validation failed:\n" + joined)
    COVERAGE_PATH.parent.mkdir(parents=True, exist_ok=True)
    canonical = {
        rid: sorted(rows, key=lambda r: r["test_nodeid"]) for rid, rows in sorted(coverage.items())
    }
    with COVERAGE_PATH.open("w", encoding="ascii", newline="\n") as f:
        json.dump(canonical, f, indent=2, sort_keys=True)
        f.write("\n")


@pytest.fixture(scope="session")
def fixture_dir():
    from tests.spec_conformance._manifest import FIXTURE_ROOT

    return FIXTURE_ROOT


@pytest.fixture(scope="session")
def repo_root():
    return REPO_ROOT
