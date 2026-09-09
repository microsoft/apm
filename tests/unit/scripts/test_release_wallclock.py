"""Read-only release wall-clock evidence rejects modeled or unsafe inputs."""

from __future__ import annotations

import io
import json
import stat
import tarfile
import zipfile
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from scripts import release_wallclock
from scripts.package_release import BINARY_NAMES, archive_name, executable_name, file_digest
from scripts.release_wallclock import compare_runs, record_job, verify_artifacts

pytestmark = pytest.mark.component

BASE_SHA = "a" * 40
PROPOSED_SHA = "b" * 40
BASE_CONTROLLER = "c" * 40
PROPOSED_CONTROLLER = "d" * 40
BASE_RUN_HEAD = BASE_CONTROLLER
PROPOSED_RUN_HEAD = "e" * 40
VERSION = "0.30.0"
ORIGIN = datetime(2026, 9, 8, tzinfo=timezone.utc)


def _at(seconds: int) -> str:
    return (ORIGIN + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


def _source_root(tmp_path: Path) -> Path:
    root = tmp_path / "source"
    (root / ".github" / "workflows").mkdir(parents=True)
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "apm-cli"\nversion = "{VERSION}"\n', encoding="ascii"
    )
    (root / "uv.lock").write_text("lock\n", encoding="ascii")
    (root / ".github" / "workflows" / "build-release.yml").write_text(
        "name: release\n", encoding="ascii"
    )
    return root


def _set_workflow_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    sha: str,
    run_id: int = 101,
    *,
    event_name: str = "push",
    run_head_sha: str | None = None,
) -> None:
    run_head_sha = run_head_sha or sha
    payload = (
        {"pull_request": {"head": {"sha": run_head_sha}}}
        if event_name == "pull_request"
        else {"after": run_head_sha}
    )
    event_path = tmp_path / f"event-{run_id}.json"
    event_path.write_text(json.dumps(payload), encoding="ascii")
    monkeypatch.setenv("GITHUB_SHA", sha)
    monkeypatch.setenv("GITHUB_RUN_ID", str(run_id))
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "1")
    monkeypatch.setenv("GITHUB_EVENT_NAME", event_name)
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event_path))
    monkeypatch.setenv("GITHUB_JOB", "verify-wallclock")
    monkeypatch.setenv("RUNNER_NAME", "runner-1")
    monkeypatch.setenv("ImageVersion", "20260901.1")


def _tar_archive(path: Path, binary_name: str, payload: bytes = b"native") -> str:
    executable = f"{binary_name}/{executable_name(binary_name)}"
    with tarfile.open(path, "w:gz") as target:
        directory = tarfile.TarInfo(binary_name)
        directory.type = tarfile.DIRTYPE
        directory.mode = 0o755
        target.addfile(directory)
        info = tarfile.TarInfo(executable)
        info.mode = 0o755
        info.size = len(payload)
        target.addfile(info, io.BytesIO(payload))
        hidden = tarfile.TarInfo(f"{binary_name}/_internal/.apm")
        hidden.mode = 0o644
        hidden.size = 1
        target.addfile(hidden, io.BytesIO(b"x"))
    return file_digest(path)


def _zip_archive(path: Path, binary_name: str, payload: bytes = b"native") -> str:
    executable = f"{binary_name}/{executable_name(binary_name)}"
    with zipfile.ZipFile(path, "w") as target:
        info = zipfile.ZipInfo(executable)
        info.create_system = 3
        info.external_attr = (stat.S_IFREG | 0o644) << 16
        target.writestr(info, payload)
        target.writestr(f"{binary_name}/_internal/.apm", b"x")
    return file_digest(path)


def _native_artifacts(root: Path, side: str, source_sha: str) -> Path:
    root.mkdir(parents=True)
    for binary_name in BINARY_NAMES:
        archive = archive_name(binary_name)
        archive_path = root / archive
        payload = f"native-{binary_name}".encode("ascii")
        if archive.endswith(".zip"):
            digest = _zip_archive(archive_path, binary_name, payload)
        else:
            digest = _tar_archive(archive_path, binary_name, payload)
        (root / f"{archive}.sha256").write_text(f"{digest}  {archive}\n", encoding="ascii")
        executable_digest = (
            release_wallclock._zip_member_digest(archive_path, binary_name)[0]
            if archive.endswith(".zip")
            else release_wallclock._tar_member_digest(archive_path, binary_name)[0]
        )
        if side == "proposed":
            metadata = {
                "schema_version": 1,
                "sha": source_sha,
                "version": VERSION,
                "binary_name": binary_name,
                "archive": archive,
                "archive_sha256": digest,
                "executable_sha256": executable_digest,
            }
            (root / f"{binary_name}.json").write_text(
                json.dumps(metadata, sort_keys=True), encoding="ascii"
            )
        elif side == "baseline":
            loose = root / binary_name / executable_name(binary_name)
            loose.parent.mkdir(parents=True, exist_ok=True)
            loose.write_bytes(payload)
            (root / f"{binary_name}.sha256").write_text(
                f"{executable_digest}  {binary_name}/{executable_name(binary_name)}\n",
                encoding="ascii",
            )
    return root


def _docs_artifact(root: Path) -> Path:
    root.mkdir()
    with tarfile.open(root / "artifact.tar", "w") as target:
        directory = tarfile.TarInfo("./")
        directory.type = tarfile.DIRTYPE
        directory.mode = 0o755
        target.addfile(directory)
        info = tarfile.TarInfo("./index.html")
        body = b"<html>docs</html>"
        info.size = len(body)
        info.mode = 0o644
        target.addfile(info, io.BytesIO(body))
    return root


