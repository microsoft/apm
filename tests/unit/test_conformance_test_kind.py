"""Pins for the closed spec-conformance test-kind rule (tests/spec_conformance/_test_kind.py)."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tests.spec_conformance._test_kind import (
    BEHAVIORAL,
    TEXT_SHAPE,
    _module_index,
    _module_tree,
    classify_definition,
)


def _classify(tmp_path: Path, source: str) -> str:
    path = tmp_path / "test_case_reqs.py"
    path.write_text(source, encoding="utf-8")
    _module_tree.cache_clear()
    _module_index.cache_clear()
    tree = ast.parse(source)
    function = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "test_it"
    )
    node = next(
        item
        for item in ast.walk(_module_tree(str(path)))
        if isinstance(item, ast.FunctionDef) and item.lineno == function.lineno
    )
    return classify_definition(str(path), node)


@pytest.mark.parametrize(
    ("source", "kind"),
    [
        pytest.param(
            "from tests.utils.artifact_snapshot import ArtifactSnapshot\n"
            "from apm_cli.utils.yaml_io import load_yaml\n"
            "def test_it(tmp_path):\n"
            "    assert load_yaml(tmp_path / 'x.yml') is None\n"
            "    assert ArtifactSnapshot.capture(tmp_path)\n",
            TEXT_SHAPE,
            id="tests-utils-and-yaml-readers-only",
        ),
        pytest.param(
            "from apm_cli.constants import APM_YML_FILENAME\n"
            "def test_it():\n"
            "    assert APM_YML_FILENAME == 'apm.yml'\n",
            TEXT_SHAPE,
            id="apm-cli-constants-only",
        ),
        pytest.param(
            "from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner\n"
            "def test_it():\n"
            "    assert ApmLifecycleRunner\n",
            BEHAVIORAL,
            id="installed-cli-runner",
        ),
        pytest.param(
            "from apm_cli.deps.registry.semver import pick_best\n"
            "def _helper():\n"
            "    return pick_best\n"
            "def test_it():\n"
            "    assert _helper()\n",
            BEHAVIORAL,
            id="apm-cli-decision-through-a-module-helper",
        ),
        pytest.param(
            "def test_it(apm_binary_path):\n    assert apm_binary_path\n",
            BEHAVIORAL,
            id="installed-binary-fixture",
        ),
    ],
)
def test_conformance_test_kind_rule(tmp_path: Path, source: str, kind: str) -> None:
    assert _classify(tmp_path, source) == kind
