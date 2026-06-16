"""SBOM serialization from a recorded lockfile (issue #1777, U5).

Produces an inventory export -- NOT a security attestation -- in either
CycloneDX or SPDX JSON. Every value is read from the lockfile as recorded:
export never re-resolves, never re-hashes, and never touches the network or the
filesystem. Output is deterministic (components sorted by purl, a pinned
timestamp, stable key order) so two runs over the same lockfile are
byte-identical.

License rendering follows the npm-faithful three-state model:

* declared + valid SPDX id  -> CycloneDX ``license.id`` / SPDX expression text
* declared SPDX expression   -> CycloneDX ``expression``  / SPDX expression text
* declared named assertion   -> CycloneDX ``license.name``/ SPDX literal text
  (e.g. ``UNLICENSED``)         -- an assertion, NEVER NOASSERTION
* not declared               -> CycloneDX OMITS licenses  / SPDX ``NOASSERTION``
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from .formats import FORMAT_CYCLONEDX, FORMAT_SPDX, SUPPORTED_FORMATS
from .purl import build_purl, scrub_url
from .spdx import KIND_EXPRESSION, KIND_ID, classify_declared_license

if TYPE_CHECKING:
    from apm_cli.deps.lockfile import LockedDependency, LockFile

CYCLONEDX_SPEC_VERSION = "1.5"
SPDX_VERSION = "SPDX-2.3"

_NOASSERTION = "NOASSERTION"


def _component_name(dep: LockedDependency) -> str:
    """Human-readable component name from lockfile fields."""
    parts = [p for p in dep.repo_url.split("/") if p]
    if parts and "." in parts[0]:
        parts = parts[1:]
    return "/".join(parts) if parts else dep.repo_url


def _component_version(dep: LockedDependency) -> str | None:
    """Best display version, lockfile-recorded only (no synthesis)."""
    return dep.version or dep.resolved_commit or dep.resolved_hash or dep.content_hash


def _cyclonedx_licenses(declared: str | None) -> list[dict[str, Any]] | None:
    """CycloneDX ``licenses`` array for a declared value, or None to OMIT.

    CycloneDX has no NOASSERTION token, so an undeclared license is represented
    by the ABSENCE of the array -- exactly how ``npm sbom`` behaves.
    """
    if not declared:
        return None
    result = classify_declared_license(declared)
    if result.kind == KIND_ID:
        return [{"license": {"id": result.value}}]
    if result.kind == KIND_EXPRESSION:
        return [{"expression": result.value}]
    return [{"license": {"name": result.value}}]


def _spdx_license_declared(declared: str | None) -> str:
    """SPDX ``licenseDeclared`` -- the verbatim value or literal NOASSERTION."""
    if not declared:
        return _NOASSERTION
    return declared


def _external_references(dep: LockedDependency) -> list[dict[str, str]]:
    """Scrubbed distribution reference, when a source URL was recorded."""
    if not dep.resolved_url:
        return []
    return [{"type": "distribution", "url": scrub_url(dep.resolved_url)}]


def _sorted_deps(lockfile: LockFile) -> list[tuple[str, LockedDependency]]:
    """(purl, dep) pairs sorted by purl for deterministic output.

    The synthetic ``<self>`` entry (the project's own local content) is skipped
    -- the SBOM inventories dependencies, not the root project itself.
    """
    pairs = [
        (build_purl(dep), dep) for dep in lockfile.dependencies.values() if dep.repo_url != "<self>"
    ]
    pairs.sort(key=lambda item: item[0])
    return pairs


def _build_cyclonedx(lockfile: LockFile, timestamp: str) -> dict[str, Any]:
    components: list[dict[str, Any]] = []
    for purl, dep in _sorted_deps(lockfile):
        comp: dict[str, Any] = {
            "type": "library",
            "name": _component_name(dep),
            "purl": purl,
            "bom-ref": purl,
        }
        version = _component_version(dep)
        if version:
            comp["version"] = version
        licenses = _cyclonedx_licenses(dep.declared_license)
        if licenses is not None:
            comp["licenses"] = licenses
        refs = _external_references(dep)
        if refs:
            comp["externalReferences"] = refs
        components.append(comp)
    return {
        "bomFormat": "CycloneDX",
        "specVersion": CYCLONEDX_SPEC_VERSION,
        "version": 1,
        "metadata": {
            "timestamp": timestamp,
            "tools": [{"vendor": "APM", "name": "apm lock export"}],
        },
        "components": components,
    }


def _build_spdx(lockfile: LockFile, timestamp: str) -> dict[str, Any]:
    packages: list[dict[str, Any]] = []
    for index, (purl, dep) in enumerate(_sorted_deps(lockfile)):
        spdx_id = f"SPDXRef-Package-{index}"
        download = scrub_url(dep.resolved_url) if dep.resolved_url else _NOASSERTION
        pkg: dict[str, Any] = {
            "SPDXID": spdx_id,
            "name": _component_name(dep),
            "downloadLocation": download or _NOASSERTION,
            # APM records provenance, it never CONCLUDES a license.
            "licenseConcluded": _NOASSERTION,
            "licenseDeclared": _spdx_license_declared(dep.declared_license),
            "externalRefs": [
                {
                    "referenceCategory": "PACKAGE-MANAGER",
                    "referenceType": "purl",
                    "referenceLocator": purl,
                }
            ],
        }
        version = _component_version(dep)
        if version:
            pkg["versionInfo"] = version
        packages.append(pkg)
    return {
        "spdxVersion": SPDX_VERSION,
        "dataLicense": "CC0-1.0",
        "SPDXID": "SPDXRef-DOCUMENT",
        "name": "apm-sbom",
        "documentNamespace": f"https://spdx.org/spdxdocs/apm-sbom-{timestamp}",
        "creationInfo": {
            "created": timestamp,
            "creators": ["Tool: apm lock export"],
        },
        "packages": packages,
    }


def export_sbom(lockfile: LockFile, fmt: str, *, timestamp: str) -> str:
    """Serialize *lockfile* to a deterministic SBOM JSON string.

    *fmt* is ``cyclonedx`` or ``spdx``. *timestamp* is caller-pinned so output
    is reproducible. Raises ``ValueError`` for an unsupported format.
    """
    normalized = fmt.lower()
    if normalized == FORMAT_CYCLONEDX:
        doc = _build_cyclonedx(lockfile, timestamp)
    elif normalized == FORMAT_SPDX:
        doc = _build_spdx(lockfile, timestamp)
    else:
        raise ValueError(f"Unsupported SBOM format: {fmt!r}. Use one of {SUPPORTED_FORMATS}.")
    return json.dumps(doc, indent=2, sort_keys=True) + "\n"


__all__ = ["export_sbom"]