def _python_distributions(root: Path, version: str = VERSION, name: str = "apm-cli") -> Path:
    root.mkdir(exist_ok=True)
    token = name.replace("-", "_")
    with zipfile.ZipFile(root / f"apm_cli-{version}-py3-none-any.whl", "w") as wheel:
        wheel.writestr("apm_cli/__init__.py", "")
        wheel.writestr(
            f"apm_cli-{version}.dist-info/METADATA",
            f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n",
        )
        wheel.writestr(
            f"apm_cli-{version}.dist-info/WHEEL",
            "Wheel-Version: 1.0\nGenerator: fixture\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        )
        wheel.writestr(f"apm_cli-{version}.dist-info/RECORD", "apm_cli/__init__.py,,\n")
    with tarfile.open(root / f"apm_cli-{version}.tar.gz", "w:gz") as sdist:
        for member, body in {
            f"{token}-{version}/PKG-INFO": (
                f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n".encode()
            ),
            f"{token}-{version}/pyproject.toml": b"[project]\nname='apm-cli'\n",
            f"{token}-{version}/src/{token}/__init__.py": b"",
        }.items():
            info = tarfile.TarInfo(member)
            info.size = len(body)
            info.mode = 0o644
            sdist.addfile(info, io.BytesIO(body))
    return root


def _verified_fixture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, side: str, source_sha: str, controller: str
) -> tuple[Path, Path, Path, Path]:
    source = _source_root(tmp_path)
    native = _native_artifacts(tmp_path / "native", side, source_sha)
    docs = _docs_artifact(tmp_path / "docs")
    python = _python_distributions(tmp_path / "python")
    monkeypatch.setattr(release_wallclock, "_git_head", lambda root: source_sha)
    _set_workflow_env(monkeypatch, tmp_path, controller)
    (native / "runner.json").write_text(
        json.dumps(record_job(side, source, source_sha)), encoding="ascii"
    )
    return source, native, docs, python


def test_record_job_captures_minimal_host_identity_without_env_dump(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _source_root(tmp_path)
    monkeypatch.setattr(release_wallclock, "_git_head", lambda root: BASE_SHA)
    _set_workflow_env(monkeypatch, tmp_path, BASE_CONTROLLER)
    monkeypatch.setenv("SECRET_TOKEN", "do-not-copy")

    evidence = record_job("baseline", source, BASE_SHA)

    assert evidence["kind"] == "release-wallclock-job"
    assert evidence["workflow"]["source_sha"] == BASE_SHA
    assert evidence["workflow"]["controller_sha"] == BASE_CONTROLLER
    assert evidence["workflow"]["run_head_sha"] == BASE_CONTROLLER
    assert evidence["environment"]["image_version"] == "20260901.1"
    assert "SECRET_TOKEN" not in json.dumps(evidence)


def test_record_job_keeps_pull_request_head_distinct_from_execution_sha(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _source_root(tmp_path)
    monkeypatch.setattr(release_wallclock, "_git_head", lambda root: PROPOSED_SHA)
    _set_workflow_env(
        monkeypatch,
        tmp_path,
        PROPOSED_CONTROLLER,
        event_name="pull_request",
        run_head_sha=PROPOSED_RUN_HEAD,
    )

    evidence = record_job("proposed", source, PROPOSED_SHA)

    assert evidence["workflow"]["source_sha"] == PROPOSED_SHA
    assert evidence["workflow"]["controller_sha"] == PROPOSED_CONTROLLER
    assert evidence["workflow"]["run_head_sha"] == PROPOSED_RUN_HEAD
    assert evidence["workflow"]["run_head_sha"] != evidence["workflow"]["controller_sha"]
    assert evidence["workflow"]["event_name"] == "pull_request"


def test_record_job_fails_bad_source_identity_and_missing_hosted_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _source_root(tmp_path)
    monkeypatch.setattr(release_wallclock, "_git_head", lambda root: PROPOSED_SHA)
    _set_workflow_env(monkeypatch, tmp_path, BASE_CONTROLLER)

    with pytest.raises(ValueError, match="does not match expected"):
        record_job("baseline", source, BASE_SHA)

    monkeypatch.setattr(release_wallclock, "_git_head", lambda root: BASE_SHA)
    monkeypatch.delenv("ImageVersion")
    with pytest.raises(ValueError, match="ImageVersion"):
        record_job("baseline", source, BASE_SHA)


@pytest.mark.parametrize(
    "side,sha,controller,metadata_count",
    [
        ("baseline", BASE_SHA, BASE_CONTROLLER, 0),
        ("proposed", PROPOSED_SHA, PROPOSED_CONTROLLER, 5),
    ],
)
def test_verify_artifacts_emits_nonpromotable_real_archive_proof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    side: str,
    sha: str,
    controller: str,
    metadata_count: int,
) -> None:
    source, native, docs, python = _verified_fixture(tmp_path, monkeypatch, side, sha, controller)

    proof = verify_artifacts(side, source, sha, native, docs, python)

    assert proof["kind"] == "release-wallclock-proof"
    assert proof["promotable"] is False
    assert "release-candidate-evidence" not in json.dumps(proof)
    assert proof["side"] == side
    assert proof["source_sha"] == sha
    assert proof["controller_sha"] == controller
    assert proof["counts"]["native_archives"] == 5
    assert proof["counts"]["candidate_metadata"] == metadata_count
    assert proof["counts"]["job_records"] == 1
    assert proof["filehashes"]["docs"]["format"] == "tar"
    assert proof["filehashes"]["python"]["wheel"]["version"] == VERSION
    original = json.loads((native / "runner.json").read_text(encoding="ascii"))
    for field in ("schema_version", "recorded_at", "environment"):
        assert proof["job_records"][0][field] == original[field]
    # One terminal observer does not cover either native DAG.
    assert proof["observation_assessment"]["status"] == "inconclusive"


@pytest.mark.parametrize(
    "side,sha,controller",
    [
        ("baseline", BASE_SHA, BASE_CONTROLLER),
        ("proposed", PROPOSED_SHA, PROPOSED_CONTROLLER),
    ],
)
def test_verify_rejects_missing_observations_before_emitting_a_proof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    side: str,
    sha: str,
    controller: str,
) -> None:
    source, native, docs, python = _verified_fixture(tmp_path, monkeypatch, side, sha, controller)
    (native / "runner.json").unlink()

    with pytest.raises(ValueError, match="must contain at least one job record"):
        verify_artifacts(side, source, sha, native, docs, python)


