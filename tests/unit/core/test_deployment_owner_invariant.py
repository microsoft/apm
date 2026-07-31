"""Source invariant for canonical deployment-state mutation."""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest


def _load_checker() -> ModuleType:
    root = Path(__file__).resolve().parents[3]
    path = root / "scripts/check_deployment_state_mutations.py"
    spec = importlib.util.spec_from_file_location("check_deployment_state_mutations", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "source",
    [
        "lock.deployed_files = []",
        "lock.deployed_files += ['new']",
        "lock.deployed_file_hashes['path'] = 'sha256:value'",
        "lock.local_deployed_files.append('path')",
        "lock.local_deployed_files.remove('path')",
        "lock.local_deployed_file_hashes.pop('path')",
        "lock.local_deployed_files.extend(['path'])",
        "lock.local_deployed_files.clear()",
        "lock.local_deployed_file_hashes.update({'path': 'hash'})",
        "lock.local_deployed_files.insert(0, 'path')",
        "lock.mcp_target_servers = {'vscode': ['server']}",
    ],
)
def test_mutation_detector_catches_negative_controls(source: str) -> None:
    assert _load_checker().mutation_lines(source) == [1]


@pytest.mark.parametrize(
    "source",
    [
        "files = lock.local_deployed_files",
        "files = [*lock.local_deployed_files]",
        "value = lock.local_deployed_file_hashes.get('path')",
    ],
)
def test_mutation_detector_allows_owned_field_reads(source: str) -> None:
    assert _load_checker().mutation_lines(source) == []


def test_only_canonical_owner_mutates_legacy_deployment_fields() -> None:
    root = Path(__file__).resolve().parents[3] / "src" / "apm_cli"
    assert _load_checker().analyze_tree(root) == []
