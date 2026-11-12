"""E2E coverage for apm compile placement and file inclusion."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from apm_cli.cli import cli
from apm_cli.compilation.context_optimizer import ContextOptimizer

INSTRUCTIONS = {
    "src-ts": "src/**/*.ts",
    "vendor-ts": "vendor/**/*.ts",
    "python": "**/*.py",
    "cfg-recursive": "**/*.cfg",
    "cfg-flat": "*.cfg",
    "workflows": ".github/workflows/*.yml",
}


def _write(project_root: Path, relative_path: str, content: str = "x\n") -> Path:
    path = project_root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _symlink(project_root: Path, relative_path: str, target: Path) -> None:
    link = project_root / relative_path
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlink creation not supported on this platform")


def _sentinel(name: str) -> str:
    return f"Placement parity sentinel {name}."


def _build_project(project_root: Path, outside: Path) -> None:
    _write(
        project_root,
        "apm.yml",
        "name: placement-parity-e2e\n"
        "version: 0.1.0\n"
        "target: agents\n"
        "compilation:\n"
        "  exclude:\n"
        '    - "src/generated/**"\n',
    )
    for name, apply_to in INSTRUCTIONS.items():
        _write(
            project_root,
            f".apm/instructions/{name}.instructions.md",
            f'---\ndescription: {name}\napplyTo: "{apply_to}"\n---\n\n{_sentinel(name)}\n',
        )

    for relative_path in (
        "src/app/main.ts",
        "src/app/util.ts",
        "src/lib/core.ts",
        "src/lib/nested/deep.ts",
        "vendor/ui/Button.ts",
        "tools/build.py",
        "tests/test_app.py",
        "docs/guide.txt",
        ".github/workflows/ci.yml",
        "src/generated/gen.ts",
        "node_modules/pkg/index.ts",
        ".hidden/secret.ts",
    ):
        _write(project_root, relative_path)

    _symlink(project_root, "src/linked/Button.ts", project_root / "vendor/ui/Button.ts")
    _symlink(project_root, "src/ext/External.cfg", _write(outside, "External.cfg"))
    _symlink(project_root, "src/ghost/Ghost.ts", outside / "missing.ts")


def _compile(project_root: Path) -> list[ContextOptimizer]:
    optimizers: list[ContextOptimizer] = []
    original_init = ContextOptimizer.__init__

    def spy_init(self: ContextOptimizer, *args, **kwargs) -> None:
        original_init(self, *args, **kwargs)
        optimizers.append(self)

    with patch.object(ContextOptimizer, "__init__", spy_init):
        result = CliRunner().invoke(cli, ["compile", "--target", "agents"], catch_exceptions=False)
    assert result.exit_code == 0, result.output
    return optimizers


def _rel(project_root: Path, directory: Path) -> str:
    return directory.relative_to(project_root).as_posix() or "."


def _sentinels_by_agents_file(project_root: Path) -> dict[str, set[str]]:
    placements: dict[str, set[str]] = {}
    for path in sorted(project_root.rglob("AGENTS.md")):
        content = path.read_text(encoding="utf-8")
        placements[path.relative_to(project_root).as_posix()] = {
            name for name in INSTRUCTIONS if _sentinel(name) in content
        }
    return placements


def _agents_snapshot(project_root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(project_root).as_posix(): path.read_bytes()
        for path in sorted(project_root.rglob("AGENTS.md"))
    }


def test_compile_placement_and_file_inclusion_parity(tmp_path: Path, monkeypatch) -> None:
    project_root = tmp_path / "project"
    _build_project(project_root, tmp_path / "outside")
    project_root = project_root.resolve()

    monkeypatch.chdir(project_root)
    optimizers = _compile(project_root)

    optimizer = optimizers[-1]
    # The dangling symlink counts toward src/ghost but is never a matchable file.
    assert {_rel(project_root, d) for d in optimizer._directory_cache} == {
        ".",
        ".github/workflows",
        "docs",
        "src/app",
        "src/ext",
        "src/ghost",
        "src/lib",
        "src/lib/nested",
        "src/linked",
        "tests",
        "tools",
        "vendor/ui",
    }
    assert {
        pattern: {_rel(project_root, d) for d in directories}
        for pattern, directories in optimizer._pattern_cache.items()
    } == {
        "src/**/*.ts": {"src/app", "src/lib", "src/lib/nested"},
        "vendor/**/*.ts": {"src/linked", "vendor/ui"},
        "**/*.py": {"tools", "tests"},
        "**/*.cfg": set(),
        "*.cfg": {"src/ext"},
        ".github/workflows/*.yml": {".github/workflows"},
    }
    assert _sentinels_by_agents_file(project_root) == {
        ".github/workflows/AGENTS.md": {"workflows"},
        "AGENTS.md": {"python", "src-ts", "vendor-ts", "cfg-recursive"},
        "src/ext/AGENTS.md": {"cfg-flat"},
    }

    first_artifacts = _agents_snapshot(project_root)
    first_directories = set(optimizer._directory_cache)
    # Repeated compile over the first run's outputs must reproduce them exactly.
    rerun_optimizer = _compile(project_root)[-1]
    assert set(rerun_optimizer._directory_cache) == first_directories
    assert _agents_snapshot(project_root) == first_artifacts
