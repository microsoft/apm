"""Installed-CLI contracts for Claude/Codex native connection collisions."""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pytest
import tomlkit

from apm_cli.core.deployment_ledger import DeploymentLedgerCodec
from apm_cli.deps.lockfile import LockFile
from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_mcp_registry import LocalMcpRegistry, LocalMcpRegistryFactory
from tests.utils.local_package import LocalPackageFactory

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
]


@pytest.mark.parametrize("user_scope", [False, True], ids=["project", "user"])
@pytest.mark.parametrize("foreign_target", ["claude", "codex"])
def test_differing_foreign_connection_is_incomplete_with_truthful_ownership(
    tmp_path: Path,
    apm_binary_path: Path,
    user_scope: bool,
    foreign_target: str,
) -> None:
    """Preserve foreign bytes and refuse before writing any target (#4268)."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "scenario", base_env=dict(os.environ))
    project = isolated.work_root / "consumer"
    project.mkdir()
    root = isolated.config_root if user_scope else project
    codex_home = isolated.home / ".codex"
    environment = isolated.subprocess_env(
        overrides={"CLAUDE_CONFIG_DIR": "", "CODEX_HOME": str(codex_home)}
    )
    paths = {
        "claude": isolated.home / ".claude.json" if user_scope else project / ".mcp.json",
        "codex": (codex_home if user_scope else project / ".codex") / "config.toml",
    }
    requested_url = "http://127.0.0.1:8000/company"
    personal_url = "http://127.0.0.1:9/personal"
    foreign_path = paths[foreign_target]
    foreign_path.parent.mkdir(parents=True, exist_ok=True)
    if foreign_target == "claude":
        foreign_path.write_text(
            json.dumps(
                {
                    "preferences": {"keep": True},
                    "mcpServers": {"notes": {"type": "http", "url": personal_url}},
                }
            ),
            encoding="utf-8",
        )
    else:
        foreign_path.write_text(
            f'model = "personal-model"\n\n[mcp_servers.notes]\nurl = "{personal_url}"\n',
            encoding="utf-8",
        )
    original = foreign_path.read_bytes()
    dump_yaml(
        {
            "name": "native-collision-consumer",
            "version": "1.0.0",
            "targets": ["claude", "codex"],
            "dependencies": {
                "mcp": [
                    {
                        "name": "notes",
                        "registry": False,
                        "transport": "http",
                        "url": requested_url,
                    }
                ]
            },
        },
        root / "apm.yml",
    )
    tree = sorted(path.relative_to(project).as_posix() for path in project.rglob("*"))
    result = ApmLifecycleRunner((str(apm_binary_path),)).run(
        (
            "install",
            *(("--global",) if user_scope else ()),
            "--target",
            "claude,codex",
            "--no-policy",
        ),
        cwd=project,
        env=environment,
        scenario_id=f"native-collision-{foreign_target}-{'user' if user_scope else 'project'}",
    )
    evidence = f"command={result.command!r}\n{result.stdout}\n{result.stderr}"
    assert foreign_path.read_bytes() == original, evidence
    other_path = paths["codex" if foreign_target == "claude" else "claude"]
    assert not other_path.exists() or "notes" not in other_path.read_text("utf-8"), evidence
    lock = LockFile.read(root / "apm.lock.yaml") if (root / "apm.lock.yaml").exists() else None
    assert lock is None or not lock.mcp_target_servers, evidence
    assert sorted(p.relative_to(project).as_posix() for p in project.rglob("*")) == tree, evidence
    assert result.returncode != 0, evidence
    assert foreign_target in (result.stdout + result.stderr).lower(), evidence


@dataclass
class _Scenario:
    isolated: IsolatedApmEnvironment
    project: Path
    root: Path
    user_scope: bool
    runner: ApmLifecycleRunner
    environment_overrides: dict[str, str] = field(default_factory=dict)

    def path(self, target: str) -> Path:
        """Resolve only the isolated native target configuration."""
        if target == "claude":
            return (
                self.isolated.home / ".claude.json"
                if self.user_scope
                else self.project / ".mcp.json"
            )
        return (self.isolated.home if self.user_scope else self.project) / ".codex/config.toml"

    def run(self, *args: str) -> CommandResult:
        """Run the installed CLI without host credentials, state, or network."""
        environment = self.isolated.subprocess_env(
            overrides={
                "CLAUDE_CONFIG_DIR": "",
                "CODEX_HOME": str(self.isolated.home / ".codex"),
            }
        )
        environment.update(self.environment_overrides)
        return self.runner.run(
            (*args, *(("--global",) if self.user_scope else ())),
            cwd=self.project,
            env=environment,
            scenario_id="native-mcp-collision-lifecycle",
        )

    def audit(self) -> CommandResult:
        """Run the CI audit at this scope's root (audit has no --global flag)."""
        environment = self.isolated.subprocess_env(
            overrides={"CLAUDE_CONFIG_DIR": "", "CODEX_HOME": str(self.isolated.home / ".codex")}
        )
        environment.update(self.environment_overrides)
        return self.runner.run(
            ("audit", "--ci", "--no-policy"),
            cwd=self.root,
            env=environment,
            scenario_id="native-mcp-collision-lifecycle",
        )

    def install(self, *flags: str, targets: str = "claude,codex") -> CommandResult:
        """Reconcile the requested scope and exact target selection."""
        return self.run("install", "--target", targets, "--no-policy", *flags)

    def declare(self, servers: list[dict[str, Any]]) -> None:
        """Change root intent without manufacturing native deployment state."""
        manifest = load_yaml(self.root / "apm.yml")
        manifest.setdefault("dependencies", {})["mcp"] = servers
        dump_yaml(manifest, self.root / "apm.yml")

    def write(self, target: str, url: str) -> None:
        """Seed a personal connection and unrelated native settings."""
        path = self.path(target)
        path.parent.mkdir(parents=True, exist_ok=True)
        if target == "claude":
            path.write_text(
                json.dumps(
                    {
                        "preferences": {"keep": True},
                        "mcpServers": {
                            "notes": {"type": "http", "url": url},
                        },
                    }
                ),
                encoding="utf-8",
            )
        else:
            path.write_text(
                f'# user comment\nmodel = "personal"\n[mcp_servers.notes]\nurl = "{url}"\n',
                encoding="utf-8",
            )

    def servers(self, target: str) -> dict:
        """Read actual native entries; an emptied .mcp.json APM created is gone (#4852)."""
        if target == "claude" and not self.path(target).exists():
            return {}
        text = self.path(target).read_text(encoding="utf-8")
        if target == "claude":
            return json.loads(text).get("mcpServers", {})
        return dict(tomlkit.parse(text).get("mcp_servers", {}))

    def edit(self, target: str, name: str, entry: dict) -> None:
        """Simulate a user's edit without discarding other native entries."""
        path = self.path(target)
        text = path.read_text(encoding="utf-8")
        if target == "claude":
            data = json.loads(text)
            data.setdefault("mcpServers", {})[name] = entry
            data["preferences"] = {"keep": True}
            path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        else:
            data = tomlkit.parse(text)
            data.setdefault("mcp_servers", {})[name] = entry
            data["model"] = "personal-model"
            path.write_text(tomlkit.dumps(data), encoding="utf-8")

    def lock(self) -> LockFile:
        """Read the actual durable per-target ownership state."""
        lock = LockFile.read(self.root / "apm.lock.yaml")
        assert lock is not None
        return lock


