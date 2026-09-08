"""Capture and compare read-only release wall-clock evidence.

This module is deliberately not release qualification code. It verifies the
artifacts needed for an apples-to-apples timing comparison, emits a
non-promotable proof, and compares two already-successful GitHub Actions
attempts by their real terminal job timestamps.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import platform
import re
import shutil
import stat
import subprocess
import tarfile
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from email.parser import Parser
from pathlib import Path
from typing import Any

import tomllib

from scripts.package_release import (
    BINARY_NAMES,
    archive_name,
    executable_name,
    file_digest,
    require_member_path,
    require_sha,
)

ABI_VERSION = 1
KIND_JOB = "release-wallclock-job"
KIND_PROOF = "release-wallclock-proof"
KIND_COMPARISON = "release-wallclock-comparison"
TERMINAL_JOB = "Verify wall-clock artifacts"
WORKFLOW_PATH = ".github/workflows/build-release.yml"
SIDE_CHOICES = ("baseline", "proposed")
BAD_JOB_CONCLUSIONS = {"failure", "cancelled", "timed_out", "action_required"}
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class ArchiveEvidence:
    """Read-only proof for one native archive."""

    binary_name: str
    archive: str
    archive_sha256: str
    executable_sha256: str
    sidecar: str
    candidate_metadata_sha256: str | None
    loose_executable_sidecar_sha256: str | None
    loose_executable_sha256: str | None
    member_count: int

    def payload(self) -> dict[str, object]:
        """Return a JSON-safe archive proof."""
        return {
            "binary_name": self.binary_name,
            "archive": self.archive,
            "archive_sha256": self.archive_sha256,
            "executable_sha256": self.executable_sha256,
            "sidecar": self.sidecar,
            "candidate_metadata_sha256": self.candidate_metadata_sha256,
            "loose_executable_sidecar_sha256": self.loose_executable_sidecar_sha256,
            "loose_executable_sha256": self.loose_executable_sha256,
            "member_count": self.member_count,
        }


def _json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="ascii")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _timestamp(value: object, label: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} timestamp is required")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"{label} timestamp must include a timezone")
    return parsed


def _seconds_between(start: datetime, end: datetime, label: str) -> float:
    value = (end - start).total_seconds()
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{label} has an invalid negative timeline")
    return value


def _positive_int(value: object, label: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a positive integer")
    try:
        result = int(str(value))
    except ValueError as error:
        raise ValueError(f"{label} must be a positive integer") from error
    if result < 1 or str(result) != str(value):
        raise ValueError(f"{label} must be a positive integer")
    return result


def _full_sha(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a full lowercase Git SHA")
    require_sha(value)
    return value


def _side(value: str) -> str:
    if value not in SIDE_CHOICES:
        raise ValueError("side must be baseline or proposed")
    return value


def _source_metadata(source_root: Path) -> tuple[str, str]:
    data = tomllib.loads((source_root / "pyproject.toml").read_text(encoding="utf-8"))
    name = data["project"]["name"]
    version = data["project"]["version"]
    if not isinstance(name, str) or not name:
        raise ValueError("source pyproject.toml must contain project.name")
    if not isinstance(version, str) or not version:
        raise ValueError("source pyproject.toml must contain project.version")
    return name, version


def _source_version(source_root: Path) -> str:
    return _source_metadata(source_root)[1]


def _canonical_project_name(value: str) -> str:
    """Normalize distribution names using the wheel/sdist comparison form."""
    if not value:
        raise ValueError("Distribution name must be nonempty")
    return re.sub(r"[-_.]+", "-", value).lower()


def _distribution_token(value: str) -> str:
    return _canonical_project_name(value).replace("-", "_")


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and SHA256_PATTERN.fullmatch(value) is not None


def _source_digests(source_root: Path) -> dict[str, str]:
    files = {
        "uv.lock": source_root / "uv.lock",
        WORKFLOW_PATH: source_root / WORKFLOW_PATH,
    }
    missing = [name for name, path in files.items() if not path.is_file()]
    if missing:
        raise FileNotFoundError(f"Missing required source files: {', '.join(missing)}")
    return {name: file_digest(path) for name, path in files.items()}


def _git_head(source_root: Path) -> str:
    git = shutil.which("git")
    if git is None:
        raise OSError("git executable is required")
    result = subprocess.run(  # noqa: S603 - constant git executable, no shell.
        [git, "-C", str(source_root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        encoding="ascii",
        timeout=30,
    )
    return result.stdout.strip()


def _workflow_env(source_sha: str) -> dict[str, object]:
    controller_sha = _full_sha(os.environ.get("GITHUB_SHA"), "GITHUB_SHA")
    event_name, run_head_sha = _event_run_head_sha(controller_sha)
    image = os.environ.get("ImageVersion", "")  # noqa: SIM112 - GitHub-hosted image contract.
    if not image:
        raise ValueError("GitHub hosted runner ImageVersion is required")
    runner_name = os.environ.get("RUNNER_NAME", "")
    github_job = os.environ.get("GITHUB_JOB", "")
    if not runner_name or not github_job:
        raise ValueError("GITHUB_JOB and RUNNER_NAME are required")
    return {
        "source_sha": source_sha,
        "controller_sha": controller_sha,
        "run_head_sha": run_head_sha,
        "event_name": event_name,
        "run_id": _positive_int(os.environ.get("GITHUB_RUN_ID"), "GITHUB_RUN_ID"),
        "run_attempt": _positive_int(os.environ.get("GITHUB_RUN_ATTEMPT"), "GITHUB_RUN_ATTEMPT"),
        "github_job": github_job,
        "runner_name": runner_name,
    }


def _event_payload() -> tuple[str, dict[str, object]]:
    event_name = os.environ.get("GITHUB_EVENT_NAME", "")
    event_path = os.environ.get("GITHUB_EVENT_PATH", "")
    if not event_name or not event_path:
        raise ValueError("GITHUB_EVENT_NAME and GITHUB_EVENT_PATH are required")
    payload = _json(Path(event_path))
    if not isinstance(payload, dict):
        raise ValueError("GitHub event payload must be a JSON object")
    return event_name, payload


def _event_run_head_sha(controller_sha: str) -> tuple[str, str]:
    """Capture immutable event/API run head separately from execution GITHUB_SHA."""
    event_name, payload = _event_payload()
    if event_name in {"pull_request", "pull_request_target"}:
        pull_request = payload.get("pull_request")
        if not isinstance(pull_request, dict):
            raise ValueError("pull_request event payload is missing pull_request")
        head = pull_request.get("head")
        if not isinstance(head, dict):
            raise ValueError("pull_request event payload is missing pull_request.head")
        return event_name, _full_sha(head.get("sha"), "pull_request.head.sha")
    if event_name == "push":
        after = _full_sha(payload.get("after"), "push after")
        if after != controller_sha:
            raise ValueError("push payload after does not match execution GITHUB_SHA")
        return event_name, after
    # Non-PR, non-push read-only probes do not have a distinct immutable PR
    # head. Bind API run.head_sha to the execution SHA for those event types.
    return event_name, controller_sha


def _host_environment(source_digests: dict[str, str]) -> dict[str, str]:
    cpu_count = os.cpu_count()
    if cpu_count is None:
        raise ValueError("CPU count is required")
    cpu = platform.processor() or platform.machine()
    if not cpu:
        raise ValueError("CPU identity is required")
    return {
        "system": platform.system(),
        "kernel": platform.release(),
        "arch": platform.machine(),
        "cpu": cpu,
        "cpu_count": str(cpu_count),
        "image_version": os.environ["ImageVersion"],  # noqa: SIM112 - GitHub-hosted image contract.
        "python": platform.python_version(),
        "source_uv_lock_sha256": source_digests["uv.lock"],
        "source_build_release_yml_sha256": source_digests[WORKFLOW_PATH],
    }


def record_job(side: str, source_root: Path, source_sha: str) -> dict[str, object]:
    """Capture a minimal hosted-runner identity for one read-only job."""
    side = _side(side)
    source_root = source_root.resolve()
    source_sha = _full_sha(source_sha, "source_sha")
    actual = _git_head(source_root)
    if actual != source_sha:
        raise ValueError(f"source-root HEAD {actual} does not match expected {source_sha}")
    source_digests = _source_digests(source_root)
    workflow = _workflow_env(source_sha)
    return {
        "kind": KIND_JOB,
        "schema_version": ABI_VERSION,
        "side": side,
        "recorded_at": _utc_now(),
        "source_root": str(source_root),
        "workflow": workflow,
        "environment": _host_environment(source_digests),
    }


def _walk_files(root: Path) -> list[Path]:
    if not root.exists():
        raise FileNotFoundError(root)
    return sorted(path for path in root.rglob("*") if path.is_file())


def _find_one(root: Path, name: str) -> Path:
    matches = [path for path in _walk_files(root) if path.name == name]
    if len(matches) != 1:
        raise ValueError(f"Expected exactly one {name} under {root}, found {len(matches)}")
    return matches[0]


def _find_optional(root: Path, name: str) -> Path | None:
    matches = [path for path in _walk_files(root) if path.name == name]
    if len(matches) > 1:
        raise ValueError(f"Expected at most one {name} under {root}, found {len(matches)}")
    return matches[0] if matches else None


def _read_checksum_sidecar(path: Path, archive: str, digest: str) -> None:
    line = path.read_text(encoding="ascii")
    match = re.fullmatch(r"([0-9a-fA-F]{64})\s+\*?([A-Za-z0-9_.-]+)\n?", line)
    if match is None:
        raise ValueError(f"Malformed checksum sidecar: {path.name}")
    observed, filename = match.group(1).lower(), match.group(2)
    if observed != digest or filename != archive:
        raise ValueError(f"Checksum sidecar mismatch: {path.name}")


def _parse_loose_checksum(path: Path, binary_name: str) -> tuple[str, str]:
    line = path.read_text(encoding="ascii")
    match = re.fullmatch(r"([0-9a-fA-F]{64})\s+\*?([A-Za-z0-9_.\-/]+)\n?", line)
    if match is None:
        raise ValueError(f"Malformed executable checksum sidecar: {path.name}")
    digest, member = match.group(1).lower(), match.group(2)
    archive_member = member.removeprefix("dist/")
    require_member_path(archive_member, binary_name)
    if archive_member != f"{binary_name}/{executable_name(binary_name)}":
        raise ValueError(f"Executable checksum sidecar targets {member}")
    return digest, member


def _normal_archive_parts(value: str) -> list[str] | None:
    if (
        not value
        or value.startswith("/")
        or "\\" in value
        or ":" in value
        or value.startswith("//")
    ):
        return None
    parts: list[str] = []
    for part in value.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if not parts:
                return None
            parts.pop()
        else:
            parts.append(part)
    return parts


def _link_is_safe(binary_name: str, member: str, target: str, *, relative_to_parent: bool) -> bool:
    if target.startswith("/") or "\\" in target or ":" in target:
        return False
    base = str(Path(member).parent).replace("\\", "/") if relative_to_parent else ""
    candidate = f"{base}/{target}" if base else target
    parts = _normal_archive_parts(candidate)
    if parts is None:
        return False
    return bool(parts) and parts[0] == binary_name


def _zip_mode(item: zipfile.ZipInfo) -> int:
    return item.external_attr >> 16


def _is_zip_symlink(item: zipfile.ZipInfo) -> bool:
    return stat.S_ISLNK(_zip_mode(item))


def _zip_member_digest(archive: Path, binary_name: str) -> tuple[str, int]:
    executable = f"{binary_name}/{executable_name(binary_name)}"
    seen: set[str] = set()
    executable_digest: str | None = None
    with zipfile.ZipFile(archive) as source:
        members = source.infolist()
        if not members:
            raise ValueError(f"{archive.name} is empty")
        for item in members:
            require_member_path(item.filename, binary_name)
            if item.filename in seen:
                raise ValueError(f"Duplicate archive member: {item.filename}")
            seen.add(item.filename)
            if _is_zip_symlink(item):
                raise ValueError("ZIP archive contains an unsupported symbolic link")
            if item.filename == executable:
                mode = _zip_mode(item)
                if item.is_dir() or (mode and not stat.S_ISREG(mode)):
                    raise ValueError(f"{executable} must be a regular file")
                if executable_name(binary_name) == "apm" and mode and not (mode & 0o111):
                    raise ValueError(f"{executable} must be executable")
                with source.open(item) as stream:
                    executable_digest = file_digest_stream(stream)
        if executable_digest is None:
            raise ValueError(f"{archive.name} must contain exactly one {executable}")
    return executable_digest, len(seen)


def file_digest_stream(stream: Any) -> str:
    """Hash an already-open binary stream without extracting it to disk."""
    import hashlib

    digest = hashlib.sha256()
    while chunk := stream.read(1024 * 1024):
        digest.update(chunk)
    return digest.hexdigest()


def _tar_member_digest(archive: Path, binary_name: str) -> tuple[str, int]:
    executable = f"{binary_name}/{executable_name(binary_name)}"
    seen: set[str] = set()
    executable_digest: str | None = None
    with tarfile.open(archive, "r:gz") as source:
        members = source.getmembers()
        if not members:
            raise ValueError(f"{archive.name} is empty")
        for item in members:
            require_member_path(item.name, binary_name)
            if item.name in seen:
                raise ValueError(f"Duplicate archive member: {item.name}")
            seen.add(item.name)
            if item.issym() and not _link_is_safe(
                binary_name, item.name, item.linkname, relative_to_parent=True
            ):
                raise ValueError("Archive symbolic link escapes its native bundle")
            if item.islnk() and not _link_is_safe(
                binary_name, item.name, item.linkname, relative_to_parent=False
            ):
                raise ValueError("Archive hard link escapes its native bundle")
            if item.name == executable:
                if not item.isfile():
                    raise ValueError(f"{executable} must be a regular file")
                if not (item.mode & 0o111):
                    raise ValueError(f"{executable} must be executable")
                stream = source.extractfile(item)
                if stream is None:
                    raise ValueError(f"Cannot read {executable}")
                with stream:
                    executable_digest = file_digest_stream(stream)
        if executable_digest is None:
            raise ValueError(f"{archive.name} must contain exactly one {executable}")
    return executable_digest, len(seen)


def _candidate_metadata(
    metadata_root: Path,
    side: str,
    binary_name: str,
    archive: str,
    source_sha: str,
    version: str,
    archive_digest: str,
    executable_digest: str,
) -> str | None:
    path = _find_optional(metadata_root, f"{binary_name}.json")
    if path is None:
        if side == "proposed":
            raise ValueError(f"Proposed proof requires candidate metadata for {binary_name}")
        return None
    data = _json(path)
    required = {
        "schema_version": 1,
        "sha": source_sha,
        "version": version,
        "binary_name": binary_name,
        "archive": archive,
        "archive_sha256": archive_digest,
        "executable_sha256": executable_digest,
    }
    for key, expected in required.items():
        if data.get(key) != expected:
            raise ValueError(f"Candidate metadata {binary_name} {key} mismatch")
    return file_digest(path)


def _loose_path(raw_root: Path, sidecar: Path, member: str) -> Path | None:
    candidates = [
        raw_root / member,
        sidecar.parent / member,
        sidecar.parent / member.removeprefix("dist/"),
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def _loose_executable_sidecar(
    raw_root: Path, side: str, binary_name: str, executable_digest: str
) -> tuple[str | None, str | None]:
    path = _find_optional(raw_root, f"{binary_name}.sha256")
    if path is None:
        if side == "baseline":
            raise ValueError(f"Baseline raw executable checksum is missing: {binary_name}.sha256")
        return None, None
    expected, member = _parse_loose_checksum(path, binary_name)
    if expected != executable_digest:
        raise ValueError(f"Loose executable checksum mismatch: {path.name}")
    loose = _loose_path(raw_root, path, member)
    if loose is None:
        if side == "baseline":
            raise ValueError(f"Baseline raw executable payload is missing: {member}")
        return file_digest(path), None
    loose_digest = file_digest(loose)
    if loose_digest != executable_digest:
        raise ValueError(f"Loose executable digest mismatch: {loose}")
    return file_digest(path), loose_digest


def _verify_native_archives(
    side: str, native_root: Path, source_sha: str, version: str
) -> dict[str, dict[str, object]]:
    archives_root = native_root / "archives" if (native_root / "archives").is_dir() else native_root
    raw_root = native_root / "raw" if (native_root / "raw").is_dir() else native_root
    inventory: dict[str, dict[str, object]] = {}
    for binary_name in BINARY_NAMES:
        archive = archive_name(binary_name)
        archive_path = _find_one(archives_root, archive)
        sidecar_path = _find_one(archives_root, f"{archive}.sha256")
        archive_digest = file_digest(archive_path)
        _read_checksum_sidecar(sidecar_path, archive, archive_digest)
        if archive.endswith(".zip"):
            executable_digest, member_count = _zip_member_digest(archive_path, binary_name)
        else:
            executable_digest, member_count = _tar_member_digest(archive_path, binary_name)
        metadata_digest = _candidate_metadata(
            archives_root,
            side,
            binary_name,
            archive,
            source_sha,
            version,
            archive_digest,
            executable_digest,
        )
        loose_sidecar_digest, loose_executable_digest = _loose_executable_sidecar(
            raw_root, side, binary_name, executable_digest
        )
        inventory[binary_name] = ArchiveEvidence(
            binary_name=binary_name,
            archive=archive,
            archive_sha256=archive_digest,
            executable_sha256=executable_digest,
            sidecar=f"{archive}.sha256",
            candidate_metadata_sha256=metadata_digest,
            loose_executable_sidecar_sha256=loose_sidecar_digest,
            loose_executable_sha256=loose_executable_digest,
            member_count=member_count,
        ).payload()
    _reject_unexpected_native_release_files(archives_root, side)
    return inventory


def _reject_unexpected_native_release_files(native_root: Path, side: str) -> None:
    expected: set[str] = set()
    for binary_name in BINARY_NAMES:
        archive = archive_name(binary_name)
        expected.update({archive, f"{archive}.sha256", f"{binary_name}.sha256"})
        if side == "proposed":
            expected.add(f"{binary_name}.json")
    release_like = [
        path.name
        for path in _walk_files(native_root)
        if path.name.startswith("apm-")
        and re.search(r"\.(tar\.gz|zip|sha256|json)$", path.name)
        and path.name not in expected
    ]
    if release_like:
        raise ValueError(f"Unexpected native release files: {', '.join(sorted(release_like))}")


def _docs_index(docs_root: Path) -> dict[str, object]:
    root_index = docs_root / "index.html"
    if root_index.is_file():
        if root_index.stat().st_size <= 0:
            raise ValueError("docs index.html is empty")
        return {"format": "directory", "index": "index.html", "sha256": file_digest(root_index)}
    tars = [
        path
        for path in _walk_files(docs_root)
        if path.suffix == ".tar" or path.name.endswith(".tar.gz")
    ]
    if len(tars) != 1:
        raise ValueError("Docs artifact must contain index.html or exactly one tar archive")
    with tarfile.open(tars[0], "r:*") as source:
        matches = [
            item
            for item in source.getmembers()
            if item.name.removeprefix("./").rstrip("/") == "index.html"
        ]
        if len(matches) != 1:
            raise ValueError("Docs artifact tar must contain exactly one root index.html")
        index = matches[0]
        if not index.isfile() or index.size <= 0:
            raise ValueError("Docs artifact index.html must be a nonempty regular file")
    return {
        "format": "tar",
        "archive": tars[0].name,
        "archive_sha256": file_digest(tars[0]),
        "index": "index.html",
    }


def _metadata_value(blob: bytes, key: str) -> str:
    parsed = Parser().parsestr(blob.decode("utf-8"))
    value = parsed.get(key)
    if not value:
        raise ValueError(f"Distribution metadata is missing {key}")
    return value


def _wheel_distribution(path: Path, project_name: str, version: str) -> dict[str, object]:
    expected_name = _canonical_project_name(project_name)
    expected_token = _distribution_token(project_name)
    if not path.name.startswith(f"{expected_token}-{version}-") or not path.name.endswith(".whl"):
        raise ValueError(f"Wheel filename does not match source project/version: {path.name}")
    with zipfile.ZipFile(path) as wheel:
        names = wheel.namelist()
        metadata = [
            name for name in names if name.endswith(".dist-info/METADATA") and name.count("/") == 1
        ]
        if len(metadata) != 1:
            raise ValueError(f"Wheel must contain exactly one METADATA: {path.name}")
        dist_info = metadata[0].removesuffix("/METADATA")
        required = {f"{dist_info}/METADATA", f"{dist_info}/WHEEL", f"{dist_info}/RECORD"}
        if not required.issubset(names):
            raise ValueError(f"Wheel is missing METADATA, WHEEL or RECORD: {path.name}")
        if not dist_info.startswith(f"{expected_token}-{version}.dist-info"):
            raise ValueError(f"Wheel dist-info directory does not match source: {path.name}")
        if not any(
            name.startswith(f"{expected_token}/") and not name.endswith("/") for name in names
        ):
            raise ValueError(f"Wheel does not contain source package code: {path.name}")
        with wheel.open(f"{dist_info}/METADATA") as stream:
            metadata_blob = stream.read()
        with wheel.open(f"{dist_info}/WHEEL") as stream:
            if not stream.read().strip():
                raise ValueError(f"Wheel WHEEL metadata is empty: {path.name}")
        with wheel.open(f"{dist_info}/RECORD") as stream:
            if not stream.read().strip():
                raise ValueError(f"Wheel RECORD metadata is empty: {path.name}")
    name = _metadata_value(metadata_blob, "Name")
    found_version = _metadata_value(metadata_blob, "Version")
    if _canonical_project_name(name) != expected_name or found_version != version:
        raise ValueError("Wheel metadata name/version does not match source pyproject")
    return {
        "name": path.name,
        "sha256": file_digest(path),
        "project_name": name,
        "version": found_version,
        "dist_info": dist_info,
    }


def _sdist_distribution(path: Path, project_name: str, version: str) -> dict[str, object]:
    expected_name = _canonical_project_name(project_name)
    expected_token = _distribution_token(project_name)
    expected_root = f"{expected_token}-{version}"
    if path.name != f"{expected_root}.tar.gz":
        raise ValueError(f"sdist filename does not match source project/version: {path.name}")
    with tarfile.open(path, "r:gz") as sdist:
        members = sdist.getmembers()
        names = [item.name.rstrip("/") for item in members if item.name.rstrip("/")]
        roots = {name.split("/", 1)[0] for name in names}
        if roots != {expected_root}:
            raise ValueError(f"sdist top-level directory does not match source: {path.name}")
        metadata = [item for item in members if item.name == f"{expected_root}/PKG-INFO"]
        if len(metadata) != 1:
            raise ValueError(f"sdist must contain exactly one top-level PKG-INFO: {path.name}")
        pyprojects = [item for item in members if item.name == f"{expected_root}/pyproject.toml"]
        if len(pyprojects) != 1 or not pyprojects[0].isfile() or pyprojects[0].size <= 0:
            raise ValueError(f"sdist must contain top-level pyproject.toml: {path.name}")
        if not any(
            item.isfile()
            and (
                item.name.startswith(f"{expected_root}/src/{expected_token}/")
                or item.name.startswith(f"{expected_root}/{expected_token}/")
            )
            for item in members
        ):
            raise ValueError(f"sdist does not contain source package code: {path.name}")
        stream = sdist.extractfile(metadata[0])
        if stream is None:
            raise ValueError(f"Cannot read sdist metadata: {path.name}")
        with stream:
            metadata_blob = stream.read()
    name = _metadata_value(metadata_blob, "Name")
    found_version = _metadata_value(metadata_blob, "Version")
    if _canonical_project_name(name) != expected_name or found_version != version:
        raise ValueError("sdist metadata name/version does not match source pyproject")
    return {
        "name": path.name,
        "sha256": file_digest(path),
        "project_name": name,
        "version": found_version,
        "root": expected_root,
    }


def _python_distributions(python_root: Path, project_name: str, version: str) -> dict[str, object]:
    wheels = [path for path in _walk_files(python_root) if path.suffix == ".whl"]
    sdists = [
        path
        for path in _walk_files(python_root)
        if path.name.endswith(".tar.gz") and not path.name.startswith("apm-")
    ]
    if len(wheels) != 1 or len(sdists) != 1:
        raise ValueError(
            f"Expected exactly one wheel and one sdist, found {len(wheels)} wheel(s) and {len(sdists)} sdist(s)"
        )
    return {
        "source_project_name": project_name,
        "wheel": _wheel_distribution(wheels[0], project_name, version),
        "sdist": _sdist_distribution(sdists[0], project_name, version),
    }


def _job_records(
    roots: list[Path], side: str, source_sha: str, workflow: dict[str, object]
) -> list[dict[str, object]]:
    records = []
    for root in roots:
        for path in _walk_files(root):
            if path.suffix != ".json":
                continue
            data = _json(path)
            if not isinstance(data, dict) or data.get("kind") != KIND_JOB:
                continue
            if data.get("side") != side:
                raise ValueError(f"Wall-clock job record side mismatch: {path}")
            record_workflow = data.get("workflow")
            if not isinstance(record_workflow, dict):
                raise ValueError(f"Wall-clock job record workflow malformed: {path}")
            for key in ("source_sha", "run_id", "run_attempt", "controller_sha", "run_head_sha"):
                if record_workflow.get(key) != workflow[key]:
                    raise ValueError(f"Wall-clock job record {key} mismatch: {path}")
            if record_workflow.get("source_sha") != source_sha:
                raise ValueError(f"Wall-clock job record source mismatch: {path}")
            records.append(
                {
                    "path": str(path),
                    "github_job": record_workflow.get("github_job"),
                    "runner_name": record_workflow.get("runner_name"),
                }
            )
    names = [(item["github_job"], item["runner_name"]) for item in records]
    if len(names) != len(set(names)):
        raise ValueError("Duplicate wall-clock job record identity")
    _validate_job_records({"job_records": records}, {"job_records": len(records)})
    return records


def verify_artifacts(
    side: str,
    source_root: Path,
    source_sha: str,
    native_root: Path,
    docs_root: Path,
    python_root: Path,
) -> dict[str, object]:
    """Verify downloaded read-only artifacts and emit a non-promotable proof."""
    side = _side(side)
    source_root = source_root.resolve()
    source_sha = _full_sha(source_sha, "source_sha")
    actual = _git_head(source_root)
    if actual != source_sha:
        raise ValueError(f"source-root HEAD {actual} does not match expected {source_sha}")
    source_digests = _source_digests(source_root)
    source_name, version = _source_metadata(source_root)
    workflow = _workflow_env(source_sha)
    native = _verify_native_archives(side, native_root, source_sha, version)
    docs = _docs_index(docs_root)
    python_dists = _python_distributions(python_root, source_name, version)
    records = _job_records([native_root, docs_root, python_root], side, source_sha, workflow)
    return {
        "kind": KIND_PROOF,
        "schema_version": ABI_VERSION,
        "abi": "scripts.release_wallclock.v1",
        "promotable": False,
        "side": side,
        "source_sha": source_sha,
        "controller_sha": workflow["controller_sha"],
        "run_head_sha": workflow["run_head_sha"],
        "event_name": workflow["event_name"],
        "run_id": workflow["run_id"],
        "run_attempt": workflow["run_attempt"],
        "version": version,
        "workflow_digest": source_digests[WORKFLOW_PATH],
        "filehashes": {
            "source_uv_lock_sha256": source_digests["uv.lock"],
            "source_build_release_yml_sha256": source_digests[WORKFLOW_PATH],
            "native": native,
            "docs": docs,
            "python": python_dists,
        },
        "counts": {
            "native_archives": len(native),
            "native_sidecars": len(BINARY_NAMES),
            "candidate_metadata": sum(
                item["candidate_metadata_sha256"] is not None for item in native.values()
            ),
            "job_records": len(records),
            "docs_indexes": 1,
            "python_distributions": 2,
        },
        "job_records": records,
        "captured_at": _utc_now(),
        "scope": "read-only wall-clock artifact proof; not release promotion evidence",
    }


def _require_sha256(value: object, label: str) -> str:
    if not _is_sha256(value):
        raise ValueError(f"{label} must be a lowercase sha256 hex digest")
    return str(value)


def _validate_native_proof(native: object, side: str, candidate_metadata_count: object) -> None:
    if not isinstance(native, dict) or set(native) != set(BINARY_NAMES):
        raise ValueError("Wall-clock proof native inventory must contain exactly five platforms")
    expected_metadata = 5 if side == "proposed" else 0
    if candidate_metadata_count != expected_metadata:
        raise ValueError(f"{side} candidate metadata count is malformed")
    for binary_name in BINARY_NAMES:
        item = native.get(binary_name)
        if not isinstance(item, dict):
            raise ValueError(f"Native proof entry is malformed: {binary_name}")
        archive = archive_name(binary_name)
        if item.get("binary_name") != binary_name or item.get("archive") != archive:
            raise ValueError(f"Native proof identity mismatch: {binary_name}")
        if item.get("sidecar") != f"{archive}.sha256":
            raise ValueError(f"Native proof sidecar mismatch: {binary_name}")
        _require_sha256(item.get("archive_sha256"), f"{binary_name} archive_sha256")
        _require_sha256(item.get("executable_sha256"), f"{binary_name} executable_sha256")
        if not isinstance(item.get("member_count"), int) or item["member_count"] <= 0:
            raise ValueError(f"Native proof member count is malformed: {binary_name}")
        metadata_digest = item.get("candidate_metadata_sha256")
        loose_sidecar = item.get("loose_executable_sidecar_sha256")
        loose_executable = item.get("loose_executable_sha256")
        if side == "proposed":
            _require_sha256(metadata_digest, f"{binary_name} candidate_metadata_sha256")
        elif metadata_digest is not None:
            raise ValueError(f"Baseline proof must not carry candidate metadata: {binary_name}")
        if side == "baseline":
            _require_sha256(loose_sidecar, f"{binary_name} loose_executable_sidecar_sha256")
            if loose_executable != item.get("executable_sha256"):
                raise ValueError(f"Baseline raw executable digest mismatch: {binary_name}")
        elif loose_sidecar is not None:
            _require_sha256(loose_sidecar, f"{binary_name} loose_executable_sidecar_sha256")
            if loose_executable is not None:
                _require_sha256(loose_executable, f"{binary_name} loose_executable_sha256")


def _validate_docs_proof(docs: object) -> None:
    if not isinstance(docs, dict) or docs.get("index") != "index.html":
        raise ValueError("Docs proof is malformed")
    if docs.get("format") == "directory":
        _require_sha256(docs.get("sha256"), "docs sha256")
    elif docs.get("format") == "tar":
        if not isinstance(docs.get("archive"), str) or not docs["archive"]:
            raise ValueError("Docs proof tar archive name is malformed")
        _require_sha256(docs.get("archive_sha256"), "docs archive_sha256")
    else:
        raise ValueError("Docs proof format is malformed")


def _validate_python_proof(python_proof: object) -> None:
    if not isinstance(python_proof, dict):
        raise ValueError("Python distribution proof is malformed")
    source_name = python_proof.get("source_project_name")
    if not isinstance(source_name, str) or not source_name:
        raise ValueError("Python proof source project name is missing")
    canonical = _canonical_project_name(source_name)
    wheel = python_proof.get("wheel")
    sdist = python_proof.get("sdist")
    for label, item in (("wheel", wheel), ("sdist", sdist)):
        if not isinstance(item, dict):
            raise ValueError(f"Python {label} proof is malformed")
        if not isinstance(item.get("name"), str) or not item["name"]:
            raise ValueError(f"Python {label} filename is missing")
        _require_sha256(item.get("sha256"), f"Python {label} sha256")
        if _canonical_project_name(str(item.get("project_name") or "")) != canonical:
            raise ValueError(f"Python {label} project name does not match source")
        if not isinstance(item.get("version"), str) or not item["version"]:
            raise ValueError(f"Python {label} version is missing")
    if wheel["version"] != sdist["version"]:
        raise ValueError("Python wheel and sdist versions do not match")
    if not str(wheel["name"]).endswith(".whl") or not str(sdist["name"]).endswith(".tar.gz"):
        raise ValueError("Python distribution filenames are malformed")


def _validate_job_records(proof: dict[str, object], counts: dict[str, object]) -> None:
    records = proof.get("job_records")
    if not isinstance(records, list) or not records:
        raise ValueError("Wall-clock proof must contain at least one job record")
    if counts.get("job_records") != len(records):
        raise ValueError("Wall-clock proof job record count does not match")
    seen: set[tuple[str, str]] = set()
    for record in records:
        if not isinstance(record, dict):
            raise ValueError("Wall-clock job record is malformed")
        github_job = record.get("github_job")
        runner_name = record.get("runner_name")
        if not isinstance(github_job, str) or not github_job:
            raise ValueError("Wall-clock job record is missing github_job")
        if not isinstance(runner_name, str) or not runner_name:
            raise ValueError("Wall-clock job record is missing runner_name")
        key = (github_job, runner_name)
        if key in seen:
            raise ValueError("Wall-clock proof contains duplicate job record identity")
        seen.add(key)


def _read_proof(path: Path, side: str, expected_sha: str) -> dict[str, object]:
    proof = _json(path)
    if not isinstance(proof, dict):
        raise ValueError(f"Malformed proof: {path}")
    if proof.get("kind") != KIND_PROOF or proof.get("schema_version") != ABI_VERSION:
        raise ValueError(f"Unsupported proof schema: {path}")
    if proof.get("promotable") is not False:
        raise ValueError(f"Wall-clock proof must be non-promotable: {path}")
    if proof.get("side") != side or proof.get("source_sha") != expected_sha:
        raise ValueError(f"Proof identity mismatch: {path}")
    if proof.get("run_attempt") != 1:
        raise ValueError(f"Proof must describe attempt 1: {path}")
    _full_sha(proof.get("controller_sha"), "proof controller_sha")
    _full_sha(proof.get("source_sha"), "proof source_sha")
    _full_sha(proof.get("run_head_sha"), "proof run_head_sha")
    counts = proof.get("counts")
    if not isinstance(counts, dict) or {
        "native_archives": counts.get("native_archives"),
        "native_sidecars": counts.get("native_sidecars"),
        "candidate_metadata": counts.get("candidate_metadata"),
        "docs_indexes": counts.get("docs_indexes"),
        "python_distributions": counts.get("python_distributions"),
    } != {
        "native_archives": len(BINARY_NAMES),
        "native_sidecars": len(BINARY_NAMES),
        "candidate_metadata": 5 if side == "proposed" else 0,
        "docs_indexes": 1,
        "python_distributions": 2,
    }:
        raise ValueError(f"Malformed wall-clock proof counts: {path}")
    filehashes = proof.get("filehashes")
    if not isinstance(filehashes, dict):
        raise ValueError(f"Malformed wall-clock proof file hashes: {path}")
    workflow_digest = _require_sha256(proof.get("workflow_digest"), "proof workflow_digest")
    if filehashes.get("source_build_release_yml_sha256") != workflow_digest:
        raise ValueError("Proof workflow digest does not match source workflow hash")
    _require_sha256(filehashes.get("source_uv_lock_sha256"), "source_uv_lock_sha256")
    _validate_native_proof(filehashes.get("native"), side, counts.get("candidate_metadata"))
    _validate_docs_proof(filehashes.get("docs"))
    _validate_python_proof(filehashes.get("python"))
    _validate_job_records(proof, counts)
    return proof


def _read_run(path: Path) -> tuple[dict[str, object], list[dict[str, object]]]:
    data = _json(path)
    if (
        not isinstance(data, dict)
        or not isinstance(data.get("run"), dict)
        or not isinstance(data.get("jobs"), list)
        or not all(isinstance(job, dict) for job in data["jobs"])
    ):
        raise ValueError(f"Run JSON must be {{run, jobs}}: {path}")
    return data["run"], data["jobs"]


def _run_id(value: object, label: str) -> int:
    return _positive_int(value, label)


def _validate_run(
    run_path: Path,
    proof_path: Path,
    side: str,
    expected_sha: str,
    expected_controller_sha: str,
    expected_run_head_sha: str,
    terminal_name: str,
    required_names: tuple[str, ...],
) -> dict[str, object]:
    if not required_names or set(required_names) == {terminal_name}:
        raise ValueError(f"{side} required job set must contain non-terminal jobs")
    if len(set(required_names)) != len(required_names):
        raise ValueError(f"{side} required job set contains duplicates")
    if terminal_name in required_names:
        raise ValueError(f"{side} required job set must not include the terminal job")
    run, jobs = _read_run(run_path)
    proof = _read_proof(proof_path, side, expected_sha)
    run_id = _run_id(run.get("id"), f"{side} run id")
    attempt = _positive_int(run.get("run_attempt"), f"{side} run_attempt")
    if attempt != 1:
        raise ValueError(f"{side} run must be attempt 1")
    if proof["run_id"] != run_id or proof["run_attempt"] != attempt:
        raise ValueError(f"{side} proof run identity does not match run JSON")
    if run.get("status") != "completed" or run.get("conclusion") != "success":
        raise ValueError(f"{side} workflow run did not complete successfully")
    if proof["controller_sha"] != expected_controller_sha:
        raise ValueError(f"{side} proof controller SHA does not match expected execution SHA")
    if proof["run_head_sha"] != expected_run_head_sha:
        raise ValueError(f"{side} proof run head SHA does not match expected event head SHA")
    if run.get("head_sha") != proof["run_head_sha"]:
        raise ValueError(f"{side} API run head_sha does not match recorded event head SHA")
    created = _timestamp(run.get("created_at"), f"{side} run.created_at")
    started = _timestamp(run.get("run_started_at"), f"{side} run.run_started_at")
    if started < created:
        raise ValueError(f"{side} run_started_at is before created_at")
    terminal = _select_terminal(jobs, run_id, terminal_name, required_names, side)
    terminal_start = _timestamp(terminal["started_at"], f"{side} terminal.started_at")
    terminal_end = _timestamp(terminal["completed_at"], f"{side} terminal.completed_at")
    if terminal_end < terminal_start:
        raise ValueError(f"{side} terminal job has an invalid timeline")
    executed_jobs = []
    runner_seconds = 0.0
    for job in jobs:
        if job.get("conclusion") == "skipped":
            continue
        start = _timestamp(job.get("started_at"), f"{side} job.started_at")
        end = _timestamp(job.get("completed_at"), f"{side} job.completed_at")
        if end < start or start < created:
            raise ValueError(f"{side} job has an invalid timeline: {job.get('name')}")
        if job is not terminal and end > terminal_start:
            raise ValueError(f"{side} terminal job started before {job.get('name')} completed")
        executed_jobs.append(job)
        runner_seconds += (end - start).total_seconds()
    return {
        "side": side,
        "run_id": run_id,
        "run_attempt": attempt,
        "source_sha": expected_sha,
        "controller_sha": proof["controller_sha"],
        "run_head_sha": proof["run_head_sha"],
        "event_name": proof.get("event_name"),
        "created_at": run["created_at"],
        "run_started_at": run["run_started_at"],
        "terminal_job": terminal_name,
        "terminal_completed_at": terminal["completed_at"],
        "wallclock_seconds_from_created_at": _seconds_between(
            created, terminal_end, f"{side} primary wall-clock"
        ),
        "wallclock_seconds_from_run_started_at": _seconds_between(
            started, terminal_end, f"{side} secondary wall-clock"
        ),
        "unweighted_job_runner_seconds": runner_seconds,
        "job_count": len(jobs),
        "executed_job_count": len(executed_jobs),
        "skipped_job_count": len(jobs) - len(executed_jobs),
        "version": proof.get("version"),
    }


def _select_terminal(
    jobs: list[dict[str, object]],
    run_id: int,
    terminal_name: str,
    required_names: tuple[str, ...],
    side: str,
) -> dict[str, object]:
    required_seen = {name: 0 for name in required_names}
    terminals = []
    for job in jobs:
        if _run_id(job.get("run_id"), f"{side} job run_id") != run_id:
            raise ValueError(f"{side} job belongs to a different run")
        if _positive_int(job.get("run_attempt"), f"{side} job run_attempt") != 1:
            raise ValueError(f"{side} job belongs to a different attempt")
        if job.get("status") != "completed":
            raise ValueError(f"{side} job did not complete: {job.get('name')}")
        conclusion = job.get("conclusion")
        if conclusion in BAD_JOB_CONCLUSIONS or conclusion not in {"success", "skipped"}:
            raise ValueError(f"{side} job has rejected conclusion {conclusion}: {job.get('name')}")
        if job.get("name") == terminal_name:
            terminals.append(job)
        if job.get("name") in required_seen:
            required_seen[str(job["name"])] += 1
            if conclusion != "success":
                raise ValueError(f"{side} required job did not succeed: {job.get('name')}")
    if len(terminals) != 1:
        raise ValueError(f"{side} expected exactly one terminal job named {terminal_name}")
    terminal = terminals[0]
    if terminal.get("conclusion") != "success":
        raise ValueError(f"{side} terminal job did not succeed")
    missing = [name for name, count in required_seen.items() if count != 1]
    if missing:
        raise ValueError(f"{side} required job missing or duplicated: {', '.join(missing)}")
    return terminal


def compare_runs(
    baseline_run: Path,
    proposed_run: Path,
    baseline_proof: Path,
    proposed_proof: Path,
    expected_baseline_sha: str,
    expected_proposed_sha: str,
    expected_baseline_controller_sha: str | None,
    expected_proposed_controller_sha: str | None,
    expected_baseline_run_head_sha: str,
    expected_proposed_run_head_sha: str,
    terminal_name: str,
    baseline_required_names: tuple[str, ...],
    proposed_required_names: tuple[str, ...],
) -> dict[str, object]:
    """Compare two real successful workflow attempts without modeling gate lag."""
    expected_baseline_sha = _full_sha(expected_baseline_sha, "expected_baseline_sha")
    expected_proposed_sha = _full_sha(expected_proposed_sha, "expected_proposed_sha")
    baseline_controller = _full_sha(
        expected_baseline_controller_sha or expected_baseline_sha,
        "expected_baseline_controller_sha",
    )
    proposed_controller = _full_sha(
        expected_proposed_controller_sha or expected_proposed_sha,
        "expected_proposed_controller_sha",
    )
    expected_baseline_run_head_sha = _full_sha(
        expected_baseline_run_head_sha, "expected_baseline_run_head_sha"
    )
    expected_proposed_run_head_sha = _full_sha(
        expected_proposed_run_head_sha, "expected_proposed_run_head_sha"
    )
    baseline = _validate_run(
        baseline_run,
        baseline_proof,
        "baseline",
        expected_baseline_sha,
        baseline_controller,
        expected_baseline_run_head_sha,
        terminal_name,
        baseline_required_names,
    )
    proposed = _validate_run(
        proposed_run,
        proposed_proof,
        "proposed",
        expected_proposed_sha,
        proposed_controller,
        expected_proposed_run_head_sha,
        terminal_name,
        proposed_required_names,
    )
    if baseline["run_id"] == proposed["run_id"]:
        raise ValueError("Baseline and proposed run IDs must differ")
    if (
        baseline["wallclock_seconds_from_created_at"] <= 0
        or baseline["wallclock_seconds_from_run_started_at"] <= 0
    ):
        raise ValueError("Baseline wall-clock denominators must be positive")
    primary_gain = (
        baseline["wallclock_seconds_from_created_at"]
        - proposed["wallclock_seconds_from_created_at"]
    )
    secondary_gain = (
        baseline["wallclock_seconds_from_run_started_at"]
        - proposed["wallclock_seconds_from_run_started_at"]
    )
    return {
        "kind": KIND_COMPARISON,
        "schema_version": ABI_VERSION,
        "readonly_adapter": True,
        "production_qualification": False,
        "terminal_job": terminal_name,
        "baseline_required_job_names": sorted(baseline_required_names),
        "proposed_required_job_names": sorted(proposed_required_names),
        "baseline": baseline,
        "proposed": proposed,
        "primary_metric": "terminal job completed_at minus workflow created_at",
        "secondary_metric": "terminal job completed_at minus workflow run_started_at",
        "delta_seconds": {
            "primary_proposed_minus_baseline": -primary_gain,
            "primary_gain_seconds": primary_gain,
            "secondary_proposed_minus_baseline": -secondary_gain,
            "secondary_gain_seconds": secondary_gain,
            "unweighted_runner_seconds_proposed_minus_baseline": proposed[
                "unweighted_job_runner_seconds"
            ]
            - baseline["unweighted_job_runner_seconds"],
        },
        "gain_fraction": {
            "primary": primary_gain / baseline["wallclock_seconds_from_created_at"],
            "secondary": secondary_gain / baseline["wallclock_seconds_from_run_started_at"],
        },
        "gain_percent": {
            "primary": 100 * primary_gain / baseline["wallclock_seconds_from_created_at"],
            "secondary": 100 * secondary_gain / baseline["wallclock_seconds_from_run_started_at"],
        },
        "scope": "actual successful GitHub workflow attempts; no counterfactual gate lag model",
    }


def main() -> int:
    """Command-line entry point for the release wall-clock toolkit."""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="operation", required=True)

    record = commands.add_parser("record-job")
    record.add_argument("--side", choices=SIDE_CHOICES, required=True)
    record.add_argument("--source-root", type=Path, required=True)
    record.add_argument("--source-sha", required=True)
    record.add_argument("--output", type=Path, required=True)

    verify = commands.add_parser("verify")
    verify.add_argument("--side", choices=SIDE_CHOICES, required=True)
    verify.add_argument("--source-root", type=Path, required=True)
    verify.add_argument("--source-sha", required=True)
    verify.add_argument("--native-root", type=Path, required=True)
    verify.add_argument("--docs-root", type=Path, required=True)
    verify.add_argument("--python-root", type=Path, required=True)
    verify.add_argument("--output", type=Path, required=True)

    compare = commands.add_parser("compare")
    compare.add_argument("--baseline-run", type=Path, required=True)
    compare.add_argument("--proposed-run", type=Path, required=True)
    compare.add_argument("--baseline-proof", type=Path, required=True)
    compare.add_argument("--proposed-proof", type=Path, required=True)
    compare.add_argument("--expected-baseline-sha", required=True)
    compare.add_argument("--expected-proposed-sha", required=True)
    compare.add_argument("--expected-baseline-controller-sha")
    compare.add_argument("--expected-proposed-controller-sha")
    compare.add_argument("--expected-baseline-run-head-sha", required=True)
    compare.add_argument("--expected-proposed-run-head-sha", required=True)
    compare.add_argument("--terminal-job-name", default=TERMINAL_JOB)
    compare.add_argument("--baseline-required-job-name", action="append", required=True)
    compare.add_argument("--proposed-required-job-name", action="append", required=True)
    compare.add_argument("--output", type=Path, required=True)

    args = parser.parse_args()
    if args.operation == "record-job":
        result = record_job(args.side, args.source_root, args.source_sha)
    elif args.operation == "verify":
        result = verify_artifacts(
            args.side,
            args.source_root,
            args.source_sha,
            args.native_root,
            args.docs_root,
            args.python_root,
        )
    else:
        result = compare_runs(
            args.baseline_run,
            args.proposed_run,
            args.baseline_proof,
            args.proposed_proof,
            args.expected_baseline_sha,
            args.expected_proposed_sha,
            args.expected_baseline_controller_sha,
            args.expected_proposed_controller_sha,
            args.expected_baseline_run_head_sha,
            args.expected_proposed_run_head_sha,
            args.terminal_job_name,
            tuple(args.baseline_required_job_name),
            tuple(args.proposed_required_job_name),
        )
    _write_json(args.output, result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
