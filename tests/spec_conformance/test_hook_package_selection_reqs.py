"""Bind selective hook updates to the existing subtree and prior-pin contract."""

from pathlib import Path

import pytest

from tests.integration.conftest import apm_binary_path as apm_binary_path
from tests.integration.test_hook_path_package_lifecycle import (
    test_selective_hook_update_preserves_unselected_package as _run_selected_hook_contract,
)

pytestmark = [pytest.mark.e2e, pytest.mark.requires_apm_binary]


@pytest.mark.req("req-rs-012")
@pytest.mark.parametrize("layout", ["hooks", ".apm/hooks"])
@pytest.mark.parametrize("selection_route", ["direct", "parent", "shared-child", "refresh"])
def test_selected_hook_update_preserves_unrelated_pins_and_bytes(
    tmp_path: Path, apm_binary_path: Path, layout: str, selection_route: str
) -> None:
    """Exercise real installs, subtree updates, and malformed unselected sources."""
    _run_selected_hook_contract(
        tmp_path,
        apm_binary_path,
        layout,
        unselected_malformed=True,
        selection_route=selection_route,
    )
