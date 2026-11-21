"""Opt-in: the 0.36.1 release refuses a v4 lock and writes nothing.

lockfile_version 4 is the intended #4000/#4026 security format change. A
project this release locks must not be silently rewritten by the previous
release: 0.36.1 exits non-zero naming the unsupported version, and the
project tree is byte-identical afterwards (steward ruling, cell 4 for the
#4517 manifest_targets fix).

Doubly opt-in (``APM_E2E_TESTS=1`` and ``APM_RUN_INTEGRATION_TESTS=1``): it
downloads the SHA-pinned 0.36.1 release artifact.
"""

from __future__ import annotations

import hashlib
import os
import platform
import tarfile
import urllib.request
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.requires_e2e_mode,
    pytest.mark.requires_network_integration,
    pytest.mark.promise("P-LAW-lockfile.version_monotonic"),
]

_TAG = "v0.36.1"
# sha256 from the release's own .sha256 assets.
_ASSETS = {
    ("Darwin", "arm64"): (
        "apm-darwin-arm64.tar.gz",
        "49334cff638a12b8cf49874e0ca681a25f641ec7602819507018e842e33852fb",
    ),
    ("Linux", "x86_64"): (
        "apm-linux-x86_64.tar.gz",
        "dd3b84b0a0279b70a80c0c1c155d348b2f112dee0aa3a95e0ae22582fc0d96ee",
    ),
}


@pytest.fixture(scope="module")
def previous_apm(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Download, digest-verify, then extract the pinned 0.36.1 release binary."""
    asset = _ASSETS.get((platform.system(), platform.machine()))
    if asset is None:
        pytest.skip("No pinned 0.36.1 artifact for this host")
    name, sha256 = asset
    url = f"https://github.com/microsoft/apm/releases/download/{_TAG}/{name}"
    with urllib.request.urlopen(url, timeout=300) as response:  # noqa: S310
        payload = response.read()
    assert hashlib.sha256(payload).hexdigest() == sha256, "0.36.1 artifact sha256 mismatch"
    root = tmp_path_factory.mktemp("apm-0361")
    archive = root / name
    archive.write_bytes(payload)
    with tarfile.open(archive) as bundle:
        bundle.extractall(root / "x", filter="data")
    for candidate in sorted((root / "x").rglob("apm")):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    pytest.skip("Pinned 0.36.1 artifact layout is not recognized")


def _tree(project: Path) -> dict[str, bytes]:
    return {
        path.relative_to(project).as_posix(): path.read_bytes()
        for path in sorted(project.rglob("*"))
        if path.is_file() and ".git" not in path.relative_to(project).parts
    }


def test_previous_release_refuses_a_v4_lock_and_writes_nothing(
    tmp_path: Path, apm_binary_path: Path, previous_apm: Path
) -> None:
    isolated = IsolatedApmEnvironment.create(tmp_path / "isolated", base_env=dict(os.environ))
    env = isolated.subprocess_env()
    package = isolated.work_root / "pkg"
    (package / ".apm" / "skills" / "s").mkdir(parents=True)
    dump_yaml({"name": "pkg", "version": "1.0.0"}, package / "apm.yml")
    (package / ".apm" / "skills" / "s" / "SKILL.md").write_text(
        "---\nname: s\ndescription: s\n---\nS\n", encoding="ascii"
    )
    project = isolated.work_root / "proj"
    project.mkdir()
    dump_yaml(
        {
            "name": "proj",
            "version": "0.1.0",
            "target": "claude",
            "dependencies": {"apm": ["../pkg"]},
        },
        project / "apm.yml",
    )
    current = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=180)
    installed = current.run(("install", "--no-policy"), cwd=project, env=env)
    assert installed.returncode == 0, installed.stdout + installed.stderr
    lock = load_yaml(project / "apm.lock.yaml")
    assert str(lock["lockfile_version"]) == "4", lock
    assert lock["manifest_targets"] == ["claude"], lock
    before = _tree(project)

    previous = ApmLifecycleRunner((str(previous_apm),), timeout_seconds=180)
    for args in (
        ("install", "--no-policy"),
        ("install", "--frozen", "--no-policy"),
        ("audit", "--ci"),
    ):
        result = previous.run(args, cwd=project, env=env)
        output = result.stdout + result.stderr
        assert result.returncode != 0, f"0.36.1 {' '.join(args)}\n{output}"
        assert "Unsupported lockfile version '4'" in output, f"0.36.1 {' '.join(args)}\n{output}"
        assert _tree(project) == before, f"0.36.1 {' '.join(args)} wrote to the project"
