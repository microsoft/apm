"""Hermetic admission and failure controls for non-activating source packages."""

import io
import json
import os
import stat
import tarfile
import zipfile
from contextlib import contextmanager
from pathlib import Path

import pytest
from click.testing import CliRunner

from apm_cli.bundle.local_bundle import detect_local_bundle
from apm_cli.bundle.packer import pack_bundle
from apm_cli.bundle.unpacker import unpack_bundle
from apm_cli.commands.pack import pack_cmd, unpack_cmd
from apm_cli.models.apm_package import APMPackage
from apm_cli.models.manifest_contract import OPENAPM_V01_SCHEMA_URI
from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.utils.source_package import make_source_package

pytestmark = pytest.mark.component


@pytest.fixture
def author(tmp_path: Path) -> Path:
    root = tmp_path / "author"
    make_source_package(root)
    return root


def _pack(author: Path, **kwargs):
    return pack_bundle(author, author.parent / "dist", fmt="apm", source=True, **kwargs)


@pytest.mark.parametrize("representation", ["directory", "zip", "tar.gz"])
@pytest.mark.windows_compat
def test_exact_roundtrip_without_author(author: Path, representation: str) -> None:
    expected = {
        path.relative_to(author).as_posix(): path.read_bytes()
        for path in author.rglob("*")
        if path.is_file()
    }
    result = _pack(
        author,
        archive=representation != "directory",
        archive_format="zip" if representation == "directory" else representation,
    )
    author.rename(author.with_name("unavailable-author"))
    restored = author.parent / "restored"
    unpacked = unpack_bundle(result.bundle_path, restored, source=True)
    actual = {
        path.relative_to(restored).as_posix(): path.read_bytes()
        for path in restored.rglob("*")
        if path.is_file()
    }
    assert actual == expected
    assert unpacked.verified and len(unpacked.files) == 17
    assert not (restored / "EXECUTED").exists()


@pytest.mark.parametrize(
    "roots",
    [
        None,
        [],
        "auto",
        ["."],
        ["/contracts"],
        ["../contracts"],
        ["contracts/../checks"],
        ["contracts/"],
        ["contracts//nested"],
        ["C:contracts"],
        [r"contracts\nested"],
        ["contracts", "contracts/nested"],
        ["contracts", "CONTRACTS"],
        [".git"],
        ["apm_modules"],
        ["factory/apm.yml"],
        ["contracts/CON"],
        ["contracts/secret:stream"],
        ["contracts/%252e%252e"],
        [1],
        ["a/" * 129 + "b"],
        ["a" * 4097],
    ],
)
def test_reject_invalid_resource_declarations(author: Path, roots: object) -> None:
    with pytest.raises(ValueError):
        APMPackage.from_mapping(
            {"name": "factory", "version": "1", "resources": roots}, package_path=author
        )


def test_resources_are_not_a_normative_contract(author: Path) -> None:
    with pytest.raises(ValueError, match="working-draft"):
        APMPackage.from_mapping(
            {
                "name": "factory",
                "version": "1",
                "$schema": OPENAPM_V01_SCHEMA_URI,
                "resources": ["contracts"],
            },
            package_path=author,
        )


@pytest.mark.parametrize("fmt", [None, "agent-plugin", "claude-plugin", "apm"])
def test_deployment_formats_refuse_resource_loss(author: Path, fmt: str | None) -> None:
    with pytest.raises(ValueError, match="non-activating"):
        pack_bundle(author, author.parent / "dist", fmt=fmt)
    assert not (author.parent / "dist").exists()


@pytest.mark.parametrize(
    "problem", ["missing", "empty", "file", "metadata", "hidden", "symlink", "fifo"]
)
def test_pack_fails_closed_on_invalid_content(author: Path, problem: str) -> None:
    directory = author / "contracts"
    if problem in {"missing", "empty", "file"}:
        directory.rename(author / "unselected")
        if problem == "empty":
            directory.mkdir()
        elif problem == "file":
            directory.write_text("not a directory")
    elif problem == "metadata":
        (directory / "apm.yml").write_text("secret: do-not-pack\n")
    elif problem == "hidden":
        (directory / ".env").write_text("secret=do-not-pack\n")
    elif problem == "symlink":
        try:
            (directory / "link").symlink_to(author / "checks", target_is_directory=True)
        except OSError:
            pytest.skip("Platform does not permit symlink creation")
    else:
        if not hasattr(os, "mkfifo"):
            pytest.skip("FIFO creation is unavailable")
        os.mkfifo(directory / "pipe")
    with pytest.raises(ValueError):
        _pack(author)
    assert not (author.parent / "dist").exists()


