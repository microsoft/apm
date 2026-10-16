"""Representative independent contracts and checks for source-package tests."""

from pathlib import Path

from apm_cli.deps.lockfile import LockFile


def make_source_package(root: Path) -> dict[str, bytes]:
    """Create 15 resources plus exact, commented author metadata."""
    root.mkdir(parents=True)
    (root / "apm.yml").write_bytes(
        b"# Author metadata must remain byte-identical.\r\n"
        b"name: software-factory\r\nversion: '1.0.0'\r\nlicense: MIT\r\n"
        b"resources: [contracts, checks]\r\n"
        b"targets: [copilot]\r\n"
        b"scripts:\r\n  postinstall: 'touch EXECUTED'\r\n"
        b"dependencies: {}\r\n"
    )
    (root / "apm.lock.yaml").write_bytes(
        b"# Preserve this original lock, not the envelope lock.\r\n"
        + LockFile().to_yaml().replace("\n", "\r\n").encode()
    )
    for index in range(10):
        path = root / f"contracts/phase-{index}.contract.md"
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(f"# Independent phase {index}\r\nInput -> output.\r\n".encode())
    for index in range(5):
        path = root / f"checks/check-{index}.sh"
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(b"#!/bin/sh\nprintf forbidden > EXECUTED\n")
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }
