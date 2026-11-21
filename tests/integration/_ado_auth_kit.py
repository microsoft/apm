"""Shared harness for Azure DevOps credential-helper lifecycle scenarios.

One ``AdoAuthScenario`` owns an isolated APM environment, a bare repository
published as ``dev.azure.com/<org>/<project>/_git/<repo>``, the scripted HTTPS
proxy that impersonates Azure DevOps and its sign-in host, and a logging
``git credential`` helper. The child PATH holds only Git and system
directories, so no Azure CLI is reachable unless a scenario installs a shim.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import stat
import subprocess
import sys
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_ado_org_fixture import (
    ADO_HOST,
    SIGNIN_HOST,
    AdoOrgFixture,
    LoggingCredentialHelper,
    install_logging_credential_helper,
)
from tests.utils.local_git_repository import LocalGitRepository, LocalGitRepositoryFactory
from tests.utils.local_package import LocalPackageFactory
from tests.utils.local_scripted_https_proxy import (
    Handler,
    ScriptedHttpsProxy,
    basic_authorization,
    local_scripted_https_proxy,
)

ORG = "contoso"
PROJECT = "platform"
REPO = "agent-skills"
SKILL = "ado-helper"
HELPER_USER = "helper-user@contoso.example"
HELPER_SECRET = "helper-credential-sentinel-7f3a"
SOURCE = f"https://{ADO_HOST}/{ORG}/{PROJECT}/_git/{REPO}"
HELPER_PATH = f"{ORG}/{PROJECT}/_git/{REPO}"
INSTALL = ("install", "--target", "copilot", "--parallel-downloads", "0")
SKILL_PATH = Path(".agents/skills") / SKILL / "SKILL.md"


def skill_document(marker: str) -> str:
    return f"---\nname: {SKILL}\ndescription: Azure DevOps helper lifecycle\n---\n# {marker}\n"


def flat(result: CommandResult) -> str:
    return " ".join((result.stdout + result.stderr).split())


def describe(result: CommandResult, proxy: ScriptedHttpsProxy | None = None) -> str:
    text = f"command={result.command!r}\nrc={result.returncode}\n{result.stdout}\n{result.stderr}"
    if proxy is not None:
        text += "\n" + trace(proxy)
    return text


def trace(proxy: ScriptedHttpsProxy) -> str:
    """Return one line per proxied request: host, method, target, status, auth scheme."""
    return "\n".join(
        f"{o.host} {o.method} {o.target[:120]} -> {o.status} "
        f"auth={(o.header('Authorization') or '-').split(' ', 1)[0]}"
        for o in proxy.observations
    )


def _system_path(git: Path) -> str:
    parts = [str(git.parent)]
    for candidate in ("/usr/bin", "/bin", "/usr/sbin", "/sbin"):
        if Path(candidate).is_dir():
            parts.append(candidate)
    return os.pathsep.join(parts)


@dataclass
class AdoAuthScenario:
    """One consumer project plus an impersonated Azure DevOps organization."""

    tmp_path: Path
    apm_binary_path: Path
    isolated: IsolatedApmEnvironment = field(init=False)
    repository: LocalGitRepository = field(init=False)
    repositories: LocalGitRepositoryFactory = field(init=False)
    packages: LocalPackageFactory = field(init=False)
    ado: AdoOrgFixture = field(init=False)
    helper: LoggingCredentialHelper | None = field(init=False, default=None)
    runner: ApmLifecycleRunner = field(init=False)
    real_git: Path = field(init=False)
    git_exec_path: str = field(init=False)
    bin_dir: Path = field(init=False)

    def __post_init__(self) -> None:
        self.isolated = IsolatedApmEnvironment.create(
            self.tmp_path / "scenario", base_env=dict(os.environ)
        )
        resolved = shutil.which("git")
        assert resolved is not None
        self.real_git = Path(resolved)
        self.git_exec_path = subprocess.run(
            (resolved, "--exec-path"), capture_output=True, text=True, check=True, timeout=10
        ).stdout.strip()
        self.bin_dir = self.isolated.root / "bin"
        self.bin_dir.mkdir()
        self.packages = LocalPackageFactory(self.isolated.package_root)
        self.repositories = LocalGitRepositoryFactory(
            self.isolated.repository_root, env=self.isolated.subprocess_env()
        )
        bundle = self.packages.create(REPO, targets=("copilot",))
        self.packages.add_skill(bundle, SKILL, skill_document("version one"))
        self.repository = self.repositories.create(REPO, source_tree=bundle.root)
        self.first_commit = self.repositories.commit(self.repository, message="seed")
        self.ado = AdoOrgFixture()
        self.ado.add_repository(ORG, PROJECT, REPO, self.repository.origin)
        self.runner = ApmLifecycleRunner(
            (str(self.apm_binary_path),), timeout_seconds=150, scenario_timeout_seconds=600
        )

    @property
    def git_config(self) -> Path:
        return Path(self.isolated.process_environment["GIT_CONFIG_GLOBAL"])

    def advance(self, marker: str) -> str:
        skill = self.repository.worktree / "skills" / SKILL / "SKILL.md"
        skill.write_text(skill_document(marker), encoding="utf-8")
        return self.repositories.commit(self.repository, message=marker).sha

    def install_helper(self, *, respond: bool = True) -> LoggingCredentialHelper:
        self.helper = install_logging_credential_helper(
            self.git_config,
            self.isolated.root / "helper",
            username=HELPER_USER,
            password=HELPER_SECRET,
            respond=respond,
        )
        self.ado.accepted_authorizations.add(basic_authorization(HELPER_USER, HELPER_SECRET))
        return self.helper

    def install_az_shim(self, script: str) -> Path:
        """Install an ``az`` executable on the child PATH running *script* (Python)."""
        shim = self.bin_dir / "az"
        log = self.isolated.root / "az-calls.jsonl"
        shim.write_text(
            f"#!{sys.executable}\nAZ_LOG = {str(log)!r}\n" + script,
            encoding="ascii",
            newline="\n",
        )
        shim.chmod(shim.stat().st_mode | stat.S_IXUSR)
        return log

    def project(self, name: str = "consumer", *, manifest: str | None = None) -> Path:
        root = self.isolated.work_root / name
        root.mkdir(parents=True, exist_ok=True)
        (root / ".github").mkdir(exist_ok=True)
        (root / ".github" / "copilot-instructions.md").write_text("# consumer\n", encoding="utf-8")
        if manifest is not None:
            (root / "apm.yml").write_text(manifest, encoding="utf-8")
        return root

    def env(
        self, proxy: ScriptedHttpsProxy, overrides: Mapping[str, str] | None = None
    ) -> dict[str, str]:
        environment = proxy.child_environment(self.isolated.subprocess_env())
        environment["PATH"] = os.pathsep.join((str(self.bin_dir), _system_path(self.real_git)))
        environment["GIT_ALLOW_PROTOCOL"] = "https:file"
        environment["GIT_EXEC_PATH"] = self.git_exec_path
        environment["APM_NON_INTERACTIVE"] = "1"
        environment.pop("CI", None)
        environment.pop("GITHUB_ACTIONS", None)
        environment.pop("TF_BUILD", None)
        environment.pop("GCM_INTERACTIVE", None)
        if overrides:
            environment.update(overrides)
        return environment

    @contextlib.contextmanager
    def serve(self, extra: Mapping[str, Handler] | None = None) -> Iterator[ScriptedHttpsProxy]:
        handlers: dict[str, Handler] = {ADO_HOST: self.ado.handle, SIGNIN_HOST: self.ado.signin}
        handlers.update(extra or {})
        with local_scripted_https_proxy(self.isolated.root / "tls", handlers) as proxy:
            yield proxy

    def run(
        self, args: tuple[str, ...], *, cwd: Path, env: Mapping[str, str], step: str
    ) -> CommandResult:
        return self.runner.run(args, scenario_id=step, cwd=cwd, env=env)


def manifest(dependencies: str = "", extra: str = "") -> str:
    text = "name: consumer\nversion: 1.0.0\ntargets: [copilot]\n"
    if dependencies:
        text += f"dependencies:\n  apm:\n{dependencies}"
    return text + extra


def ado_dependency_entry(ref: str = "main") -> str:
    return f"    - git: {SOURCE}\n      path: skills/{SKILL}\n      ref: {ref}\n"


def received_helper_credential(proxy: ScriptedHttpsProxy) -> bool:
    return HELPER_SECRET in "".join(
        value for o in proxy.requests_to(ADO_HOST) for value in o.header_values()
    )
