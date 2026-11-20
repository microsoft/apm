"""Guard the one locked content-hash verdict shared by install and audit replay (#4187).

``install/source_helpers.locked_content_mismatch`` alone decides whether package
bytes at the locked commit contradict a lock row's ``content_hash``: it verifies
through ``utils/content_hash.verify_package_hash`` and admits only the known
0.28 plugin receipt delta. ``apm install`` (fresh downloads) and every
``apm audit --ci`` replay path (warm copy check, live copy reuse, scratch
download) ask it, so ``install --frozen`` and ``audit --ci`` reach the same
verdict on the same lockfile.
"""

from __future__ import annotations

from scripts.architecture_linter.checks.install_deployment_shared import (
    _facts_for,
    _name_calls_in,
    _present,
    _summary,
)
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.models import Violation

GUARD = "install-deployment-locked-content-verdict"
OWNER = "src/apm_cli/install/source_helpers.py"
SOURCES = "src/apm_cli/install/sources.py"
REPLAY = "src/apm_cli/install/drift.py"
AUDIT_REPLAY = "src/apm_cli/install/audit_replay.py"
LEGACY = "src/apm_cli/install/legacy_plugin_compat.py"
# Names a cache copy that contradicts the lock before re-downloading (report only).
DOWNLOAD = "src/apm_cli/install/phases/download.py"
# Frozen lock-sync judges declarations only from copies that match the lock (#4389).
MCP_VIEW = "src/apm_cli/integration/mcp_config_view.py"

# (path, function, callee, purpose): each consumer asks the verdict owner.
_ROUTES = (
    (OWNER, "locked_content_mismatch", "verify_package_hash", "verify via content_hash"),
    (
        OWNER,
        "locked_content_mismatch",
        "matches_fresh_legacy_plugin_hash",
        "admit only the 0.28 plugin receipt delta",
    ),
    (REPLAY, "_verify_remote_cache_candidate", "locked_content_mismatch", "check live copies"),
    (REPLAY, "_materialize_install_path", "locked_content_mismatch", "check scratch downloads"),
    (AUDIT_REPLAY, "_live_copies_stale", "locked_content_mismatch", "check warm audit copies"),
)
# Closed world: who may call each half of the verdict.
_ALLOWED_CALLERS = {
    "locked_content_mismatch(": frozenset(
        {OWNER, SOURCES, REPLAY, AUDIT_REPLAY, DOWNLOAD, MCP_VIEW}
    ),
    "matches_fresh_legacy_plugin_hash(": frozenset({OWNER, LEGACY}),
}


def check_locked_content_verdict(provider: FactsProvider) -> tuple[Violation, ...]:
    """Require install and audit replay to share one locked content-hash verdict."""
    findings: list[Violation] = []
    for path, function, callee, purpose in _ROUTES:
        facts, failures = _facts_for(provider, path, GUARD)
        findings.extend(failures)
        if not failures and callee not in _name_calls_in(facts, function):
            findings.append(_summary(GUARD, path, f"{function} must call {callee} to {purpose}"))
    facts, failures = _facts_for(provider, SOURCES, GUARD)
    findings.extend(failures)
    if not failures and not _present(facts, "if locked_content_mismatch("):
        findings.append(
            _summary(GUARD, SOURCES, "fresh downloads must ask locked_content_mismatch")
        )
    for path in sorted(provider.inventory):
        if not (path.startswith("src/apm_cli/") and path.endswith(".py")):
            continue
        facts, failures = _facts_for(provider, path, GUARD)
        if failures:
            continue
        for number, line in enumerate(getattr(facts, "lines", ()), start=1):
            if line.lstrip().startswith("#"):
                continue
            for token, allowed in _ALLOWED_CALLERS.items():
                if token in line and path not in allowed:
                    findings.append(
                        _summary(
                            GUARD,
                            path,
                            f"line {number}: ask source_helpers.locked_content_mismatch "
                            "for the locked content-hash verdict",
                        )
                    )
    return tuple(findings)