def _http(url: str = "http://127.0.0.1:8000/company") -> dict[str, Any]:
    return {"name": "notes", "registry": False, "transport": "http", "url": url}


def _apm_state(isolated: IsolatedApmEnvironment) -> list[tuple[str, bytes | None]]:
    """Every path (directories included) under APM home and the APM cache."""
    state: list[tuple[str, bytes | None]] = []
    for base in (isolated.config_root, isolated.cache_root):
        for path in sorted(base.rglob("*")) if base.exists() else ():
            data = path.read_bytes() if path.is_file() else None
            state.append((path.relative_to(isolated.root).as_posix(), data))
    return state


def _success(result: CommandResult) -> None:
    assert result.returncode == 0, f"{result.command!r}\n{result.stdout}\n{result.stderr}"


@pytest.fixture(params=[False, True], ids=["project", "user"])
def scenario_factory(
    tmp_path: Path, request: pytest.FixtureRequest
) -> Callable[[ApmLifecycleRunner], _Scenario]:
    """Create isolated state; each test injects its canonical installed CLI."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "native", base_env=dict(os.environ))
    project = isolated.work_root / "consumer"
    project.mkdir()
    root = isolated.config_root if request.param else project
    dump_yaml(
        {
            "name": "native-consumer",
            "version": "1.0.0",
            "targets": ["claude", "codex"],
            "dependencies": {},
        },
        root / "apm.yml",
    )
    return lambda runner: _Scenario(isolated, project, root, request.param, runner)


@pytest.mark.parametrize("foreign_target", ["claude", "codex"])
@pytest.mark.parametrize("identical", [False, True], ids=["differing", "identical"])
def test_direct_collision_repeat_force_frozen_and_prune(
    scenario_factory: Callable[[ApmLifecycleRunner], _Scenario],
    apm_binary_path: Path,
    foreign_target: str,
    identical: bool,
) -> None:
    """Direct adds never adopt foreign matches or overwrite differing connections.

    An identical foreign entry needs no write, so install and --frozen both pass
    without claiming it; a differing one is refused by both.
    """
    scenario = scenario_factory(ApmLifecycleRunner((str(apm_binary_path),)))
    requested = _http()["url"]
    scenario.write(foreign_target, requested if identical else "http://127.0.0.1:9/personal")
    before = scenario.path(foreign_target).read_bytes()
    manifest_before = (scenario.root / "apm.yml").read_bytes()
    added = scenario.install("--mcp", "notes", "--url", requested)
    assert (added.returncode == 0) is identical, added.stdout + added.stderr
    other = "codex" if foreign_target == "claude" else "claude"
    if not identical:
        # A refused direct add writes nothing (#4759). Record the entry on the
        # other target alone, then the repeats below exercise the two-target
        # reconciliation against that recorded claim.
        assert (scenario.root / "apm.yml").read_bytes() == manifest_before
        assert not scenario.path(other).exists()
        assert scenario.path(foreign_target).read_bytes() == before
        scenario.declare([_http()])
        _success(scenario.install(targets=other))
    assert scenario.path(foreign_target).read_bytes() == before
    assert scenario.servers(other)["notes"]["url"] == requested
    assert scenario.lock().mcp_target_servers == {other: ["notes"]}
    lock_bytes = (scenario.root / "apm.lock.yaml").read_bytes()
    other_bytes = scenario.path(other).read_bytes()
    for flags in ((), ("--force",), ("--frozen",)):
        repeated = scenario.install(*flags)
        assert (repeated.returncode == 0) is identical, repeated.stdout + repeated.stderr
        if "--frozen" in flags and not identical:
            assert "ownership is not recorded" in " ".join(repeated.stdout.split())
        assert scenario.path(foreign_target).read_bytes() == before
        assert scenario.path(other).read_bytes() == other_bytes
        assert scenario.servers(other)["notes"]["url"] == requested
        assert scenario.lock().mcp_target_servers == {other: ["notes"]}
        assert (scenario.root / "apm.lock.yaml").read_bytes() == lock_bytes
    scenario.declare([])
    _success(scenario.install())
    assert scenario.path(foreign_target).read_bytes() == before
    assert scenario.servers(other) == {}
    assert scenario.lock().mcp_target_servers == {}


@pytest.mark.parametrize("modified_target", ["claude", "codex"])
def test_managed_connection_user_edits_block_replay_and_removal(
    scenario_factory: Callable[[ApmLifecycleRunner], _Scenario],
    apm_binary_path: Path,
    modified_target: str,
) -> None:
    """An old ownership name cannot authorize overwriting or deleting edited content."""
    scenario = scenario_factory(ApmLifecycleRunner((str(apm_binary_path),)))
    scenario.declare([_http()])
    _success(scenario.install(targets=modified_target))
    scenario.write(modified_target, "http://127.0.0.1:9/personal")
    before = scenario.path(modified_target).read_bytes()
    original_lock = (scenario.root / "apm.lock.yaml").read_bytes()
    for flags in ((), ("--force",), ("--frozen",)):
        result = scenario.install(*flags, targets=modified_target)
        assert result.returncode != 0, result.stdout + result.stderr
        assert scenario.path(modified_target).read_bytes() == before
        assert (scenario.root / "apm.lock.yaml").read_bytes() == original_lock
    scenario.declare([])
    removed = scenario.install(targets=modified_target)
    assert removed.returncode != 0, removed.stdout + removed.stderr
    assert scenario.path(modified_target).read_bytes() == before
    assert scenario.lock().mcp_target_servers == {modified_target: ["notes"]}


@pytest.mark.parametrize("foreign_target", ["claude", "codex"])
def test_historical_false_claim_preserves_foreign_connection(
    scenario_factory: Callable[[ApmLifecycleRunner], _Scenario],
    apm_binary_path: Path,
    foreign_target: str,
) -> None:
    """Historical ownership of both targets is not trusted over differing native bytes."""
    scenario = scenario_factory(ApmLifecycleRunner((str(apm_binary_path),)))
    other = "codex" if foreign_target == "claude" else "claude"
    scenario.declare([_http()])
    _success(scenario.install(targets=other))
    scenario.write(foreign_target, "http://127.0.0.1:9/personal")
    lock = scenario.lock()
    DeploymentLedgerCodec.replace_mcp_target_servers(
        lock, {"claude": ["notes"], "codex": ["notes"]}
    )
    lock.write(scenario.root / "apm.lock.yaml")
    before = scenario.path(foreign_target).read_bytes()
    scenario.declare([])
    result = scenario.install()
    assert result.returncode != 0, result.stdout + result.stderr
    assert scenario.path(foreign_target).read_bytes() == before
    assert "baseline" in (result.stdout + result.stderr).lower()
    assert "notes" in scenario.lock().mcp_target_servers[foreign_target]


def test_managed_package_root_override_and_target_narrowing(
    scenario_factory: Callable[[ApmLifecycleRunner], _Scenario], apm_binary_path: Path
) -> None:
    """A root override updates the unchanged old target and writes the new target."""
    scenario = scenario_factory(ApmLifecycleRunner((str(apm_binary_path),)))
    package = LocalPackageFactory(scenario.isolated.work_root / "packages").create(
        "notes-package", mcp_dependencies=(_http("http://127.0.0.1:8000/package"),)
    )
    _success(scenario.install(str(package.root), "--trust-transitive-mcp", targets="claude"))
    assert scenario.lock().mcp_target_servers == {"claude": ["notes"]}
    _success(scenario.install("--mcp", "notes", "--url", _http()["url"]))
    for target in ("claude", "codex"):
        assert scenario.servers(target)["notes"]["url"] == _http()["url"]
    assert scenario.lock().mcp_target_servers == {"claude": ["notes"], "codex": ["notes"]}
    before = tuple(scenario.path(target).read_bytes() for target in ("claude", "codex"))
    _success(scenario.install("--trust-transitive-mcp"))
    assert tuple(scenario.path(target).read_bytes() for target in ("claude", "codex")) == before
    # A narrowed --target leaves claude's still-declared entry alone (#4562).
    _success(scenario.install("--trust-transitive-mcp", targets="codex"))
    assert tuple(scenario.path(target).read_bytes() for target in ("claude", "codex")) == before
    assert scenario.lock().mcp_target_servers == {"claude": ["notes"], "codex": ["notes"]}


@pytest.mark.parametrize("target", ["claude", "codex"])
def test_package_uninstall_preserves_user_modified_connection(
    scenario_factory: Callable[[ApmLifecycleRunner], _Scenario],
    apm_binary_path: Path,
    target: str,
) -> None:
    """Package removal cannot delete a user's edited connection under an old claim."""
    scenario = scenario_factory(ApmLifecycleRunner((str(apm_binary_path),)))
    package = LocalPackageFactory(scenario.isolated.work_root / "packages").create(
        "notes-package", mcp_dependencies=(_http(),)
    )
    _success(scenario.install(str(package.root), "--trust-transitive-mcp", targets=target))
    scenario.write(target, "http://127.0.0.1:9/personal")
    before = scenario.path(target).read_bytes()
    result = scenario.run("uninstall", str(package.root))
    assert result.returncode != 0, result.stdout + result.stderr
    assert scenario.path(target).read_bytes() == before
    assert scenario.lock().mcp_target_servers == {target: ["notes"]}


