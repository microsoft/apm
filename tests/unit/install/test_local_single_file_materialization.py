"""Local admission and shared single-file materialization contracts."""

from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from apm_cli.deps.github_downloader import GitHubPackageDownloader
from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.install.drift import _materialize_install_path
from apm_cli.install.phases.local_content import (
    _copy_local_package,
    compute_local_package_hash,
    read_local_package_source,
)
from apm_cli.install.validation import _local_path_failure_reason, _validate_local_package
from apm_cli.integration.mcp_config_view import CurrentMcpConfigView
from apm_cli.models.apm_package import APMPackage
from apm_cli.models.dependency import DependencyReference
from apm_cli.utils.yaml_io import load_yaml
from tests.utils.artifact_snapshot import ArtifactSnapshot, assert_unchanged

pytestmark = pytest.mark.component


@pytest.mark.parametrize("suffix", ["agent", "instructions", "prompt"])
@pytest.mark.parametrize("nested", [False, True], ids=["direct", "nested"])
def test_mcp_view_reads_current_file_through_shared_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, suffix: str, nested: bool
) -> None:
    """Never infer MCP from frontmatter or trust an installed snapshot over its source."""
    root = tmp_path / "consumer"
    root.mkdir()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr("apm_cli.config.get_apm_temp_dir", lambda: str(scratch))
    parent = tmp_path / "parent" if nested else root
    parent.mkdir(exist_ok=True)
    (parent / "apm.yml").write_text("name: parent\nversion: 1.0.0\n", encoding="utf-8")
    source = parent / f"selected.{suffix}.md"
    source.write_bytes(
        b"---\ndescription: Selected file\ndependencies:\n  mcp: [not-a-manifest]\n---\n# Guidance\n"
    )
    dependency = LockedDependency(
        repo_url=f"_local/{source.name}",
        source="local",
        local_path=f"./{source.name}",
        package_type="apm_package",
        depth=2 if nested else 1,
        resolved_by="_local/parent" if nested else None,
        declaring_parent="../parent" if nested else None,
        anchored_local_path=source.as_posix() if nested else None,
    )
    lock = LockFile(dependencies={dependency.get_unique_key(): dependency})
    if nested:
        parent_dependency = LockedDependency(
            repo_url="_local/parent", source="local", local_path="../parent"
        )
        lock.dependencies[parent_dependency.get_unique_key()] = parent_dependency
    modules = root / "apm_modules"
    installed = dependency.to_dependency_ref().get_install_path(modules)
    installed.mkdir(parents=True)
    (installed / "apm.yml").write_text(
        "name: stale-cache\nversion: 1.0.0\ndependencies:\n  mcp: [stale-server]\n",
        encoding="utf-8",
    )
    manifest = APMPackage(name="consumer", version="1.0.0", package_path=root)
    before = ArtifactSnapshot.capture(tmp_path)
    view = CurrentMcpConfigView.derive(manifest, lock, modules, trust_transitive_self_defined=True)
    assert view.problems == ()
    assert view.dependencies == ()
    assert view.configs == {}
    assert_unchanged(before, ArtifactSnapshot.capture(tmp_path))

    source.write_bytes(b"---\ndescription: [invalid\n---\n# Guidance\n")
    before = ArtifactSnapshot.capture(tmp_path)
    invalid = CurrentMcpConfigView.derive(
        manifest, lock, modules, trust_transitive_self_defined=True
    )
    assert len(invalid.problems) == 1
    assert "Invalid primitive content" in invalid.problems[0].message
    assert_unchanged(before, ArtifactSnapshot.capture(tmp_path))
    source.unlink()
    before = ArtifactSnapshot.capture(tmp_path)
    missing = CurrentMcpConfigView.derive(
        manifest, lock, modules, trust_transitive_self_defined=True
    )
    assert len(missing.problems) == 1
    assert "not found" in missing.problems[0].message
    assert_unchanged(before, ArtifactSnapshot.capture(tmp_path))


