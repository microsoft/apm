"""An org policy discovered by the installed CLI decides what install writes.

PROMISE: transaction.failed_command_preserves_state and
outcome.status_matches_state (tests/fixtures/lifecycle_bug_ledger.json) for
install-time org policy enforcement (#827, #4081); owners
policy-remote-origin-reader and policy-violation-subjects
(.apm/architecture/owners/install-deployment.json).

Every row drives the installed ``apm`` binary against a real org policy: the
policy lives in a local ``contoso/.github`` git repository served over the
loopback GitHub HTTPS fixture, and the consumer's ``origin`` remote points at
``contoso`` so the CLI discovers that policy itself. Nothing in discovery or
enforcement is patched. Each row asserts the exit code, the user-visible
output and a before/after ``LifecycleStateSnapshot``; a refused install must
leave the project byte-identical.

The in-process routing rows for the same matrix (discovery replaced by a
fixed result) live in ``test_policy_install_e2e.py``. Target-policy and MCP
transport rows have installed-CLI homes in
``test_target_declaration_install_lifecycle.py`` and
``test_mcp_transport_policy_lifecycle.py``.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml
from tests.integration._policy_scope_kit import (
    PolicyScopeWorld,
    deployed_skills,
    lock_repo_urls,
    output,
    run_step,
    tree_snapshot,
)
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.lifecycle_state import LifecycleStateSnapshot
from tests.utils.local_github_https_fixture import (
    local_github_https_fixture,
    openssl_executable,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.lifecycle_smoke,
    pytest.mark.requires_apm_binary,
    pytest.mark.requires_e2e_mode,
    pytest.mark.skipif(openssl_executable() is None, reason="openssl CLI not available"),
]

_POLICY = {"name": "contoso-policy", "version": "1.0.0"}
_BLOCKED = "blocked by org policy"


def _policy(enforcement: str, **dependencies: object) -> dict[str, object]:
    policy: dict[str, object] = {**_POLICY, "enforcement": enforcement}
    if dependencies:
        policy["dependencies"] = dict(dependencies)
    return policy


def _assert_output_oracles(result: CommandResult) -> None:
    """I6 ASCII output and I9 no duplicate status lines per stream."""
    for stream in (result.stdout, result.stderr):
        assert stream.isascii(), output(result)
        lines = [line.strip() for line in stream.splitlines() if line.lstrip().startswith("[")]
        assert len(lines) == len(set(lines)), output(result)


def _flat(text: str) -> str:
    """Join Rich soft-wrapped lines so assertions see whole messages."""
    return " ".join(text.split())


@dataclass
class _Governed:
    """One consumer project, its isolated world and the installed CLI."""

    world: PolicyScopeWorld
    project: Path
    env: dict[str, str]
    runner: ApmLifecycleRunner

    def snapshot(self) -> tuple[LifecycleStateSnapshot, dict[str, str]]:
        return (
            LifecycleStateSnapshot.capture(self.project, targets=("copilot",)),
            tree_snapshot(self.project),
        )

    def install(
        self,
        *args: str,
        step: str,
        expected: int,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        result = run_step(
            self.runner,
            ("install", *args),
            cwd=self.project,
            env=env if env is not None else self.env,
            step=step,
            expected=expected,
        )
        _assert_output_oracles(result)
        return result

    def assert_refused_unchanged(
        self, before: tuple[LifecycleStateSnapshot, dict[str, str]], result: CommandResult
    ) -> None:
        after = self.snapshot()
        assert after[0] == before[0], f"refused install changed lifecycle state\n{output(result)}"
        assert after[1] == before[1], f"refused install changed project bytes\n{output(result)}"
        assert not (self.project / "apm.lock.yaml").exists(), output(result)
        assert not deployed_skills(self.project), output(result)

    def assert_installed(
        self,
        before: tuple[LifecycleStateSnapshot, dict[str, str]],
        result: CommandResult,
        packages: Sequence[str],
    ) -> None:
        after = self.snapshot()
        assert after[0] != before[0], f"install left lifecycle state unchanged\n{output(result)}"
        assert after[0].lockfile_bytes is not None, output(result)
        assert lock_repo_urls(self.project) == {f"contoso/{name}" for name in packages}
        assert deployed_skills(self.project) == {f"{name}-skill" for name in packages}
        for record in after[0].deployment_records:
            assert (self.project / record.locator.value).exists(), record


@contextmanager
def _governed(
    tmp_path: Path,
    apm_binary_path: Path,
    *,
    policy: Mapping[str, object] | None,
    packages: Sequence[str] = ("pkg-a",),
    undeclared: Sequence[str] = (),
    extra_dependencies: Sequence[str] = (),
    parent_policies: Mapping[str, Mapping[str, object]] | None = None,
) -> Iterator[_Governed]:
    """Publish packages and policies, then serve them to one consumer."""
    world = PolicyScopeWorld(tmp_path, dict(os.environ))
    published = [world.publish_package(name) for name in packages]
    for name in undeclared:
        world.publish_package(name)
    if policy is not None:
        world.publish_policy(policy)
    served = world.served_repositories()
    for name, parent in (parent_policies or {}).items():
        source = world.isolated.package_root / name
        source.mkdir(parents=True)
        dump_yaml(dict(parent), source / "apm-policy.yml")
        repository = world.repositories.create(name, source_tree=source)
        world.repositories.commit(repository, message=name)
        served[f"contoso/{name}"] = repository
    project = world.consumer(dependencies=[*(pkg.slug for pkg in published), *extra_dependencies])
    runner = ApmLifecycleRunner((str(apm_binary_path),), scenario_timeout_seconds=900)
    with local_github_https_fixture(tmp_path / "certs", served) as fx:
        yield _Governed(world, project, fx.child_environment(world.git_env()), runner)


def test_block_policy_refuses_denied_dependency_without_writing(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """I1: a denied direct dependency fails the install and writes nothing."""
    policy = _policy("block", deny=["contoso/pkg-denied"])
    with _governed(
        tmp_path, apm_binary_path, policy=policy, packages=("pkg-a", "pkg-denied")
    ) as gov:
        before = gov.snapshot()
        result = gov.install(step="install-blocked", expected=1)

        stderr = _flat(result.stderr)
        assert "Policy violation: github.com/contoso/pkg-denied" in stderr, stderr
        assert _BLOCKED in stderr, stderr
        assert "contoso/pkg-a" not in stderr, stderr
        gov.assert_refused_unchanged(before, result)


def test_allowlist_refuses_unlisted_dependency_without_writing(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """I4: a dependency outside the allow list fails the install."""
    policy = _policy("block", allow=["contoso/pkg-a"])
    with _governed(
        tmp_path, apm_binary_path, policy=policy, packages=("pkg-a", "pkg-rogue")
    ) as gov:
        before = gov.snapshot()
        result = gov.install(step="install-unlisted", expected=1)

        stderr = _flat(result.stderr)
        assert "Policy violation: github.com/contoso/pkg-rogue" in stderr, stderr
        assert "allow" in stderr.lower(), stderr
        assert _BLOCKED in stderr, stderr
        gov.assert_refused_unchanged(before, result)


def test_refused_package_install_restores_manifest_bytes(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """I12: ``apm install <pkg>`` for a denied package restores apm.yml byte-equal."""
    policy = _policy("block", deny=["contoso/pkg-denied"])
    with _governed(tmp_path, apm_binary_path, policy=policy, undeclared=("pkg-denied",)) as gov:
        gov.install(step="install-allowed", expected=0)
        before = gov.snapshot()
        result = gov.install("contoso/pkg-denied", step="install-denied-pkg", expected=1)

        stderr = _flat(result.stderr)
        assert "Policy violation: github.com/contoso/pkg-denied" in stderr, stderr
        after = gov.snapshot()
        assert after[0] == before[0], output(result)
        assert after[1] == before[1], output(result)
        assert "pkg-denied" not in (gov.project / "apm.yml").read_text(encoding="utf-8")


def test_dry_run_previews_blocked_dependencies_without_writing(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """I10 + I11: dry-run lists five would-be-blocked lines plus an overflow tail."""
    denied = [f"contoso/blocked-{index}" for index in range(7)]
    policy = _policy("block", deny=["contoso/blocked-*"])
    with _governed(tmp_path, apm_binary_path, policy=policy, extra_dependencies=denied) as gov:
        before = gov.snapshot()
        result = gov.install("--dry-run", step="dry-run", expected=0)

        out = _flat(output(result))
        assert out.count("Would be blocked by policy:") == 5, out
        assert "and 2 more" in out, out
        assert "Would be blocked by policy: github.com/contoso/pkg-a" not in out, out
        gov.assert_refused_unchanged(before, result)


def test_warn_policy_installs_and_reports_every_violation(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """I3 + I20: warn mode installs and reports each violation, not only the first."""
    packages = ("pkg-1", "pkg-2", "pkg-3")
    policy = _policy("warn", deny=["contoso/pkg-*"])
    with _governed(tmp_path, apm_binary_path, policy=policy, packages=packages) as gov:
        before = gov.snapshot()
        result = gov.install(step="install-warn", expected=0)

        out = _flat(output(result))
        assert _BLOCKED not in out, out
        for name in packages:
            assert f"github.com/contoso/{name}" in out, out
        assert "policy warning" in out.lower(), out
        gov.assert_installed(before, result, packages)

        converged = gov.snapshot()
        gov.install(step="install-warn-again", expected=0)
        assert gov.snapshot() == converged, "I2: converged warn-mode install changed state"


def test_three_level_extends_chain_enforces_root_deny(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """I21: a deny rule only on the root of leaf -> mid -> root still blocks."""
    leaf = {**_policy("warn"), "extends": "contoso/policy-mid"}
    parents = {
        "policy-mid": {**_policy("warn"), "extends": "contoso/policy-root"},
        "policy-root": _policy("block", deny=["contoso/pkg-denied"]),
    }
    with _governed(
        tmp_path,
        apm_binary_path,
        policy=leaf,
        packages=("pkg-a", "pkg-denied"),
        parent_policies=parents,
    ) as gov:
        before = gov.snapshot()
        result = gov.install(step="install-chain", expected=1)

        stderr = _flat(result.stderr)
        assert "Policy violation: github.com/contoso/pkg-denied" in stderr, stderr
        gov.assert_refused_unchanged(before, result)


@pytest.mark.parametrize(
    ("flag", "env_disable", "reason"),
    [
        pytest.param(("--no-policy",), None, "--no-policy", id="no-policy-flag"),
        pytest.param((), "1", "APM_POLICY_DISABLE=1", id="policy-disable-env"),
    ],
)
def test_policy_opt_out_installs_denied_dependency_with_loud_warning(
    tmp_path: Path,
    apm_binary_path: Path,
    flag: tuple[str, ...],
    env_disable: str | None,
    reason: str,
) -> None:
    """I2 + I9: each escape hatch installs a denied dependency and says so."""
    policy = _policy("block", deny=["contoso/pkg-a"])
    with _governed(tmp_path, apm_binary_path, policy=policy) as gov:
        env = dict(gov.env)
        if env_disable is not None:
            env["APM_POLICY_DISABLE"] = env_disable
        before = gov.snapshot()
        result = gov.install(*flag, step="install-opt-out", expected=0, env=env)

        out = _flat(output(result))
        assert reason in out, out
        assert _BLOCKED not in out, out
        gov.assert_installed(before, result, ("pkg-a",))


_UNUSABLE_POLICY_TEXT = {
    "malformed-policy": "name: contoso-policy\nversion: 1.0.0\nenforcement: explode\n",
    "garbage-policy": "<html><body>Sign in to continue</body></html>\n",
}


@pytest.mark.parametrize(
    "scenario",
    ["enforcement-off", "absent-policy", "malformed-policy", "garbage-policy", "no-git-remote"],
)
def test_non_enforcing_policy_outcomes_install(
    tmp_path: Path, apm_binary_path: Path, scenario: str
) -> None:
    """I13, I14, I19, I16, I17: outcomes that must not block install."""
    policy: dict[str, object] | None = {
        "enforcement-off": _policy("off", deny=["contoso/pkg-a"]),
        "absent-policy": None,
        "malformed-policy": _policy("block"),
        "garbage-policy": _policy("block"),
        "no-git-remote": _policy("block", deny=["contoso/pkg-a"]),
    }[scenario]
    with _governed(tmp_path, apm_binary_path, policy=policy) as gov:
        if scenario in _UNUSABLE_POLICY_TEXT:
            repository = gov.world.policy_repository
            assert repository is not None
            (repository.worktree / "apm-policy.yml").write_text(
                _UNUSABLE_POLICY_TEXT[scenario], encoding="utf-8"
            )
            gov.world.repositories.commit(repository, message=scenario)
        if scenario == "no-git-remote":
            subprocess.run(
                ["git", "remote", "remove", "origin"],
                cwd=gov.project,
                env=gov.env,
                check=True,
            )
        before = gov.snapshot()
        result = gov.install(step=f"install-{scenario}", expected=0)

        out = _flat(output(result))
        assert _BLOCKED not in out, out
        assert "Policy violation" not in out, out
        if scenario == "malformed-policy":
            assert "Policy at org:contoso/.github is malformed" in out, out
        if scenario == "garbage-policy":
            assert "is not valid YAML" in out, out
            assert "proceeding without policy enforcement" in out, out
        gov.assert_installed(before, result, ("pkg-a",))
