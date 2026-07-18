"""Source invariant for canonical deployment-state mutation."""

import ast
from pathlib import Path

import pytest

_OWNED_FIELDS = frozenset(
    {
        "deployed_files",
        "deployed_file_hashes",
        "local_deployed_files",
        "local_deployed_file_hashes",
        "mcp_target_servers",
    }
)
_MUTATORS = frozenset({"append", "remove", "pop", "extend", "clear", "update", "insert"})
_ALLOWED = {
    Path("core/deployment_state.py"),
    Path("core/deployment_ledger.py"),
    Path("deps/lockfile.py"),
}


def _owned_attribute(node: ast.AST) -> bool:
    return isinstance(node, ast.Attribute) and node.attr in _OWNED_FIELDS


def _mutation_lines(source: str) -> list[int]:
    tree = ast.parse(source)
    violations: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if _owned_attribute(target) or (
                    isinstance(target, ast.Subscript) and _owned_attribute(target.value)
                ):
                    violations.add(node.lineno)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in _MUTATORS
            and _owned_attribute(node.func.value)
        ):
            violations.add(node.lineno)
    return sorted(violations)


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
    assert _mutation_lines(source) == [1]


@pytest.mark.parametrize(
    "source",
    [
        "files = lock.local_deployed_files",
        "files = [*lock.local_deployed_files]",
        "value = lock.local_deployed_file_hashes.get('path')",
    ],
)
def test_mutation_detector_allows_owned_field_reads(source: str) -> None:
    assert _mutation_lines(source) == []


def test_only_canonical_owner_mutates_legacy_deployment_fields() -> None:
    root = Path(__file__).resolve().parents[3] / "src" / "apm_cli"
    violations: list[str] = []
    for source in root.rglob("*.py"):
        relative = source.relative_to(root)
        if relative in _ALLOWED:
            continue
        for line_number in _mutation_lines(source.read_text(encoding="utf-8")):
            violations.append(f"{relative.as_posix()}:{line_number}")
    assert violations == []