@pytest.mark.parametrize("target", ["claude", "codex"])
def test_scoped_retirement_preserves_distinct_personal_short_name(
    scenario_factory: Callable[[ApmLifecycleRunner], _Scenario],
    apm_binary_path: Path,
    target: str,
) -> None:
    """Verification of a scoped key never authorizes deleting its short alias."""
    scenario = scenario_factory(ApmLifecycleRunner((str(apm_binary_path),)))
    scenario.declare([{**_http(), "name": "@company/notes"}])
    _success(scenario.install(targets=target))
    personal = {"url": "http://127.0.0.1:9/personal"}
    if target == "claude":
        personal["type"] = "http"
    scenario.edit(target, "notes", personal)
    scenario.declare([])
    _success(scenario.install(targets=target))
    assert scenario.servers(target) == {"notes": personal}
    assert scenario.lock().mcp_target_servers == {}


@pytest.mark.parametrize("target", ["claude", "codex"])
def test_matching_foreign_revocation_is_not_target_retirement(
    scenario_factory: Callable[[ApmLifecycleRunner], _Scenario],
    apm_binary_path: Path,
    target: str,
) -> None:
    """An explicitly requested user edit is a successful unowned, byte-stable no-op."""
    scenario = scenario_factory(ApmLifecycleRunner((str(apm_binary_path),)))
    scenario.declare([_http()])
    _success(scenario.install(targets=target))
    personal = {**scenario.servers(target)["notes"], "url": "http://127.0.0.1:9/personal"}
    scenario.edit(target, "notes", personal)
    before = scenario.path(target).read_bytes()
    _success(
        scenario.install("--mcp", "notes", "--url", personal["url"], "--force", targets=target)
    )
    assert scenario.path(target).read_bytes() == before
    assert scenario.lock().mcp_target_servers == {}
    scenario.declare([])
    _success(scenario.install(targets=target))
    assert scenario.path(target).read_bytes() == before


