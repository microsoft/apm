"""Catalog remediations printed by install/ drift replay, audit replay, cache
pins, executable gates, local bundles, native plugin admission, install/mcp
and install/helpers are true (#4090).

Every entry in the W4 drift-trust block of ``core/remediation.py`` names the
state it clears and renders a command line that resolves in the real Click
tree. Entries whose state an installed-CLI test can induce cheaply are executed
end to end in ``tests/integration/test_remediation_advice_lifecycle.py``
(:data:`EXECUTED`); the rest are listed in :data:`COMPONENT_ONLY` with the
reason an executed proof is out of reach.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.unit,
    pytest.mark.promise("P-OWN-remediation-advice"),
]

_OWNER = Path(__file__).resolve().parents[3] / "src" / "apm_cli" / "core" / "remediation.py"
_BLOCK = re.compile(
    r"# -- W4 drift-trust --\n(?P<body>.*?)# -- end W4 drift-trust --",
    re.S,
)

# Executed installed-CLI proofs: test_w4_drift_trust_remediation_clears_the_reported_state.
EXECUTED = frozenset(
    {
        "agent-source-symlinked",
        "hooks-unapproved",
        "native-plugin-cache-differs",
        "replay-cache-missing",
    }
)

_LOCKFILE_EDIT = "needs a hand-edited lockfile row the installer never writes"
_USER_SCOPE = (
    "needs a user-scope audit replay over missing or diverged package copies; "
    "test_global_update_audit_materialization_lifecycle drives the missing case"
)
_BIN = (
    "needs a global-scope Claude plugin shipping bin/ executables; covered by "
    "test_plugin_bin_trust_posture and test_claude_native_hook_lifecycle"
)

COMPONENT_ONLY: dict[str, str] = {
    "absence-unconfirmed": "needs an lstat permission fault the hermetic world cannot induce",
    "agent-plugin-executables-unapproved": "needs an Agent Plugin shipping executables under "
    "an executables gate; covered by test_pr2705_folds",
    "bin-executables-not-retrusted": _BIN,
    "bin-executables-unapproved": _BIN,
    "bin-executables-untrusted": _BIN,
    "bundle-hooks-retired": "needs a deployed local bundle whose hook approval was revoked "
    "and an approval route for an unpacked bundle",
    "bundle-instructions-staged": "an informational hint every local bundle install with "
    "staged instructions prints; the advised compile does not silence it",
    "cached-checkout-tampered": "needs a tampered persistent git cache shard read by "
    "apm audit --ci; the install heal is executed by tests/lifecycle/security/test_integrity.py",
    "claude-plugin-cache-invalid": "needs a corrupted legacy Claude Plugin cache entry",
    "local-bundle-executables-denied": "executed by test_bundle_deny_retires_lsp_lifecycle "
    "and test_bundle_deny_retires_executables_lifecycle (RA-041): the reinstall names the deny",
    "local-bundle-executables-parked": "executed by test_local_bundle_hooks_lifecycle::"
    "test_update_names_a_bundle_whose_executables_are_parked (#4441)",
    "local-bundle-executables-unapproved": "needs apm approve for content-bound bundle keys "
    "(#4490); executed by test_local_bundle_approval_hint_is_the_catalog_remedy_and_runs",
    "mcp-registry-url-invalid": "the retried --mcp install resolves against the live default "
    "MCP registry (network)",
    "mcp-update-flag": "usage refusal; the reported state is the flag combination, not "
    "project state",
    "native-plugin-hash-missing": _LOCKFILE_EDIT,
    "remote-dependency-unpinned": _LOCKFILE_EDIT,
    "replacement-sources-unrecorded": "needs apm.yml replacement rules that diverge from "
    "apm.lock.yaml, which the shared published-package world does not declare",
    "replay-cache-content-stale": "needs an apm_modules copy at the locked commit whose bytes "
    "contradict the locked content hash",
    "replay-cache-stale": "needs an apm_modules copy whose pin marker contradicts the locked "
    "commit",
    "user-package-copies-stale": _USER_SCOPE,
    "user-package-materialization-missing": _USER_SCOPE,
}


def _block_ids() -> list[str]:
    match = _BLOCK.search(_OWNER.read_text(encoding="utf-8"))
    assert match, "W4 drift-trust block markers missing from core/remediation.py"
    return re.findall(r'_entry\(\s*"([a-z0-9-]+)"', match.group("body"))


_IDS = _block_ids()


def test_executed_ids_match_the_installed_cli_proofs() -> None:
    from tests.integration.test_remediation_advice_lifecycle import W4_DRIFT_TRUST_PROOFS

    assert {proof.remediation_id for proof in W4_DRIFT_TRUST_PROOFS} == EXECUTED
