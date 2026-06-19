"""Golden-file determinism trap for SBOM export (issue #1777, U5).

Asserts the serialized CycloneDX and SPDX documents are byte-identical to a
committed fixture. A change to component ordering, key order, or field shape
breaks this on purpose -- determinism is a contract (reproducible inventory).
"""

from __future__ import annotations

from pathlib import Path

from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.export.sbom import export_sbom

_TS = "2024-01-01T00:00:00+00:00"
_GOLDEN = Path(__file__).parent / "golden"


def _fixture_lockfile() -> LockFile:
    lf = LockFile()
    lf.add_dependency(
        LockedDependency(
            repo_url="github.com/acme/git-utils",
            host_type="github",
            resolved_commit="def789ghi012",
            resolved_ref="v2.0.0",
            declared_license="MIT",
        )
    )
    lf.add_dependency(
        LockedDependency(
            repo_url="github.com/acme/dual",
            host_type="github",
            resolved_commit="c0ffee00",
            declared_license="(MIT OR Apache-2.0)",
        )
    )
    lf.add_dependency(
        LockedDependency(
            repo_url="github.com/acme/proprietary",
            host_type="github",
            resolved_commit="abc123",
            declared_license="UNLICENSED",
        )
    )
    lf.add_dependency(
        LockedDependency(
            repo_url="github.com/acme/local-helper",
            source="local",
            local_path="./packages/local-helper",
            content_hash="sha256:aaa111",
        )
    )
    lf.add_dependency(
        LockedDependency(
            repo_url="github.com/acme/oci-tools",
            source="registry",
            resolved_url="oci://registry.example.com/acme/oci-tools@sha256:abc123",
            resolved_hash="sha256:abc123",
        )
    )
    return lf


def test_cyclonedx_matches_golden():
    expected = (_GOLDEN / "sbom.cyclonedx.json").read_text(encoding="utf-8")
    assert export_sbom(_fixture_lockfile(), "cyclonedx", timestamp=_TS) == expected


def test_spdx_matches_golden():
    expected = (_GOLDEN / "sbom.spdx.json").read_text(encoding="utf-8")
    assert export_sbom(_fixture_lockfile(), "spdx", timestamp=_TS) == expected


def test_insertion_order_does_not_change_output():
    # Reversed insertion order must yield identical bytes (purl-sorted output).
    forward = export_sbom(_fixture_lockfile(), "cyclonedx", timestamp=_TS)
    lf = _fixture_lockfile()
    reversed_lf = LockFile()
    for dep in reversed(list(lf.dependencies.values())):
        reversed_lf.add_dependency(dep)
    assert export_sbom(reversed_lf, "cyclonedx", timestamp=_TS) == forward
