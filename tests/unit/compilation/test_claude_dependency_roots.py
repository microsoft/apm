"""Metadata-backed Claude imports and their filesystem boundaries."""

from pathlib import Path

import pytest

from apm_cli.compilation.claude_formatter import ClaudeFormatter
from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.models.dependency import DependencyReference
from apm_cli.primitives.discovery import get_dependency_declaration_order
from apm_cli.primitives.models import PrimitiveCollection
from apm_cli.utils.yaml_io import dump_yaml

pytestmark = pytest.mark.component


def _manifest(root: Path, dependencies: list) -> None:
    root.mkdir(parents=True, exist_ok=True)
    dump_yaml(
        {"name": "consumer", "version": "1.0.0", "dependencies": {"apm": dependencies}},
        root / "apm.yml",
    )


def _memory(root: Path, relative: str) -> Path:
    path = root / "apm_modules" / relative / "CLAUDE.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# Package memory\n", encoding="utf-8")
    return path


@pytest.mark.windows_compat
@pytest.mark.parametrize(
    ("dependency", "relative"),
    [
        ("MixedOrg/Standards", "MixedOrg/Standards"),
        ("dev.azure.com/contoso/platform/standards", "contoso/platform/standards"),
        (
            {"git": "https://gitlab.com/group/subgroup/team/standards"},
            "group/subgroup/team/standards",
        ),
        (
            {"git": "https://gitlab.com/group/subgroup/team/standards", "path": "nested/rules"},
            "group/subgroup/team/standards/nested/rules",
        ),
        (
            {
                "git": "https://dev.azure.com/contoso/platform/_git/standards",
                "path": "nested/rules",
            },
            "contoso/platform/standards/nested/rules",
        ),
        ({"git": "https://github.com/org/repo", "alias": "rules-alias"}, "rules-alias"),
        ({"path": "../local-rules"}, "_local/local-rules"),
    ],
)
def test_manifest_materialization_roots(
    tmp_path: Path, dependency: str | dict, relative: str
) -> None:
    """The real reference parser and path owner agree through rendered output."""
    _manifest(tmp_path, [dependency])
    path = _memory(tmp_path, relative)
    nested = path.parent / "docs/CLAUDE.md"
    nested.parent.mkdir()
    nested.write_text("# Not a package root\n", encoding="utf-8")
    formatter = ClaudeFormatter(str(tmp_path))
    result = formatter.format_distributed(PrimitiveCollection(), {})
    assert result.success, result.errors
    imports = [
        line
        for line in result.content_map[tmp_path / "CLAUDE.md"].splitlines()
        if line.startswith("@")
    ]
    assert imports == [f"@apm_modules/{relative}/CLAUDE.md"]
    assert get_dependency_declaration_order(str(tmp_path)) == [relative]


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("has_manifest", [False, True])
def test_redirected_lockfile_includes_transitives(
    tmp_path: Path, legacy: bool, has_manifest: bool
) -> None:
    source, deploy = tmp_path / "source", tmp_path / "deploy"
    source.mkdir()
    deploy.mkdir()
    if has_manifest:
        _manifest(source, ["owner/direct"])
    lock = LockFile()
    for depth, reference in enumerate(
        ("owner/direct", "dev.azure.com/org/project/transitive", "gitlab.com/group/sub/team/leaf")
    ):
        ref = DependencyReference.parse(reference)
        lock.add_dependency(LockedDependency.from_dependency_ref(ref, "a" * 40, depth, None))
        _memory(
            deploy, ref.get_install_path(Path("apm_modules")).relative_to("apm_modules").as_posix()
        )
    lock_path = deploy / ("apm.lock" if legacy else "apm.lock.yaml")
    lock.write(lock_path)
    before = lock_path.read_bytes()
    assert ClaudeFormatter(str(deploy), str(source))._collect_dependencies() == [
        "@apm_modules/group/sub/team/leaf/CLAUDE.md",
        "@apm_modules/org/project/transitive/CLAUDE.md",
        "@apm_modules/owner/direct/CLAUDE.md",
    ]
    assert lock_path.read_bytes() == before
    if legacy:
        assert not (deploy / "apm.lock.yaml").exists()


