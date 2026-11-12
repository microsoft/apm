"""Existing inventory non-authority requirement on MCP omission evidence."""

from pathlib import Path

import pytest

from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.integration.mcp_config_view import CurrentMcpConfigView
from apm_cli.models.apm_package import APMPackage

pytestmark = pytest.mark.component


@pytest.mark.req("req-lk-019")
@pytest.mark.parametrize("field", ["name", "version"])
def test_omission_evidence_never_uses_inventory_as_identity(tmp_path: Path, field: str) -> None:
    """Inventory edits cannot change which dependency recorded evidence identifies."""
    root_path = tmp_path / "apm.yml"
    root_path.write_text("name: consumer\nversion: 1.0.0\n", encoding="utf-8")
    root = APMPackage.from_apm_yml(root_path)
    package = tmp_path / "package"
    package.mkdir()
    manifest = (
        "name: package\nversion: 1.0.0\ndependencies:\n"
        "  mcp:\n    - name: service\n      registry: false\n"
        "      transport: stdio\n      command: inert\n"
    )
    (package / "apm.yml").write_text(manifest, encoding="utf-8")
    dependency = LockedDependency(
        repo_url="_local/package",
        source="local",
        local_path=str(package),
        name="inventory-name",
        version="1.0.0",
        depth=1,
    )
    lock = LockFile()
    lock.add_dependency(dependency)
    modules = tmp_path / "apm_modules"
    selected = CurrentMcpConfigView.derive(
        root, lock, modules, trust_transitive_self_defined=False, include_dependency_mcp=False
    )
    assert selected.problems == ()
    assert selected.configs == {}
    lock.mcp_omissions = selected.omission_evidence(lock)
    assert lock.mcp_omissions == {
        "service": {
            "declaration": selected.declaration_fingerprints["service"],
            "reason": "only-apm",
        }
    }
    original_evidence = lock.mcp_omissions
    setattr(dependency, field, "different-inventory-value")
    lock_path = tmp_path / "apm.lock.yaml"
    lock.write(lock_path)
    restored = LockFile.read(lock_path)
    assert restored is not None
    current = CurrentMcpConfigView.derive(
        root, restored, modules, trust_transitive_self_defined=True
    )
    assert current.problems == ()
    assert set(current.configs) == {"service"}
    assert current.configs["service"]["command"] == "inert"
    assert (
        current.declaration_fingerprints["service"] == original_evidence["service"]["declaration"]
    )
    audited = current.with_recorded_omissions(restored)
    assert audited.configs == {}
    assert audited.problems == ()

    other_package = tmp_path / "other-package"
    other_package.mkdir()
    (other_package / "apm.yml").write_text(manifest, encoding="utf-8")
    different_identity = next(iter(restored.dependencies.values()))
    different_identity.local_path = str(other_package)
    restored.dependencies.clear()
    restored.add_dependency(different_identity)
    changed = CurrentMcpConfigView.derive(
        root, restored, modules, trust_transitive_self_defined=True
    )
    assert changed.configs == current.configs
    assert (
        changed.declaration_fingerprints["service"] != original_evidence["service"]["declaration"]
    )
    assert [
        problem.package_key for problem in changed.with_recorded_omissions(restored).problems
    ] == ["service"]
