"""Existing lockfile clauses exercised through aggregate install reconciliation."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from apm_cli.core.scope import InstallScope
from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.install.phases import cleanup
from apm_cli.install.phases.lockfile import LockfileBuilder
from apm_cli.integration.targets import resolve_targets
from apm_cli.models.apm_package import APMPackage
from apm_cli.utils.diagnostics import DiagnosticCollector
from apm_cli.utils.yaml_io import load_yaml
from tests.spec_conformance._helpers import assert_spec_contains, sha256_hex

pytestmark = pytest.mark.component

_AGGREGATE = ".copilot/copilot-instructions.md"
_CURRENT_BYTES = (
    b"<!-- apm-managed: copilot-instructions.md -->\n"
    b"<!-- apm:source:first -->\nFirst contribution.\n<!-- /apm:source -->\n"
    b"<!-- apm:source:second -->\nSecond contribution.\n<!-- /apm:source -->\n"
    b"<!-- apm:source:local -->\nRoot contribution.\n<!-- /apm:source -->\n"
)


@pytest.fixture
def aggregate_install_state(tmp_path: Path) -> tuple[SimpleNamespace, LockFile]:
    """Model completed materialization before non-frozen cleanup and attachment."""
    target = tmp_path / _AGGREGATE
    target.parent.mkdir()
    target.write_bytes(_CURRENT_BYTES)
    (tmp_path / "apm.yml").write_text(
        "name: aggregate-conformance\nversion: 1.0.0\ntarget: copilot\n",
        encoding="ascii",
    )
    prior = LockFile()
    prior.add_dependency(
        LockedDependency(
            repo_url="fixture/previous-identity",
            deployed_files=[_AGGREGATE],
            deployed_file_hashes={_AGGREGATE: "sha256:" + sha256_hex(_CURRENT_BYTES)},
        )
    )
    prior.local_deployed_files = [_AGGREGATE]
    prior.local_deployed_file_hashes = {_AGGREGATE: "sha256:" + "a" * 64}
    current = LockFile()
    for name in ("first", "second"):
        current.add_dependency(LockedDependency(repo_url=f"fixture/{name}"))
    context = SimpleNamespace(
        project_root=tmp_path,
        scope=InstallScope.USER,
        frozen=False,
        lockfile_only=False,
        only_packages=False,
        existing_lockfile=prior,
        intended_dep_keys=set(current.dependencies),
        package_deployed_files={key: [_AGGREGATE] for key in current.dependencies},
        local_deployed_files=[_AGGREGATE],
        apm_package=APMPackage.from_apm_yml(tmp_path / "apm.yml"),
        targets=resolve_targets(tmp_path, user_scope=True, explicit_target=["copilot"]),
        diagnostics=DiagnosticCollector(),
        logger=None,
    )
    return context, current


@pytest.mark.req("req-lk-020")
def test_nonfrozen_orphan_cleanup_preserves_fresh_aggregate(
    aggregate_install_state: tuple[SimpleNamespace, LockFile],
) -> None:
    """A prior different identity cannot delete the active install's fresh path."""
    context, current = aggregate_install_state
    cleanup.run(context)
    LockfileBuilder(context)._attach_deployed_files(current)

    assert (context.project_root / _AGGREGATE).read_bytes() == _CURRENT_BYTES
    assert context.orphan_cleanup_retained == {}
    assert all(dep.deployed_files == [_AGGREGATE] for dep in current.dependencies.values())
    assert current.local_deployed_files == [_AGGREGATE]
    assert_spec_contains(
        "During orphan cleanup, the consumer MUST preserve any path freshly\n"
        "deployed by an active dependency in the current install, even when the\n"
        "same path is also recorded by a prior lockfile entry under a different\n"
        "dependency identity."
    )


@pytest.mark.req("req-lk-016")
def test_aggregate_install_serializes_current_hash_envelopes(
    aggregate_install_state: tuple[SimpleNamespace, LockFile],
) -> None:
    """Attachment and local-state preservation serialize the final file's digest."""
    context, current = aggregate_install_state
    builder = LockfileBuilder(context)
    builder._attach_deployed_files(current)
    builder._preserve_existing_local_state(current)
    lock_path = context.project_root / "apm.lock.yaml"
    current.save(lock_path)
    document = load_yaml(lock_path)

    expected = "sha256:" + sha256_hex(_CURRENT_BYTES)
    assert len(document["deployments"]) == 1
    assert document["deployments"][0]["content_hash"] == expected
    assert document["local_deployed_file_hashes"] == {_AGGREGATE: expected}
    assert len(document["dependencies"]) == 2
    for dependency in document["dependencies"]:
        assert dependency["deployed_file_hashes"] == {_AGGREGATE: expected}
    assert_spec_contains(
        "hash values as `<algo>:<hex>` envelopes",
        "`local_deployed_file_hashes` (each value), `content_hash`,",
        "writers MUST emit the explicit envelope\nform.",
    )