@pytest.mark.parametrize("kind", ["bytes", "entries"])
def test_inventory_work_is_bounded(author: Path, monkeypatch, kind: str) -> None:
    from apm_cli.agent_plugins import assets

    monkeypatch.setattr(
        assets, "MAX_COMPONENT_ASSET_BYTES" if kind == "bytes" else "MAX_COMPONENT_ASSET_ENTRIES", 1
    )
    with pytest.raises(ValueError, match=r"budget|limit"):
        _pack(author)
    assert not (author.parent / "dist").exists()


@pytest.mark.parametrize(
    "damage", ["corrupt", "missing", "extra", "inventory", "marker", "version", "duplicate-key"]
)
def test_restore_refuses_tampering_before_output(author: Path, damage: str) -> None:
    bundle = _pack(author).bundle_path
    metadata = load_yaml(bundle / "apm.lock.yaml")
    path = bundle / "package/contracts/phase-0.contract.md"
    if damage == "corrupt":
        path.write_bytes(b"changed")
    elif damage == "missing":
        path.unlink()
    elif damage == "extra":
        (bundle / "plugin.json").write_text("{}")
    elif damage == "inventory":
        metadata["pack"]["bundle_files"] = {}
    elif damage == "marker":
        metadata["pack"]["source"] = "true"
    elif damage == "version":
        metadata["lockfile_version"] = "999"
    elif damage == "duplicate-key":
        with (bundle / "apm.lock.yaml").open("a") as handle:
            handle.write("pack: {}\n")
    if damage in {"inventory", "marker", "version"}:
        dump_yaml(metadata, bundle / "apm.lock.yaml")
    output = author.parent / "restored"
    with pytest.raises(ValueError):
        unpack_bundle(bundle, output, source=True)
    assert not output.exists()


@pytest.mark.parametrize("archive_format", ["zip", "tar.gz"])
@pytest.mark.parametrize(
    "member",
    ["../escape", "package/a", "PACKAGE/a", "package/a:stream", "package/CON", "package/a/child"],
)
def test_strict_archives_reject_ambiguous_members(
    tmp_path: Path, archive_format: str, member: str
) -> None:
    archive = tmp_path / f"hostile.{archive_format}"
    entries = [("package/a", b"first"), (member, b"second")]
    if archive_format == "zip":
        with zipfile.ZipFile(archive, "w") as handle:
            for name, payload in entries:
                handle.writestr(name, payload)
    else:
        with tarfile.open(archive, "w:gz") as handle:
            for name, payload in entries:
                info = tarfile.TarInfo(name)
                info.size = len(payload)
                handle.addfile(info, io.BytesIO(payload))
    with pytest.raises(ValueError):
        unpack_bundle(archive, tmp_path / "restored", source=True)
    assert not (tmp_path / "restored").exists()
    assert not (tmp_path / "escape").exists()


@pytest.mark.parametrize("archive", [False, True])
def test_marked_source_cannot_enter_deployment(author: Path, archive: bool) -> None:
    bundle = _pack(author, archive=archive).bundle_path
    with pytest.raises(ValueError, match="cannot be deployed"):
        unpack_bundle(bundle, author.parent / "consumer")
    with pytest.raises(ValueError, match="cannot be deployed"):
        detect_local_bundle(bundle)
    assert not (author.parent / "consumer").exists()


def test_source_marker_blocks_declarative_plugin_admission(author: Path) -> None:
    from apm_cli.models.validation import detect_package_type

    bundle = _pack(author).bundle_path
    (bundle / "plugin.json").write_text('{"name": "injected-plugin"}')
    with pytest.raises(ValueError, match="cannot be deployed"):
        detect_package_type(bundle)


