"""Installed consumer refusal evidence for proposed replacement frozen semantics."""

import sys
from dataclasses import replace
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.integration.test_dependency_replacement_lifecycle import (
    _INSTALL,
    _assert_empty_graph_replacement_lifecycle,
    _replacement_fixture,
    _snapshot,
)
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner

pytestmark = pytest.mark.e2e


@pytest.mark.req("req-rpl-003", "req-rpl-004")
@pytest.mark.parametrize("keep_targets", [True, False], ids=["declared-targets", "no-target"])
def test_empty_graph_replacement_intent_is_persisted(tmp_path: Path, keep_targets: bool) -> None:
    _assert_empty_graph_replacement_lifecycle(
        tmp_path, ApmLifecycleRunner((sys.executable, "-m", "apm_cli.cli")), keep_targets
    )


@pytest.mark.req("req-rpl-002", "req-rpl-004")
@pytest.mark.parametrize("damage", ["missing-baseline", "changed-source", "missing-source"])
def test_frozen_replacement_refuses_untrusted_content_without_rewriting(
    tmp_path: Path, damage: str
) -> None:
    fixture = replace(
        _replacement_fixture(tmp_path, Path(sys.executable)),
        runner=ApmLifecycleRunner((sys.executable, "-m", "apm_cli.cli")),
    )
    fixture.replace({"path": "../local-base"})
    fixture.install()
    valid = fixture.run(*_INSTALL, "--frozen")
    assert valid.returncode == 0, valid.stdout + valid.stderr
    lock_path = fixture.consumer.root / "apm.lock.yaml"
    if damage == "missing-baseline":
        data = load_yaml(lock_path)
        data["dependencies"][0].pop("content_hash")
        dump_yaml(data, lock_path)
    elif damage == "changed-source":
        (fixture.local.root / "added.txt").write_bytes(b"Changed selected bytes\n")
    else:
        fixture.local.root.rename(fixture.local.root.with_name("unavailable"))
    snapshot = _snapshot(fixture.consumer.root)
    manifest = fixture.consumer.manifest_path.read_bytes()
    result = fixture.run(*_INSTALL, "--frozen")
    assert result.returncode != 0, result.stdout + result.stderr
    assert _snapshot(fixture.consumer.root) == snapshot
    assert fixture.consumer.manifest_path.read_bytes() == manifest
    fixture.assert_bytes("LOCAL")
