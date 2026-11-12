"""Bind GitLab sparse materialization to existing port and cache requirements."""

import pytest

from tests.integration.test_gitlab_sparse_transport_contract import (
    materialization as materialization,
)
from tests.integration.test_gitlab_sparse_transport_contract import (
    test_real_git_manifest_remote_bytes_and_single_fetch as _run_sparse_transport_contract,
)

pytestmark = pytest.mark.component


@pytest.mark.req("req-sc-013")
@pytest.mark.req("req-rs-016")
@pytest.mark.parametrize("ref_name", ["main", "v1", "sha"])
def test_gitlab_sparse_fetch_preserves_port_identity_and_ref(
    materialization: dict, ref_name: str
) -> None:
    """Exercise non-default port fidelity and same-identity/ref checkout reuse."""
    _run_sparse_transport_contract(materialization, ref_name)