def test_source_probe_preserves_legacy_metadata_semantics(tmp_path: Path) -> None:
    bundle = tmp_path / "legacy"
    bundle.mkdir()
    (bundle / "plugin.json").write_text('{"name": "legacy"}')
    (bundle / "apm.lock.yaml").write_text(
        "lockfile_version: '1'\n"
        "defaults: &base {format: apm}\n"
        "pack:\n  <<: *base\n  format: claude-plugin\n"
        + "# padding for an ordinary legacy lock "
        + "x" * (4 * 1024 * 1024)
        + "\n"
    )
    detected = detect_local_bundle(bundle)
    assert detected is not None
    assert detected.package_id == "legacy"
    assert detected.lockfile["pack"]["format"] == "claude-plugin"


@pytest.mark.parametrize("metadata", ["apm.yml", "apm.lock.yaml"])
def test_exact_metadata_names_and_presence_are_required(author: Path, metadata: str) -> None:
    (author / metadata).rename(author / metadata.upper())
    with pytest.raises(ValueError, match="exact"):
        _pack(author)


def test_unsupported_author_lock_version(author: Path) -> None:
    (author / "apm.lock.yaml").write_text("lockfile_version: '999'\n")
    with pytest.raises(ValueError, match="Unsupported lockfile version"):
        _pack(author)
    assert not (author.parent / "dist").exists()


@pytest.mark.parametrize("entry_type", [tarfile.FIFOTYPE, tarfile.SYMTYPE, tarfile.LNKTYPE])
def test_source_tar_rejects_special_members(tmp_path: Path, entry_type: bytes) -> None:
    archive = tmp_path / "special.tar.gz"
    with tarfile.open(archive, "w:gz") as handle:
        info = tarfile.TarInfo("special")
        info.type = entry_type
        info.linkname = "other"
        handle.addfile(info)
    with pytest.raises(ValueError, match="Nonregular"):
        unpack_bundle(archive, tmp_path / "restored", source=True)
    assert not (tmp_path / "restored").exists()


@pytest.mark.parametrize("archive_format", ["zip", "tar.gz"])
def test_source_archives_reject_special_permission_bits(
    tmp_path: Path, archive_format: str
) -> None:
    archive = tmp_path / f"permissions.{archive_format}"
    if archive_format == "zip":
        with zipfile.ZipFile(archive, "w") as handle:
            info = zipfile.ZipInfo("unsafe")
            info.external_attr = (stat.S_IFREG | 0o4755) << 16
            handle.writestr(info, b"not executable")
    else:
        with tarfile.open(archive, "w:gz") as handle:
            info = tarfile.TarInfo("unsafe")
            info.mode = 0o4755
            handle.addfile(info)
    with pytest.raises(ValueError, match="special permission bits"):
        unpack_bundle(archive, tmp_path / "restored", source=True)
    assert not (tmp_path / "restored").exists()


@pytest.mark.parametrize("option", ["--force", "--skip-verify"])
def test_source_cli_rejects_verification_bypass(author: Path, option: str) -> None:
    bundle = _pack(author).bundle_path
    output = author.parent / "restored"
    result = CliRunner().invoke(unpack_cmd, ["--source", str(bundle), "-o", str(output), option])
    assert result.exit_code == 1
    assert "cannot bypass verification" in result.output
    assert not output.exists()


def test_no_execution_and_no_other_build_producers(author: Path, monkeypatch) -> None:
    def forbidden(*args, **kwargs):
        pytest.fail("Source operation crossed an execution, network, or plugin boundary")

    monkeypatch.setattr("subprocess.run", forbidden)
    monkeypatch.setattr("subprocess.Popen", forbidden)
    monkeypatch.setattr("requests.sessions.Session.request", forbidden)
    monkeypatch.setattr("apm_cli.core.build_orchestrator.PluginManifestProducer.produce", forbidden)
    monkeypatch.setattr("apm_cli.core.build_orchestrator.MarketplaceProducer.produce", forbidden)
    monkeypatch.chdir(author)
    runner = CliRunner()
    packed = runner.invoke(pack_cmd, ["--format", "apm", "--source"])
    assert packed.exit_code == 0, packed.output
    assert "apm unpack --source" in packed.output
    assert "apm install" not in packed.output
    restored = runner.invoke(
        unpack_cmd, ["--source", "build/software-factory-1.0.0", "-o", "../restored"]
    )
    assert restored.exit_code == 0, restored.output
    assert not (author / ".github").exists()
    assert not (author / "EXECUTED").exists()