def test_source_modules_take_precedence_and_imports_resolve_from_output(tmp_path: Path) -> None:
    source, deploy = tmp_path / "source", tmp_path / "deploy"
    _manifest(source, ["owner/direct"])
    expected = _memory(source, "owner/direct")
    _memory(deploy, "owner/unrelated")
    imports = ClaudeFormatter(str(deploy), str(source))._collect_dependencies()
    assert imports == ["@../source/apm_modules/owner/direct/CLAUDE.md"]
    assert (deploy / imports[0][1:]).resolve() == expected


@pytest.mark.parametrize("metadata", ["manifest", "lock", "none"])
def test_nested_and_unrecorded_memory_not_imported(tmp_path: Path, metadata: str) -> None:
    if metadata == "manifest":
        _manifest(tmp_path, [])
    elif metadata == "lock":
        LockFile().write(tmp_path / "apm.lock.yaml")
    _memory(tmp_path, "owner/rootless/docs")
    _memory(tmp_path, "deep/org/project/repo")
    _memory(tmp_path, "owner/legacy")
    _memory(tmp_path, ".hidden/package")
    expected = ["@apm_modules/owner/legacy/CLAUDE.md"] if metadata == "none" else []
    assert ClaudeFormatter(str(tmp_path))._collect_dependencies() == expected


def test_declared_root_without_memory_does_not_import_nested_docs(tmp_path: Path) -> None:
    _manifest(tmp_path, ["owner/rootless"])
    _memory(tmp_path, "owner/rootless/docs")
    assert ClaudeFormatter(str(tmp_path))._collect_dependencies() == []


@pytest.mark.parametrize("metadata", ["manifest", "lock"])
def test_traversal_metadata_fails_without_fallback(tmp_path: Path, metadata: str) -> None:
    _memory(tmp_path, "owner/unrecorded")
    if metadata == "manifest":
        _manifest(tmp_path, [{"git": "https://github.com/owner/repo", "path": "../../outside"}])
    else:
        dump_yaml(
            {
                "lockfile_version": "1",
                "dependencies": [
                    {
                        "repo_url": "owner/repo",
                        "resolved_ref": "main",
                        "resolved_commit": "a" * 40,
                        "alias": "../outside",
                    }
                ],
            },
            tmp_path / "apm.lock.yaml",
        )
    result = ClaudeFormatter(str(tmp_path)).format_distributed(PrimitiveCollection(), {})
    assert not result.success
    assert result.errors
    assert not result.content_map


@pytest.mark.parametrize("filename", ["apm.yml", "apm.lock.yaml"])
def test_malformed_metadata_fails_without_fallback(tmp_path: Path, filename: str) -> None:
    _memory(tmp_path, "owner/unrecorded")
    (tmp_path / filename).write_text("[unclosed", encoding="utf-8")
    result = ClaudeFormatter(str(tmp_path)).format_distributed(PrimitiveCollection(), {})
    assert not result.success
    assert result.errors
    assert not result.content_map


@pytest.mark.parametrize("boundary", ["modules", "owner", "package", "file", "manifest", "lock"])
def test_escaping_symlinks_fail_closed(tmp_path: Path, boundary: str) -> None:
    project, outside = tmp_path / "project", tmp_path / "outside"
    _manifest(project, ["owner/pkg"])
    outside.mkdir()
    sentinel = outside / "CLAUDE.md"
    sentinel.write_text("# Outside secret\n", encoding="utf-8")
    paths = {
        "modules": project / "apm_modules",
        "owner": project / "apm_modules/owner",
        "package": project / "apm_modules/owner/pkg",
        "file": project / "apm_modules/owner/pkg/CLAUDE.md",
        "manifest": project / "apm.yml",
        "lock": project / "apm.lock.yaml",
    }
    link = paths[boundary]
    if boundary == "manifest":
        link.unlink()
    link.parent.mkdir(parents=True, exist_ok=True)
    if boundary in {"manifest", "lock"}:
        _memory(project, "owner/pkg")
    link.symlink_to(sentinel if boundary in {"file", "manifest", "lock"} else outside)
    result = ClaudeFormatter(str(project)).format_distributed(PrimitiveCollection(), {})
    assert not result.success
    assert any("outside" in error for error in result.errors)
    assert not result.content_map
    assert sentinel.read_text(encoding="utf-8") == "# Outside secret\n"
