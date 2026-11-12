"""Native whole-unit replay uses canonical copying and existing remote digests."""

from pathlib import Path

import pytest

from apm_cli.agent_plugins.errors import AgentPluginDeploymentBoundaryError
from apm_cli.deps.lockfile import LockedDependency
from apm_cli.install.native_plugin_admission import verify_native_plugin_cache
from apm_cli.install.phases.local_content import _copy_tree_dereferencing_validated
from apm_cli.utils.content_hash import compute_package_hash
from tests.unit.copilot_plugins._builders import write_agent_plugin

pytestmark = pytest.mark.component


def test_local_native_replay_uses_install_symlink_materialization(tmp_path: Path) -> None:
    """Hashing the raw local source would incorrectly reject an installed symlink."""
    source = write_agent_plugin(tmp_path / "source", name="linked", with_mcp=False)
    resource = source / "resource.txt"
    resource.write_text("Harmless owned resource.\n", encoding="ascii")
    (source / "alias.txt").symlink_to("resource.txt")
    lock = LockedDependency(
        repo_url="_local/source",
        source="local",
        local_path=str(source),
        package_type="agent_plugin",
    )
    modules, scratch = tmp_path / "modules", tmp_path / "scratch"
    candidate = lock.to_dependency_ref().get_install_path(modules)
    _copy_tree_dereferencing_validated(source, candidate, source)
    assert not (candidate / "alias.txt").is_symlink()
    assert (candidate / "alias.txt").read_bytes() == resource.read_bytes()
    replay = lock.to_dependency_ref().get_install_path(scratch / "apm_modules")
    verify_native_plugin_cache(lock, source, replay, modules, scratch)
    assert compute_package_hash(replay) == compute_package_hash(candidate)
    assert compute_package_hash(source) != compute_package_hash(candidate)


@pytest.mark.parametrize("cold_cache", [False, True])
@pytest.mark.parametrize("tamper", ["modified", "missing", "missing-hash"])
def test_remote_native_replay_requires_matching_recorded_digest(
    tmp_path: Path, cold_cache: bool, tamper: str
) -> None:
    """A pin marker alone is insufficient for either live or scratch native bytes."""
    lock = LockedDependency(
        repo_url="fixture/native",
        resolved_commit="a" * 40,
        package_type="agent_plugin",
    )
    modules, scratch = tmp_path / "modules", tmp_path / "scratch"
    materialized = lock.to_dependency_ref().get_install_path(
        scratch / "apm_modules" if cold_cache else modules
    )
    write_agent_plugin(materialized, name="remote-witness", with_mcp=False)
    lock.content_hash = compute_package_hash(materialized)
    replay = scratch / "replay"
    verify_native_plugin_cache(lock, materialized, replay, modules, scratch)
    skill = materialized / "skills/remote-witness-skill/SKILL.md"
    if tamper == "modified":
        skill.write_bytes(skill.read_bytes() + b"\nChanged cached bytes.\n")
    elif tamper == "missing":
        skill.unlink()
    else:
        lock.content_hash = None
    expected = "has no content hash" if tamper == "missing-hash" else "cache differs"
    with pytest.raises(AgentPluginDeploymentBoundaryError, match=expected):
        verify_native_plugin_cache(lock, materialized, replay, modules, scratch)
