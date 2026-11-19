"""Guard the canonical apm_modules copy-validity owner (#3884).

``drift.locked_copy_proof`` alone decides whether an existing package copy
holds its locked commit or content. The resolver gate, the parallel
pre-download skip, the integrate skip and the ``.apm-pin`` writer consume
it; none re-derives the answer from a git HEAD probe or a raw content-hash
comparison, and no marker is stamped on an unproven copy.
"""

from __future__ import annotations

from scripts.architecture_linter.checks.install_deployment_shared import (
    _facts_for,
    _present,
    _summary,
)
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.models import Violation

GUARD = "install-deployment-locked-copy-validity"
DRIFT = "src/apm_cli/drift.py"
DOWNLOAD = "src/apm_cli/install/phases/download.py"
INTEGRATE = "src/apm_cli/install/phases/integrate.py"
CACHE_PIN = "src/apm_cli/install/cache_pin.py"

_REQUIRED: dict[str, tuple[tuple[str, str], ...]] = {
    DRIFT: (
        ("def locked_copy_proof(", "own proof"),
        ("return LOCKED_COPY_GIT_HEAD if head == commit else None", "let a readable HEAD decide"),
        ("verify_marker(package_path, commit)", "accept the .apm-pin locked commit"),
        (
            "return package_path is not None and locked_copy_is_stale(locked_dep, package_path)",
            "keep the resolver gate behind the copy-validity owner",
        ),
    ),
    DOWNLOAD: (
        ("locked_copy_proof(_pd_locked_chk, _pd_path, for_deployment=True)", "consume the owner"),
    ),
    INTEGRATE: (
        (
            "else locked_copy_proof(locked_dep, install_path, for_deployment=True)",
            "consume the owner",
        ),
    ),
    CACHE_PIN: (
        ("if not _copy_holds_locked_commit(dep, install_path):", "stamp only proven copies"),
        ("if locked_copy_proof(dep, install_path) is not None:", "prove copies via the owner"),
    ),
}

_REDERIVATIONS = ("_should_skip_redownload(", "head.commit.hexsha", "verify_marker(")


def check_locked_copy_validity(provider: FactsProvider) -> tuple[Violation, ...]:
    """Require one copy-validity owner and reject consumer re-derivations."""
    findings: list[Violation] = []
    for path, needles in _REQUIRED.items():
        facts, failures = _facts_for(provider, path, GUARD)
        findings.extend(failures)
        if failures:
            continue
        for needle, purpose in needles:
            if not _present(facts, needle):
                findings.append(_summary(GUARD, path, f"Locked copy validity must {purpose}"))
        if path in (DRIFT, CACHE_PIN):
            continue
        for number, line in enumerate(getattr(facts, "lines", ()), start=1):
            if any(token in line for token in _REDERIVATIONS):
                findings.append(
                    _summary(
                        GUARD,
                        path,
                        f"line {number}: ask drift.locked_copy_proof whether the copy "
                        "holds the locked content instead of re-deriving it",
                    )
                )
    return tuple(findings)