@pytest.mark.parametrize(
    ("suffix", "directory"),
    [(".agent.md", "agents"), (".instructions.md", "instructions"), (".prompt.md", "prompts")],
)
def test_local_and_remote_materialize_the_same_selected_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, suffix: str, directory: str
) -> None:
    """Acquisition differs, but package layout and selected bytes do not."""
    monkeypatch.chdir(tmp_path)
    filename = f"xyz{suffix}"
    content = b"---\ndescription: Selected primitive\n---\n# Selected\nGuidance.\n"
    source = tmp_path / filename
    source.write_bytes(content)
    (tmp_path / "companion.txt").write_text("Do not harvest this asset.", encoding="utf-8")
    local = DependencyReference.parse(f"./{filename}")
    remote = DependencyReference.parse(f"owner/repo/primitives/{filename}")
    local_target = tmp_path / "apm_modules" / "_local" / filename
    remote_target = tmp_path / "remote"
    logger = Mock()

    assert _validate_local_package(local, logger)
    assert _local_path_failure_reason(local) is None
    copied = _copy_local_package(
        local, local_target, tmp_path, project_root=tmp_path, logger=logger
    )
    downloader = GitHubPackageDownloader()
    with (
        patch.object(downloader, "_resolve_commit_sha_for_ref", return_value="a" * 40),
        patch.object(downloader, "download_raw_file", return_value=content),
    ):
        remote_info = downloader.download_virtual_file_package(remote, remote_target)

    assert copied == local_target
    for target in (local_target, remote_target):
        primitive = target / ".apm" / directory / filename
        assert primitive.read_bytes() == content
        assert not primitive.is_symlink()
        assert sorted(
            p.relative_to(target).as_posix() for p in target.rglob("*") if p.is_file()
        ) == [
            f".apm/{directory}/{filename}",
            "apm.yml",
        ]
        assert load_yaml(target / "apm.yml")["description"] == "Selected primitive"
    assert load_yaml(local_target / "apm.yml")["name"] == filename
    assert remote_info.package.name == "repo-xyz"
    assert remote_info.resolved_reference.resolved_commit == "a" * 40
    assert local.get_unique_key() == f"./{filename}"
    assert source.read_bytes() == content
    logger.error.assert_not_called()


@pytest.mark.parametrize("suffix", ["agent", "instructions", "prompt"])
@pytest.mark.parametrize("bom", [b"", b"\xef\xbb\xbf"], ids=["utf8", "utf8-bom"])
def test_local_file_admission_preserves_encoded_bytes(
    tmp_path: Path, suffix: str, bom: bytes
) -> None:
    """Canonical decoding validates frontmatter without changing the selected bytes."""
    source = tmp_path / f"encoded.{suffix}.md"
    body = b"---\ndescription: Encoded guidance\n---\n# Guidance\nPreserve caf\xc3\xa9.\n"
    content = bom + body
    source.write_bytes(content)
    dependency = DependencyReference.parse(f"./{source.name}")

    admitted = read_local_package_source(dependency, tmp_path)
    assert admitted.path == source.resolve()
    assert admitted.content == content
    target = tmp_path / "apm_modules" / "_local" / source.name
    copied = _copy_local_package(dependency, target, tmp_path, project_root=tmp_path, logger=Mock())
    assert copied == target
    primitives = list((target / ".apm").rglob(source.name))
    assert len(primitives) == 1
    assert primitives[0].read_bytes() == content
    assert source.read_bytes() == content


@pytest.mark.parametrize(
    ("filename", "content", "diagnostic"),
    [
        ("missing.agent.md", None, "path does not exist"),
        ("unsupported.md", b"# Not a single-file format\n", "Unsupported single-file dependency"),
        ("invalid.prompt.md", b"---\ndescription: [bad\n---\nBody\n", "Invalid primitive content"),
        ("invalid.instructions.md", b"\xff", "Invalid primitive content"),
        (
            "bom-invalid.prompt.md",
            b"\xef\xbb\xbf---\ndescription: [bad\n---\nBody\n",
            "Invalid primitive content",
        ),
        ("bom-invalid.instructions.md", b"\xef\xbb\xbf\xff", "Invalid primitive content"),
        (
            "document.agent.md",
            b"---\ntitle: Documentation\n---\nBody\n",
            "Invalid primitive content",
        ),
    ],
)
def test_bad_input_preserves_prior_materialized_package(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    filename: str,
    content: bytes | None,
    diagnostic: str,
) -> None:
    """Neither CLI preflight nor copying may admit bad input or erase old bytes."""
    monkeypatch.chdir(tmp_path)
    source = tmp_path / filename
    if content is not None:
        source.write_bytes(content)
    dependency = DependencyReference.parse(f"./{filename}")
    destination = tmp_path / "apm_modules" / "_local" / filename
    destination.mkdir(parents=True)
    prior = destination / "prior.txt"
    prior.write_bytes(b"Keep prior package bytes.\n")
    logger = Mock()

    assert not _validate_local_package(dependency, logger)
    assert diagnostic in _local_path_failure_reason(dependency)
    assert (
        _copy_local_package(dependency, destination, tmp_path, project_root=tmp_path, logger=logger)
        is None
    )
    assert prior.read_bytes() == b"Keep prior package bytes.\n"
    logger.error.assert_called_once()
    assert diagnostic in logger.error.call_args.args[0]


