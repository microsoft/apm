"""Installed-CLI contract: Copilot project MCP in root .mcp.json; `vscode` an alias.

#3866: `vscode` is a deprecated alias that deploys exactly like `copilot`; the
`copilot` target writes project MCP servers to workspace-root `.mcp.json`,
shared with Claude Code; APM-managed entries are healed out of the retired
`.github/mcp.json` and `.vscode/mcp.json`; a 0.36 lockfile that records
`vscode` replays under `--frozen`.

#3868: `apm manifest migrate` is gone, and the singular `target:` warning shows
the `targets:` value that deploys exactly the same files.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment

pytestmark = [pytest.mark.e2e, pytest.mark.integration, pytest.mark.requires_apm_binary]

_ECHO = {"name": "echo", "registry": False, "transport": "stdio", "command": "echo"}
_USER_ROOT = {"type": "stdio", "command": "user-root-server"}
_USER_GITHUB = {"type": "local", "command": "user-github-server"}
_USER_VSCODE = {"type": "stdio", "command": "user-vscode-server"}


class _Project:
    def __init__(self, tmp_path: Path, apm_binary_path: Path, name: str = "app") -> None:
        isolated = IsolatedApmEnvironment.create(tmp_path / name, base_env=dict(os.environ))
        self.env = {**isolated.subprocess_env(), "COLUMNS": "400"}
        self.root = isolated.work_root / name
        self.root.mkdir(parents=True)
        self.runner = ApmLifecycleRunner((str(apm_binary_path),))

    def manifest(self, **fields: object) -> None:
        dump_yaml({"name": "app", "version": "1.0.0", **fields}, self.root / "apm.yml")

    def write_json(self, relative: str, payload: object) -> None:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    def read_json(self, relative: str) -> dict:
        return json.loads((self.root / relative).read_text(encoding="utf-8"))

    def apm(self, *args: str) -> CommandResult:
        return self.runner.run(args, cwd=self.root, env=self.env)

    def install(self, *args: str) -> str:
        result = self.apm("install", "--no-policy", *args)
        output = result.stdout + result.stderr
        assert result.returncode == 0, output
        return output

    def lock(self) -> dict:
        return load_yaml(self.root / "apm.lock.yaml")

    def deployed_files(self) -> set[str]:
        skipped = {"apm.yml", "apm.lock.yaml"}
        return {
            path.relative_to(self.root).as_posix()
            for path in self.root.rglob("*")
            if path.is_file()
            and path.name not in skipped
            and not path.relative_to(self.root).as_posix().startswith(".apm/")
        }


def _flat(text: str) -> str:
    return " ".join(text.split())


def test_copilot_writes_root_mcp_json_and_leaves_user_entries(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    project = _Project(tmp_path, apm_binary_path)
    project.manifest(targets=["copilot"], dependencies={"mcp": [_ECHO]})
    project.write_json(".mcp.json", {"mcpServers": {"mine": _USER_ROOT}})
    project.write_json(".github/mcp.json", {"mcpServers": {"gh-mine": _USER_GITHUB}})
    project.write_json(".vscode/mcp.json", {"servers": {"vs-mine": _USER_VSCODE}, "inputs": []})
    github_before = (project.root / ".github/mcp.json").read_bytes()
    vscode_before = (project.root / ".vscode/mcp.json").read_bytes()

    project.install()

    assert project.read_json(".mcp.json")["mcpServers"] == {
        "mine": _USER_ROOT,
        "echo": {"type": "stdio", "command": "echo", "args": []},
    }
    assert (project.root / ".github/mcp.json").read_bytes() == github_before
    assert (project.root / ".vscode/mcp.json").read_bytes() == vscode_before
    assert project.lock()["mcp_target_servers"] == {"copilot": ["echo"]}

    project.manifest(targets=["copilot"])
    project.install()
    assert project.read_json(".mcp.json")["mcpServers"] == {"mine": _USER_ROOT}
    assert (project.root / ".github/mcp.json").read_bytes() == github_before


def _seed_036_project(project: _Project) -> None:
    """A project as APM 0.36 left it: copilot in .github/mcp.json, vscode in .vscode/mcp.json."""
    servers = [
        {**_ECHO, "name": "gh-managed"},
        {**_ECHO, "name": "gh-edited"},
        {**_ECHO, "name": "vs-managed"},
    ]
    project.manifest(targets=["copilot"], dependencies={"mcp": servers})
    legacy_copilot = {"type": "local", "tools": ["*"], "id": "", "command": "echo", "args": []}
    project.write_json(
        ".github/mcp.json",
        {
            "mcpServers": {
                "gh-mine": _USER_GITHUB,
                "gh-managed": legacy_copilot,
                "gh-edited": {**legacy_copilot, "args": ["--edited-by-user"]},
            }
        },
    )
    project.write_json(
        ".vscode/mcp.json",
        {"servers": {"vs-managed": {"type": "stdio", "command": "echo", "args": []}}, "inputs": []},
    )
    configs = {server["name"]: server for server in servers}
    dump_yaml(
        {
            "lockfile_version": "1",
            "apm_version": "0.36.1",
            "dependencies": [],
            "mcp_servers": sorted(configs),
            "mcp_configs": configs,
            "mcp_target_servers": {
                "copilot": ["gh-edited", "gh-managed"],
                "vscode": ["vs-managed"],
            },
        },
        project.root / "apm.lock.yaml",
    )


@pytest.mark.parametrize("frozen", [False, True], ids=["install", "frozen-replay"])
def test_upgrade_heals_only_apm_entries_out_of_retired_files(
    tmp_path: Path, apm_binary_path: Path, frozen: bool
) -> None:
    project = _Project(tmp_path, apm_binary_path)
    _seed_036_project(project)

    output = _flat(project.install(*(["--frozen"] if frozen else [])))

    assert "Removed APM-managed MCP server 'gh-managed' from .github/mcp.json" in output
    assert "Removed APM-managed MCP server 'vs-managed' from .vscode/mcp.json" in output
    assert "Removed empty .vscode/mcp.json" in output
    assert "Kept MCP server 'gh-edited' in .github/mcp.json" in output
    github = project.read_json(".github/mcp.json")["mcpServers"]
    assert set(github) == {"gh-mine", "gh-edited"}
    assert github["gh-mine"] == _USER_GITHUB
    # APM created .vscode/mcp.json and nothing of the user's remained in it.
    assert not (project.root / ".vscode/mcp.json").exists()
    root = project.read_json(".mcp.json")["mcpServers"]
    assert set(root) == {"gh-managed", "gh-edited", "vs-managed"}
    assert all(entry["type"] == "stdio" for entry in root.values())

    rerun = project.apm("install", "--no-policy", "--frozen")
    assert rerun.returncode == 0, rerun.stdout + rerun.stderr
    assert set(project.read_json(".mcp.json")["mcpServers"]) == set(root)


def test_claude_and_copilot_share_one_mcp_json_entry(tmp_path: Path, apm_binary_path: Path) -> None:
    project = _Project(tmp_path, apm_binary_path)
    (project.root / ".claude").mkdir()
    project.write_json(".mcp.json", {"mcpServers": {"mine": _USER_ROOT}})
    project.manifest(targets=["claude", "copilot"], dependencies={"mcp": [_ECHO]})

    project.install()
    entry = {"type": "stdio", "command": "echo", "args": []}
    assert project.read_json(".mcp.json")["mcpServers"] == {"mine": _USER_ROOT, "echo": entry}
    assert project.lock()["mcp_target_servers"] == {"claude": ["echo"], "copilot": ["echo"]}
    replay = project.apm("install", "--no-policy", "--frozen")
    assert replay.returncode == 0, replay.stdout + replay.stderr

    # Retiring one runtime never removes an entry the other still owns.
    for remaining in (["copilot"], ["claude"]):
        project.manifest(targets=remaining, dependencies={"mcp": [_ECHO]})
        project.install()
        assert project.read_json(".mcp.json")["mcpServers"] == {
            "mine": _USER_ROOT,
            "echo": entry,
        }
        assert project.lock()["mcp_target_servers"] == {remaining[0]: ["echo"]}

    project.manifest(targets=["claude", "copilot"])
    project.install()
    assert project.read_json(".mcp.json")["mcpServers"] == {"mine": _USER_ROOT}


@pytest.mark.parametrize(
    ("fields", "args", "warning"),
    [
        ({"targets": ["vscode"]}, (), "apm.yml: replace `vscode` with `copilot` in 'targets:'"),
        ({"target": "vscode"}, (), "replace `vscode` with `copilot`"),
        ({}, ("--target", "vscode"), "replace `vscode` with `copilot`"),
        ({}, ("--runtime", "vscode"), "replace `vscode` with `copilot`"),
    ],
    ids=["targets", "target", "flag-target", "flag-runtime"],
)
def test_vscode_alias_deploys_exactly_like_copilot(
    tmp_path: Path,
    apm_binary_path: Path,
    fields: dict,
    args: tuple[str, ...],
    warning: str,
) -> None:
    results = {}
    for label, declared, flags in (
        ("vscode", fields, args),
        ("copilot", {"targets": ["copilot"]}, ()),
    ):
        project = _Project(tmp_path / label, apm_binary_path)
        project.manifest(**declared, dependencies={"mcp": [_ECHO]})
        output = _flat(project.install(*flags))
        if label == "vscode":
            assert output.count(warning) == 1, output
            assert "will be removed in a future release" in output
            assert not re.search(r"will be removed in v\d", output)
        assert not (project.root / ".vscode").exists()
        assert "vscode" not in (project.root / "apm.lock.yaml").read_text(encoding="utf-8")
        results[label] = (
            project.read_json(".mcp.json"),
            project.lock()["mcp_target_servers"],
            project.deployed_files(),
        )

    assert results["vscode"] == results["copilot"]


def test_dependency_manifest_vscode_installs_for_copilot_without_warning(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    project = _Project(tmp_path, apm_binary_path)
    dep = project.root / "dep"
    (dep / ".apm/instructions").mkdir(parents=True)
    dump_yaml({"name": "legacy-dep", "version": "1.0.0", "targets": ["vscode"]}, dep / "apm.yml")
    (dep / ".apm/instructions/legacy.instructions.md").write_text(
        "---\napplyTo: '**'\n---\nLegacy guidance.\n", encoding="utf-8"
    )
    (project.root / ".claude").mkdir()
    project.manifest(targets=["claude", "copilot"], dependencies={"apm": [{"path": "./dep"}]})

    output = _flat(project.install())

    assert "vscode" not in output
    deployed = project.deployed_files()
    assert any(path.startswith(".github/") and "legacy" in path for path in deployed)
    assert not any(path.startswith(".claude/") and "legacy" in path for path in deployed)


def _suggested_targets(output: str) -> list[str]:
    match = re.search(r"with 'targets: \[([^\]]*)\]'", _flat(output))
    assert match, output
    return [name.strip() for name in match.group(1).split(",")]


@pytest.mark.parametrize(
    ("legacy", "suggestion"),
    [
        ("claude,copilot", ["claude", "copilot"]),
        (["agents", "claude"], ["copilot", "claude"]),
        ("vscode", ["copilot"]),
        (
            "all",
            [
                "claude",
                "codex",
                "copilot",
                "cursor",
                "gemini",
                "grok-build",
                "kiro",
                "opencode",
                "windsurf",
            ],
        ),
    ],
    ids=["csv", "list-with-alias", "vscode-alias", "legacy-all"],
)
def test_target_warning_suggests_a_value_that_deploys_identically(
    tmp_path: Path, apm_binary_path: Path, legacy: object, suggestion: list[str]
) -> None:
    deployments = {}
    for form in ("legacy", "suggested"):
        project = _Project(tmp_path / form, apm_binary_path)
        (project.root / ".claude").mkdir()
        (project.root / ".apm/instructions").mkdir(parents=True)
        (project.root / ".apm/instructions/style.instructions.md").write_text(
            "---\napplyTo: '**'\n---\nUse the house style.\n", encoding="utf-8"
        )
        targets = {"target": legacy} if form == "legacy" else {"targets": suggestion}
        project.manifest(**targets, dependencies={"mcp": [_ECHO]})
        output = project.install()
        if form == "legacy":
            flat = _flat(output)
            assert "uses the deprecated 'target:' key" in flat
            assert "'target:' will be removed in a future release" in flat
            assert not re.search(r"'target:' will be removed in v\d", flat)
            assert _suggested_targets(output) == suggestion
        deployments[form] = (project.deployed_files(), project.lock()["mcp_target_servers"])

    assert deployments["legacy"] == deployments["suggested"]


def test_plural_all_is_not_a_replacement_for_singular_all(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """Evidence for #3868: `targets: [all]` auto-detects, `target: all` deploys every target."""
    deployed = {}
    for label, fields in (("singular", {"target": "all"}), ("plural", {"targets": ["all"]})):
        project = _Project(tmp_path / label, apm_binary_path)
        (project.root / ".claude").mkdir()
        (project.root / ".apm/instructions").mkdir(parents=True)
        (project.root / ".apm/instructions/style.instructions.md").write_text(
            "---\napplyTo: '**'\n---\nUse the house style.\n", encoding="utf-8"
        )
        project.manifest(**fields)
        project.install()
        deployed[label] = {path.split("/", 1)[0] for path in project.deployed_files()}

    assert deployed["plural"] < deployed["singular"]
    assert ".github" in deployed["singular"]
    assert ".github" not in deployed["plural"]