def test_verify_proof_keeps_pull_request_head_distinct_from_execution_sha(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _source_root(tmp_path)
    native = _native_artifacts(tmp_path / "native", "proposed", PROPOSED_SHA)
    docs = _docs_artifact(tmp_path / "docs")
    python = _python_distributions(tmp_path / "python")
    monkeypatch.setattr(release_wallclock, "_git_head", lambda root: PROPOSED_SHA)
    _set_workflow_env(
        monkeypatch,
        tmp_path,
        PROPOSED_CONTROLLER,
        event_name="pull_request",
        run_head_sha=PROPOSED_RUN_HEAD,
    )
    (native / "runner.json").write_text(
        json.dumps(record_job("proposed", source, PROPOSED_SHA)), encoding="ascii"
    )

    proof = verify_artifacts("proposed", source, PROPOSED_SHA, native, docs, python)

    assert proof["source_sha"] == PROPOSED_SHA
    assert proof["controller_sha"] == PROPOSED_CONTROLLER
    assert proof["run_head_sha"] == PROPOSED_RUN_HEAD
    assert proof["event_name"] == "pull_request"


def test_verify_baseline_archives_raw_layout_binds_archives_to_original_executables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _source_root(tmp_path)
    flat_native = _native_artifacts(tmp_path / "flat-native", "baseline", BASE_SHA)
    native = tmp_path / "native-layout"
    archives = native / "archives"
    raw = native / "raw" / "candidate-1-apm-linux-x86_64" / "dist"
    archives.mkdir(parents=True)
    raw.mkdir(parents=True)
    for artifact in flat_native.iterdir():
        if artifact.is_file() and artifact.name.endswith((".tar.gz", ".zip")):
            (archives / artifact.name).write_bytes(artifact.read_bytes())
    for binary_name in BINARY_NAMES:
        archive = archive_name(binary_name)
        (archives / f"{archive}.sha256").write_bytes(
            (flat_native / f"{archive}.sha256").read_bytes()
        )
        executable_digest = (
            release_wallclock._zip_member_digest(archives / archive, binary_name)[0]
            if archive.endswith(".zip")
            else release_wallclock._tar_member_digest(archives / archive, binary_name)[0]
        )
        loose = raw / binary_name / executable_name(binary_name)
        loose.parent.mkdir()
        loose.write_bytes((flat_native / binary_name / executable_name(binary_name)).read_bytes())
        (raw / f"{binary_name}.sha256").write_text(
            f"{executable_digest}  dist/{binary_name}/{executable_name(binary_name)}\n",
            encoding="ascii",
        )
    docs = _docs_artifact(tmp_path / "docs")
    python = _python_distributions(tmp_path / "python")
    monkeypatch.setattr(release_wallclock, "_git_head", lambda root: BASE_SHA)
    _set_workflow_env(monkeypatch, tmp_path, BASE_CONTROLLER)

    (native / "runner.json").write_text(
        json.dumps(record_job("baseline", source, BASE_SHA)), encoding="ascii"
    )
    proof = verify_artifacts("baseline", source, BASE_SHA, native, docs, python)

    observed = proof["filehashes"]["native"]["apm-linux-x86_64"]
    assert observed["loose_executable_sidecar_sha256"] == file_digest(
        raw / "apm-linux-x86_64.sha256"
    )
    assert proof["counts"]["candidate_metadata"] == 0


def test_verify_baseline_raw_layout_rejects_original_executable_digest_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _source_root(tmp_path)
    flat_native = _native_artifacts(tmp_path / "flat-native", "baseline", BASE_SHA)
    native = tmp_path / "native-layout"
    archives = native / "archives"
    raw = native / "raw" / "candidate-1-apm-linux-x86_64" / "dist"
    archives.mkdir(parents=True)
    raw.mkdir(parents=True)
    for artifact in flat_native.iterdir():
        if artifact.is_file() and artifact.name.endswith((".tar.gz", ".zip")):
            (archives / artifact.name).write_bytes(artifact.read_bytes())
    for binary_name in BINARY_NAMES:
        archive = archive_name(binary_name)
        (archives / f"{archive}.sha256").write_bytes(
            (flat_native / f"{archive}.sha256").read_bytes()
        )
        loose = raw / binary_name / executable_name(binary_name)
        loose.parent.mkdir()
        loose.write_bytes((flat_native / binary_name / executable_name(binary_name)).read_bytes())
        digest = file_digest(loose)
        if binary_name == "apm-linux-x86_64":
            digest = "1" * 64
        (raw / f"{binary_name}.sha256").write_text(
            f"{digest}  dist/{binary_name}/{executable_name(binary_name)}\n",
            encoding="ascii",
        )
    docs = _docs_artifact(tmp_path / "docs")
    python = _python_distributions(tmp_path / "python")
    monkeypatch.setattr(release_wallclock, "_git_head", lambda root: BASE_SHA)
    _set_workflow_env(monkeypatch, tmp_path, BASE_CONTROLLER)

    with pytest.raises(ValueError, match="Loose executable checksum mismatch"):
        verify_artifacts("baseline", source, BASE_SHA, native, docs, python)


@pytest.mark.parametrize("fault", ["missing-sidecar", "missing-payload"])
def test_verify_baseline_requires_all_raw_sidecars_and_loose_executables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    source, native, docs, python = _verified_fixture(
        tmp_path, monkeypatch, "baseline", BASE_SHA, BASE_CONTROLLER
    )
    binary_name = "apm-linux-x86_64"
    if fault == "missing-sidecar":
        (native / f"{binary_name}.sha256").unlink()
        match = "raw executable checksum is missing"
    else:
        (native / binary_name / executable_name(binary_name)).unlink()
        match = "raw executable payload is missing"

    with pytest.raises(ValueError, match=match):
        verify_artifacts("baseline", source, BASE_SHA, native, docs, python)


def test_verify_rejects_proposed_archive_without_candidate_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, native, docs, python = _verified_fixture(
        tmp_path, monkeypatch, "proposed", PROPOSED_SHA, PROPOSED_CONTROLLER
    )
    (native / "apm-linux-x86_64.json").unlink()

    with pytest.raises(ValueError, match="requires candidate metadata"):
        verify_artifacts("proposed", source, PROPOSED_SHA, native, docs, python)


def test_verify_rejects_unsafe_archive_member_even_with_matching_sidecar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, native, docs, python = _verified_fixture(
        tmp_path, monkeypatch, "baseline", BASE_SHA, BASE_CONTROLLER
    )
    binary_name = "apm-linux-x86_64"
    archive = archive_name(binary_name)
    archive_path = native / archive
    with tarfile.open(archive_path, "w:gz") as target:
        info = tarfile.TarInfo(f"{binary_name}/../escape")
        info.size = 4
        target.addfile(info, io.BytesIO(b"nope"))
    digest = file_digest(archive_path)
    (native / f"{archive}.sha256").write_text(f"{digest}  {archive}\n", encoding="ascii")

    with pytest.raises(ValueError, match="unsafe member path"):
        verify_artifacts("baseline", source, BASE_SHA, native, docs, python)


def test_verify_rejects_duplicate_executable_members(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, native, docs, python = _verified_fixture(
        tmp_path, monkeypatch, "baseline", BASE_SHA, BASE_CONTROLLER
    )
    binary_name = "apm-linux-arm64"
    archive = archive_name(binary_name)
    executable = f"{binary_name}/{executable_name(binary_name)}"
    archive_path = native / archive
    with tarfile.open(archive_path, "w:gz") as target:
        for body in (b"one", b"two"):
            info = tarfile.TarInfo(executable)
            info.mode = 0o755
            info.size = len(body)
            target.addfile(info, io.BytesIO(body))
    digest = file_digest(archive_path)
    (native / f"{archive}.sha256").write_text(f"{digest}  {archive}\n", encoding="ascii")

    with pytest.raises(ValueError, match="Duplicate archive member"):
        verify_artifacts("baseline", source, BASE_SHA, native, docs, python)


def _tar_with_link(path: Path, link_type: bytes, linkname: str) -> None:
    binary_name = "apm-darwin-arm64"
    with tarfile.open(path, "w:gz") as target:
        executable = tarfile.TarInfo(f"{binary_name}/{executable_name(binary_name)}")
        executable.mode = 0o755
        executable.size = 6
        target.addfile(executable, io.BytesIO(b"native"))
        real = tarfile.TarInfo(f"{binary_name}/_internal/real-target")
        real.mode = 0o644
        real.size = 4
        target.addfile(real, io.BytesIO(b"real"))
        link = tarfile.TarInfo(f"{binary_name}/_internal/Foo.framework/Versions/Current")
        link.type = link_type
        link.linkname = linkname
        link.mode = 0o777
        target.addfile(link)


def test_tar_validator_accepts_real_format_macos_internal_symlinks(tmp_path: Path) -> None:
    binary_name = "apm-darwin-arm64"
    archive = tmp_path / archive_name(binary_name)
    with tarfile.open(archive, "w:gz") as target:
        executable = tarfile.TarInfo(f"{binary_name}/{executable_name(binary_name)}")
        executable.mode = 0o755
        executable.size = 6
        target.addfile(executable, io.BytesIO(b"native"))
        for member, linkname in (
            (f"{binary_name}/_internal/Python", "Python.framework/Versions/3.12/Python"),
            (f"{binary_name}/_internal/Python.framework/Python", "Versions/Current/Python"),
            (
                f"{binary_name}/_internal/Python.framework/Resources",
                "Versions/Current/Resources",
            ),
            (f"{binary_name}/_internal/Python.framework/Versions/Current", "3.12"),
        ):
            link = tarfile.TarInfo(member)
            link.type = tarfile.SYMTYPE
            link.linkname = linkname
            target.addfile(link)

    digest, count = release_wallclock._tar_member_digest(archive, binary_name)

    assert digest == release_wallclock.file_digest_stream(io.BytesIO(b"native"))
    assert count == 5


def test_tar_validator_rejects_escaping_symlink(tmp_path: Path) -> None:
    archive = tmp_path / "escaping-symlink.tar.gz"
    _tar_with_link(archive, tarfile.SYMTYPE, "../../../../outside")

    with pytest.raises(ValueError, match="symbolic link escapes"):
        release_wallclock._tar_member_digest(archive, "apm-darwin-arm64")


def test_tar_validator_uses_archive_root_for_hardlinks(tmp_path: Path) -> None:
    archive = tmp_path / "escaping-hardlink.tar.gz"
    _tar_with_link(archive, tarfile.LNKTYPE, "outside/target")

    with pytest.raises(ValueError, match="hard link escapes"):
        release_wallclock._tar_member_digest(archive, "apm-darwin-arm64")

    safe = tmp_path / "safe-hardlink.tar.gz"
    _tar_with_link(safe, tarfile.LNKTYPE, "apm-darwin-arm64/_internal/real-target")
    release_wallclock._tar_member_digest(safe, "apm-darwin-arm64")


@pytest.mark.parametrize("fault", ["empty-docs-index", "python-version"])
def test_verify_rejects_mismatched_metadata_or_missing_docs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    source, native, docs, python = _verified_fixture(
        tmp_path, monkeypatch, "baseline", BASE_SHA, BASE_CONTROLLER
    )
    if fault == "empty-docs-index":
        docs.joinpath("artifact.tar").unlink()
        docs.joinpath("index.html").write_text("", encoding="ascii")
        match = "docs index.html is empty"
    else:
        python = _python_distributions(tmp_path / "wrong-python", "9.9.9")
        match = "project/version"

    with pytest.raises(ValueError, match=match):
        verify_artifacts("baseline", source, BASE_SHA, native, docs, python)


@pytest.mark.parametrize(
    "fault", ["wrong-wheel-name", "missing-wheel-record", "sdist-metadata-only"]
)
def test_verify_rejects_fake_or_wrong_project_python_distributions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    source, native, docs, python = _verified_fixture(
        tmp_path, monkeypatch, "baseline", BASE_SHA, BASE_CONTROLLER
    )
    for path in python.iterdir():
        path.unlink()
    if fault == "wrong-wheel-name":
        _python_distributions(python, VERSION, name="evil-pkg")
        match = "Wheel metadata name/version"
    elif fault == "missing-wheel-record":
        with zipfile.ZipFile(python / f"apm_cli-{VERSION}-py3-none-any.whl", "w") as wheel:
            wheel.writestr("apm_cli/__init__.py", "")
            wheel.writestr(
                f"apm_cli-{VERSION}.dist-info/METADATA",
                f"Metadata-Version: 2.1\nName: apm-cli\nVersion: {VERSION}\n",
            )
            wheel.writestr(f"apm_cli-{VERSION}.dist-info/WHEEL", "Wheel-Version: 1.0\n")
        with tarfile.open(python / f"apm_cli-{VERSION}.tar.gz", "w:gz") as sdist:
            for member, body in {
                f"apm_cli-{VERSION}/PKG-INFO": (
                    f"Metadata-Version: 2.1\nName: apm-cli\nVersion: {VERSION}\n".encode()
                ),
                f"apm_cli-{VERSION}/pyproject.toml": b"[project]\nname='apm-cli'\n",
                f"apm_cli-{VERSION}/src/apm_cli/__init__.py": b"",
            }.items():
                info = tarfile.TarInfo(member)
                info.size = len(body)
                sdist.addfile(info, io.BytesIO(body))
        match = "missing METADATA, WHEEL or RECORD"
    else:
        with zipfile.ZipFile(python / f"apm_cli-{VERSION}-py3-none-any.whl", "w") as wheel:
            wheel.writestr("apm_cli/__init__.py", "")
            wheel.writestr(
                f"apm_cli-{VERSION}.dist-info/METADATA",
                f"Metadata-Version: 2.1\nName: apm-cli\nVersion: {VERSION}\n",
            )
            wheel.writestr(f"apm_cli-{VERSION}.dist-info/WHEEL", "Wheel-Version: 1.0\n")
            wheel.writestr(f"apm_cli-{VERSION}.dist-info/RECORD", "apm_cli/__init__.py,,\n")
        with tarfile.open(python / f"apm_cli-{VERSION}.tar.gz", "w:gz") as sdist:
            body = f"Metadata-Version: 2.1\nName: apm-cli\nVersion: {VERSION}\n".encode()
            info = tarfile.TarInfo(f"apm_cli-{VERSION}/PKG-INFO")
            info.size = len(body)
            sdist.addfile(info, io.BytesIO(body))
        match = "top-level pyproject.toml"

    with pytest.raises(ValueError, match=match):
        verify_artifacts("baseline", source, BASE_SHA, native, docs, python)


def _observation_records(side: str) -> list[dict[str, object]]:
    """Model the real eight-observer baseline and fifteen-observer proposed DAG."""
    records = []
    for system, arch in (
        ("Linux", "x86_64"),
        ("Linux", "aarch64"),
        ("Windows", "AMD64"),
        ("Darwin", "x86_64"),
        ("Darwin", "arm64"),
    ):
        if side == "proposed":
            jobs = ("unit-tests", "build", "integration-tests")
        elif system == "Darwin":
            jobs = (
                "build-and-validate-macos-intel"
                if arch == "x86_64"
                else "build-and-validate-macos-arm",
            )
        else:
            jobs = ("build-and-test", "integration-tests")
        for job in jobs:
            records.append(
                {
                    "path": f"wallclock/{side}-{system}-{arch}-{job}.json",
                    "github_job": job,
                    "runner_name": f"{side}-{system}-{arch}-{job}",
                    "schema_version": 1,
                    "recorded_at": _at(10),
                    "environment": {
                        "system": system,
                        "kernel": "fixture-kernel",
                        "arch": arch,
                        "cpu": f"fixture-{arch}",
                        "cpu_count": "4",
                        "image_version": f"fixture-{system}-{arch}-20260901.1",
                        "python": "3.12.10",
                        "source_uv_lock_sha256": "1" * 64,
                        "source_build_release_yml_sha256": "e" * 64,
                    },
                }
            )
    return records


def _proof(
    path: Path,
    side: str,
    run_id: int,
    source_sha: str,
    controller_sha: str,
    run_head_sha: str,
) -> Path:
    observations = _observation_records(side)
    native = {}
    for index, binary_name in enumerate(BINARY_NAMES, start=1):
        archive = archive_name(binary_name)
        executable_digest = f"{index + 10:064x}"
        native[binary_name] = {
            "binary_name": binary_name,
            "archive": archive,
            "archive_sha256": f"{index:064x}",
            "executable_sha256": executable_digest,
            "sidecar": f"{archive}.sha256",
            "candidate_metadata_sha256": f"{index + 20:064x}" if side == "proposed" else None,
            "loose_executable_sidecar_sha256": f"{index + 30:064x}" if side == "baseline" else None,
            "loose_executable_sha256": executable_digest if side == "baseline" else None,
            "member_count": 2,
        }
    path.write_text(
        json.dumps(
            {
                "kind": "release-wallclock-proof",
                "schema_version": 1,
                "promotable": False,
                "side": side,
                "source_sha": source_sha,
                "controller_sha": controller_sha,
                "run_head_sha": run_head_sha,
                "event_name": "pull_request" if run_head_sha != controller_sha else "push",
                "run_id": run_id,
                "run_attempt": 1,
                "version": VERSION,
                "workflow_digest": "e" * 64,
                "counts": {
                    "native_archives": 5,
                    "native_sidecars": 5,
                    "candidate_metadata": 5 if side == "proposed" else 0,
                    "job_records": len(observations),
                    "docs_indexes": 1,
                    "python_distributions": 2,
                },
                "filehashes": {
                    "source_uv_lock_sha256": "1" * 64,
                    "source_build_release_yml_sha256": "e" * 64,
                    "native": native,
                    "docs": {
                        "format": "tar",
                        "archive": "artifact.tar",
                        "archive_sha256": "2" * 64,
                        "index": "index.html",
                    },
                    "python": {
                        "source_project_name": "apm-cli",
                        "wheel": {
                            "name": f"apm_cli-{VERSION}-py3-none-any.whl",
                            "sha256": "3" * 64,
                            "project_name": "apm-cli",
                            "version": VERSION,
                        },
                        "sdist": {
                            "name": f"apm_cli-{VERSION}.tar.gz",
                            "sha256": "4" * 64,
                            "project_name": "apm-cli",
                            "version": VERSION,
                        },
                    },
                },
                "job_records": observations,
            }
        ),
        encoding="ascii",
    )
    return path


def _job(run_id: int, name: str, start: int, end: int, conclusion: str = "success") -> dict:
    return {
        "id": f"{run_id}-{name}",
        "run_id": run_id,
        "run_attempt": 1,
        "name": name,
        "status": "completed",
        "conclusion": conclusion,
        "started_at": _at(start),
        "completed_at": _at(end),
    }


def _run_json(
    path: Path,
    run_id: int,
    run_head_sha: str,
    run_started: int,
    terminal_completed: int,
) -> Path:
    jobs = [
        _job(run_id, "Build/test/native/docs/wheel proof", run_started + 10, run_started + 20),
        {
            "id": f"{run_id}-skipped",
            "run_id": run_id,
            "run_attempt": 1,
            "name": "Imported PR-only workflow",
            "status": "completed",
            "conclusion": "skipped",
        },
        _job(run_id, "Verify wall-clock artifacts", terminal_completed - 5, terminal_completed),
    ]
    payload = {
        "run": {
            "id": run_id,
            "run_attempt": 1,
            "status": "completed",
            "conclusion": "success",
            "head_sha": run_head_sha,
            "pull_requests": [{"head": {"sha": "f" * 40}}],
            "created_at": _at(0),
            "run_started_at": _at(run_started),
        },
        "jobs": jobs,
    }
    path.write_text(json.dumps(payload), encoding="ascii")
    return path


def _compare_fixture(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    return (
        _run_json(tmp_path / "baseline-run.json", 201, BASE_RUN_HEAD, 0, 100),
        _run_json(tmp_path / "proposed-run.json", 301, PROPOSED_RUN_HEAD, 0, 90),
        _proof(
            tmp_path / "baseline-proof.json",
            "baseline",
            201,
            BASE_SHA,
            BASE_CONTROLLER,
            BASE_RUN_HEAD,
        ),
        _proof(
            tmp_path / "proposed-proof.json",
            "proposed",
            301,
            PROPOSED_SHA,
            PROPOSED_CONTROLLER,
            PROPOSED_RUN_HEAD,
        ),
    )


def _compare_ok(
    baseline_run: Path,
    proposed_run: Path,
    baseline_proof: Path,
    proposed_proof: Path,
    baseline_required: tuple[str, ...] = ("Build/test/native/docs/wheel proof",),
    proposed_required: tuple[str, ...] = ("Build/test/native/docs/wheel proof",),
) -> dict[str, object]:
    return compare_runs(
        baseline_run,
        proposed_run,
        baseline_proof,
        proposed_proof,
        BASE_SHA,
        PROPOSED_SHA,
        BASE_CONTROLLER,
        PROPOSED_CONTROLLER,
        BASE_RUN_HEAD,
        PROPOSED_RUN_HEAD,
        "Verify wall-clock artifacts",
        baseline_required,
        proposed_required,
    )


def test_compare_matches_distinct_native_dags_without_claiming_controlled_acceptance(
    tmp_path: Path,
) -> None:
    result = _compare_ok(*_compare_fixture(tmp_path))

    assert len(_observation_records("baseline")) == 8
    assert len(_observation_records("proposed")) == 15
    for side in ("baseline", "proposed"):
        assessment = result[side]["observation_assessment"]
        assert assessment["status"] == "complete"
        assert set(assessment["roles"]) == {
            f"{role}/{binary_name}"
            for role in ("unit", "build", "integration")
            for binary_name in BINARY_NAMES
        }
    assert result["clock_arithmetic"] == "validated"
    assert result["comparability"]["status"] == "matched"
    assert result["comparability"]["issues"] == []
    assert result["comparability"]["controlled_acceptance"] is False
    assert result["production_qualification"] is False


@pytest.mark.parametrize("field", ["schema_version", "recorded_at", "environment"])
def test_job_record_ingestion_preserves_missing_metadata_as_unassessed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    source, native, docs, python = _verified_fixture(
        tmp_path, monkeypatch, "baseline", BASE_SHA, BASE_CONTROLLER
    )
    path = native / "runner.json"
    payload = json.loads(path.read_text(encoding="ascii"))
    payload.pop(field)
    path.write_text(json.dumps(payload), encoding="ascii")

    proof = verify_artifacts("baseline", source, BASE_SHA, native, docs, python)

    assert proof["job_records"][0][field] is None
    assert proof["observation_assessment"]["status"] == "unassessed"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", None),
        ("schema_version", True),
        ("schema_version", 1.0),
        ("schema_version", 2),
        ("recorded_at", None),
        ("recorded_at", "not-a-time"),
        ("recorded_at", "2026-09-08T00:00:10"),
        ("environment", None),
        ("environment", {}),
        ("image_version", ""),
        ("cpu_count", "0"),
        ("cpu_count", True),
        ("source_uv_lock_sha256", "not-a-digest"),
    ],
)
def test_compare_incomplete_metadata_cannot_count_as_controlled_gain(
    tmp_path: Path, field: str, value: object
) -> None:
    paths = _compare_fixture(tmp_path)
    payload = json.loads(paths[3].read_text(encoding="ascii"))
    record = payload["job_records"][0]
    target = (
        record
        if field in {"schema_version", "recorded_at", "environment"}
        else record["environment"]
    )
    if value is None:
        target.pop(field)
    else:
        target[field] = value
    # A saved claim must not override recomputation.
    payload["observation_assessment"] = {"status": "complete", "issues": []}
    paths[3].write_text(json.dumps(payload), encoding="ascii")

    result = _compare_ok(*paths)

    assert result["comparability"]["status"] == "unassessed"
    assert result["comparability"]["issues"]
    assert result["comparability"]["controlled_acceptance"] is False
    assert result["delta_seconds"]["primary_gain_seconds"] == 10
    assert result["clock_arithmetic"] == "validated"


def test_compare_legacy_projected_records_keep_raw_clocks_without_inventing_metadata(
    tmp_path: Path,
) -> None:
    paths = _compare_fixture(tmp_path)
    for path in paths[2:]:
        payload = json.loads(path.read_text(encoding="ascii"))
        payload["job_records"] = [
            {"path": "historical/job.json", "github_job": "build", "runner_name": "legacy"}
        ]
        payload["counts"]["job_records"] = 1
        path.write_text(json.dumps(payload), encoding="ascii")

    result = _compare_ok(*paths)

    assert result["gain_percent"]["primary"] == 10
    assert result["baseline"]["wallclock_seconds_from_created_at"] == 100
    assert result["proposed"]["wallclock_seconds_from_created_at"] == 90
    assert result["comparability"]["status"] == "unassessed"
    assert result["comparability"]["controlled_acceptance"] is False
    assert result["baseline"]["observation_assessment"]["roles"] == {}


@pytest.mark.parametrize(
    ("side", "index"),
    [("baseline", index) for index in range(8)] + [("proposed", index) for index in range(15)],
)
def test_compare_requires_every_observer_role_and_platform(
    tmp_path: Path, side: str, index: int
) -> None:
    paths = _compare_fixture(tmp_path)
    path = paths[2] if side == "baseline" else paths[3]
    payload = json.loads(path.read_text(encoding="ascii"))
    payload["job_records"].pop(index)
    payload["counts"]["job_records"] -= 1
    path.write_text(json.dumps(payload), encoding="ascii")

    result = _compare_ok(*paths)

    assert result[side]["observation_assessment"]["status"] == "inconclusive"
    assert any("Missing observer roles:" in issue for issue in result["comparability"]["issues"])
    assert result["comparability"]["controlled_acceptance"] is False
    assert result["delta_seconds"]["primary_gain_seconds"] == 10


@pytest.mark.parametrize(
    ("fault", "message"),
    [
        ("image", "image_version differs"),
        ("cpu", "cpu_count differs"),
        ("duplicate-role", "duplicate observer role/platform"),
        ("wrong-platform", "Missing observer roles"),
        ("wrong-job", "unexpected observer role/platform"),
        ("timestamp", "outside the run timeline"),
        ("timestamp-before", "outside the run timeline"),
        ("source-digest", "does not match proof"),
    ],
)
def test_compare_environment_drift_and_ambiguous_coverage_are_inconclusive(
    tmp_path: Path, fault: str, message: str
) -> None:
    paths = _compare_fixture(tmp_path)
    payload = json.loads(paths[3].read_text(encoding="ascii"))
    record = payload["job_records"][0]
    if fault == "image":
        record["environment"]["image_version"] = "different-image"
    elif fault == "cpu":
        record["environment"]["cpu_count"] = "8"
    elif fault == "duplicate-role":
        duplicate = dict(record, runner_name="another-runner")
        payload["job_records"].append(duplicate)
        payload["counts"]["job_records"] += 1
    elif fault == "wrong-platform":
        record["environment"]["arch"] = "aarch64"
    elif fault == "wrong-job":
        record["github_job"] = "unrecognized-job"
    elif fault == "timestamp":
        record["recorded_at"] = _at(91)
    elif fault == "timestamp-before":
        record["recorded_at"] = _at(-1)
    else:
        record["environment"]["source_build_release_yml_sha256"] = "f" * 64
    paths[3].write_text(json.dumps(payload), encoding="ascii")

    result = _compare_ok(*paths)

    assert result["comparability"]["status"] == "inconclusive"
    assert any(message in issue for issue in result["comparability"]["issues"])
    assert result["comparability"]["controlled_acceptance"] is False
    assert result["delta_seconds"]["primary_gain_seconds"] == 10


def test_compare_allows_intentional_workflow_digest_change_between_sides(tmp_path: Path) -> None:
    paths = _compare_fixture(tmp_path)
    payload = json.loads(paths[3].read_text(encoding="ascii"))
    payload["workflow_digest"] = "f" * 64
    payload["filehashes"]["source_build_release_yml_sha256"] = "f" * 64
    for record in payload["job_records"]:
        record["environment"]["source_build_release_yml_sha256"] = "f" * 64
    paths[3].write_text(json.dumps(payload), encoding="ascii")

    result = _compare_ok(*paths)

    assert result["comparability"]["status"] == "matched"
    assert result["comparability"]["controlled_acceptance"] is False


def test_observation_validation_record_visits_scale_linearly() -> None:
    """Count scans and identity comparisons at N and 10N without wall clocks."""

    class CountedName(str):
        """Expose a quadratic list-based replacement for identity set membership."""

        comparisons = 0

        def __eq__(self, other: object) -> bool:
            type(self).comparisons += 1
            return super().__eq__(other)

        def __hash__(self) -> int:
            return super().__hash__()

    class CountedRecords(list[dict[str, object]]):
        """Expose repeated scans, including index-based scans, deterministically."""

        visits = 0

        def __iter__(self) -> Iterator[dict[str, object]]:
            for record in super().__iter__():
                self.visits += 1
                yield record

        def __getitem__(self, key: int) -> dict[str, object]:
            self.visits += 1
            return super().__getitem__(key)

    visits = []
    template = _observation_records("proposed")[0]
    for size in (30, 300):
        CountedName.comparisons = 0
        records = CountedRecords(
            dict(template, runner_name=CountedName(f"runner-{index}")) for index in range(size)
        )
        assessment = release_wallclock._validate_job_records(
            {"side": "proposed", "job_records": records}, {"job_records": size}
        )
        assert assessment["status"] == "inconclusive"  # Duplicate roles, not identities.
        operations = records.visits + CountedName.comparisons
        assert size <= operations <= 3 * size
        visits.append(operations)
    assert visits[1] <= 12 * visits[0]


@pytest.mark.parametrize(
    ("fault", "match"),
    [
        ("empty-filehashes", "workflow digest"),
        ("missing-native", "native inventory"),
        ("missing-proposed-metadata", "candidate_metadata_sha256"),
        ("empty-jobs", "at least one job record"),
    ],
)
def test_compare_rejects_structurally_fake_or_incomplete_proofs(
    tmp_path: Path, fault: str, match: str
) -> None:
    baseline_run, proposed_run, baseline_proof, proposed_proof = _compare_fixture(tmp_path)
    target = proposed_proof if fault == "missing-proposed-metadata" else baseline_proof
    payload = json.loads(target.read_text(encoding="ascii"))
    if fault == "empty-filehashes":
        payload["filehashes"] = {}
    elif fault == "missing-native":
        payload["filehashes"]["native"].pop("apm-linux-x86_64")
    elif fault == "missing-proposed-metadata":
        payload["filehashes"]["native"]["apm-linux-x86_64"]["candidate_metadata_sha256"] = None
    else:
        payload["counts"]["job_records"] = 0
        payload["job_records"] = []
    target.write_text(json.dumps(payload), encoding="ascii")

    with pytest.raises(ValueError, match=match):
        _compare_ok(baseline_run, proposed_run, baseline_proof, proposed_proof)


@pytest.mark.parametrize("required", [(), ("Verify wall-clock artifacts",)])
def test_compare_rejects_empty_or_terminal_only_required_job_sets(
    tmp_path: Path, required: tuple[str, ...]
) -> None:
    baseline_run, proposed_run, baseline_proof, proposed_proof = _compare_fixture(tmp_path)

    with pytest.raises(ValueError, match="required job set"):
        _compare_ok(
            baseline_run,
            proposed_run,
            baseline_proof,
            proposed_proof,
            baseline_required=required,
        )


def test_compare_uses_actual_terminal_wallclock_not_runner_sum_or_modeled_queue(
    tmp_path: Path,
) -> None:
    baseline_run = _run_json(tmp_path / "baseline-run.json", 201, BASE_RUN_HEAD, 30, 120)
    proposed_run = _run_json(tmp_path / "proposed-run.json", 301, PROPOSED_RUN_HEAD, 0, 100)
    baseline_proof = _proof(
        tmp_path / "baseline-proof.json", "baseline", 201, BASE_SHA, BASE_CONTROLLER, BASE_RUN_HEAD
    )
    proposed_proof = _proof(
        tmp_path / "proposed-proof.json",
        "proposed",
        301,
        PROPOSED_SHA,
        PROPOSED_CONTROLLER,
        PROPOSED_RUN_HEAD,
    )

    result = compare_runs(
        baseline_run,
        proposed_run,
        baseline_proof,
        proposed_proof,
        BASE_SHA,
        PROPOSED_SHA,
        BASE_CONTROLLER,
        PROPOSED_CONTROLLER,
        BASE_RUN_HEAD,
        PROPOSED_RUN_HEAD,
        "Verify wall-clock artifacts",
        ("Build/test/native/docs/wheel proof",),
        ("Build/test/native/docs/wheel proof",),
    )

    assert result["readonly_adapter"] is True
    assert result["production_qualification"] is False
    assert result["baseline"]["wallclock_seconds_from_created_at"] == 120
    assert result["proposed"]["controller_sha"] == PROPOSED_CONTROLLER
    assert result["proposed"]["run_head_sha"] == PROPOSED_RUN_HEAD
    assert result["baseline"]["wallclock_seconds_from_run_started_at"] == 90
    assert result["proposed"]["wallclock_seconds_from_created_at"] == 100
    assert result["delta_seconds"]["primary_gain_seconds"] == 20
    assert result["delta_seconds"]["secondary_gain_seconds"] == -10
    assert result["delta_seconds"]["unweighted_runner_seconds_proposed_minus_baseline"] == 0
    assert result["delta_seconds"]["unweighted_runner_seconds_proposed_minus_baseline"] != -20


@pytest.mark.parametrize(
    ("fault", "match"),
    [
        ("api-head", "API run head_sha"),
        ("expected-head", "expected event head SHA"),
        ("expected-controller", "expected execution SHA"),
    ],
)
def test_compare_rejects_unexpected_run_head_or_execution_sha_changes(
    tmp_path: Path, fault: str, match: str
) -> None:
    baseline_run = _run_json(tmp_path / "baseline-run.json", 201, BASE_RUN_HEAD, 0, 100)
    proposed_run = _run_json(tmp_path / "proposed-run.json", 301, PROPOSED_RUN_HEAD, 0, 90)
    baseline_proof = _proof(
        tmp_path / "baseline-proof.json", "baseline", 201, BASE_SHA, BASE_CONTROLLER, BASE_RUN_HEAD
    )
    proposed_proof = _proof(
        tmp_path / "proposed-proof.json",
        "proposed",
        301,
        PROPOSED_SHA,
        PROPOSED_CONTROLLER,
        PROPOSED_RUN_HEAD,
    )
    expected_head = PROPOSED_RUN_HEAD
    expected_controller = PROPOSED_CONTROLLER
    if fault == "api-head":
        data = json.loads(proposed_run.read_text(encoding="ascii"))
        data["run"]["head_sha"] = "1" * 40
        data["run"]["pull_requests"][0]["head"]["sha"] = PROPOSED_RUN_HEAD
        proposed_run.write_text(json.dumps(data), encoding="ascii")
    elif fault == "expected-head":
        expected_head = "1" * 40
    else:
        expected_controller = "1" * 40

    with pytest.raises(ValueError, match=match):
        compare_runs(
            baseline_run,
            proposed_run,
            baseline_proof,
            proposed_proof,
            BASE_SHA,
            PROPOSED_SHA,
            BASE_CONTROLLER,
            expected_controller,
            BASE_RUN_HEAD,
            expected_head,
            "Verify wall-clock artifacts",
            ("Build/test/native/docs/wheel proof",),
            ("Build/test/native/docs/wheel proof",),
        )


@pytest.mark.parametrize("fault", ["failed-control", "missing-required", "early-terminal"])
def test_compare_rejects_failed_controls_missing_gates_and_bad_timelines(
    tmp_path: Path, fault: str
) -> None:
    baseline_run = _run_json(tmp_path / "baseline-run.json", 201, BASE_RUN_HEAD, 0, 100)
    proposed_run = _run_json(tmp_path / "proposed-run.json", 301, PROPOSED_RUN_HEAD, 0, 90)
    data = json.loads(proposed_run.read_text(encoding="ascii"))
    if fault == "failed-control":
        data["jobs"][0]["conclusion"] = "failure"
        required = ("Build/test/native/docs/wheel proof",)
        match = "rejected conclusion"
    elif fault == "missing-required":
        required = ("Required native gate",)
        match = "required job missing"
    else:
        data["jobs"][0]["completed_at"] = _at(88)
        data["jobs"][2]["started_at"] = _at(87)
        required = ("Build/test/native/docs/wheel proof",)
        match = "terminal job started before"
    proposed_run.write_text(json.dumps(data), encoding="ascii")

    with pytest.raises(ValueError, match=match):
        compare_runs(
            baseline_run,
            proposed_run,
            _proof(
                tmp_path / "baseline-proof.json",
                "baseline",
                201,
                BASE_SHA,
                BASE_CONTROLLER,
                BASE_RUN_HEAD,
            ),
            _proof(
                tmp_path / "proposed-proof.json",
                "proposed",
                301,
                PROPOSED_SHA,
                PROPOSED_CONTROLLER,
                PROPOSED_RUN_HEAD,
            ),
            BASE_SHA,
            PROPOSED_SHA,
            BASE_CONTROLLER,
            PROPOSED_CONTROLLER,
            BASE_RUN_HEAD,
            PROPOSED_RUN_HEAD,
            "Verify wall-clock artifacts",
            ("Build/test/native/docs/wheel proof",),
            required,
        )


def test_compare_rejects_promotable_or_duplicate_run_proofs(tmp_path: Path) -> None:
    baseline_run = _run_json(tmp_path / "baseline-run.json", 201, BASE_RUN_HEAD, 0, 100)
    proposed_run = _run_json(tmp_path / "proposed-run.json", 201, PROPOSED_RUN_HEAD, 0, 90)
    baseline_proof = _proof(
        tmp_path / "baseline-proof.json", "baseline", 201, BASE_SHA, BASE_CONTROLLER, BASE_RUN_HEAD
    )
    proposed_proof = _proof(
        tmp_path / "proposed-proof.json",
        "proposed",
        201,
        PROPOSED_SHA,
        PROPOSED_CONTROLLER,
        PROPOSED_RUN_HEAD,
    )
    payload = json.loads(proposed_proof.read_text(encoding="ascii"))
    payload["promotable"] = True
    proposed_proof.write_text(json.dumps(payload), encoding="ascii")

    with pytest.raises(ValueError, match="non-promotable"):
        compare_runs(
            baseline_run,
            proposed_run,
            baseline_proof,
            proposed_proof,
            BASE_SHA,
            PROPOSED_SHA,
            BASE_CONTROLLER,
            PROPOSED_CONTROLLER,
            BASE_RUN_HEAD,
            PROPOSED_RUN_HEAD,
            "Verify wall-clock artifacts",
            ("Build/test/native/docs/wheel proof",),
            ("Build/test/native/docs/wheel proof",),
        )

    payload["promotable"] = False
    proposed_proof.write_text(json.dumps(payload), encoding="ascii")
    with pytest.raises(ValueError, match="run IDs must differ"):
        compare_runs(
            baseline_run,
            proposed_run,
            baseline_proof,
            proposed_proof,
            BASE_SHA,
            PROPOSED_SHA,
            BASE_CONTROLLER,
            PROPOSED_CONTROLLER,
            BASE_RUN_HEAD,
            PROPOSED_RUN_HEAD,
            "Verify wall-clock artifacts",
            ("Build/test/native/docs/wheel proof",),
            ("Build/test/native/docs/wheel proof",),
        )