def test_unreadable_file_preserves_prior_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A read failure is diagnosed before any replacement write."""
    source = tmp_path / "private.prompt.md"
    source.write_bytes(b"# Private\n")
    destination = tmp_path / "apm_modules" / "_local" / source.name
    destination.mkdir(parents=True)
    prior = destination / "prior.txt"
    prior.write_text("untouched", encoding="utf-8")
    dependency = DependencyReference.parse(source.as_posix())
    logger = Mock()
    read_bytes = Path.read_bytes

    def read(path: Path) -> bytes:
        if path == source:
            raise PermissionError("source permission denied")
        return read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", read)
    assert (
        _copy_local_package(dependency, destination, tmp_path, project_root=tmp_path, logger=logger)
        is None
    )
    assert prior.read_text(encoding="utf-8") == "untouched"
    assert "Cannot read local package" in logger.error.call_args.args[0]


def test_selected_file_symlink_does_not_expand_directory_permissions(tmp_path: Path) -> None:
    """Single-file selection does not turn sibling assets into an implicit bundle."""
    source = tmp_path / "source.agent.md"
    source.write_bytes(b"# Source\n")
    selected = tmp_path / "selected.agent.md"
    selected.symlink_to(source)
    dependency = DependencyReference.parse(selected.as_posix())
    destination = tmp_path / "apm_modules" / "_local" / selected.name
    logger = Mock()
    assert (
        _copy_local_package(dependency, destination, tmp_path, project_root=tmp_path, logger=logger)
        is None
    )
    assert "is a symlink" in logger.error.call_args.args[0]
    assert not destination.exists()
    assert source.read_bytes() == b"# Source\n"


@pytest.mark.parametrize(
    ("suffix", "directory"),
    [(".agent.md", "agents"), (".instructions.md", "instructions"), (".prompt.md", "prompts")],
)
def test_local_file_hash_matches_copied_package_without_changing_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, suffix: str, directory: str
) -> None:
    """Frozen verification hashes the same two-file image as ordinary install."""
    from hashlib import sha256

    source = tmp_path / f"selected{suffix}"
    source.write_bytes(b"---\ndescription: Snapshot parity\n---\n# Current guidance\n")
    dependency = DependencyReference.parse(source.as_posix())
    destination = tmp_path / "installed"
    scratch = tmp_path / "temp"
    scratch.mkdir()
    monkeypatch.setattr("apm_cli.config.get_apm_temp_dir", lambda: scratch)
    assert (
        _copy_local_package(dependency, destination, tmp_path, project_root=tmp_path, logger=Mock())
        == destination
    )
    digest = sha256()
    for relative in (f".apm/{directory}/{source.name}", "apm.yml"):
        digest.update(relative.encode("utf-8"))
        digest.update((destination / relative).read_bytes())
    before = ArtifactSnapshot.capture(tmp_path)
    assert compute_local_package_hash(source) == f"sha256:{digest.hexdigest()}"
    assert_unchanged(before, ArtifactSnapshot.capture(tmp_path))
    assert list(scratch.iterdir()) == []
    source.write_bytes(source.read_bytes().replace(b"Current", b"Changed"))
    assert compute_local_package_hash(source) != f"sha256:{digest.hexdigest()}"


def test_local_file_hash_rejects_selected_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hash check cannot bypass the file-admission boundary."""
    source = tmp_path / "actual.prompt.md"
    source.write_bytes(b"# Prompt\n")
    selected = tmp_path / "selected.prompt.md"
    selected.symlink_to(source)
    scratch = tmp_path / "temp"
    scratch.mkdir()
    monkeypatch.setattr("apm_cli.config.get_apm_temp_dir", lambda: scratch)
    before = ArtifactSnapshot.capture(tmp_path)
    with pytest.raises(ValueError, match="is a symlink"):
        compute_local_package_hash(selected)
    assert_unchanged(before, ArtifactSnapshot.capture(tmp_path))
    assert list(scratch.iterdir()) == []


@pytest.mark.parametrize("nested", [False, True], ids=["direct", "nested"])
def test_audit_materializes_selected_file_in_scratch_and_preserves_admission(
    tmp_path: Path, nested: bool
) -> None:
    """Audit uses the source declaration, not live package bytes or a resolved symlink."""
    project = tmp_path / "consumer"
    project.mkdir()
    parent_source = tmp_path / "parent" if nested else project
    parent_source.mkdir(exist_ok=True)
    selected = parent_source / "guide.instructions.md"
    content = b"---\ndescription: Audit parity\n---\n# Current guidance\n"
    selected.write_bytes(content)
    parent = LockedDependency(repo_url="_local/parent", source="local", local_path="../parent")
    dependency = LockedDependency(
        repo_url="_local/guide.instructions.md",
        source="local",
        local_path="./guide.instructions.md",
        resolved_by=parent.repo_url if nested else None,
    )
    lock = LockFile(dependencies={dependency.get_unique_key(): dependency})
    if nested:
        lock.dependencies[parent.get_unique_key()] = parent
    scratch = tmp_path / "scratch" / "apm_modules"
    path = _materialize_install_path(dependency, project, scratch, cache_only=True, lockfile=lock)
    assert path.is_relative_to(scratch)
    assert (path / ".apm/instructions/guide.instructions.md").read_bytes() == content
    assert selected.read_bytes() == content
    assert not (project / "apm_modules").exists()
    selected.rename(parent_source / "real.instructions.md")
    selected.symlink_to(parent_source / "real.instructions.md")
    with pytest.raises(ValueError, match="is a symlink"):
        _materialize_install_path(dependency, project, scratch, cache_only=True, lockfile=lock)
    assert (path / ".apm/instructions/guide.instructions.md").read_bytes() == content