def test_manifest_migrate_command_no_longer_exists(tmp_path: Path, apm_binary_path: Path) -> None:
    project = _Project(tmp_path, apm_binary_path)
    project.manifest(target="claude")

    result = project.apm("manifest", "migrate")

    assert result.returncode != 0
    assert "No such command" in result.stdout + result.stderr


def _seed_036_copilot_only(project: _Project, *, targets: list[str] | None) -> None:
    """A copilot-only 0.36 project: APM's entry and a user entry in .github/mcp.json."""
    fields = {"targets": targets} if targets is not None else {}
    project.manifest(**fields, dependencies={"mcp": [_ECHO]})
    legacy = {"type": "local", "tools": ["*"], "id": "", "command": "echo", "args": []}
    project.write_json(".github/mcp.json", {"mcpServers": {"echo": legacy}})
    dump_yaml(
        {
            "lockfile_version": "1",
            "apm_version": "0.36.1",
            "dependencies": [],
            "mcp_servers": ["echo"],
            "mcp_configs": {"echo": _ECHO},
            "mcp_target_servers": {"copilot": ["echo"]},
        },
        project.root / "apm.lock.yaml",
    )


def test_heal_runs_once_and_keeps_an_autodetected_project_installable(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """The heal settles in one install; .github/mcp.json stays as the copilot signal."""
    project = _Project(tmp_path, apm_binary_path)
    _seed_036_copilot_only(project, targets=None)

    first = _flat(project.install())
    assert "Removed APM-managed MCP server 'echo' from .github/mcp.json" in first
    assert project.read_json(".github/mcp.json") == {"mcpServers": {}}

    for _ in range(2):
        again = _flat(project.install())
        assert "Removed APM-managed" not in again and "Kept MCP server" not in again, again
        assert "echo" in project.read_json(".mcp.json")["mcpServers"]

    # A user who later writes the old shape by hand keeps it.
    legacy = {"type": "local", "tools": ["*"], "id": "", "command": "echo", "args": []}
    project.write_json(".github/mcp.json", {"mcpServers": {"echo": legacy}})
    assert "Removed APM-managed" not in _flat(project.install())
    assert project.read_json(".github/mcp.json") == {"mcpServers": {"echo": legacy}}


def test_edited_vscode_entry_is_kept_when_copilot_is_not_selected(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """A retired vscode claim never reaches unproven cleanup, even without copilot."""
    project = _Project(tmp_path, apm_binary_path)
    (project.root / ".claude").mkdir()
    project.manifest(targets=["claude"], dependencies={"mcp": [_ECHO]})
    edited = {"type": "stdio", "command": "echo", "args": ["--mine"]}
    project.write_json(".vscode/mcp.json", {"servers": {"echo": edited}, "inputs": []})
    dump_yaml(
        {
            "lockfile_version": "1",
            "apm_version": "0.36.1",
            "dependencies": [],
            "mcp_servers": ["echo"],
            "mcp_configs": {"echo": _ECHO},
            "mcp_target_servers": {"vscode": ["echo"]},
        },
        project.root / "apm.lock.yaml",
    )

    output = _flat(project.install())

    assert "Kept MCP server 'echo' in .vscode/mcp.json" in output
    assert project.read_json(".vscode/mcp.json")["servers"] == {"echo": edited}
    project.manifest(targets=["claude"])
    project.install()
    assert project.read_json(".vscode/mcp.json")["servers"] == {"echo": edited}


def test_saved_vscode_default_target_deploys_like_copilot(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    project = _Project(tmp_path, apm_binary_path)
    project.manifest(dependencies={"mcp": [_ECHO]})

    configured = project.apm("config", "set", "target", "vscode")
    assert configured.returncode == 0, configured.stdout + configured.stderr
    # The alias is saved as the target it selects.
    assert "Default install target set to: copilot" in _flat(configured.stdout)
    project.install()

    assert "echo" in project.read_json(".mcp.json")["mcpServers"]
    assert project.lock()["mcp_target_servers"] == {"copilot": ["echo"]}
    assert not (project.root / ".vscode").exists()


def test_user_scope_copilot_keeps_its_global_config(tmp_path: Path, apm_binary_path: Path) -> None:
    project = _Project(tmp_path, apm_binary_path)

    result = project.apm(
        "install", "-g", "--target", "copilot", "--mcp", "echo", "--", "echo", "hi"
    )

    assert result.returncode == 0, result.stdout + result.stderr
    home = Path(project.env["HOME"])
    config = json.loads((home / ".copilot" / "mcp-config.json").read_text(encoding="utf-8"))
    assert "echo" in config["mcpServers"]
    assert not (project.root / ".mcp.json").exists()


def test_direct_mcp_retarget_keeps_the_entry_the_new_runtime_owns(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """Retargeting `install --mcp` from claude to copilot retires only claude's claim."""
    project = _Project(tmp_path, apm_binary_path)
    (project.root / ".claude").mkdir()
    project.manifest()
    entry = {"type": "stdio", "command": "echo", "args": []}

    project.install("--mcp", "echo", "--target", "claude", "--", "echo")
    assert project.read_json(".mcp.json")["mcpServers"] == {"echo": entry}

    project.install("--mcp", "echo", "--target", "copilot", "--", "echo")
    assert project.read_json(".mcp.json")["mcpServers"] == {"echo": entry}
    assert project.lock()["mcp_target_servers"] == {"copilot": ["echo"]}


def _seed_036_lock(project: _Project, servers: list[dict], target_servers: dict) -> None:
    configs = {server["name"]: server for server in servers}
    dump_yaml(
        {
            "lockfile_version": "1",
            "apm_version": "0.36.1",
            "dependencies": [],
            "mcp_servers": sorted(configs),
            "mcp_configs": configs,
            "mcp_target_servers": target_servers,
        },
        project.root / "apm.lock.yaml",
    )


@pytest.mark.parametrize("first", ["install", "install-mcp"])
def test_legacy_copilot_claim_never_adopts_a_user_root_entry(
    tmp_path: Path, apm_binary_path: Path, first: str
) -> None:
    """A 0.36 copilot claim proves a .github/mcp.json write, not ownership of .mcp.json."""
    project = _Project(tmp_path, apm_binary_path)
    user_root = {"type": "stdio", "command": "echo", "args": []}
    project.write_json(".mcp.json", {"mcpServers": {"echo": user_root}})
    legacy = {"type": "local", "tools": ["*"], "id": "", "command": "echo", "args": []}
    project.write_json(".github/mcp.json", {"mcpServers": {"echo": legacy}})
    project.manifest(targets=["copilot"], dependencies={"mcp": [_ECHO]})
    _seed_036_lock(project, [_ECHO], {"copilot": ["echo"]})

    project.install(
        *(["--mcp", "echo", "--target", "copilot", "--", "echo"] if first == "install-mcp" else [])
    )
    assert project.read_json(".mcp.json")["mcpServers"] == {"echo": user_root}
    assert "echo" in project.read_json(".github/mcp.json")["mcpServers"]

    # Dropping the dependency removes only APM's retired entry, never the user's.
    project.manifest(targets=["copilot"])
    project.install()
    assert project.read_json(".mcp.json")["mcpServers"] == {"echo": user_root}
    assert project.read_json(".github/mcp.json")["mcpServers"] == {}


def test_partial_install_keeps_unhealed_retired_claims_for_the_retry(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """A connection conflict after some root writes never strands retired APM entries."""
    project = _Project(tmp_path, apm_binary_path)
    servers = [{**_ECHO, "name": "vs-a"}, {**_ECHO, "name": "vs-b"}]
    managed = {"type": "stdio", "command": "echo", "args": []}
    project.write_json(
        ".vscode/mcp.json", {"servers": {"vs-a": managed, "vs-b": managed}, "inputs": []}
    )
    project.write_json(".mcp.json", {"mcpServers": {"vs-b": _USER_ROOT}})
    project.manifest(targets=["copilot"], dependencies={"mcp": servers})
    _seed_036_lock(project, servers, {"vscode": ["vs-a", "vs-b"]})

    failed = project.apm("install", "--no-policy")
    assert failed.returncode != 0, failed.stdout + failed.stderr
    assert set(project.read_json(".vscode/mcp.json")["servers"]) == {"vs-b"}
    assert "vscode" not in project.lock()["mcp_target_servers"]

    # The user resolves the conflict; the retry moves the rest and heals it.
    project.write_json(".mcp.json", {"mcpServers": {"vs-a": managed}})
    project.install()
    assert project.read_json(".mcp.json")["mcpServers"] == {"vs-a": managed, "vs-b": managed}
    assert not (project.root / ".vscode/mcp.json").exists()
    assert "vscode" not in project.lock()["mcp_target_servers"]


def test_legacy_vscode_claim_never_adopts_an_identical_user_root_entry(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """A .vscode/mcp.json entry fingerprints like its .mcp.json twin; the user's twin stays."""
    project = _Project(tmp_path, apm_binary_path)
    entry = {"type": "stdio", "command": "echo", "args": []}
    project.write_json(".mcp.json", {"mcpServers": {"echo": entry}})
    project.write_json(".vscode/mcp.json", {"servers": {"echo": entry}, "inputs": []})
    project.manifest(targets=["copilot"], dependencies={"mcp": [_ECHO]})
    _seed_036_lock(project, [_ECHO], {"vscode": ["echo"]})

    project.install()
    project.install()
    assert project.read_json(".mcp.json")["mcpServers"] == {"echo": entry}

    project.manifest(targets=["copilot"])
    project.install()
    assert project.read_json(".mcp.json")["mcpServers"] == {"echo": entry}
    assert not (project.root / ".vscode/mcp.json").exists()


def _seed_pre_root_package_project(project: _Project) -> None:
    """A pre-0.37 project whose next install also rewrites the package lock."""
    dep = project.root / "dep"
    (dep / ".apm/instructions").mkdir(parents=True)
    dump_yaml({"name": "dep", "version": "1.0.0"}, dep / "apm.yml")
    (dep / ".apm/instructions/dep.instructions.md").write_text(
        "---\napplyTo: '**'\n---\nDep guidance.\n", encoding="utf-8"
    )
    project.manifest(targets=["copilot"], dependencies={"apm": [{"path": "./dep"}], "mcp": [_ECHO]})
    legacy = {"type": "local", "tools": ["*"], "id": "", "command": "echo", "args": []}
    project.write_json(".github/mcp.json", {"mcpServers": {"echo": legacy}})
    dump_yaml(
        {
            "lockfile_version": "1",
            "apm_version": "0.35.0",
            "dependencies": [],
            "mcp_servers": ["echo"],
            "mcp_configs": {"echo": _ECHO},
            "mcp_target_servers": {"copilot": ["echo"]},
        },
        project.root / "apm.lock.yaml",
    )


@pytest.mark.parametrize("first", ["only-apm", "mcp-conflict"])
def test_a_lock_rewrite_before_the_mcp_phase_keeps_the_heal_pending(
    tmp_path: Path, apm_binary_path: Path, first: str
) -> None:
    """The package phase rewrites the lock; the MCP phase alone may restamp it."""
    project = _Project(tmp_path, apm_binary_path)
    _seed_pre_root_package_project(project)
    if first == "only-apm":
        project.install("--only", "apm")
    else:
        project.write_json(".mcp.json", {"mcpServers": {"echo": _USER_ROOT}})
        failed = project.apm("install", "--no-policy")
        assert failed.returncode != 0, failed.stdout + failed.stderr
        (project.root / ".mcp.json").unlink()
    lock = project.lock()
    assert lock["dependencies"], lock
    # A restamped version would read the 0.36 claim as .mcp.json ownership.
    assert lock["apm_version"] == "0.35.0"
    assert lock["mcp_target_servers"] == {"copilot": ["echo"]}

    output = _flat(project.install())

    assert "Removed APM-managed MCP server 'echo' from .github/mcp.json" in output
    assert project.read_json(".github/mcp.json") == {"mcpServers": {}}
    assert project.lock()["apm_version"] != "0.35.0"


@pytest.mark.parametrize("runtime", ["copilot", "vscode"])
def test_frozen_upgrade_keeps_the_root_entry_apm_wrote(
    tmp_path: Path, apm_binary_path: Path, runtime: str
) -> None:
    """A --frozen heal cannot record its .mcp.json write; the next install adopts it."""
    project = _Project(tmp_path, apm_binary_path)
    project.manifest(targets=["copilot"], dependencies={"mcp": [_ECHO]})
    if runtime == "copilot":
        legacy = {"type": "local", "tools": ["*"], "id": "", "command": "echo", "args": []}
        project.write_json(".github/mcp.json", {"mcpServers": {"echo": legacy}})
    else:
        entry = {"type": "stdio", "command": "echo", "args": []}
        project.write_json(".vscode/mcp.json", {"servers": {"echo": entry}, "inputs": []})
    _seed_036_lock(project, [_ECHO], {runtime: ["echo"]})
    project.install("--frozen")
    assert "echo" in project.read_json(".mcp.json")["mcpServers"]

    project.install()
    assert project.lock()["mcp_target_servers"] == {"copilot": ["echo"]}

    project.manifest(targets=["copilot"], dependencies={"mcp": [{**_ECHO, "args": ["hi"]}]})
    project.install()
    assert project.read_json(".mcp.json")["mcpServers"]["echo"]["args"] == ["hi"]

    project.manifest(targets=["copilot"])
    project.install()
    assert project.read_json(".mcp.json")["mcpServers"] == {}


def test_frozen_upgrade_then_a_changed_declaration_updates_the_root_entry(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """The entry --frozen wrote from the locked declaration is APM's even after apm.yml changes."""
    project = _Project(tmp_path, apm_binary_path)
    _seed_036_copilot_only(project, targets=["copilot"])
    project.install("--frozen")

    project.manifest(targets=["copilot"], dependencies={"mcp": [{**_ECHO, "args": ["hi"]}]})
    project.install()

    assert project.read_json(".mcp.json")["mcpServers"]["echo"]["args"] == ["hi"]
    assert project.lock()["mcp_target_servers"] == {"copilot": ["echo"]}


def test_moved_claim_never_adopts_a_differing_root_entry(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    project = _Project(tmp_path, apm_binary_path)
    project.write_json(".github/mcp.json", {"mcpServers": {}})
    project.write_json(".mcp.json", {"mcpServers": {"echo": _USER_ROOT}})
    project.manifest(targets=["copilot"], dependencies={"mcp": [_ECHO]})
    _seed_036_lock(project, [_ECHO], {"copilot": ["echo"]})

    failed = project.apm("install", "--no-policy")

    assert failed.returncode != 0, failed.stdout + failed.stderr
    assert project.read_json(".mcp.json")["mcpServers"] == {"echo": _USER_ROOT}


@pytest.mark.parametrize(
    "case",
    ["undeclared", "lock-0.37", "claims-another-name", "github-file-missing", "github-symlinked"],
)
def test_identical_root_entry_stays_the_users_unless_every_adoption_bound_holds(
    tmp_path: Path, apm_binary_path: Path, case: str
) -> None:
    """Each case breaks one bound; the user's identical .mcp.json entry is never adopted."""
    project = _Project(tmp_path, apm_binary_path)
    rendered = {"type": "stdio", "command": "echo", "args": []}
    checked = "gone" if case == "undeclared" else "echo"
    servers = [_ECHO, {**_ECHO, "name": "gone"}, {**_ECHO, "name": "other"}]
    claims = {
        "undeclared": {"copilot": ["echo", "gone"]},
        "lock-0.37": {"vscode": ["echo"]},
        "claims-another-name": {"copilot": ["other"]},
    }.get(case, {"copilot": ["echo"]})
    project.write_json(".mcp.json", {"mcpServers": {checked: rendered}})
    if case == "github-symlinked":
        outside = tmp_path / "outside-mcp.json"
        outside.write_text('{"mcpServers": {}}\n', encoding="utf-8")
        (project.root / ".github").mkdir()
        (project.root / ".github/mcp.json").symlink_to(outside)
    elif case != "github-file-missing":
        project.write_json(".github/mcp.json", {"mcpServers": {}})
    project.manifest(targets=["copilot"], dependencies={"mcp": [_ECHO]})
    _seed_036_lock(project, servers, claims)
    if case == "lock-0.37":
        lock = project.lock()
        dump_yaml({**lock, "apm_version": "0.37.0"}, project.root / "apm.lock.yaml")

    project.install()
    assert checked not in project.lock().get("mcp_target_servers", {}).get("copilot", [])

    project.manifest(targets=["copilot"])
    project.install()
    assert project.read_json(".mcp.json")["mcpServers"][checked] == rendered
