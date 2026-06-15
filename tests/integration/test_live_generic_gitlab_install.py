"""Live smoke test for installing from gitlab.com via the generic git backend.

This is intentionally separate from the hermetic GitLab REST-path coverage in
``test_gitlab_install_e2e.py``. The gap tracked by microsoft/apm#1229 is the
non-GitHub/non-ADO clone path: ``apm install gitlab.com/<group>/<repo>`` should
delegate to git, validate the package, and stamp the lockfile with the concrete
GitLab host and resolved commit.

The fixture repository is configured by ``APM_LIVE_GENERIC_PACKAGE``. Keep it
unset in ordinary CI; the scheduled/manual workflow step enables this smoke as
soon as maintainers provide a stable public APM-shaped GitLab repo, for example:

    APM_LIVE_GENERIC_PACKAGE=gitlab.com/microsoft-apm-fixtures/smoke-pkg
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from apm_cli.models.apm_package import DependencyReference

pytestmark = [
    pytest.mark.live,
    pytest.mark.live_generic,
    pytest.mark.requires_apm_binary,
    pytest.mark.requires_network_integration,
]

_LIVE_PACKAGE_ENV = "APM_LIVE_GENERIC_PACKAGE"
_LIVE_HOST_ENV = "APM_LIVE_GENERIC_HOST"
_DEFAULT_HOST = "gitlab.com"
INSTALL_TIMEOUT_SECONDS = 240
_FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def _configured_package() -> DependencyReference:
    raw = os.environ.get(_LIVE_PACKAGE_ENV, "").strip()
    if not raw:
        pytest.skip(f"{_LIVE_PACKAGE_ENV} is not set")

    dep = DependencyReference.parse(raw)
    expected_host = os.environ.get(_LIVE_HOST_ENV, _DEFAULT_HOST).strip() or _DEFAULT_HOST
    if dep.host != expected_host:
        pytest.fail(
            f"{_LIVE_PACKAGE_ENV} must point at {expected_host}; "
            f"parsed host={dep.host!r} from {raw!r}"
        )
    if dep.is_virtual:
        pytest.fail(f"{_LIVE_PACKAGE_ENV} must be an APM-shaped repo, not a virtual path: {raw!r}")
    return dep


def _write_consumer_project(project: Path, package_ref: str) -> None:
    project.mkdir(parents=True)
    (project / "apm.yml").write_text(
        yaml.safe_dump(
            {
                "name": "live-generic-gitlab-smoke",
                "version": "0.1.0",
                "target": "copilot",
                "dependencies": {"apm": [package_ref], "mcp": []},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def _env_with_isolated_home(home: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env.setdefault("NO_COLOR", "1")
    if sys.platform == "win32":
        env["USERPROFILE"] = str(home)
    return env


def _read_lockfile(project: Path) -> dict[str, object]:
    lock_path = project / "apm.lock.yaml"
    assert lock_path.exists(), "apm install did not create apm.lock.yaml"
    data = yaml.safe_load(lock_path.read_text(encoding="utf-8"))
    assert isinstance(data, dict), (
        f"apm.lock.yaml must contain a YAML mapping, got {type(data).__name__}: {data!r}"
    )
    return data


def _locked_dep(lockfile: dict[str, object], expected: DependencyReference) -> dict | None:
    deps = lockfile.get("dependencies")
    assert isinstance(deps, list), (
        f"apm.lock.yaml dependencies must be a list, got {type(deps).__name__}: {deps!r}"
    )
    for dep in deps:
        assert isinstance(dep, dict), f"lockfile dependency entry must be a mapping: {dep!r}"
        if dep.get("host") == expected.host and dep.get("repo_url") == expected.repo_url:
            return dep
    return None


def test_live_gitlab_generic_install_clones_validates_and_stamps_lockfile(
    apm_binary_path: Path, tmp_path: Path
) -> None:
    """Run ``apm install`` against a real gitlab.com repo through GenericGitBackend."""
    dep = _configured_package()
    project = tmp_path / "consumer"
    fake_home = tmp_path / "home"
    fake_home.mkdir()

    package_ref = dep.to_canonical()
    _write_consumer_project(project, package_ref)

    result = subprocess.run(
        [str(apm_binary_path), "install"],
        cwd=project,
        capture_output=True,
        text=True,
        timeout=INSTALL_TIMEOUT_SECONDS,
        env=_env_with_isolated_home(fake_home),
        check=False,
    )
    assert result.returncode == 0, (
        "live generic GitLab install failed\n"
        f"package: {package_ref}\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )

    installed_manifests = list((project / "apm_modules").rglob("apm.yml"))
    assert installed_manifests, "install did not materialize an APM package under apm_modules/"

    lockfile = _read_lockfile(project)
    locked = _locked_dep(lockfile, dep)
    assert locked is not None, (
        f"lockfile did not contain {dep.host}/{dep.repo_url}; "
        f"dependencies={lockfile.get('dependencies')}"
    )
    assert locked.get("host") == dep.host
    resolved_commit = locked.get("resolved_commit")
    assert isinstance(resolved_commit, str) and resolved_commit, (
        f"resolved_commit must be a non-empty string in lockfile entry: {locked}"
    )
    assert _FULL_SHA_RE.match(resolved_commit), (
        f"resolved_commit is not a full commit SHA: {resolved_commit!r}"
    )