@pytest.mark.parametrize("target", ["claude", "codex"])
def test_partial_install_retains_deferred_names_in_active_target(
    scenario_factory: Callable[[ApmLifecycleRunner], _Scenario],
    apm_binary_path: Path,
    target: str,
) -> None:
    """A user-edited recorded claim refuses before any write, then retires cleanly (#4741).

    The later refused retirement records the entry it wrote, so APM owns it
    and the final removal retires it (#4792).
    """
    scenario = scenario_factory(ApmLifecycleRunner((str(apm_binary_path),)))
    scenario.declare([{**_http(), "name": "old"}, _http()])
    _success(scenario.install(targets=target))
    personal = {**scenario.servers(target)["notes"], "url": "http://127.0.0.1:9/personal"}
    scenario.edit(target, "notes", personal)
    config_before = scenario.path(target).read_bytes()
    lock_before = (scenario.root / "apm.lock.yaml").read_bytes()
    scenario.declare([{**_http(), "name": "new"}, _http()])
    result = scenario.install(targets=target)
    assert result.returncode == 1, result.stdout + result.stderr
    assert scenario.path(target).read_bytes() == config_before
    assert (scenario.root / "apm.lock.yaml").read_bytes() == lock_before
    # The refusal kept the edited claim, so retiring it preserves the entry.
    scenario.declare([{**_http(), "name": "new"}])
    kept = scenario.install(targets=target)
    assert kept.returncode == 1, kept.stdout + kept.stderr
    kept_output = " ".join((kept.stdout + kept.stderr).split())
    assert "Preserved MCP connection(s) notes" in kept_output, kept_output
    assert set(scenario.servers(target)) == {"new", "notes", "old"}
    assert scenario.servers(target)["notes"] == personal
    # The refused retirement still records the entry it wrote (#4792).
    assert scenario.lock().mcp_target_servers == {target: ["new", "notes", "old"]}
    path = scenario.path(target)
    if target == "claude":
        document = json.loads(path.read_text(encoding="utf-8"))
        document["mcpServers"].pop("notes")
        path.write_text(json.dumps(document), encoding="utf-8")
    else:
        document = tomlkit.parse(path.read_text(encoding="utf-8"))
        document["mcp_servers"].pop("notes")
        path.write_text(tomlkit.dumps(document), encoding="utf-8")
    _success(scenario.install(targets=target))
    assert set(scenario.servers(target)) == {"new"}
    assert scenario.lock().mcp_target_servers == {target: ["new"]}
    scenario.declare([])
    _success(scenario.install(targets=target))
    assert scenario.servers(target) == {}
    assert scenario.lock().mcp_target_servers == {}


@pytest.mark.parametrize("target", ["claude", "codex"])
def test_a_refused_retirement_records_the_entry_it_wrote(
    scenario_factory: Callable[[ApmLifecycleRunner], _Scenario],
    apm_binary_path: Path,
    target: str,
) -> None:
    """A preserved edit stops retirement after a write; the lock still claims it (#4792).

    The install writes ``new``, then refuses to retire the user-edited ``notes``.
    The lockfile records ``new`` as APM's and keeps the ``notes`` claim it did
    not remove, so once the user resolves ``notes`` the next install converges
    and a later removal retires ``new`` too.
    """
    scenario = scenario_factory(ApmLifecycleRunner((str(apm_binary_path),)))
    scenario.declare([_http()])
    _success(scenario.install(targets=target))
    personal = {**scenario.servers(target)["notes"], "url": "http://127.0.0.1:9/personal"}
    scenario.edit(target, "notes", personal)
    scenario.declare([{**_http(), "name": "new"}])

    refused = scenario.install(targets=target)

    output = " ".join((refused.stdout + refused.stderr).split())
    assert refused.returncode == 1, output
    assert "Preserved MCP connection(s) notes" in output, output
    assert set(scenario.servers(target)) == {"new", "notes"}
    assert scenario.servers(target)["notes"] == personal
    assert scenario.lock().mcp_target_servers == {target: ["new", "notes"]}

    path = scenario.path(target)
    if target == "claude":
        document = json.loads(path.read_text(encoding="utf-8"))
        document["mcpServers"].pop("notes")
        path.write_text(json.dumps(document), encoding="utf-8")
    else:
        document = tomlkit.parse(path.read_text(encoding="utf-8"))
        document["mcp_servers"].pop("notes")
        path.write_text(tomlkit.dumps(document), encoding="utf-8")
    _success(scenario.install(targets=target))
    assert set(scenario.servers(target)) == {"new"}
    assert scenario.lock().mcp_target_servers == {target: ["new"]}

    scenario.declare([])
    _success(scenario.install(targets=target))
    assert scenario.servers(target) == {}
    assert scenario.lock().mcp_target_servers == {}


def test_a_refused_legacy_cleanup_keeps_the_runtime_it_did_not_clean(
    scenario_factory: Callable[[ApmLifecycleRunner], _Scenario],
    apm_binary_path: Path,
) -> None:
    """A lock without per-target ownership cleans claude, then codex's edit refuses (#4792).

    Recording ``new`` gives the lock ownership, which ends legacy cleanup, so
    the codex retirement that did not run stays claimed: once the user
    restores APM's entry, the next install retires it.
    """
    scenario = scenario_factory(ApmLifecycleRunner((str(apm_binary_path),)))
    if scenario.user_scope:
        pytest.skip("legacy cleanup is project scope only")
    scenario.declare([_http()])
    _success(scenario.install())
    lock_path = scenario.root / "apm.lock.yaml"
    legacy = load_yaml(lock_path)
    legacy.pop("mcp_target_servers", None)
    legacy.pop("deployment_ledger", None)
    dump_yaml(legacy, lock_path)
    written = scenario.servers("codex")["notes"]
    scenario.edit("codex", "notes", {**written, "url": "http://127.0.0.1:9/personal"})
    scenario.declare([{**_http(), "name": "new"}])

    refused = scenario.install()

    output = " ".join((refused.stdout + refused.stderr).split())
    assert refused.returncode == 1, output
    assert "Preserved MCP connection(s) notes" in output, output
    assert "notes" not in scenario.servers("claude")
    assert scenario.lock().mcp_target_servers["codex"] == ["new", "notes"]

    scenario.edit("codex", "notes", written)
    _success(scenario.install())
    assert set(scenario.servers("codex")) == {"new"}
    assert scenario.lock().mcp_target_servers == {"claude": ["new"], "codex": ["new"]}


def test_a_refused_deselection_keeps_the_claim_it_did_not_remove(
    scenario_factory: Callable[[ApmLifecycleRunner], _Scenario],
    apm_binary_path: Path,
) -> None:
    """``targets:`` drops claude, but its entry was edited: the claude claim stays (#4792).

    Once the user restores APM's entry, the next install retires it.
    """
    scenario = scenario_factory(ApmLifecycleRunner((str(apm_binary_path),)))
    scenario.declare([_http()])
    _success(scenario.install())
    assert scenario.lock().mcp_target_servers == {"claude": ["notes"], "codex": ["notes"]}
    written = scenario.servers("claude")["notes"]
    scenario.edit("claude", "notes", {**written, "url": "http://127.0.0.1:9/personal"})
    scenario.declare([{**_http(), "targets": ["codex"]}])

    refused = scenario.install()

    output = " ".join((refused.stdout + refused.stderr).split())
    assert refused.returncode == 1, output
    assert "Preserved MCP connection(s) notes" in output, output
    assert scenario.lock().mcp_target_servers == {"claude": ["notes"], "codex": ["notes"]}

    scenario.edit("claude", "notes", written)
    _success(scenario.install())
    assert "notes" not in scenario.servers("claude")
    assert "notes" in scenario.servers("codex")
    assert scenario.lock().mcp_target_servers == {"codex": ["notes"]}


