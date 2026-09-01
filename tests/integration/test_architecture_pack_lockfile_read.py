"""Architecture guard for read-only bundle lockfile resolution."""

import ast
from pathlib import Path


def test_bundle_lockfile_reads_have_one_read_only_owner() -> None:
    """Bundle producers must not invoke mutating lockfile primitives directly."""
    root = Path(__file__).parents[2]
    owner = root / "src/apm_cli/deps/lockfile.py"
    tree = ast.parse(owner.read_text(encoding="utf-8"))
    definitions = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "resolve_lockfile_path_for_read"
    ]
    assert len(definitions) == 1
    resolver = definitions[0]
    read_only_guards = [
        node
        for node in resolver.body
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.Name)
        and node.test.id == "read_only"
    ]
    assert len(read_only_guards) == 1

    lockfile_class = next(
        node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "LockFile"
    )
    installed_paths = next(
        node
        for node in lockfile_class.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "installed_paths_for_project"
    )
    installed_calls = [
        node
        for node in ast.walk(installed_paths)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "resolve_lockfile_path_for_read"
    ]
    assert len(installed_calls) == 1
    assert any(
        keyword.arg == "read_only"
        and isinstance(keyword.value, ast.Constant)
        and keyword.value.value is True
        for keyword in installed_calls[0].keywords
    )
    assert not any(
        isinstance(node, ast.Name) and node.id == "LEGACY_LOCKFILE_NAME"
        for node in ast.walk(installed_paths)
    )

    consumers = (
        root / "src/apm_cli/bundle/packer.py",
        root / "src/apm_cli/bundle/plugin_exporter.py",
        root / "src/apm_cli/bundle/agent_plugin_exporter.py",
    )
    for consumer in consumers:
        source = consumer.read_text(encoding="utf-8")
        consumer_tree = ast.parse(source)
        imported = {
            alias.name
            for node in ast.walk(consumer_tree)
            if isinstance(node, ast.ImportFrom)
            for alias in node.names
        }
        assert not {"get_lockfile_path", "migrate_lockfile_if_needed"} & imported
        calls = [
            node
            for node in ast.walk(consumer_tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "resolve_lockfile_path_for_read"
        ]
        assert len(calls) == 1
        assert any(
            keyword.arg == "read_only"
            and isinstance(keyword.value, ast.Name)
            and keyword.value.id == "dry_run"
            for keyword in calls[0].keywords
        )

    guard = (root / "scripts/lint-architecture-boundaries.sh").read_text(encoding="utf-8")
    assert "AC37: read-only lockfile path authority" in guard
    architecture = (root / ".apm/instructions/architecture.instructions.md").read_text(
        encoding="utf-8"
    )
    assert "| Read-only lockfile path resolution |" in architecture
