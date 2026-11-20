"""APMLifecycle: a missing or inaccessible repository is reported as such.

PROMISE: outcome.status_matches_state -- when a host answers 404 for a
dependency, install reports the repository as not found (never a network
error), names the host and the credential source it tried (never the secret),
prints each line once, ends with one runnable ``Run 'apm ...'`` step, and the
failed command leaves durable state unchanged (I7). Running the step reaches
its stated outcome. (source: #4199, #4174; docs/src/content/docs/getting-started/authentication.md)
CELL: OP=install SRC=git NET=404 SCOPE=project TGT=copilot
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.lifecycle_state import LifecycleStateSnapshot
from tests.utils.local_git_repository import LocalGitRepository, LocalGitRepositoryFactory
from tests.utils.local_github_https_fixture import openssl_executable
from tests.utils.local_package import LocalPackageFactory
from tests.utils.local_scripted_https_proxy import (
    ScriptedRequest,
    ScriptedResponse,
    git_smart_http,
    local_scripted_https_proxy,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.skipif(openssl_executable() is None, reason="openssl CLI required"),
    pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only fixture"),
    pytest.mark.promise("P-LAW-auth.credentials_isolated"),
]

_TARGET = "copilot"
_OWNER = "fixture"
_NEXT_STEP = re.compile(r"Run '(apm [^']+)'")
_GHE_HOST = "lifecycle.ghe.com"
_GHE_TOKEN = "ghe-org-scoped-sentinel-7c31"


def _evidence(result: CommandResult) -> str:
    return (
        f"command={result.command!r}\nexit={result.returncode}\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )


def _flat(result: CommandResult) -> str:
    return " ".join(f"{result.stdout}\n{result.stderr}".split())


def _assert_output_oracles(result: CommandResult, *, secret: str | None = None) -> None:
    """I6 ASCII, I9 each line once, and no credential value in output."""
    for stream in (result.stdout, result.stderr):
        assert stream.isascii(), _evidence(result)
        lines = [line.strip() for line in stream.splitlines() if line.strip()]
        repeated = sorted({line for line in lines if lines.count(line) > 1})
        assert not repeated, f"I9: repeated lines {repeated}\n{_evidence(result)}"
        assert "Traceback" not in stream, _evidence(result)
        if secret is not None:
            assert secret not in stream, "credential value printed"


def _single_next_step(result: CommandResult) -> tuple[str, ...]:
    steps = _NEXT_STEP.findall(_flat(result))
    assert len(steps) == 1, _evidence(result)
    assert "<" not in steps[0], f"next step is a placeholder: {steps[0]!r}"
    argv = tuple(shlex.split(steps[0]))
    assert argv[0] == "apm", steps[0]
    return argv[1:]


def _repository_handler(
    repositories: dict[str, LocalGitRepository], missing_body: bytes
) -> Callable[[ScriptedRequest], ScriptedResponse]:
    """Serve *repositories* by ``owner/name``; every other path answers 404."""
    pattern = re.compile(r"^/(?P<repo>[^/]+/[^/]+?)(?:\.git)?(?P<service>/.*)$")

    def handle(request: ScriptedRequest) -> ScriptedResponse:
        match = pattern.match(request.path)
        repository = repositories.get(match.group("repo")) if match else None
        if repository is not None:
            return git_smart_http(repository.origin, request, match.group("service"))
        return ScriptedResponse(404, {"Content-Type": "text/plain"}, missing_body)

    return handle


def _not_found(_request: ScriptedRequest) -> ScriptedResponse:
    return ScriptedResponse(404, {"Content-Type": "application/json"}, b'{"message":"Not Found"}')


def _child_env(isolated: IsolatedApmEnvironment, proxy, extra: dict[str, str]) -> dict[str, str]:
    env = proxy.child_environment(isolated.subprocess_env())
    env["APM_NON_INTERACTIVE"] = "1"
    env["GIT_ALLOW_PROTOCOL"] = "file:https"
    env["PATH"] = os.pathsep.join(p for p in ("/usr/bin", "/bin") if Path(p).is_dir())
    env.update(extra)
    return env


def _snap(project: Path) -> LifecycleStateSnapshot:
    return LifecycleStateSnapshot.capture(project, targets=(_TARGET,))


def _skill(name: str) -> str:
    return f"---\nname: {name}\ndescription: Not-found lifecycle fixture\n---\n# {name}\n"


@pytest.mark.parametrize(
    "missing_body",
    (
        pytest.param(b"Repository not found.\n", id="github-wording"),
        pytest.param(b"", id="bare-404"),
    ),
)
def test_missing_declared_repository_is_not_found_with_runnable_next_step(
    tmp_path: Path, apm_binary_path: Path, missing_body: bytes
) -> None:
    """#4199: a declared repository the host answers 404 for is not a network error."""
    if shutil.which("git") is None:
        pytest.skip("git executable not available")
    isolated = IsolatedApmEnvironment.create(tmp_path / "world", base_env=dict(os.environ))
    packages = LocalPackageFactory(isolated.package_root)
    present = packages.create("present", targets=(_TARGET,))
    packages.add_skill(present, "present", _skill("present"))
    repositories = LocalGitRepositoryFactory(
        isolated.repository_root, env=isolated.subprocess_env()
    )
    repository = repositories.create("present", source_tree=present.root)
    repositories.tag(repository, "v1.0.0", repositories.commit(repository, message="seed"))
    consumers = LocalPackageFactory(isolated.work_root)
    project = consumers.create(
        "consumer", dependencies=(f"{_OWNER}/present#v1.0.0",), targets=(_TARGET,)
    )
    runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=180)
    handlers = {
        "github.com": _repository_handler({f"{_OWNER}/present": repository}, missing_body),
        "api.github.com": _not_found,
        "codeload.github.com": _not_found,
        "raw.githubusercontent.com": _not_found,
    }
    with local_scripted_https_proxy(isolated.root / "tls", handlers) as proxy:
        env = _child_env(isolated, proxy, {})

        def apm(*args: str) -> CommandResult:
            result = runner.run(args, cwd=project.root, env=env)
            _assert_output_oracles(result)
            return result

        installed = apm("install")
        assert installed.returncode == 0, _evidence(installed)
        consumers.replace_apm_dependencies(
            project, (f"{_OWNER}/present#v1.0.0", f"{_OWNER}/missing#v1.0.0")
        )
        manifest_bytes = project.manifest_path.read_bytes()
        before = _snap(project.root)

        failed = apm("install", "--verbose")

        assert failed.returncode == 1, _evidence(failed)  # I8
        output = _flat(failed)
        assert "network error" not in output, _evidence(failed)
        assert "protocol-fallback" not in output, _evidence(failed)
        assert f"Repository {_OWNER}/missing not found on github.com" in output, _evidence(failed)
        assert "no credential found" in output, _evidence(failed)
        assert project.manifest_path.read_bytes() == manifest_bytes
        assert _snap(project.root) == before, "I7: a failed install changed durable state"

        next_step = _single_next_step(failed)
        assert next_step == ("uninstall", f"{_OWNER}/missing"), _evidence(failed)
        removed = apm(*next_step)
        assert removed.returncode == 0, _evidence(removed)
        manifest = load_yaml(project.manifest_path)
        assert manifest["dependencies"]["apm"] == [f"{_OWNER}/present#v1.0.0"]
        converged = apm("install")
        assert converged.returncode == 0, _evidence(converged)
        assert (project.root / ".agents/skills/present/SKILL.md").is_file()