def test_a_refused_deselection_keeps_its_baseline_across_a_declaration_change(
    scenario_factory: Callable[[ApmLifecycleRunner], _Scenario],
    apm_binary_path: Path,
) -> None:
    """The URL changes and ``targets:`` drops claude while claude holds the new URL (#4792).

    The refusal keeps the recorded baseline for the pending claude retirement,
    so an identical retry refuses again instead of deleting the user's entry.
    """
    scenario = scenario_factory(ApmLifecycleRunner((str(apm_binary_path),)))
    scenario.declare([_http()])
    _success(scenario.install())
    updated = "http://127.0.0.1:8000/updated"
    edited = {**scenario.servers("claude")["notes"], "url": updated}
    scenario.edit("claude", "notes", edited)
    scenario.declare([{**_http(updated), "targets": ["codex"]}])

    for _attempt in range(2):
        refused = scenario.install()
        output = " ".join((refused.stdout + refused.stderr).split())
        assert refused.returncode == 1, output
        assert "Preserved MCP connection(s) notes" in output, output
        assert scenario.servers("claude")["notes"] == edited
        assert scenario.lock().mcp_target_servers == {"claude": ["notes"], "codex": ["notes"]}
    assert scenario.servers("codex")["notes"]["url"] == updated


def test_a_refused_deselection_without_a_recorded_declaration_keeps_the_new_one(
    scenario_factory: Callable[[ApmLifecycleRunner], _Scenario],
    apm_binary_path: Path,
) -> None:
    """The lock claims ``notes`` on both runtimes but lost its declaration (#4792).

    A missing baseline cannot vouch for the refused claude retirement, so the
    unverifiable claude claim is dropped and the new declaration is recorded:
    codex's entry stays retirable and a later removal retires it.
    """
    scenario = scenario_factory(ApmLifecycleRunner((str(apm_binary_path),)))
    scenario.declare([_http()])
    _success(scenario.install())
    lock_path = scenario.root / "apm.lock.yaml"
    document = load_yaml(lock_path)
    document["mcp_configs"].pop("notes")
    dump_yaml(document, lock_path)
    edited = {**scenario.servers("claude")["notes"], "url": "http://127.0.0.1:9/personal"}
    scenario.edit("claude", "notes", edited)
    scenario.declare([{**_http(), "targets": ["codex"]}])

    refused = scenario.install()

    output = " ".join((refused.stdout + refused.stderr).split())
    assert refused.returncode == 1, output
    assert "Preserved MCP connection(s) notes" in output, output
    assert scenario.lock().mcp_target_servers == {"codex": ["notes"]}
    assert "notes" in scenario.lock().mcp_configs

    scenario.declare([])
    _success(scenario.install())
    assert "notes" not in scenario.servers("codex")
    assert scenario.servers("claude")["notes"] == edited
    assert scenario.lock().mcp_target_servers == {}


@pytest.mark.parametrize("targets", ["claude,codex", "codex"])
def test_a_refused_deselection_never_vouches_for_a_newly_targeted_runtime(
    scenario_factory: Callable[[ApmLifecycleRunner], _Scenario],
    apm_binary_path: Path,
    targets: str,
) -> None:
    """The URL changes and the server moves to codex for the first time (#4792).

    APM never wrote the old URL in codex, so the refused claude baseline must
    not vouch for it there: the edited claude entry stays (unowned) and a
    later user edit to codex is still refused, never overwritten. With
    ``--target codex`` claude is not active, and its claim is still dropped.
    """
    scenario = scenario_factory(ApmLifecycleRunner((str(apm_binary_path),)))
    scenario.declare([{**_http(), "targets": ["claude"]}])
    _success(scenario.install())
    assert scenario.lock().mcp_target_servers == {"claude": ["notes"]}
    original = scenario.servers("claude")["notes"]
    updated = "http://127.0.0.1:8000/updated"
    edited = {**original, "url": updated}
    scenario.edit("claude", "notes", edited)
    scenario.declare([{**_http(updated), "targets": ["codex"]}])

    refused = scenario.install(targets=targets)

    output = " ".join((refused.stdout + refused.stderr).split())
    assert refused.returncode == 1, output
    assert scenario.servers("claude")["notes"] == edited
    assert scenario.servers("codex")["notes"]["url"] == updated
    assert scenario.lock().mcp_target_servers == {"codex": ["notes"]}

    codex_edit = {**scenario.servers("codex")["notes"], "url": original["url"]}
    scenario.edit("codex", "notes", codex_edit)
    retried = scenario.install(targets=targets)
    assert retried.returncode == 1, retried.stdout + retried.stderr
    assert scenario.servers("codex")["notes"] == codex_edit
    assert scenario.servers("claude")["notes"] == edited


@pytest.mark.parametrize("cwd_mode", ["absent", "declared", "native-only"])
def test_claude_managed_env_removal_repeats_and_retires(
    scenario_factory: Callable[[ApmLifecycleRunner], _Scenario],
    apm_binary_path: Path,
    cwd_mode: str,
) -> None:
    """Omitted managed fields are removed, while runtime-owned OAuth state survives."""
    scenario = scenario_factory(ApmLifecycleRunner((str(apm_binary_path),)))
    dependency = {
        "name": "notes",
        "registry": False,
        "transport": "stdio",
        "command": "echo",
        "args": ["hello"],
    }
    old_dependency = {**dependency, "env": {"OLD_SETTING": "value"}}
    if cwd_mode == "declared":
        old_dependency["cwd"] = "/fixture-cwd"
    scenario.declare([old_dependency])
    _success(scenario.install(targets="claude"))
    scenario.edit(
        "claude",
        "notes",
        {
            **scenario.servers("claude")["notes"],
            "oauthAccount": {"id": "runtime-owned"},
            **({"cwd": "/fixture-cwd"} if cwd_mode == "native-only" else {}),
        },
    )
    scenario.declare([dependency])
    _success(scenario.install(targets="claude"))
    assert "env" not in scenario.servers("claude")["notes"]
    assert scenario.servers("claude")["notes"]["oauthAccount"] == {"id": "runtime-owned"}
    assert ("cwd" in scenario.servers("claude")["notes"]) is (cwd_mode == "native-only")
    before = scenario.path("claude").read_bytes()
    _success(scenario.install(targets="claude"))
    assert scenario.path("claude").read_bytes() == before
    scenario.declare([])
    retired = scenario.install(targets="claude")
    if cwd_mode == "native-only":
        assert retired.returncode == 1
        assert scenario.path("claude").read_bytes() == before
    else:
        _success(retired)
        assert scenario.servers("claude") == {}


