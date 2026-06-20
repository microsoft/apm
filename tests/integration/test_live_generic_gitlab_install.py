"""Live smoke test for installing from gitlab.com via the GitLab backend.

This is intentionally separate from the hermetic GitLab REST-path coverage in
``test_gitlab_install_e2e.py``. The gap tracked by microsoft/apm#1229 is the
non-GitHub/non-ADO clone path: ``apm install gitlab.com/<group>/<repo>`` should
route through ``GitLabBackend`` (``kind=gitlab``), delegate to git, validate the
package, and stamp the lockfile with the concrete GitLab host and resolved
commit.

The fixture repository is configured by ``APM_LIVE_GENERIC_PACKAGE`` and pinned
by ``APM_LIVE_GENERIC_EXPECTED_SHA``. Keep them unset in ordinary CI; the
scheduled/manual workflow step enables this smoke as soon as maintainers provide
a stable public APM-shaped GitLab repo, for example:

    APM_LIVE_GENERIC_PACKAGE=gitlab.com/microsoft-apm-fixtures/smoke-pkg
    APM_LIVE_GENERIC_EXPECTED_SHA=<40-char commit sha>
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
    pytest.mark.requires_live_generic_fixture,
    pytest.mark.requires_apm_binary,
    pytest.mark.requires_network_integration,
]

_LIVE_PACKAGE_ENV = "APM_LIVE_GENERIC_PACKAGE"
_LIVE_HOST_ENV = "APM_LIVE_GENERIC_HOST"
_LIVE_EXPECTED_SHA_ENV = "APM_LIVE_GENERIC_EXPECTED_SHA"
_DEFAULT_HOST = "gitlab.com"
INSTALL_TIMEOUT_SECONDS = 240
_OUTPUT_TAIL_CHARS = 2000
_FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_SUBPROCESS_ENV_ALLOWLIST = {
    "APM_E2E_TESTS",
    "APM_RUN_INTEGRATION_TESTS",
    "GIT_SSL_CAINFO",
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "NO_PROXY",
    "PATH",
    "REQUESTS_CA_BUNDLE",
    "SSL_CERT_FILE",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "TMPDIR",
    "WINDIR",
}
_TOKEN_ENV_DENYLIST = {
    "ACTIONS_RUNTIME_TOKEN",
    "GITHUB_TOKEN",
    "GITLAB_APM_PAT",
    "GITLAB_TOKEN",
}


def _configured_package() -> DependencyReference:
    raw = os.environ.get(_LIVE_PACKAGE_ENV, "").strip()
    if not raw:
        pytest.fail(
            f"{_LIVE_PACKAGE_ENV} is not set; "
            f"set {_LIVE_PACKAGE_ENV}=gitlab.com/<group>/<repo>"
        )

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


def _expected_sha() -> str:
    raw = os.environ.get(_LIVE_EXPECTED_SHA_ENV, "").strip().lower()
    if not raw:
        pytest.fail(
            f"{_LIVE_EXPECTED_SHA_ENV} must be set to the fixture's pinned "
            "40-character commit SHA"
        )
    if not _FULL_SHA_RE.fullmatch(raw):
        pytest.fail(f"{_LIVE_EXPECTED_SHA_ENV} must be a full commit SHA, got {raw!r}")
    return raw


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
    env = {
        key: value
        for key, value in os.environ.items()
        if key.upper() in _SUBPROCESS_ENV_ALLOWLIST and key.upper() not in _TOKEN_ENV_DENYLIST
    }
    env["HOME"] = str(home)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["NO_COLOR"] = "1"
    env["APM_E2E_TESTS"] = "1"
    if sys.platform == "win32":
        env["USERPROFILE"] = str(home)
        if system_root := os.environ.get("SYSTEMROOT"):
            env["SYSTEMROOT"] = system_root
    return env


def _tail_output(text: str) -> str:
    if len(text) <= _OUTPUT_TAIL_CHARS:
        return text
    return f"[truncated to last {_OUTPUT_TAIL_CHARS} chars]\n{text[-_OUTPUT_TAIL_CHARS:]}"


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


def _run_install(apm_binary_path: Path, project: Path, fake_home: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(apm_binary_path), "install"],
        cwd=project,
        capture_output=True,
        text=True,
        timeout=INSTALL_TIMEOUT_SECONDS,
        env=_env_with_isolated_home(fake_home),
        check=False,
    )


def _assert_install_succeeded(result: subprocess.CompletedProcess, package_ref: str) -> None:
    assert result.returncode == 0, (
        "live GitLab install failed\n"
        f"package: {package_ref}\n"
        f"stdout:\n{_tail_output(result.stdout)}\n"
        f"stderr:\n{_tail_output(result.stderr)}"
    )


def _assert_install_output_mentions_success(
    result: subprocess.CompletedProcess, dep: DependencyReference
) -> None:
    package_name = dep.repo_url.rsplit("/", 1)[-1].lower()
    output = f"{result.stdout}\n{result.stderr}".lower()
    assert "installed" in output or package_name in output, (
        "live GitLab install output did not mention an install summary or package name; "
        f"stdout:\n{_tail_output(result.stdout)}\n"
        f"stderr:\n{_tail_output(result.stderr)}"
    )


def _assert_installed_package_manifest(project: Path) -> None:
    installed_manifests = list((project / "apm_modules").rglob("apm.yml"))
    assert installed_manifests, "install did not materialize an APM package under apm_modules/"
    for manifest in installed_manifests:
        data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("name"), str) and data["name"]:
            return
    raise AssertionError(
        f"installed apm.yml files did not contain package names: {installed_manifests}"
    )


def test_live_gitlab_install_clones_validates_and_stamps_lockfile(
    apm_binary_path: Path, tmp_path: Path
) -> None:
    """Run ``apm install`` against a real gitlab.com repo through GitLabBackend."""
    dep = _configured_package()
    expected_sha = _expected_sha()
    project = tmp_path / "consumer"
    fake_home = tmp_path / "home"
    fake_home.mkdir()

    package_ref = dep.to_canonical()
    _write_consumer_project(project, package_ref)

    result = _run_install(apm_binary_path, project, fake_home)
    _assert_install_succeeded(result, package_ref)
    _assert_install_output_mentions_success(result, dep)
    _assert_installed_package_manifest(project)

    lock_path = project / "apm.lock.yaml"
    first_lock_text = lock_path.read_text(encoding="utf-8")
    lockfile = _read_lockfile(project)
    locked = _locked_dep(lockfile, dep)
    assert locked is not None, (
        f"lockfile did not contain {dep.host}/{dep.repo_url}; "
        f"dependencies={lockfile.get('dependencies')}"
    )
    assert locked.get("host") == dep.host
    assert locked.get("repo_url") == dep.repo_url
    resolved_commit = locked.get("resolved_commit")
    assert isinstance(resolved_commit, str) and resolved_commit, (
        f"resolved_commit must be a non-empty string in lockfile entry: {locked}"
    )
    assert _FULL_SHA_RE.fullmatch(resolved_commit), (
        f"resolved_commit is not a full commit SHA: {resolved_commit!r}"
    )
    assert resolved_commit == expected_sha, (
        f"resolved_commit did not match {_LIVE_EXPECTED_SHA_ENV}: "
        f"expected {expected_sha}, got {resolved_commit}"
    )

    second_result = _run_install(apm_binary_path, project, fake_home)
    _assert_install_succeeded(second_result, package_ref)
    second_lock_text = lock_path.read_text(encoding="utf-8")
    assert second_lock_text == first_lock_text, "second install changed apm.lock.yaml"
