"""Release publication uses the same checked archive as native validation."""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import tarfile
import zipfile
from pathlib import Path

import pytest

from scripts.package_release import (
    BINARY_NAMES,
    archive_name,
    file_digest,
    main,
    package,
    require_binary_identity,
    verify_extract,
)

pytestmark = pytest.mark.component
SHA = "a" * 40


@pytest.fixture
def candidate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, Path]:
    """Create a minimal versioned native bundle without an external build."""
    dist = tmp_path / "dist"
    archives = tmp_path / "release-assets"
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nversion = "1.2.3"\n', encoding="ascii")
    monkeypatch.setattr(
        "scripts.package_release.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args, 0, stdout=f"Agent Package Manager (APM) CLI version 1.2.3 ({SHA[:9]})\n"
        ),
    )
    return dist, archives, pyproject


@pytest.mark.parametrize("operation", ["pack", "verify-extract"])
def test_cli_reports_the_completed_candidate_path(
    candidate: tuple[Path, Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    operation: str,
) -> None:
    dist, archives, pyproject = candidate
    binary_name = "apm-linux-x86_64"
    bundle = dist / binary_name
    bundle.mkdir(parents=True)
    (bundle / "apm").write_bytes(b"candidate")
    expected = archives / archive_name(binary_name)
    action = "Packaged"
    if operation == "verify-extract":
        package(binary_name, dist, archives, SHA, pyproject)
        dist = tmp_path / "isolated"
        expected = dist / binary_name / "apm"
        action = "Verified"
    monkeypatch.setattr(
        "sys.argv",
        [
            "package_release.py",
            operation,
            "--binary-name",
            binary_name,
            "--sha",
            SHA,
            "--dist",
            str(dist),
            "--archives",
            str(archives),
            "--pyproject",
            str(pyproject),
        ],
    )

    main()

    assert capsys.readouterr().out == f"[+] {action} {binary_name}: {expected.resolve()}\n"
    assert expected.is_file()


@pytest.mark.parametrize("binary_name", BINARY_NAMES)
def test_archive_round_trip_keeps_candidate_bytes_and_hidden_files(
    candidate: tuple[Path, Path, Path], tmp_path: Path, binary_name: str
) -> None:
    dist, archives, pyproject = candidate
    bundle = dist / binary_name
    bundle.mkdir(parents=True)
    executable = bundle / ("apm.exe" if binary_name.endswith("windows-x86_64") else "apm")
    executable.write_bytes(b"candidate-native-executable")
    hidden = bundle / "_internal" / ".apm"
    hidden.mkdir(parents=True)
    (hidden / "fixture").write_bytes(b"hidden")
    metadata = package(binary_name, dist, archives, SHA, pyproject)

    extracted = verify_extract(binary_name, archives, tmp_path / "isolated", SHA)

    assert metadata["version"] == "1.2.3"
    assert extracted.read_bytes() == executable.read_bytes()
    assert (extracted.parent / "_internal" / ".apm" / "fixture").read_bytes() == b"hidden"
    assert (archives / archive_name(binary_name)).is_file()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("sha", "b" * 40),
        ("archive", "../unexpected.zip"),
        ("binary_name", "apm-linux-arm64"),
        ("archive_sha256", "0" * 64),
        ("schema_version", 2),
    ],
)
def test_candidate_identity_or_digest_drift_fails_before_extraction(
    candidate: tuple[Path, Path, Path], tmp_path: Path, field: str, value: str | int
) -> None:
    dist, archives, pyproject = candidate
    binary_name = "apm-linux-x86_64"
    (dist / binary_name).mkdir(parents=True)
    (dist / binary_name / "apm").write_bytes(b"candidate")
    metadata = package(binary_name, dist, archives, SHA, pyproject)
    metadata[field] = value
    (archives / f"{binary_name}.json").write_text(json.dumps(metadata), encoding="ascii")

    with pytest.raises(ValueError, match="Candidate"):
        verify_extract(binary_name, archives, tmp_path / "isolated", SHA)

    assert not (tmp_path / "isolated").exists()