def _registry(scenario: _Scenario) -> LocalMcpRegistry:
    """Serve one real registry response, allowing only that loopback port."""
    from urllib.parse import urlparse

    registry = LocalMcpRegistryFactory(scenario.isolated.work_root / "registry").start(
        {
            "name": "io.example/registered",
            "description": "Local lifecycle fixture",
            "version": "1.0.0",
            "remotes": [{"type": "streamable-http", "url": "http://127.0.0.1:8000/registered"}],
        }
    )
    scenario.environment_overrides = {
        "APM_TEST_LOOPBACK_PORTS": str(urlparse(registry.url).port),
        "MCP_REGISTRY_ALLOW_HTTP": "1",
    }
    return registry


@pytest.mark.parametrize("target", ["claude", "codex"])
def test_fresh_registry_write_can_be_retired_without_new_lookup(
    scenario_factory: Callable[[ApmLifecycleRunner], _Scenario],
    apm_binary_path: Path,
    target: str,
) -> None:
    """The actual write, not a later registry response, proves removable native content."""
    scenario = scenario_factory(ApmLifecycleRunner((str(apm_binary_path),)))
    with _registry(scenario) as registry:
        scenario.declare([{"name": "io.example/registered", "registry": registry.url}])
        _success(scenario.install(targets=target))
        assert registry.request_paths
        receipts = DeploymentLedgerCodec.mcp_native_hashes(scenario.lock())
        assert set(receipts[target]) == {"io.example/registered"}
        assert receipts[target]["io.example/registered"].startswith("sha256:")
        _success(scenario.install(targets=target))
        assert DeploymentLedgerCodec.mcp_native_hashes(scenario.lock()) == receipts
    _success(scenario.install("--only", "apm", targets=target))
    assert DeploymentLedgerCodec.mcp_native_hashes(scenario.lock()) == receipts
    assert scenario.lock().mcp_target_servers == {target: ["io.example/registered"]}
    scenario.declare([])
    _success(scenario.install(targets=target))
    assert scenario.servers(target) == {}
    assert scenario.lock().mcp_target_servers == {}


@pytest.mark.parametrize("target", ["claude", "codex"])
def test_registry_receipt_survives_package_lock_rebuild(
    scenario_factory: Callable[[ApmLifecycleRunner], _Scenario], apm_binary_path: Path, target: str
) -> None:
    """An actual APM dependency exercises the full lockfile-builder path."""
    from apm_cli.core.deployment_ledger import DeploymentLedgerCodec

    scenario = scenario_factory(ApmLifecycleRunner((str(apm_binary_path),)))
    package = LocalPackageFactory(scenario.isolated.work_root / "packages").create("receipt-owner")
    with _registry(scenario) as registry:
        scenario.declare([{"name": "io.example/registered", "registry": registry.url}])
        _success(scenario.install(str(package.root), targets=target))
        assert scenario.lock().dependencies
        receipts = DeploymentLedgerCodec.mcp_native_hashes(scenario.lock())
        assert receipts[target]["io.example/registered"].startswith("sha256:")
        native_before = scenario.path(target).read_bytes()
        for flags in (("--only", "apm"), ()):
            _success(scenario.install(*flags, targets=target))
            assert DeploymentLedgerCodec.mcp_native_hashes(scenario.lock()) == receipts
            assert scenario.lock().mcp_target_servers == {target: ["io.example/registered"]}
            assert scenario.path(target).read_bytes() == native_before
    scenario.declare([])
    _success(scenario.install(targets=target))
    assert scenario.servers(target) == {}
    assert scenario.lock().dependencies


def test_a_refused_deselection_never_keeps_a_baseline_with_another_native_key(
    scenario_factory: Callable[[ApmLifecycleRunner], _Scenario],
    apm_binary_path: Path,
) -> None:
    """A registry server turns self-defined for codex only; claude's edit refuses (#4899).

    The registry baseline keys the entry ``registered`` while codex now writes
    ``io.example/registered``, so the refused claude baseline is not kept: the
    lock records the new declaration and a later removal retires codex's entry.
    """
    scenario = scenario_factory(ApmLifecycleRunner((str(apm_binary_path),)))
    with _registry(scenario) as registry:
        scenario.declare([{"name": "io.example/registered", "registry": registry.url}])
        _success(scenario.install())
    assert set(scenario.servers("codex")) == {"registered"}
    edited = {**scenario.servers("claude")["registered"], "url": "http://127.0.0.1:9/personal"}
    scenario.edit("claude", "registered", edited)
    document = tomlkit.parse(scenario.path("codex").read_text(encoding="utf-8"))
    document["mcp_servers"].pop("registered")
    scenario.path("codex").write_text(tomlkit.dumps(document), encoding="utf-8")
    scenario.declare(
        [
            {
                **_http("http://127.0.0.1:8000/registered"),
                "name": "io.example/registered",
                "targets": ["codex"],
            }
        ]
    )

    refused = scenario.install()

    output = " ".join((refused.stdout + refused.stderr).split())
    assert refused.returncode == 1, output
    assert "Preserved MCP connection(s)" in output, output
    assert set(scenario.servers("codex")) == {"io.example/registered"}
    assert scenario.lock().mcp_target_servers == {"codex": ["io.example/registered"]}
    assert scenario.lock().mcp_configs["io.example/registered"].get("registry") is False

    scenario.declare([])
    _success(scenario.install())
    assert scenario.servers("codex") == {}
    assert scenario.servers("claude")["registered"] == edited
    assert scenario.lock().mcp_target_servers == {}


