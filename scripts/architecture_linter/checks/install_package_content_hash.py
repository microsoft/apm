"""Guard the canonical package content-hash owner (#4000).

``utils/content_hash.py`` alone frames and digests package trees: one framing
primitive serves the package and native plugin fingerprints, and the legacy
stream survives only to verify lockfiles at versions 1-3. Lock rows are
verified in the stream their lockfile version names, only the install
recorder writes ``ctx.package_hashes``, and only the lockfile phase promotes a
lockfile to the framed stream.
"""

from __future__ import annotations

import re

from scripts.architecture_linter.checks.install_deployment_shared import (
    _facts_for,
    _present,
    _summary,
)
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.models import Violation

GUARD = "install-deployment-package-content-hash"
OWNER = "src/apm_cli/utils/content_hash.py"
RECORDER = "src/apm_cli/install/source_helpers.py"
LOCKFILE = "src/apm_cli/deps/lockfile.py"

_REQUIRED: dict[str, tuple[tuple[str, str], ...]] = {
    OWNER: (
        ('return relative.as_posix().encode("utf-8") + b"\\0" + mode', "frame path, NUL, mode"),
        ("hashlib.sha256(payload).digest()", "frame each file by its own digest"),
        ("framed.update(_frame_entry(rel_path, payload))", "frame the package stream"),
        ("hasher.update(_frame_entry(relative, path.read_bytes(), mode))", "frame native pins"),
    ),
    RECORDER: (
        (
            "framed = existing is None or existing.framed_content_hashes",
            "record in the lock stream",
        ),
        ("ctx.package_framed_hashes[dep_key] = digests.framed", "keep the promotion digest"),
    ),
    LOCKFILE: (
        ('FRAMED_CONTENT_HASH_VERSION = "4"', "name the framed lockfile version"),
        (
            'framed_content_hashes=data.get("lockfile_version") == FRAMED_CONTENT_HASH_VERSION',
            "derive the stream from the lockfile version",
        ),
    ),
    "src/apm_cli/install/phases/lockfile.py": (
        ("self._promote_content_hashes(lockfile)", "promote only in the lockfile phase"),
        ('if getattr(self.ctx, "frozen", False)', "never promote under --frozen"),
        (
            "return digests.framed if digests.legacy == dep.content_hash else None",
            "re-hash verified rows",
        ),
    ),
    "src/apm_cli/deps/carried_license.py": (("framed=framed", "verify in the row's stream"),),
    "src/apm_cli/install/phases/_redownload.py": (
        ('framed=getattr(locked_dep, "content_hash_framed", True)', "verify in the row's stream"),
    ),
    "src/apm_cli/install/phases/integrate.py": (
        ("framed=_dep_locked_chk.content_hash_framed", "verify in the row's stream"),
    ),
    "src/apm_cli/install/native_plugin_admission.py": (
        ("framed=lock_dep.content_hash_framed", "verify in the row's stream"),
    ),
    "src/apm_cli/install/service.py": (
        ("selected_source, framed=dependency.content_hash_framed", "verify in the row's stream"),
    ),
    "src/apm_cli/security/trust.py": (
        (
            'framed=getattr(locked_dependency, "content_hash_framed", True)',
            "verify in the row's stream",
        ),
    ),
}

_OWNED_DEFINITIONS = re.compile(
    r"^\s*def (compute_package_hash|compute_package_digests|verify_package_hash|_frame_entry)\("
)
_TREE_HASHER = re.compile(r"\.update\(.*\.as_posix\(\)\.encode\(")
_RAW_COMPARE = re.compile(r"compute_package_hash\([^)]*\)\s*[!=]=|[!=]=\s*compute_package_hash\(")
_RECORD_WRITE = re.compile(r"package_(framed_)?hashes\[[^\]]+\]\s*=(?!=)")


def _closed_world(provider: FactsProvider) -> list[Violation]:
    findings: list[Violation] = []
    for path in sorted(provider.inventory):
        if not (path.startswith("src/apm_cli/") and path.endswith(".py")):
            continue
        facts, failures = _facts_for(provider, path, GUARD)
        if failures:
            findings.extend(failures)
            continue
        for number, line in enumerate(getattr(facts, "lines", ()), start=1):
            reason = None
            if path != OWNER and (_OWNED_DEFINITIONS.search(line) or _TREE_HASHER.search(line)):
                reason = "hash package trees only in utils/content_hash.py"
            elif path != OWNER and _RAW_COMPARE.search(line):
                reason = "verify a recorded hash with verify_package_hash in the row's stream"
            elif path != RECORDER and _RECORD_WRITE.search(line):
                reason = "record package hashes through source_helpers.record_package_digests"
            if reason is not None:
                findings.append(_summary(GUARD, path, f"line {number}: {reason}"))
    return findings


def check_package_content_hash(provider: FactsProvider) -> tuple[Violation, ...]:
    """Require one package content-hash owner and reject second hashers."""
    findings: list[Violation] = []
    for path, needles in _REQUIRED.items():
        facts, failures = _facts_for(provider, path, GUARD)
        findings.extend(failures)
        if failures:
            continue
        for needle, purpose in needles:
            if not _present(facts, needle):
                findings.append(_summary(GUARD, path, f"Package content hashing must {purpose}"))
    findings.extend(_closed_world(provider))
    return tuple(findings)
