"""Installed-CLI policy-outcome routing parity (#4084).

Every command that reads org policy applies one routing decision per
discovery outcome: ``apm install`` (the reference consumer), ``apm audit
--ci`` and ``apm approve`` either all block or all proceed. A loopback
GitLab policy host produces each of the closed set of discovery outcomes;
``apm policy status`` proves the outcome before the commands run.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_github_https_fixture import openssl_executable
from tests.utils.local_gitlab_policy_https_fixture import (
    LocalGitLabPolicyFixture,
    local_gitlab_policy_fixture,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.promise("P-OWN-policy-outcome-routing"),
    pytest.mark.lifecycle_smoke,
    pytest.mark.requires_apm_binary,
    pytest.mark.requires_e2e_mode,
    pytest.mark.skipif(openssl_executable() is None, reason="openssl CLI not available"),
]

_ORIGIN = "https://gitlab.com/acme/dept-a/team-x/service.git"
_COMPANY = "acme/apm-policy"
_DEPT = "acme/dept-a/apm-policy"
_TEAM = "acme/dept-a/team-x/apm-policy"
_ALL = (_COMPANY, _DEPT, _TEAM)

_FOUND = "name: company\nenforcement: warn\ndependencies:\n  deny: [nobody/nothing]\n"
_FOUND_FETCH_BLOCK = _FOUND + "fetch_failure: block\n"


def _pin(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class _Outcome:
    projects: dict[str, str | int]
    blocks: bool
    fetch_failure_default: str = "warn"
    git_remote: bool = True
    pin: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    # Seed the cache from these projects, then age it past its TTL.
    stale_seed: dict[str, str] | None = None


# Closed outcome set: one row per outcome, plus the fetch_failure_default
# variants that flip the decision. ``blocks`` is install's routed decision.
_OUTCOMES: dict[str, _Outcome] = {
    "found": _Outcome({_COMPANY: _FOUND}, blocks=False),
    "absent": _Outcome({}, blocks=False),
    "absent-block": _Outcome({}, blocks=True, fetch_failure_default="block"),
    "empty-block": _Outcome(
        {_COMPANY: "name: company\n"}, blocks=True, fetch_failure_default="block"
    ),
    "malformed": _Outcome({_COMPANY: "name: c\nenforcement: sometimes\n"}, blocks=False),
    "malformed-block": _Outcome(
        {_COMPANY: "name: c\nenforcement: sometimes\n"}, blocks=True, fetch_failure_default="block"
    ),
    "garbage_response-block": _Outcome(
        {_COMPANY: "<html>not yaml</html>"}, blocks=True, fetch_failure_default="block"
    ),
    "cache_miss_fetch_fail-block": _Outcome(
        dict.fromkeys(_ALL, 500), blocks=True, fetch_failure_default="block"
    ),
    "cached_stale-fetch_failure_block": _Outcome(
        dict.fromkeys(_ALL, 500), blocks=True, stale_seed={_COMPANY: _FOUND_FETCH_BLOCK}
    ),
    "hash_mismatch": _Outcome({_COMPANY: _FOUND}, blocks=True, pin=_pin("name: other\n")),
    "incomplete_chain": _Outcome({_COMPANY: 500, _TEAM: _FOUND}, blocks=True),
    "no_git_remote-block": _Outcome(
        {}, blocks=True, fetch_failure_default="block", git_remote=False
    ),
    "disabled": _Outcome(
        {}, blocks=False, fetch_failure_default="block", env={"APM_POLICY_DISABLE": "1"}
    ),
}


class _Project:
    def __init__(
        self,
        tmp_path: Path,
        apm_binary_path: Path,
        case: str,
        spec: _Outcome,
        *,
        hooks: bool = False,
    ) -> None:
        self.isolated = IsolatedApmEnvironment.create(tmp_path / case, base_env=dict(os.environ))
        self.project = self.isolated.work_root / "service"
        self.project.mkdir()
        package = self.project / "pkg"
        (package / ".apm" / "instructions").mkdir(parents=True)
        (package / "apm.yml").write_text("name: pkg\nversion: 1.0.0\n", encoding="utf-8")
        (package / ".apm" / "instructions" / "pkg.instructions.md").write_text(
            '---\napplyTo: "**"\n---\nLocal rule.\n', encoding="utf-8"
        )
        if hooks:
            (package / ".apm" / "hooks").mkdir()
            (package / ".apm" / "hooks" / "pre-tool-use.json").write_text("{}", encoding="utf-8")
        manifest = (
            "name: service\nversion: 1.0.0\ndescription: governed\ntargets:\n  - copilot\n"
            "dependencies:\n  apm:\n    - ./pkg\n"
            f"policy:\n  fetch_failure_default: {spec.fetch_failure_default}\n"
        )
        if spec.pin is not None:
            manifest += f"  hash: {spec.pin}\n"
        (self.project / "apm.yml").write_text(manifest, encoding="utf-8")
        env = self.isolated.subprocess_env()
        git_steps = [("init", "--quiet")]
        if spec.git_remote:
            git_steps.append(("remote", "add", "origin", _ORIGIN))
        for args in git_steps:
            subprocess.run(
                ["git", *args], cwd=self.project, env=env, check=True, capture_output=True
            )
        self.extra_env = dict(spec.env)
        self.runner = ApmLifecycleRunner((str(apm_binary_path),), scenario_timeout_seconds=240)

    def run(
        self,
        fixture: LocalGitLabPolicyFixture,
        *args: str,
        scenario: str,
        policy_disabled: bool = False,
    ) -> CommandResult:
        env = fixture.child_environment(self.isolated.subprocess_env())
        env.update(self.extra_env)
        if policy_disabled:
            env["APM_POLICY_DISABLE"] = "1"
        return self.runner.run(args, scenario_id=scenario, cwd=self.project, env=env)

    def ensure_lockfile(self, fixture: LocalGitLabPolicyFixture) -> None:
        """Give audit a lockfile when install was (rightly) blocked."""
        if (self.project / "apm.lock.yaml").is_file():
            return
        seeded = self.run(
            fixture, "install", "--target", "copilot", scenario="seed-lock", policy_disabled=True
        )
        assert seeded.returncode == 0, seeded.stdout + seeded.stderr

    def age_policy_cache(self) -> None:
        for meta in (self.isolated.cache_root / "policy_v1").rglob("*.meta.json"):
            data = json.loads(meta.read_text(encoding="utf-8"))
            data["cached_at"] = time.time() - 2 * 3600
            meta.write_text(json.dumps(data), encoding="utf-8")


def _outcome_id(case: str) -> str:
    return case.split("-", 1)[0]


def _blocked(result: CommandResult) -> bool:
    return result.returncode != 0


@pytest.mark.parametrize("case", sorted(_OUTCOMES))
def test_every_policy_reader_routes_the_outcome_like_install(
    tmp_path: Path, apm_binary_path: Path, case: str
) -> None:
    """install, audit --ci and approve make one decision per discovery outcome.

    install runs first on a fresh project, so its policy gate decides; audit and
    approve then run against a lockfile (seeded with policy disabled when install
    blocked), so only the policy outcome can fail them.
    """
    spec = _OUTCOMES[case]
    project = _Project(tmp_path, apm_binary_path, case, spec)
    with local_gitlab_policy_fixture(tmp_path / "certs") as fixture:
        if spec.stale_seed is not None:
            fixture.publish(spec.stale_seed)
            seeded = project.run(fixture, "policy", "status", "--json", scenario="seed")
            assert json.loads(seeded.stdout)["outcome"] == "found", seeded.stdout
            project.age_policy_cache()
        fixture.publish(spec.projects)
        status = project.run(fixture, "policy", "status", "--json", scenario="status")
        install = project.run(fixture, "install", "--target", "copilot", scenario="install")
        project.ensure_lockfile(fixture)
        audit = project.run(fixture, "audit", "--ci", "--no-drift", scenario="audit")
        approve = project.run(fixture, "approve", "--recommended", scenario="approve")

    assert json.loads(status.stdout)["outcome"] == _outcome_id(case), status.stdout
    decisions = {
        "install": _blocked(install),
        "audit": _blocked(audit),
        "approve": _blocked(approve),
    }
    evidence = "\n".join(
        f"--- {name} (exit {r.returncode})\n{r.stdout}\n{r.stderr}"
        for name, r in (("install", install), ("audit", audit), ("approve", approve))
    )
    assert decisions == dict.fromkeys(decisions, spec.blocks), evidence


@pytest.mark.parametrize(
    ("fetch_failure_default", "blocks"), [("block", True), ("warn", False)], ids=["block", "warn"]
)
def test_approve_under_unreachable_policy_follows_fetch_failure_default(
    tmp_path: Path, apm_binary_path: Path, fetch_failure_default: str, blocks: bool
) -> None:
    """An unreachable org policy under block stops approve before it writes apm.yml."""
    spec = _Outcome(
        dict.fromkeys(_ALL, 500), blocks=blocks, fetch_failure_default=fetch_failure_default
    )
    project = _Project(tmp_path, apm_binary_path, fetch_failure_default, spec, hooks=True)
    with local_gitlab_policy_fixture(tmp_path / "certs") as fixture:
        project.ensure_lockfile(fixture)
        fixture.publish(spec.projects)
        before = (project.project / "apm.yml").read_bytes()
        approve = project.run(fixture, "approve", "--all", scenario="approve")
        after = (project.project / "apm.yml").read_bytes()

    output = approve.stdout + approve.stderr
    if blocks:
        assert approve.returncode == 1, output
        assert "Blocked by org policy" in output, output
        assert "apm policy status --no-cache" in output, output
        assert after == before
    else:
        assert approve.returncode == 0, output
        assert after != before
        assert "executables:" in after.decode("utf-8")


# Wrongly typed policy fields (#4140): each is a malformed policy, routed like
# any other malformed policy, never a crash.
_WRONGLY_TYPED = {
    "enforcement-list": "enforcement: [block]\n",
    "fetch_failure-list": "fetch_failure: [block]\n",
    "require_resolution-list": "dependencies:\n  require_resolution: [block]\n",
    "self_defined-mapping": "mcp:\n  self_defined: {deny: true}\n",
    "scripts-list": "manifest:\n  scripts: [deny]\n",
    "unmanaged_action-list": "unmanaged_files:\n  action: [deny]\n",
    "on_install-list": "security:\n  audit:\n    on_install: [block]\n",
    "mcp_deny-mapping": "mcp:\n  deny: {evil: true}\n",
    "executables_deny-list-of-maps": "executables:\n  deny:\n    - {pkg: evil}\n",
    "extends-list": "extends: [org]\n",
}


@pytest.mark.parametrize("fetch_failure_default", ["warn", "block"])
@pytest.mark.parametrize("field_case", sorted(_WRONGLY_TYPED))
def test_wrongly_typed_policy_field_routes_as_malformed(
    tmp_path: Path, apm_binary_path: Path, field_case: str, fetch_failure_default: str
) -> None:
    """A wrongly typed field never crashes a command; it is a malformed policy."""
    policy = "name: company\n" + _WRONGLY_TYPED[field_case]
    spec = _Outcome(
        {_COMPANY: policy},
        blocks=fetch_failure_default == "block",
        fetch_failure_default=fetch_failure_default,
    )
    project = _Project(tmp_path, apm_binary_path, field_case, spec)
    with local_gitlab_policy_fixture(tmp_path / "certs") as fixture:
        fixture.publish(spec.projects)
        status = project.run(fixture, "policy", "status", "--json", scenario="status")
        install = project.run(fixture, "install", "--target", "copilot", scenario="install")
        project.ensure_lockfile(fixture)
        audit = project.run(fixture, "audit", "--ci", "--no-drift", scenario="audit")

    results = {"status": status, "install": install, "audit": audit}
    for name, result in results.items():
        output = result.stdout + result.stderr
        assert "Traceback" not in output, f"{name}: {output}"
        assert "unhashable" not in output, f"{name}: {output}"
    assert json.loads(status.stdout)["outcome"] == "malformed", status.stdout
    assert _blocked(install) is spec.blocks, install.stdout + install.stderr
    assert _blocked(audit) is spec.blocks, audit.stdout + audit.stderr