def test_a_skipped_runtime_baseline_never_masks_a_native_key_change(
    scenario_factory: Callable[[ApmLifecycleRunner], _Scenario],
    apm_binary_path: Path,
) -> None:
    """Cursor is skipped but keeps its claim while codex writes a new native key (#4949).

    The skipped runtime keeps the old registry declaration as its drift
    baseline, which must not decide the refused claude retirement: the lock
    records the declaration codex actually wrote, so a later removal retires
    codex's ``io.example/registered`` entry instead of orphaning it.
    """
    scenario = scenario_factory(ApmLifecycleRunner((str(apm_binary_path),)))
    if scenario.user_scope:
        pytest.skip("the cursor project destination is the subject")
    manifest = load_yaml(scenario.root / "apm.yml")
    manifest["targets"] = ["claude", "codex", "cursor"]
    dump_yaml(manifest, scenario.root / "apm.yml")
    (scenario.project / ".cursor").mkdir()
    every = "claude,codex,cursor"
    with _registry(scenario) as registry:
        scenario.declare([{"name": "io.example/registered", "registry": registry.url}])
        _success(scenario.install(targets=every))
    assert set(scenario.servers("codex")) == {"registered"}
    edited = {**scenario.servers("claude")["registered"], "url": "http://127.0.0.1:9/personal"}
    scenario.edit("claude", "registered", edited)
    document = tomlkit.parse(scenario.path("codex").read_text(encoding="utf-8"))
    document["mcp_servers"].pop("registered")
    scenario.path("codex").write_text(tomlkit.dumps(document), encoding="utf-8")
    scenario.declare(
        [
            {
                **_http("http://127.0.0.1:8000/registered"),
                "name": "io.example/registered",
                "targets": ["codex", "cursor"],
            }
        ]
    )

    refused = scenario.install()

    output = " ".join((refused.stdout + refused.stderr).split())
    assert refused.returncode == 1, output
    assert "Preserved MCP connection(s)" in output, output
    assert set(scenario.servers("codex")) == {"io.example/registered"}
    assert "codex" in scenario.lock().mcp_target_servers
    assert scenario.lock().mcp_configs["io.example/registered"].get("registry") is False

    scenario.declare([])
    _success(scenario.install(targets=every))
    assert scenario.servers("codex") == {}
    assert scenario.servers("claude")["registered"] == edited
    assert "codex" not in scenario.lock().mcp_target_servers


def test_a_skipped_runtime_claim_still_guards_a_shared_file_entry_a_peer_retires(
    scenario_factory: Callable[[ApmLifecycleRunner], _Scenario],
    apm_binary_path: Path,
) -> None:
    """Copilot is skipped but still claims the .mcp.json entry claude retires (#4949)."""
    scenario = scenario_factory(ApmLifecycleRunner((str(apm_binary_path),)))
    if scenario.user_scope:
        pytest.skip("the shared project .mcp.json is the subject")
    manifest = load_yaml(scenario.root / "apm.yml")
    manifest["targets"] = ["claude", "codex", "copilot"]
    dump_yaml(manifest, scenario.root / "apm.yml")
    every = "claude,codex,copilot"
    with _registry(scenario) as registry:
        scenario.declare([{"name": "io.example/registered", "registry": registry.url}])
        _success(scenario.install(targets=every))
    assert set(scenario.servers("claude")) == {"registered"}
    scenario.declare(
        [
            {
                **_http("http://127.0.0.1:8000/registered"),
                "name": "io.example/registered",
                "targets": ["codex", "copilot"],
            }
        ]
    )

    result = scenario.install()

    assert result.returncode == 0, result.stdout + result.stderr
    assert set(scenario.servers("claude")) == {"registered"}
    assert "registered" in scenario.path("claude").read_text(encoding="utf-8")


@pytest.mark.parametrize("foreign_target", ["claude", "codex"])
def test_registry_partial_conflict_does_not_skip_managed_self_defined_update(
    scenario_factory: Callable[[ApmLifecycleRunner], _Scenario],
    apm_binary_path: Path,
    foreign_target: str,
) -> None:
    """A registry collision refuses before any write, so no baseline is published (#4741)."""
    scenario = scenario_factory(ApmLifecycleRunner((str(apm_binary_path),)))
    scenario.declare([_http()])
    _success(scenario.install())
    personal = {"url": "http://127.0.0.1:9/personal"}
    if foreign_target == "claude":
        personal["type"] = "http"
    scenario.edit(foreign_target, "registered", personal)
    updated = _http("http://127.0.0.1:8000/updated")
    with _registry(scenario) as registry:
        scenario.declare([{"name": "io.example/registered", "registry": registry.url}, updated])
        configs_before = {
            target: scenario.path(target).read_bytes() for target in ("claude", "codex")
        }
        lock_before = (scenario.root / "apm.lock.yaml").read_bytes()
        refused = scenario.install()
        assert refused.returncode == 1, refused.stdout + refused.stderr
        for target in ("claude", "codex"):
            assert scenario.path(target).read_bytes() == configs_before[target]
        assert (scenario.root / "apm.lock.yaml").read_bytes() == lock_before
        scenario.edit(
            foreign_target,
            "registered",
            {**personal, "url": "http://127.0.0.1:8000/registered"},
        )
        _success(scenario.install())
        assert scenario.lock().mcp_target_servers[foreign_target] == ["notes"]
        for target in ("claude", "codex"):
            assert urlparse(scenario.servers(target)["notes"]["url"]) == urlparse(updated["url"])


@pytest.mark.parametrize("target", ["claude", "codex"])
def test_metadata_only_replacement_write_retains_registry_receipt(
    scenario_factory: Callable[[ApmLifecycleRunner], _Scenario], apm_binary_path: Path, target: str
) -> None:
    """Compose replacement-only lock persistence with actual MCP write ownership."""
    scenario = scenario_factory(ApmLifecycleRunner((str(apm_binary_path),)))
    with _registry(scenario) as registry:
        scenario.declare([{"name": "io.example/registered", "registry": registry.url}])
        _success(scenario.install(targets=target))
    receipts = DeploymentLedgerCodec.mcp_native_hashes(scenario.lock())
    assert receipts[target]["io.example/registered"].startswith("sha256:")
    native_before = scenario.path(target).read_bytes()
    manifest = load_yaml(scenario.root / "apm.yml")
    manifest["$schema"] = (
        "https://microsoft.github.io/apm/specs/schemas/manifest-source-replacements-v1.schema.json"
    )
    manifest["replace"] = []
    dump_yaml(manifest, scenario.root / "apm.yml")
    _success(scenario.install("--only", "apm", targets=target))
    assert scenario.lock().replacements == []
    assert not scenario.lock().dependencies
    assert DeploymentLedgerCodec.mcp_native_hashes(scenario.lock()) == receipts
    assert scenario.path(target).read_bytes() == native_before
    scenario.declare([])
    _success(scenario.install(targets=target))
    assert scenario.servers(target) == {}


@pytest.mark.parametrize("target", ["claude", "codex"])
def test_a_self_defined_conflict_is_refused_before_the_registry_write(
    scenario_factory: Callable[[ApmLifecycleRunner], _Scenario],
    apm_binary_path: Path,
    target: str,
) -> None:
    """An unrecorded self-defined collision refuses before any MCP write (#4268)."""
    scenario = scenario_factory(ApmLifecycleRunner((str(apm_binary_path),)))
    scenario.write(target, "http://127.0.0.1:9/personal")
    personal = scenario.servers(target)["notes"]
    with _registry(scenario) as registry:
        scenario.declare([{"name": "io.example/registered", "registry": registry.url}, _http()])
        result = scenario.install(targets=target)
        assert result.returncode == 1, result.stdout + result.stderr
    assert scenario.servers(target) == {"notes": personal}
    lock_path = scenario.root / "apm.lock.yaml"
    assert not lock_path.exists() or not scenario.lock().mcp_target_servers
    scenario.declare([])
    _success(scenario.install(targets=target))
    assert scenario.servers(target) == {"notes": personal}


