"""APMLifecycle: a plugin dependency can opt out of the native plugin route (#4995).

PROMISE: a dependency marked ``native: false`` (or a project with
``native_plugins: false``) never takes the native plugin route on any harness:
every primitive APM projects deploys loose, as before 0.37, with no
``.agents/plugins`` copy and no ``extraKnownMarketplaces``/``enabledPlugins``
entry. ``native: true`` or no key keeps the automatic route
(source: #4995 scope record, docs reference/manifest-schema.md 3 and 4.1).
CELL: OP=install,audit SRC=local PRIM=plugin(skills,commands) SCOPE=project,user
TGT=claude,copilot NATIVE=false|true|auto x NATIVE_PLUGINS=false|auto
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.integration,
    pytest.mark.requires_apm_binary,
    pytest.mark.promise("P-OWN-claude-native-plugin-registration", "P-INV-I1", "P-INV-I7"),
]

_SETTINGS = (".claude/settings.json", ".github/copilot/settings.json")
_USER_ENTRY = "mine@somewhere"


class _World:
    def __init__(self, tmp_path: Path, binary: Path) -> None:
        self.isolated = IsolatedApmEnvironment.create(tmp_path / "world", base_env=dict(os.environ))
        self.env = self.isolated.subprocess_env(overrides={"NO_COLOR": "1"})
        self.runner = ApmLifecycleRunner((str(binary),))
        self.project = self.isolated.work_root / "project"
        (self.project / ".claude").mkdir(parents=True)
        self.packages = self.isolated.work_root / "packages"

    def plugin(self, name: str, skills: tuple[str, ...] = ("alpha",)) -> Path:
        """A complete Claude-format plugin with skills and a command."""
        root = self.packages / name
        (root / ".claude-plugin").mkdir(parents=True, exist_ok=True)
        (root / ".claude-plugin/plugin.json").write_text(
            json.dumps({"name": name, "version": "1.0.0"}), encoding="utf-8"
        )
        for skill in skills:
            (root / "skills" / skill).mkdir(parents=True, exist_ok=True)
            (root / "skills" / skill / "SKILL.md").write_text(
                f"---\nname: {skill}\ndescription: {skill}\n---\n{skill} body.\n", encoding="utf-8"
            )
        (root / "commands").mkdir(exist_ok=True)
        (root / "commands" / f"{name}-go.md").write_text(
            "---\ndescription: go\n---\nGo.\n", encoding="utf-8"
        )
        return root

    def manifest(self, deps: list, *, top: str = "", root: Path | None = None) -> None:
        lines = ["name: consumer", "version: 0.0.1", "targets: [claude, copilot]"]
        if top:
            lines.append(top)
        lines += ["dependencies:", "  apm:"]
        for dep in deps:
            if isinstance(dep, str):
                lines.append(f"    - {dep}")
            else:
                lines.append(f"    - path: {dep['path']}")
                for key, value in dep.items():
                    if key != "path":
                        lines.append(f"      {key}: {value}")
        ((root or self.project) / "apm.yml").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def run(self, *args: str, cwd: Path | None = None) -> CommandResult:
        return self.runner.run(
            args, cwd=cwd or self.project, env=self.env, scenario_id=" ".join(args)
        )

    def ok(self, *args: str) -> CommandResult:
        result = self.run(*args)
        assert result.returncode == 0, _evidence(result)
        return result

    def settings(self) -> dict[str, dict]:
        out = {}
        for relative in _SETTINGS:
            path = self.project / relative
            out[relative] = json.loads(path.read_text("utf-8")) if path.exists() else {}
        return out


def _evidence(result: CommandResult) -> str:
    return f"exit={result.returncode}\nstdout={result.stdout}\nstderr={result.stderr}"


def _text(result: CommandResult) -> str:
    return " ".join(f"{result.stdout}\n{result.stderr}".split())


def _apm_registration(settings: dict[str, dict]) -> list[str]:
    """APM marketplace and enabledPlugins keys (APM's project marketplace is ``apm-p-*``)."""
    keys = []
    for data in settings.values():
        keys += [k for k in data.get("extraKnownMarketplaces", {}) if k.startswith("apm-")]
        keys += [k for k in data.get("enabledPlugins", {}) if "@apm-" in k]
    return keys


def _assert_loose(world: _World, name: str, skills: tuple[str, ...] = ("alpha",)) -> None:
    for skill in skills:
        assert (world.project / ".claude/skills" / skill / "SKILL.md").is_file(), skill
    assert (world.project / ".claude/commands" / f"{name}-go.md").is_file()
    assert not (world.project / ".agents/plugins" / name).exists()
    assert _apm_registration(world.settings()) == []


def _assert_native(world: _World, name: str) -> None:
    assert (world.project / ".agents/plugins" / name / ".claude-plugin/plugin.json").is_file()
    assert any(key.startswith(f"{name}@") for key in _apm_registration(world.settings()))


@pytest.fixture
def world(tmp_path: Path, apm_binary_path: Path) -> _World:
    return _World(tmp_path, apm_binary_path)


def test_native_false_deploys_every_primitive_loose(world: _World) -> None:
    """(1) native: false projects the plugin loose, with no native registration."""
    plugin = world.plugin("dn")
    world.manifest([{"path": plugin.as_posix(), "native": "false"}])

    world.ok("install", "--no-policy")

    _assert_loose(world, "dn")
    assert not (world.project / ".agents/plugins").exists()


def test_project_default_goes_loose_and_a_dependency_key_overrides_it(world: _World) -> None:
    """(2) native_plugins: false sends keyless deps loose; native: true stays native."""
    loose, native = world.plugin("lo"), world.plugin("na")
    world.manifest(
        [loose.as_posix(), {"path": native.as_posix(), "native": "true"}],
        top="native_plugins: false",
    )

    world.ok("install", "--no-policy")

    assert (world.project / ".claude/skills/alpha/SKILL.md").is_file()
    assert (world.project / ".claude/commands/lo-go.md").is_file()
    assert not (world.project / ".agents/plugins/lo").exists()
    _assert_native(world, "na")


def test_flipping_the_route_moves_the_plugin_and_keeps_user_entries(world: _World) -> None:
    """(3) native -> loose retires APM's registration only; loose -> native removes loose files."""
    plugin = world.plugin("dn")
    world.manifest([plugin.as_posix()])
    world.ok("install", "--no-policy")
    _assert_native(world, "dn")
    settings_path = world.project / ".claude/settings.json"
    data = json.loads(settings_path.read_text("utf-8"))
    data["enabledPlugins"][_USER_ENTRY] = True
    settings_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    world.manifest([{"path": plugin.as_posix(), "native": "false"}])
    world.ok("install", "--no-policy")

    _assert_loose(world, "dn")
    assert json.loads(settings_path.read_text("utf-8"))["enabledPlugins"] == {_USER_ENTRY: True}
    world.ok("audit", "--ci", "--no-policy")

    world.manifest([plugin.as_posix()])
    world.ok("install", "--no-policy")

    _assert_native(world, "dn")
    assert not (world.project / ".claude/skills/alpha").exists()
    assert not (world.project / ".claude/commands/dn-go.md").exists()
    assert json.loads(settings_path.read_text("utf-8"))["enabledPlugins"][_USER_ENTRY] is True


def test_a_skill_a_later_version_adds_deploys_loose_without_a_manifest_change(
    world: _World,
) -> None:
    """(4) native: false follows the package: a new skill arrives on the next install."""
    plugin = world.plugin("dn")
    world.manifest([{"path": plugin.as_posix(), "native": "false"}])
    world.ok("install", "--no-policy")

    world.plugin("dn", skills=("alpha", "beta"))
    world.ok("install", "--no-policy")

    _assert_loose(world, "dn", skills=("alpha", "beta"))


def test_frozen_refuses_a_route_change_until_a_normal_install(world: _World) -> None:
    """(5) --frozen names the route change; after a normal install it passes."""
    plugin = world.plugin("dn")
    world.manifest([plugin.as_posix()])
    world.ok("install", "--no-policy")
    world.manifest([{"path": plugin.as_posix(), "native": "false"}])

    frozen = world.run("install", "--frozen", "--no-policy")

    assert frozen.returncode != 0, _evidence(frozen)
    assert "native" in _text(frozen) and "route" in _text(frozen), _evidence(frozen)
    _assert_native(world, "dn")

    world.ok("install", "--no-policy")
    world.ok("install", "--frozen", "--no-policy")
    _assert_loose(world, "dn")


@pytest.mark.parametrize("project_default", ["", "native_plugins: false"], ids=["auto", "off"])
def test_a_transitive_plugin_follows_the_project_default(
    world: _World, project_default: str
) -> None:
    """(6) A parent's native key binds only the parent; its plugin dep follows the project."""
    plugin = world.plugin("tp")
    parent = world.packages / "parent"
    (parent / ".apm/instructions").mkdir(parents=True)
    (parent / ".apm/instructions/p.instructions.md").write_text(
        '---\napplyTo: "**"\ndescription: p\n---\nP.\n', encoding="utf-8"
    )
    (parent / "apm.yml").write_text(
        f"name: parent\nversion: 1.0.0\ndependencies:\n  apm:\n    - {plugin.as_posix()}\n",
        encoding="utf-8",
    )
    world.manifest([{"path": parent.as_posix(), "native": "false"}], top=project_default)

    world.ok("install", "--no-policy")

    if project_default:
        _assert_loose(world, "tp")
    else:
        _assert_native(world, "tp")


def test_user_scope_native_false_deploys_loose_under_the_claude_home(world: _World) -> None:
    """(7) At user scope the same key reads from ~/.apm/apm.yml."""
    plugin = world.plugin("dn")
    user_manifest = world.isolated.config_root
    user_manifest.mkdir(parents=True, exist_ok=True)
    world.manifest([{"path": plugin.as_posix(), "native": "false"}], root=user_manifest)
    (world.isolated.home / ".claude").mkdir(exist_ok=True)
    elsewhere = world.isolated.work_root / "elsewhere"
    elsewhere.mkdir()

    result = world.run("install", "-g", "--no-policy", cwd=elsewhere)

    assert result.returncode == 0, _evidence(result)
    assert (world.isolated.home / ".claude/skills/alpha/SKILL.md").is_file(), _evidence(result)
    assert not (world.isolated.home / ".claude/plugins/marketplaces").exists() or not any(
        "dn" in path.name for path in (world.isolated.home / ".claude/plugins").rglob("*")
    )


def test_an_opted_out_dependency_gets_no_native_route_notice(world: _World) -> None:
    """(8) A native: false dependency is not on the native route, so no notice names it."""
    plugin = world.plugin("dn")
    world.manifest([{"path": plugin.as_posix(), "native": "false"}])

    result = world.ok("install", "--no-policy")

    assert "native plugin" not in _text(result).lower(), _evidence(result)
    assert "worktree" not in _text(result).lower(), _evidence(result)


@pytest.mark.parametrize(
    ("dep_key", "top", "field"),
    [
        ('"no"', "", "'native' field must be true or false"),
        ("", "native_plugins: maybe", "'native_plugins' must be true or false"),
    ],
    ids=["native", "native_plugins"],
)
def test_an_invalid_value_is_refused_at_parse_time(
    world: _World, dep_key: str, top: str, field: str
) -> None:
    """(9) Only booleans are accepted; the error names the field and writes nothing."""
    plugin = world.plugin("dn")
    dep = {"path": plugin.as_posix(), "native": dep_key} if dep_key else plugin.as_posix()
    world.manifest([dep], top=top)

    result = world.run("install", "--no-policy")

    assert result.returncode != 0, _evidence(result)
    assert field in _text(result), _evidence(result)
    assert not (world.project / "apm.lock.yaml").exists()
    assert not (world.project / ".agents").exists()


def test_the_native_route_prints_one_diagnostic_naming_the_opt_out(world: _World) -> None:
    """(10) One diagnostic names worktrees, claude -p, Copilot CLI 1.0.106 and native: false."""
    plugin = world.plugin("dn")
    world.manifest([plugin.as_posix()])

    result = world.ok("install", "--no-policy")

    text = _text(result)
    assert text.count("native: false") == 1, _evidence(result)
    for needle in ("worktree", "claude -p", "claude-code-action", "1.0.106"):
        assert needle in text, (needle, _evidence(result))
