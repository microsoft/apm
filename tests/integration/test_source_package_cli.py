"""Real installed Python CLI roundtrips; no released/native binary claims."""

import hashlib
import os
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

from apm_cli.bundle.packer import pack_bundle
from tests.utils.source_package import make_source_package

pytestmark = pytest.mark.e2e


@pytest.mark.parametrize("archive_format", ["zip", "tar.gz"])
def test_source_archive_survives_author_removal(
    tmp_path: Path, apm_engine_command: tuple[str, ...], archive_format: str
) -> None:
    author = tmp_path / "author"
    expected = make_source_package(author)
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    (consumer / "seed.txt").write_bytes(b"consumer-owned")
    home = tmp_path / "home"
    home.mkdir()
    environment = os.environ | {
        "HOME": str(home),
        "USERPROFILE": str(home),
        "APM_DISABLE_UPDATE_CHECK": "1",
    }

    def run(cwd: Path, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [*apm_engine_command, *args],
            cwd=cwd,
            env=environment,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    packed = run(
        author,
        "pack",
        "--format",
        "apm",
        "--source",
        "--archive",
        "--archive-format",
        archive_format,
        "-o",
        "../artifacts",
    )
    assert packed.returncode == 0, packed.stdout + packed.stderr
    archive = tmp_path / f"artifacts/software-factory-1.0.0.{archive_format}"
    assert archive.is_file()
    shutil.rmtree(author)
    restored = run(consumer, "unpack", "--source", str(archive), "-o", "acquired")
    assert restored.returncode == 0, restored.stdout + restored.stderr
    actual = {
        path.relative_to(consumer / "acquired").as_posix(): path.read_bytes()
        for path in (consumer / "acquired").rglob("*")
        if path.is_file()
    }
    assert actual == expected
    assert {path: hashlib.sha256(data).hexdigest() for path, data in actual.items()} == {
        path: hashlib.sha256(data).hexdigest() for path, data in expected.items()
    }
    assert (consumer / "seed.txt").read_bytes() == b"consumer-owned"
    assert sorted(path.name for path in consumer.iterdir()) == ["acquired", "seed.txt"]
    rejected = run(consumer, "install", str(archive))
    assert rejected.returncode != 0
    assert "apm unpack --source" in rejected.stdout + rejected.stderr
    assert sorted(path.name for path in consumer.iterdir()) == ["acquired", "seed.txt"]


@pytest.mark.parametrize("damage", ["corrupt", "missing", "unsafe"])
def test_real_cli_rejects_damaged_source_archives(
    tmp_path: Path, apm_engine_command: tuple[str, ...], damage: str
) -> None:
    author = tmp_path / "author"
    make_source_package(author)
    bundle = pack_bundle(author, tmp_path / "artifacts", fmt="apm", source=True, archive=True)
    damaged = tmp_path / "damaged.zip"
    resource = "software-factory-1.0.0/package/contracts/phase-0.contract.md"
    with zipfile.ZipFile(bundle.bundle_path) as original, zipfile.ZipFile(damaged, "w") as target:
        for item in original.infolist():
            payload = original.read(item)
            if item.filename == resource:
                if damage == "missing":
                    continue
                if damage == "corrupt":
                    payload = b"changed after publication"
            target.writestr(item, payload)
        if damage == "unsafe":
            target.writestr("../escape", b"must never escape")
    shutil.rmtree(author)
    output = tmp_path / "restore"
    result = subprocess.run(
        [*apm_engine_command, "unpack", "--source", str(damaged), "-o", str(output)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert not output.exists()
    assert not (tmp_path / "escape").exists()