def test_existing_output_and_bypasses_are_rejected(author: Path) -> None:
    bundle = _pack(author).bundle_path
    existing = author.parent / "consumer"
    existing.mkdir()
    (existing / "keep").write_bytes(b"mine")
    for options in ({}, {"force": True}, {"skip_verify": True}, {"dry_run": True}):
        with pytest.raises(ValueError):
            unpack_bundle(bundle, existing, source=True, **options)
    assert list(existing.iterdir()) == [existing / "keep"]
    assert (existing / "keep").read_bytes() == b"mine"
    with pytest.raises(ValueError, match="already exists"):
        _pack(author)


def test_dry_run_validates_without_writing(author: Path) -> None:
    preview = _pack(author, dry_run=True)
    assert len(preview.files) == 17
    assert not (author.parent / "dist").exists()
    bundle = _pack(author).bundle_path
    output = author.parent / "preview"
    assert unpack_bundle(bundle, output, source=True, dry_run=True).verified
    assert not output.exists()


def test_nested_directory_selection_keeps_package_relative_identity(author: Path) -> None:
    (author / "factory").mkdir()
    (author / "contracts").rename(author / "factory/contracts")
    data = load_yaml(author / "apm.yml")
    data["resources"] = ["factory/contracts", "checks"]
    dump_yaml(data, author / "apm.yml")
    bundle = _pack(author).bundle_path
    output = author.parent / "restored"
    restored = unpack_bundle(bundle, output, source=True)
    assert "factory/contracts/phase-0.contract.md" in restored.files
    assert (output / "factory/contracts/phase-0.contract.md").read_bytes() == (
        author / "factory/contracts/phase-0.contract.md"
    ).read_bytes()
    assert not (output / "contracts").exists()


def test_copy_race_fails_before_publishing(author: Path, monkeypatch) -> None:
    from apm_cli.agent_plugins.assets import AssetInventory

    bundle = _pack(author).bundle_path
    original = AssetInventory.open_verified_asset

    @contextmanager
    def changed(self, expected):
        with original(self, expected) as handle:
            if expected.path == "contracts/phase-0.contract.md":
                expected.source.path.write_bytes(b"x" * expected.size)
            yield handle

    monkeypatch.setattr(AssetInventory, "open_verified_asset", changed)
    output = author.parent / "restored"
    with pytest.raises(ValueError, match="copy failed integrity"):
        unpack_bundle(bundle, output, source=True)
    assert not output.exists()


@pytest.mark.parametrize("archive_format", ["zip", "tar.gz"])
def test_source_roundtrip_preserves_private_metadata_mode(
    author: Path, archive_format: str
) -> None:
    lock = author / "apm.lock.yaml"
    check = author / "checks/check-0.sh"
    lock.chmod(0o600)
    check.chmod(0o755)
    expected = (stat.S_IMODE(lock.stat().st_mode), stat.S_IMODE(check.stat().st_mode))
    bundle = _pack(author, archive=True, archive_format=archive_format).bundle_path
    output = author.parent / "restored"
    unpack_bundle(bundle, output, source=True)
    actual = (
        stat.S_IMODE((output / "apm.lock.yaml").stat().st_mode),
        stat.S_IMODE((output / "checks/check-0.sh").stat().st_mode),
    )
    assert actual == expected


def test_source_json_reports_exact_payload_paths(author: Path, monkeypatch) -> None:
    monkeypatch.chdir(author)
    result = CliRunner().invoke(pack_cmd, ["--format", "apm", "--source", "--dry-run", "--json"])
    assert result.exit_code == 0, result.output
    body = json.loads(result.stdout)
    assert body["ok"] is True and body["dry_run"] is True
    assert body["bundle"]["source"] is True
    assert len(body["bundle"]["files"]) == 17
    assert {
        "apm.yml",
        "apm.lock.yaml",
        "checks/check-0.sh",
        "contracts/phase-0.contract.md",
    } <= set(body["bundle"]["files"])
    assert not (author / "build").exists()
