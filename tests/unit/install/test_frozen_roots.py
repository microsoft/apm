"""Frozen preflight reads the selected store, never a source-side substitute."""

from pathlib import Path

import pytest

from apm_cli.core.scope import InstallScope
from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.install.errors import FrozenInstallError
from apm_cli.install.request import InstallRequest
from apm_cli.install.service import InstallService
from apm_cli.models.apm_package import APMPackage

pytestmark = pytest.mark.component


@pytest.mark.parametrize("scope", [InstallScope.PROJECT, InstallScope.USER])
@pytest.mark.parametrize(
    ("source_state", "selected_state"),
    [
        ("missing", "valid"),
        ("stale", "valid"),
        ("valid", "missing"),
        ("valid", "stale"),
        ("missing", "stale"),
        ("missing", "missing"),
    ],
)
def test_frozen_scoped_lockfile_selection_is_read_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scope: InstallScope,
    source_state: str,
    selected_state: str,
) -> None:
    source = tmp_path / "source"
    deploy = tmp_path / "deploy"
    home = tmp_path / "home"
    selected = home / ".apm" if scope is InstallScope.USER else deploy
    for root in (source, deploy, selected):
        root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(deploy)
    manifest = source / "apm.yml"
    manifest.write_text(
        "name: frozen-roots\nversion: 1.0.0\ndependencies:\n  apm:\n    - owner/package\n",
        encoding="utf-8",
    )
    package = APMPackage.from_apm_yml(manifest)
    for root, state in ((source, source_state), (selected, selected_state)):
        if state == "missing":
            continue
        lock = LockFile()
        if state == "valid":
            lock.add_dependency(
                LockedDependency(
                    repo_url="owner/package",
                    resolved_ref="main",
                    resolved_commit="a" * 40,
                    depth=1,
                )
            )
        lock.write(root / "apm.lock.yaml")
    before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    request = InstallRequest(apm_package=package, scope=scope, frozen=True)
    if selected_state == "valid":
        InstallService.enforce_frozen(request)
    else:
        message = "requires apm.lock.yaml" if selected_state == "missing" else "out of sync"
        with pytest.raises(FrozenInstallError, match=message.replace(".", r"\.")):
            InstallService.enforce_frozen(request)
    assert {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()} == before
