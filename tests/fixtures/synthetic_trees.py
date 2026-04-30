"""Synthetic fixtures for deep dependency tree testing.

Generates apm.yml manifests that simulate real-world dependency patterns:
- 100 packages across 10 depth levels
- Diamond dependencies (A→B, A→C, B→D, C→D)
- Large primitive counts per package
"""

import textwrap
from pathlib import Path
from typing import Dict, List  # noqa: F401, UP035


def generate_deep_tree_fixtures(
    base_dir: Path, n_packages: int = 100, max_depth: int = 10
) -> dict[str, Path]:
    """Generate a tree of apm.yml files simulating deep transitive deps.

    Returns a dict of package_name -> apm.yml path.
    """
    packages: dict[str, Path] = {}

    for i in range(n_packages):
        depth = (i % max_depth) + 1
        pkg_name = f"test-pkg-{i}"
        pkg_dir = base_dir / f"owner/{pkg_name}"
        pkg_dir.mkdir(parents=True, exist_ok=True)

        # Build dependency list (each package depends on 1-2 others at next depth)
        deps = []
        if depth < max_depth:
            child_idx = (i + max_depth) % n_packages
            deps.append(f"owner/test-pkg-{child_idx}")
            # Diamond: add a second dep sometimes
            if i % 3 == 0:
                child_idx2 = (i + max_depth + 1) % n_packages
                deps.append(f"owner/test-pkg-{child_idx2}")

        deps_yaml = ""
        if deps:
            dep_lines = "\n".join(f"    - {d}" for d in deps)
            deps_yaml = f"\ndependencies:\n  apm:\n{dep_lines}\n"

        content = textwrap.dedent(f"""\
            name: {pkg_name}
            version: 1.0.0
            description: Synthetic package at depth {depth}
            {deps_yaml}""")

        apm_yml = pkg_dir / "apm.yml"
        apm_yml.write_text(content)
        packages[pkg_name] = apm_yml

    return packages


def generate_primitive_heavy_package(
    base_dir: Path, n_instructions: int = 200, n_contexts: int = 50
) -> Path:
    """Generate a package with many primitives for conflict detection benchmarks."""
    pkg_dir = base_dir / "heavy-pkg"
    pkg_dir.mkdir(parents=True, exist_ok=True)

    apm_yml = pkg_dir / "apm.yml"
    apm_yml.write_text("name: heavy-pkg\nversion: 1.0.0\n")

    # Create instruction files
    instructions_dir = pkg_dir / ".apm" / "instructions"
    instructions_dir.mkdir(parents=True, exist_ok=True)
    for i in range(n_instructions):
        (instructions_dir / f"instr-{i}.instructions.md").write_text(
            f"---\ndescription: Instruction {i}\napplyTo: '**'\n---\nContent {i}\n"
        )

    # Create context files
    contexts_dir = pkg_dir / ".apm" / "contexts"
    contexts_dir.mkdir(parents=True, exist_ok=True)
    for i in range(n_contexts):
        (contexts_dir / f"ctx-{i}.md").write_text(f"Context content {i}\n")

    return pkg_dir
