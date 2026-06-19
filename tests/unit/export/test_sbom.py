"""Tests for SBOM serialization (U5): CycloneDX + SPDX, deterministic output.

Invariants under test (supply-chain critical):
* licenses[] representation table (id / expression / named / omit) is exact.
* CycloneDX OMITS licenses for undeclared deps; SPDX writes literal NOASSERTION.
* declared assertions (UNLICENSED) are NEVER rendered as NOASSERTION.
* output is byte-identical across runs with a pinned timestamp (golden).
* export reads the lockfile ONLY -- never re-hashes or touches the filesystem.
* credentials in recorded URLs never leak into output.
"""

from __future__ import annotations

import json

from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.export.sbom import export_sbom

_TS = "2024-01-01T00:00:00+00:00"


def _lockfile(*deps):
    lf = LockFile()
    for dep in deps:
        lf.add_dependency(dep)
    return lf


def _git(repo, commit, **kw):
    return LockedDependency(repo_url=repo, host_type="github", resolved_commit=commit, **kw)


def _components(doc):
    return doc["components"]


def _by_purl(doc, purl):
    for comp in doc["components"]:
        if comp["purl"] == purl:
            return comp
    raise AssertionError(f"component {purl} not found")


# --------------------------------------------------------------- CycloneDX


def test_cyclonedx_basic_structure():
    lf = _lockfile(_git("github.com/acme/a", "c1", declared_license="MIT"))
    doc = json.loads(export_sbom(lf, "cyclonedx", timestamp=_TS))
    assert doc["bomFormat"] == "CycloneDX"
    assert doc["specVersion"]
    assert len(doc["components"]) == 1


def test_cyclonedx_single_spdx_id_uses_license_id():
    lf = _lockfile(_git("github.com/acme/a", "c1", declared_license="MIT"))
    doc = json.loads(export_sbom(lf, "cyclonedx", timestamp=_TS))
    comp = _by_purl(doc, "pkg:github/acme/a@c1")
    assert comp["licenses"] == [{"license": {"id": "MIT"}}]


def test_cyclonedx_expression_uses_expression_key():
    lf = _lockfile(_git("github.com/acme/a", "c1", declared_license="(MIT OR Apache-2.0)"))
    doc = json.loads(export_sbom(lf, "cyclonedx", timestamp=_TS))
    comp = _by_purl(doc, "pkg:github/acme/a@c1")
    assert comp["licenses"] == [{"expression": "(MIT OR Apache-2.0)"}]


def test_cyclonedx_named_for_unlicensed():
    lf = _lockfile(_git("github.com/acme/a", "c1", declared_license="UNLICENSED"))
    doc = json.loads(export_sbom(lf, "cyclonedx", timestamp=_TS))
    comp = _by_purl(doc, "pkg:github/acme/a@c1")
    assert comp["licenses"] == [{"license": {"name": "UNLICENSED"}}]


def test_cyclonedx_named_for_see_license_in():
    lf = _lockfile(_git("github.com/acme/a", "c1", declared_license="SEE LICENSE IN LICENSE.txt"))
    doc = json.loads(export_sbom(lf, "cyclonedx", timestamp=_TS))
    comp = _by_purl(doc, "pkg:github/acme/a@c1")
    assert comp["licenses"] == [{"license": {"name": "SEE LICENSE IN LICENSE.txt"}}]


def test_cyclonedx_omits_licenses_when_undeclared():
    lf = _lockfile(_git("github.com/acme/a", "c1"))
    doc = json.loads(export_sbom(lf, "cyclonedx", timestamp=_TS))
    comp = _by_purl(doc, "pkg:github/acme/a@c1")
    assert "licenses" not in comp


# --------------------------------------------------------------- SPDX


def test_spdx_basic_structure():
    lf = _lockfile(_git("github.com/acme/a", "c1", declared_license="MIT"))
    doc = json.loads(export_sbom(lf, "spdx", timestamp=_TS))
    assert doc["spdxVersion"].startswith("SPDX-")
    assert doc["packages"]


def test_spdx_license_declared_passthrough():
    lf = _lockfile(_git("github.com/acme/a", "c1", declared_license="MIT"))
    doc = json.loads(export_sbom(lf, "spdx", timestamp=_TS))
    pkg = doc["packages"][0]
    assert pkg["licenseDeclared"] == "MIT"


def test_spdx_noassertion_when_undeclared():
    lf = _lockfile(_git("github.com/acme/a", "c1"))
    doc = json.loads(export_sbom(lf, "spdx", timestamp=_TS))
    pkg = doc["packages"][0]
    assert pkg["licenseDeclared"] == "NOASSERTION"


def test_spdx_unlicensed_is_not_noassertion():
    # CRITICAL mutation-break: a declared assertion must never collapse to the
    # genuinely-unknown NOASSERTION sentinel.
    lf = _lockfile(_git("github.com/acme/a", "c1", declared_license="UNLICENSED"))
    doc = json.loads(export_sbom(lf, "spdx", timestamp=_TS))
    pkg = doc["packages"][0]
    assert pkg["licenseDeclared"] == "UNLICENSED"
    assert pkg["licenseDeclared"] != "NOASSERTION"


# --------------------------------------------------------------- determinism


def test_output_is_byte_identical_across_runs():
    lf = _lockfile(
        _git("github.com/acme/b", "c2", declared_license="MIT"),
        _git("github.com/acme/a", "c1", declared_license="Apache-2.0"),
    )
    first = export_sbom(lf, "cyclonedx", timestamp=_TS)
    second = export_sbom(lf, "cyclonedx", timestamp=_TS)
    assert first == second


def test_components_sorted_by_purl():
    lf = _lockfile(
        _git("github.com/acme/z", "c9", declared_license="MIT"),
        _git("github.com/acme/a", "c1", declared_license="MIT"),
    )
    doc = json.loads(export_sbom(lf, "cyclonedx", timestamp=_TS))
    purls = [c["purl"] for c in doc["components"]]
    assert purls == sorted(purls)


# --------------------------------------------------- reads-lockfile-only


def test_export_does_not_touch_filesystem(monkeypatch):
    # A corrupted/missing on-disk tree must still export the lockfile-recorded
    # hash: export never re-hashes. We prove it by exporting a local dep whose
    # path does not exist and asserting its recorded content_hash surfaces.
    import apm_cli.utils.content_hash as ch

    def _boom(*_a, **_k):
        raise AssertionError("export must not hash files")

    monkeypatch.setattr(ch, "compute_file_hash", _boom, raising=False)

    dep = LockedDependency(
        repo_url="github.com/acme/local",
        source="local",
        local_path="/does/not/exist",
        content_hash="sha256:deadbeef",
    )
    doc = json.loads(export_sbom(_lockfile(dep), "cyclonedx", timestamp=_TS))
    comp = _by_purl(doc, "pkg:generic/local@sha256:deadbeef")
    assert comp is not None


# ------------------------------------------------------- credential scrub


def test_credentials_scrubbed_from_external_references():
    dep = LockedDependency(
        repo_url="registry.example.com/acme/oci-tools",
        source="registry",
        resolved_url="oci://user:secret@registry.example.com/acme/oci-tools@sha256:abc",
        resolved_hash="sha256:abc",
    )
    out = export_sbom(_lockfile(dep), "cyclonedx", timestamp=_TS)
    assert "secret" not in out
