"""Package once, then verify and extract the exact native release candidate."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import tarfile
import zipfile
from pathlib import Path

import tomllib

BINARY_NAMES = tuple(
    row["binary_name"]
    for row in json.loads(Path(__file__).with_name("release-platforms.json").read_text("ascii"))
)


def file_digest(path: Path) -> str:
    """Hash a file without loading the binary bundle into memory."""
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def archive_name(binary_name: str) -> str:
    """Return the public archive name for one supported native platform."""
    if binary_name not in BINARY_NAMES:
        raise ValueError(f"Unsupported release binary: {binary_name}")
    suffix = ".zip" if binary_name == "apm-windows-x86_64" else ".tar.gz"
    return binary_name + suffix


def executable_name(binary_name: str) -> str:
    """Return the platform's native executable filename."""
    return "apm.exe" if binary_name == "apm-windows-x86_64" else "apm"


def require_sha(sha: str) -> None:
    """Reject abbreviated or malformed candidate identities."""
    if re.fullmatch(r"[0-9a-f]{40}", sha) is None:
        raise ValueError("Candidate SHA must be a full lowercase Git commit SHA")


def require_binary_identity(executable: Path, version: str, sha: str) -> None:
    """Reject an old or unbound executable even when its archive has valid hashes."""
    result = subprocess.run(  # noqa: S603 - executes only this trusted native build.
        [str(executable.resolve()), "--version"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=True,
        env={**os.environ, "NO_COLOR": "1", "COLUMNS": "200"},
    )
    match = re.search(rf"\bversion {re.escape(version)}\s+\(([0-9a-f]{{7,40}})\)", result.stdout)
    if match is None or not sha.startswith(match.group(1)):
        raise ValueError("Candidate executable version/build SHA does not match the checkout")


def package(
    binary_name: str, dist: Path, output: Path, sha: str, pyproject: Path
) -> dict[str, str | int]:
    """Create the archive that both native validation and publication consume."""
    require_sha(sha)
    name = archive_name(binary_name)
    bundle = dist / binary_name
    executable = bundle / executable_name(binary_name)
    if not executable.is_file():
        raise FileNotFoundError(f"Candidate executable not found: {executable}")
    if executable.name == "apm":
        executable.chmod(executable.stat().st_mode | 0o111)
    version = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["version"]
    require_binary_identity(executable, version, sha)
    output.mkdir(parents=True, exist_ok=True)
    archive = output / name
    if archive.suffix == ".zip":
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as target:
            for path in sorted(bundle.rglob("*")):
                target.write(path, path.relative_to(dist).as_posix())
    else:
        with tarfile.open(archive, "w:gz") as target:
            target.add(bundle, arcname=binary_name)
    digest = file_digest(archive)
    metadata: dict[str, str | int] = {
        "schema_version": 1,
        "sha": sha,
        "version": version,
        "binary_name": binary_name,
        "archive": name,
        "archive_sha256": digest,
        "executable_sha256": file_digest(executable),
    }
    (output / f"{name}.sha256").write_text(f"{digest}  {name}\n", encoding="ascii")
    (output / f"{binary_name}.json").write_text(
        json.dumps(metadata, sort_keys=True, indent=2) + "\n", encoding="ascii"
    )
    # Signing can change the executable after the build script wrote its checksum.
    (dist / f"{binary_name}.sha256").write_text(
        f"{metadata['executable_sha256']}  {binary_name}/{executable.name}\n",
        encoding="ascii",
    )
    return metadata


def verify_extract(binary_name: str, archives: Path, destination: Path, sha: str) -> Path:
    """Fail closed on identity/hash drift before extracting a candidate archive."""
    require_sha(sha)
    name = archive_name(binary_name)
    metadata = json.loads((archives / f"{binary_name}.json").read_text(encoding="ascii"))
    for key, expected in (
        ("schema_version", 1),
        ("sha", sha),
        ("binary_name", binary_name),
        ("archive", name),
    ):
        if metadata.get(key) != expected:
            raise ValueError(f"Candidate {key} does not match {expected!r}")
    archive = archives / name
    digest = file_digest(archive)
    if metadata.get("archive_sha256") != digest:
        raise ValueError(f"Candidate archive checksum mismatch: {name}")
    checksum = (archives / f"{name}.sha256").read_text(encoding="ascii")
    if checksum != f"{digest}  {name}\n":
        raise ValueError(f"Candidate checksum sidecar mismatch: {name}")
    bundle = destination / binary_name
    if bundle.exists():
        raise FileExistsError(f"Candidate extraction destination already exists: {bundle}")
    destination.mkdir(parents=True, exist_ok=True)
    if archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as source:
            members = source.infolist()
            for item in members:
                require_member_path(item.filename, binary_name)
                if stat.S_ISLNK(item.external_attr >> 16):
                    raise ValueError("Candidate ZIP contains an unsupported symbolic link")
            for item in members:
                source.extract(item, destination)
    else:
        with tarfile.open(archive, "r:gz") as source:
            for item in source.getmembers():
                require_member_path(item.name, binary_name)
                if item.issym() or item.islnk():
                    parent = (destination / item.name).parent if item.issym() else destination
                    if not (parent / item.linkname).resolve().is_relative_to(bundle.resolve()):
                        raise ValueError("Candidate archive link escapes its native bundle")
            source.extractall(destination, filter="data")
    executable = bundle / executable_name(binary_name)
    if file_digest(executable) != metadata.get("executable_sha256"):
        raise ValueError("Extracted candidate executable checksum mismatch")
    require_binary_identity(executable, metadata["version"], sha)
    return executable


def require_member_path(name: str, binary_name: str) -> None:
    """Keep every archive entry inside its one native bundle on either OS."""
    parts = name.rstrip("/").split("/")
    if parts[0] != binary_name or any(
        part in {"", ".", ".."} or "\\" in part or ":" in part for part in parts
    ):
        raise ValueError("Candidate archive contains an unsafe member path")


def main() -> None:
    """Expose packaging and isolated candidate validation to release jobs."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("pack", "verify-extract"))
    parser.add_argument("--binary-name", required=True, choices=BINARY_NAMES)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--dist", type=Path, default=Path("dist"))
    parser.add_argument("--archives", type=Path, default=Path("release-assets"))
    parser.add_argument("--pyproject", type=Path, default=Path("pyproject.toml"))
    args = parser.parse_args()
    if args.operation == "pack":
        package(args.binary_name, args.dist, args.archives, args.sha, args.pyproject)
        artifact = args.archives / archive_name(args.binary_name)
        action = "Packaged"
    else:
        artifact = verify_extract(args.binary_name, args.archives, args.dist, args.sha)
        action = "Verified"
    print(f"[+] {action} {args.binary_name}: {artifact.resolve()}")


if __name__ == "__main__":
    main()
