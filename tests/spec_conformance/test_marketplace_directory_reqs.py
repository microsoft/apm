"""Versioned Git marketplace directories conform to existing virtual-package rules."""

from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import load_yaml
from tests.integration import conftest as engine_fixtures
from tests.integration import test_marketplace_versioned_directory_lifecycle as lifecycle

pytestmark = pytest.mark.e2e
apm_engine_command = engine_fixtures.apm_engine_command


@pytest.mark.req("req-mf-008")
@pytest.mark.parametrize("version", ["1.2.3", "2026.9.3"])
def test_versioned_marketplace_directory_installs_and_replays_as_subdirectory(
    tmp_path: Path, apm_engine_command: tuple[str, ...], version: str
) -> None:
    """A dotted pack directory retains its subdirectory identity through real CLI replay."""
    lifecycle.test_pack_bundle_installs_from_versioned_marketplace_directory(
        tmp_path, apm_engine_command, version, False
    )
    lockfile = tmp_path / "scenario" / "work" / "consumer" / "apm.lock.yaml"
    entry = load_yaml(lockfile)["dependencies"][0]
    assert entry["virtual_path"] == f"plugins/my-plugin-{version}"
    assert entry["is_virtual"] is True
