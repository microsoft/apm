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
        assert "resolve_lockfile_path_for_read(project_root, read_only=dry_run)" in source

    guard = (root / "scripts/lint-architecture-boundaries.sh").read_text(encoding="utf-8")
    assert "AC37: read-only lockfile path authority" in guard
    architecture = (root / ".apm/instructions/architecture.instructions.md").read_text(
        encoding="utf-8"
    )
    assert "| Read-only lockfile path resolution |" in architecture
