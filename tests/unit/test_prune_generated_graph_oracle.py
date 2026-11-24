"""The generated-graph oracle signs only a real re-key as #5238 (#5126).

PROMISE: P-OWN-orphan-package-selection -- the convergence oracle that guards
``apm prune`` reports a depth or dev-status drift on an alias-spelled row as a
failure; only a changed lock identity carries the #5238 known-failure
signature (source: #5126 scope record).
"""

from __future__ import annotations

import pytest

from tests.integration.test_prune_generated_local_graphs import _rekey_verdict

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-OWN-orphan-package-selection")]


@pytest.mark.parametrize(("field", "value"), [("depth", 3), ("is_dev", True)])
def test_a_metadata_drift_on_an_alias_row_is_not_signed_as_a_rekey(field: str, value) -> None:
    """Only an identity change carries the #5238 signature; depth or is_dev drift fails."""
    row = {
        "repo_url": "_local/f",
        "depth": 2,
        "is_dev": None,
        "resolved_by": "_local/z",
        "declaring_parent": "local:vendor/z",
        "local_path": "../f-link",
        "anchored_local_path": "vendor/f",
    }
    settled = {"f": row, "z": {**row, "repo_url": "_local/z", "resolved_by": None}}
    drifted = {**settled, "f": {**row, field: value}}
    renamed = {**settled, "f": {**row, "repo_url": "_local/f-link"}}

    assert "install re-keyed" not in _rekey_verdict(1, settled, drifted, {"f"})
    assert f"'{field}'" in _rekey_verdict(1, settled, drifted, {"f"})
    assert "install re-keyed" in _rekey_verdict(1, settled, renamed, {"f"})
    assert _rekey_verdict(1, settled, settled, {"f"}) == "converged"