@pytest.mark.parametrize(
    "output",
    [
        f"Agent Package Manager (APM) CLI version 1.2.2 ({SHA[:9]})",
        "Agent Package Manager (APM) CLI version 1.2.3 (bbbbbbbbb)",
        "Agent Package Manager (APM) CLI version 1.2.3",
    ],
)
def test_published_or_unbound_binary_cannot_become_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, output: str
) -> None:
    monkeypatch.setattr(
        "scripts.package_release.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, stdout=output),
    )
    with pytest.raises(ValueError, match="version/build SHA"):
        require_binary_identity(tmp_path / "apm", "1.2.3", SHA)


def test_tampered_archive_is_not_extracted(
    candidate: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    dist, archives, pyproject = candidate
    binary_name = "apm-linux-x86_64"
    (dist / binary_name).mkdir(parents=True)
    (dist / binary_name / "apm").write_bytes(b"candidate")
    package(binary_name, dist, archives, SHA, pyproject)
    (archives / archive_name(binary_name)).write_bytes(b"tampered")

    with pytest.raises(ValueError, match="checksum mismatch"):
        verify_extract(binary_name, archives, tmp_path / "isolated", SHA)

    assert not (tmp_path / "isolated").exists()


@pytest.mark.parametrize("binary_name", ["apm-linux-x86_64", "apm-windows-x86_64"])
@pytest.mark.parametrize(
    "suffix", ["/../escape", "/nested/../../escape", "/C:stream", r"/..\escape"]
)
def test_matching_digest_does_not_allow_archive_path_escape(
    candidate: tuple[Path, Path, Path], tmp_path: Path, binary_name: str, suffix: str
) -> None:
    dist, archives, pyproject = candidate
    (dist / binary_name).mkdir(parents=True)
    executable = "apm.exe" if binary_name.startswith("apm-windows") else "apm"
    (dist / binary_name / executable).write_bytes(b"candidate")
    metadata = package(binary_name, dist, archives, SHA, pyproject)
    archive = archives / archive_name(binary_name)
    if archive.suffix == ".zip":
        with zipfile.ZipFile(archive, "w") as target:
            target.writestr(binary_name + suffix, b"unsafe")
    else:
        with tarfile.open(archive, "w:gz") as target:
            target.addfile(tarfile.TarInfo(binary_name + suffix))
    digest = file_digest(archive)
    metadata["archive_sha256"] = digest
    (archives / f"{binary_name}.json").write_text(json.dumps(metadata), encoding="ascii")
    (archives / f"{archive.name}.sha256").write_text(
        f"{digest}  {archive.name}\n", encoding="ascii"
    )

    with pytest.raises(ValueError, match="unsafe member path"):
        verify_extract(binary_name, archives, tmp_path / "isolated", SHA)

    assert not (tmp_path / "isolated" / binary_name).exists()


@pytest.mark.windows_compat
@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_windows_build_injects_sha_into_a_multiline_version_module(newline: str) -> None:
    pwsh = shutil.which("pwsh")
    if pwsh is None:
        pytest.skip("PowerShell is required for the native build-script regression")
    script = Path(__file__).resolve().parents[3] / "scripts/windows/build-binary.ps1"
    assignment = next(
        line.strip()
        for line in script.read_text("utf-8").splitlines()
        if line.strip().startswith("$newContent = ")
    )
    original = newline.join(['"""Version metadata."""', "__BUILD_SHA__ = None", ""])
    probe = (
        "$ErrorActionPreference = 'Stop'; "
        "$originalContent = [Text.Encoding]::UTF8.GetString("
        "[Convert]::FromBase64String($env:PROBE_CONTENT)); "
        f"$BuildSHA = '{SHA[:9]}'; {assignment}; "
        "$newContent | ConvertTo-Json -Compress"
    )
    result = subprocess.run(
        [pwsh, "-NoProfile", "-NonInteractive", "-Command", probe],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
        env={
            **os.environ,
            "PROBE_CONTENT": base64.b64encode(original.encode("utf-8")).decode("ascii"),
        },
    )
    updated = json.loads(result.stdout)
    assert f'__BUILD_SHA__ = "{SHA[:9]}"' in updated
    assert "__BUILD_SHA__ = None" not in updated
    assert updated.startswith('"""Version metadata."""')
