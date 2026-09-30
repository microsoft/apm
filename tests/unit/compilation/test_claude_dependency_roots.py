"""Metadata-backed Claude imports and their filesystem boundaries."""

from pathlib import Path

import pytest

from apm_cli.compilation.claude_formatter import ClaudeFormatter
from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.models.dependency import DependencyReference
from apm_cli.primitives.discovery import get_dependency_declaration_order
from apm_cli.primitives.models import PrimitiveCollection
from apm_cli.utils.yaml_io import dump_yaml
from tests.utils.artifact_snapshot import ArtifactSnapshot

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


@pytest.mark.parametrize(
    ("modern", "legacy", "selected"),
    [
        ("absent", "absent", None),
        ("absent", "dangling", "apm.lock"),
        ("absent", "valid-link", "apm.lock"),
        ("dangling", "valid-link", "apm.lock.yaml"),
        ("valid-link", "dangling", "apm.lock.yaml"),
        ("dangling", "dangling", "apm.lock.yaml"),
        ("valid-link", "valid-link", "apm.lock.yaml"),
    ],
)
def test_lock_metadata_links_preserve_precedence_and_fail_closed(
    tmp_path: Path, modern: str, legacy: str, selected: str | None
) -> None:
    _memory(tmp_path, "owner/unrecorded")
    for filename, state in (("apm.lock.yaml", modern), ("apm.lock", legacy)):
        if state == "absent":
            continue
        target = tmp_path / f"{filename}.target"
        if state == "valid-link":
            LockFile().write(target)
        (tmp_path / filename).symlink_to(target)
    before = ArtifactSnapshot.capture(tmp_path)
    result = ClaudeFormatter(str(tmp_path)).format_distributed(PrimitiveCollection(), {})
    invalid = selected is not None and (
        modern == "dangling" if selected == "apm.lock.yaml" else legacy == "dangling"
    )
    assert result.success is not invalid
    if invalid:
        assert any(selected in error for error in result.errors)
        assert not result.content_map
    else:
        imports = [
            line
            for line in result.content_map.get(tmp_path / "CLAUDE.md", "").splitlines()
            if line.startswith("@")
        ]
        assert imports == (["@apm_modules/owner/unrecorded/CLAUDE.md"] if selected is None else [])
    assert ArtifactSnapshot.capture(tmp_path) == before


@pytest.mark.parametrize("file_count", [500, 5000])
def test_local_bundle_containment_work_is_per_unique_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, file_count: int
) -> None:
    from apm_cli.primitives import discovery

    lock = LockFile()
    lock.local_deployed_files = [
        f"apm_modules/bundle/.apm/instructions/rule-{index}.instructions.md"
        for index in range(file_count)
    ]
    lock.write(tmp_path / "apm.lock.yaml")
    _memory(tmp_path, "bundle")
    calls = []
    original = discovery.ensure_path_within

    def track(path: Path, base: Path) -> Path:
        calls.append(path)
        return original(path, base)

    monkeypatch.setattr(discovery, "ensure_path_within", track)
    assert get_dependency_declaration_order(str(tmp_path)) == ["bundle"]
    assert calls.count(tmp_path / "apm_modules/bundle") == 1


def test_cross_drive_recovery_respects_selected_source_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, output = tmp_path / "source", tmp_path / "output"
    _manifest(source, ["owner/pkg"])
    _memory(source, "owner/pkg")
    monkeypatch.setattr(
        "apm_cli.compilation.claude_formatter.portable_link_relpath", lambda *_: None
    )
    result = ClaudeFormatter(str(output), str(source)).format_distributed(PrimitiveCollection(), {})
    assert not result.success
    message = "\n".join(result.errors)
    assert str(source / "apm_modules") in message
    assert str(output) in message
    assert "selected module store's drive" in message


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