@pytest.mark.parametrize("target", ["claude", "codex"])
def test_a_colliding_registry_server_is_refused_before_any_write(
    scenario_factory: Callable[[ApmLifecycleRunner], _Scenario],
    apm_binary_path: Path,
    target: str,
) -> None:
    """A registry server is projected before any write; recorded, it installs again (#4741)."""
    scenario = scenario_factory(ApmLifecycleRunner((str(apm_binary_path),)))
    personal_url = "http://127.0.0.1:9/personal"
    scenario.write(target, personal_url)
    personal = (
        {"type": "http", "url": personal_url} if target == "claude" else {"url": personal_url}
    )
    scenario.edit(target, "registered", personal)
    before = scenario.path(target).read_bytes()
    other = scenario.path("codex" if target == "claude" else "claude")
    with _registry(scenario) as registry:
        scenario.declare([{"name": "io.example/registered", "registry": registry.url}])
        refused = scenario.install()
        output = " ".join((refused.stdout + refused.stderr).split())
        assert refused.returncode == 1, output
        assert f"conflicts with the existing {target} connection" in output, output
        assert scenario.path(target).read_bytes() == before, output
        assert not other.exists() or "registered" not in other.read_text("utf-8"), output
        lock_path = scenario.root / "apm.lock.yaml"
        assert not lock_path.exists() or not scenario.lock().mcp_target_servers, output

        data = scenario.path(target).read_text(encoding="utf-8")
        if target == "claude":
            document = json.loads(data)
            document["mcpServers"].pop("registered")
            scenario.path(target).write_text(json.dumps(document), encoding="utf-8")
        else:
            document = tomlkit.parse(data)
            document["mcp_servers"].pop("registered")
            scenario.path(target).write_text(tomlkit.dumps(document), encoding="utf-8")
        _success(scenario.install())
        _success(scenario.install())
    assert "registered" in scenario.servers(target)
    assert sorted(scenario.lock().mcp_target_servers) == ["claude", "codex"]


@pytest.mark.parametrize("target", ["claude", "codex"])
@pytest.mark.parametrize("evidence", ["edited", "hashless", "deleted-lock"])
def test_registry_receipt_never_adopts_or_deletes_unverified_native_state(
    scenario_factory: Callable[[ApmLifecycleRunner], _Scenario],
    apm_binary_path: Path,
    target: str,
    evidence: str,
) -> None:
    """Names, registry lookups, and missing locks cannot substitute for an actual-write receipt."""
    scenario = scenario_factory(ApmLifecycleRunner((str(apm_binary_path),)))
    with _registry(scenario) as registry:
        scenario.declare([{"name": "io.example/registered", "registry": registry.url}])
        _success(scenario.install(targets=target))
        if evidence == "edited":
            scenario.edit(
                target,
                "registered",
                {
                    **scenario.servers(target)["registered"],
                    "url": "http://127.0.0.1:9/personal",
                },
            )
        elif evidence == "hashless":
            lock = scenario.lock()
            DeploymentLedgerCodec.replace_mcp_target_servers(
                lock, lock.mcp_target_servers, native_hashes={}
            )
            lock.write(scenario.root / "apm.lock.yaml")
            scenario.declare([])
            refused = scenario.install(targets=target)
            assert refused.returncode == 1, refused.stdout + refused.stderr
            scenario.declare([{"name": "io.example/registered", "registry": registry.url}])
        else:
            (scenario.root / "apm.lock.yaml").unlink()
        before = scenario.path(target).read_bytes()
        repeated = scenario.install(targets=target)
        assert repeated.returncode == (1 if evidence == "edited" else 0)
        assert scenario.path(target).read_bytes() == before
        if evidence != "edited":
            assert DeploymentLedgerCodec.mcp_native_hashes(scenario.lock()) == {}
    scenario.declare([])
    result = scenario.install(targets=target)
    assert result.returncode == (1 if evidence == "edited" else 0)
    assert scenario.path(target).read_bytes() == before


def test_user_disabled_matching_codex_connection_installs_and_replays_frozen(
    scenario_factory: Callable[[ApmLifecycleRunner], _Scenario],
    apm_binary_path: Path,
) -> None:
    """A user's own entry equal to the declaration, only switched off, is no conflict."""
    scenario = scenario_factory(ApmLifecycleRunner((str(apm_binary_path),)))
    declared = {"name": "notes", "registry": False, "transport": "stdio", "command": "INERT"}
    scenario.declare([declared])
    manifest = load_yaml(scenario.root / "apm.yml")
    manifest["executables"] = {"allow": {"notes": {"mcp": True}}}
    dump_yaml(manifest, scenario.root / "apm.yml")
    path = scenario.path("codex")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '# user comment\n[mcp_servers.notes]\ncommand = "INERT"\nenabled = false\n',
        encoding="utf-8",
    )
    before = path.read_bytes()

    # Zero bytes of the user's entry change, and replay and audit agree it is fine.
    _success(scenario.install(targets="codex"))
    assert path.read_bytes() == before
    assert scenario.lock().mcp_target_servers == {}
    lock_before = (scenario.root / "apm.lock.yaml").read_bytes()
    _success(scenario.install("--frozen", targets="codex"))
    _success(scenario.audit())
    assert path.read_bytes() == before
    assert (scenario.root / "apm.lock.yaml").read_bytes() == lock_before

    # A different command stays a conflict on install and on replay.
    scenario.edit("codex", "notes", {"command": "OTHER", "enabled": False})
    edited = path.read_bytes()
    for flags in ((), ("--frozen",)):
        refused = scenario.install(*flags, targets="codex")
        assert refused.returncode == 1, refused.stdout + refused.stderr
        assert path.read_bytes() == edited
    # The CI audit reaches the frozen verdict for the same entry and names it (#4477),
    # and comparing it writes nothing under APM home or the APM cache, even on a
    # CI runner whose APM cache starts empty.
    shutil.rmtree(scenario.isolated.cache_root)
    scenario.isolated.cache_root.mkdir()
    apm_state = _apm_state(scenario.isolated)
    audited = scenario.audit()
    assert audited.returncode == 1, audited.stdout + audited.stderr
    assert "MCP server 'notes' target 'codex'" in audited.stdout + audited.stderr
    assert path.read_bytes() == edited
    assert _apm_state(scenario.isolated) == apm_state