def test_inaccessible_repository_names_host_and_credential_source(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#4174: the failure names the host and the credential source, never its value."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "world", base_env=dict(os.environ))
    project = LocalPackageFactory(isolated.work_root).create("consumer", targets=(_TARGET,))
    runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=180)
    package = f"{_GHE_HOST}/lifecycle/p-missing"
    handlers = {_GHE_HOST: _not_found, f"api.{_GHE_HOST}": _not_found}
    with local_scripted_https_proxy(isolated.root / "tls", handlers) as proxy:
        env = _child_env(isolated, proxy, {"GITHUB_APM_PAT_LIFECYCLE": _GHE_TOKEN})

        def apm(*args: str) -> CommandResult:
            result = runner.run(args, cwd=project.root, env=env)
            _assert_output_oracles(result, secret=_GHE_TOKEN)
            return result

        manifest_bytes = project.manifest_path.read_bytes()
        before = _snap(project.root)

        failed = apm("install", package)

        assert failed.returncode == 1, _evidence(failed)  # I8
        output = _flat(failed)
        assert f"[x] {package} -- not accessible or doesn't exist" in output, _evidence(failed)
        assert f"host: {_GHE_HOST}" in output, _evidence(failed)
        assert "credential tried: GITHUB_APM_PAT_LIFECYCLE" in output, _evidence(failed)
        assert project.manifest_path.read_bytes() == manifest_bytes
        assert _snap(project.root) == before, "I7: a failed install changed durable state"

        next_step = _single_next_step(failed)
        assert next_step == ("install", "--verbose", package), _evidence(failed)
        verbose = apm(*next_step)
        assert verbose.returncode == 1, _evidence(verbose)
        assert "source=GITHUB_APM_PAT_LIFECYCLE" in _flat(verbose), _evidence(verbose)
        assert "Run '" not in _flat(verbose), "the verbose form repeats its own advice"
        assert project.manifest_path.read_bytes() == manifest_bytes
        assert proxy.hosts_receiving(_GHE_TOKEN) <= {_GHE_HOST, f"api.{_GHE_HOST}"}
