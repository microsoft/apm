"""Installed-CLI proof that an older APM refuses a newer generator's lockfile (#2334)."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.lifecycle_smoke,
    pytest.mark.requires_apm_binary,
]

_GUIDE = '---\napplyTo: "**"\n---\n# guide\n'
_DEPLOYED = Path(".github/instructions/guide.instructions.md")
_REFUSING = (
    ("install",),
    ("install", "--frozen"),
    ("install", "--dry-run"),
    ("update", "--yes"),
    ("uninstall", "../pkg"),
    ("prune",),
    ("lock",),
    ("deps", "update"),
    ("audit",),
    ("audit", "--ci"),
    ("pack",),
)
_WARNING = (("deps", "list"), ("outdated",))


def _output(result: CommandResult) -> str:
    return " ".join((result.stdout + result.stderr).split())


def _evidence(result: CommandResult) -> str:
    return f"command={result.command!r}\nrc={result.returncode}\n{result.stdout}\n{result.stderr}"


class _Project:
    def __init__(self, tmp_path: Path, apm_binary_path: Path) -> None:
        self.isolated = IsolatedApmEnvironment.create(
            tmp_path.resolve() / "isolated", base_env=dict(os.environ)
        )
        self.env = self.isolated.subprocess_env()
        self.runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=120)
        self.package = self.isolated.work_root / "pkg"
        (self.package / ".apm" / "instructions").mkdir(parents=True)
        (self.package / "apm.yml").write_text("name: pkg\nversion: 1.0.0\n", encoding="utf-8")
        self.guide = self.package / ".apm" / "instructions" / "guide.instructions.md"
        self.guide.write_text(_GUIDE, encoding="utf-8")
        self.root = self.isolated.work_root / "proj"
        self.root.mkdir()
        (self.root / "apm.yml").write_text(
            "name: proj\nversion: 1.0.0\ntargets: [copilot]\n"
            "dependencies:\n  apm:\n    - path: ../pkg\n",
            encoding="utf-8",
        )
        self.lock = self.root / "apm.lock.yaml"

    def run(self, *args: str) -> CommandResult:
        return self.runner.run(args, scenario_id=" ".join(args), cwd=self.root, env=self.env)

    def stamp(self, version: str) -> None:
        text = self.lock.read_text("utf-8")
        self.lock.write_text(
            re.sub(r"(?m)^apm_version: .*$", f"apm_version: {version}", text), encoding="utf-8"
        )

    def stamped(self) -> str:
        return re.search(r"(?m)^apm_version: (.*)$", self.lock.read_text("utf-8")).group(1)

    def seed(self) -> None:
        seeded = self.run("install")
        assert seeded.returncode == 0, _evidence(seeded)

    def state(self) -> dict[Path, bytes]:
        return {
            path: path.read_bytes()
            for path in (self.root / "apm.yml", self.lock, self.root / _DEPLOYED)
        }


def _release(project: _Project) -> tuple[int, int, int]:
    match = re.match(r"(\d+)\.(\d+)\.(\d+)", project.stamped())
    assert match, project.stamped()
    return int(match[1]), int(match[2]), int(match[3])


def test_older_cli_refuses_every_lockfile_command_without_mutation(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """A CLI older than the generator refuses before writing, replaying or certifying."""
    project = _Project(tmp_path, apm_binary_path)
    project.seed()
    current = project.stamped()
    major, minor, patch = _release(project)
    newer = f"{major}.{minor}.{patch + 1}a1"
    project.stamp(newer)
    # Content drift means a permitted install would rewrite the lockfile.
    project.guide.write_text(_GUIDE + "# changed\n", encoding="utf-8")
    before = project.state()

    for args in _REFUSING:
        result = project.run(*args)
        output = _output(result)
        assert result.returncode == 1, _evidence(result)
        assert f"written by APM {newer}, newer than this APM {current}" in output, output
        assert "brew upgrade apm" in output
        assert "apm self-update" in output
        assert "pip install --upgrade apm-cli" in output
        assert "Error installing dependencies" not in output
        # One renderer, on stderr, for every command (#3967).
        assert "Refusing" not in result.stdout, _evidence(result)
        assert "[x] Refusing" in result.stderr, _evidence(result)
        assert project.state() == before, args

    for args in _WARNING:
        result = project.run(*args)
        assert result.returncode == 0, _evidence(result)
        assert f"written by APM {newer}" in _output(result)
    assert project.state() == before


@pytest.mark.parametrize(
    ("label", "stamp"),
    [
        ("equal", lambda current, _release: current),
        ("build-metadata", lambda current, _release: f"{current}+build.7"),
        # a0 is the lowest prerelease of the running release, never newer.
        ("own-prerelease", lambda _current, release: "{}.{}.{}a0".format(*release)),
        ("older", lambda _current, _release: "0.1.0"),
        ("unknown", lambda _current, _release: "unknown"),
    ],
)
def test_equal_or_older_generators_continue(
    tmp_path: Path, apm_binary_path: Path, label: str, stamp
) -> None:
    """Equal, older, build-suffixed and prerelease generators keep working."""
    project = _Project(tmp_path, apm_binary_path)
    project.seed()
    current = project.stamped()
    project.stamp(stamp(current, _release(project)))
    project.guide.write_text(_GUIDE + f"# {label}\n", encoding="utf-8")

    result = project.run("install")
    assert result.returncode == 0, _evidence(result)
    assert project.stamped() == current
    assert (project.root / _DEPLOYED).read_text("utf-8").endswith(f"# {label}\n")


@pytest.mark.parametrize(
    ("label", "edit"),
    [
        (
            "duplicate",
            lambda text: text.replace("apm_version:", "apm_version: 0.1.0\napm_version:", 1),
        ),
        ("invalid", lambda text: re.sub(r"(?m)^apm_version: .*$", "apm_version: 1.0", text)),
        ("null", lambda text: re.sub(r"(?m)^apm_version: .*$", "apm_version: null", text)),
    ],
)
def test_unverifiable_generator_metadata_fails_closed(
    tmp_path: Path, apm_binary_path: Path, label: str, edit
) -> None:
    """Duplicate or malformed apm_version is unsafe rather than silently ignored."""
    project = _Project(tmp_path, apm_binary_path)
    project.seed()
    project.lock.write_text(edit(project.lock.read_text("utf-8")), encoding="utf-8")
    before = project.state()

    result = project.run("install")
    assert result.returncode == 1, _evidence(result)
    assert "Restore the lockfile from version control" in _output(result), label
    assert project.state() == before


def test_flow_style_and_legacy_lockfiles_are_guarded(tmp_path: Path, apm_binary_path: Path) -> None:
    """A newer generator is seen in flow-style YAML and in a legacy ``apm.lock``."""
    project = _Project(tmp_path, apm_binary_path)
    project.seed()
    major = _release(project)[0]
    flow = f'{{"lockfile_version": "1", "apm_version": "{major + 1}.0.0", "dependencies": []}}\n'
    for name in ("apm.lock.yaml", "apm.lock"):
        project.lock.unlink(missing_ok=True)
        (project.root / name).write_text(flow, encoding="utf-8")
        before = (project.root / name).read_bytes()
        for args in (("install",), ("pack", "--json")):
            result = project.run(*args)
            assert result.returncode == 1, _evidence(result)
            assert f"written by APM {major + 1}.0.0" in _output(result), (name, args)
            assert (project.root / name).read_bytes() == before
            if args[0] == "pack":
                envelope = json.loads(result.stdout)
                assert envelope["ok"] is False
                assert envelope["errors"][0]["code"] == "lockfile_generator"
        (project.root / name).unlink()


def test_bundle_from_newer_apm_installs_with_a_warning(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """A bundle whose embedded lock a newer APM wrote installs, but says so (#3963)."""
    from tests.integration._dependency_graph_world import NO_POLICY, World

    world = World.create(tmp_path, apm_binary_path)
    world.publish("tool")
    world.manifest(["test/tool"])
    world.apm("install", NO_POLICY)
    current = re.search(r"(?m)^apm_version: (.*)$", world.lock_bytes().decode()).group(1)
    major = int(current.split(".", 1)[0])
    newer = f"{major + 1}.0.0"
    world.apm("pack")
    bundle = next(iter(sorted((world.project / "build").iterdir())))
    embedded = bundle / "apm.lock.yaml"
    embedded.write_text(
        re.sub(r"(?m)^apm_version: .*$", f"apm_version: {newer}", embedded.read_text("utf-8")),
        encoding="utf-8",
    )
    world.project = world.isolated.work_root / "consumer"
    world.project.mkdir()
    world.manifest([])

    result = world.apm("install", str(bundle), NO_POLICY)
    output = _output(result)
    assert f"written by APM {newer}, newer than this APM {current}" in output, output
    assert "brew upgrade apm" in output
    assert (world.project / "apm.lock.yaml").exists()


def test_dependency_failure_is_not_a_version_refusal(tmp_path: Path, apm_binary_path: Path) -> None:
    """A real dependency failure on a current lockfile never reads as a version refusal."""
    project = _Project(tmp_path, apm_binary_path)
    project.seed()
    project.guide.unlink()
    (project.package / "apm.yml").unlink()

    result = project.run("install")
    assert result.returncode != 0, _evidence(result)
    assert "written by APM" not in _output(result)
    assert "Refusing to run" not in _output(result)


def test_user_scope_lockfile_is_guarded(tmp_path: Path, apm_binary_path: Path) -> None:
    """``-g`` commands guard the user-scope lockfile the same way."""
    project = _Project(tmp_path, apm_binary_path)
    project.seed()
    major = _release(project)[0]
    user_lock = project.isolated.config_root / "apm.lock.yaml"
    (project.isolated.config_root / "apm.yml").write_text(
        "name: user\nversion: 1.0.0\n", encoding="utf-8"
    )
    user_lock.write_text(
        f'lockfile_version: "1"\napm_version: {major + 1}.0.0\ndependencies: []\n',
        encoding="utf-8",
    )
    before = user_lock.read_bytes()

    for args in (("install", "-g"), ("update", "-g", "--yes"), ("uninstall", "-g", "pkg")):
        result = project.run(*args)
        assert result.returncode == 1, _evidence(result)
        assert f"written by APM {major + 1}.0.0" in _output(result)
        # One refusal renderer, naming the user-scope lockfile by path (#3967).
        assert "[x] Refusing" in _output(result), _evidence(result)
        assert "Error: Refusing" not in _output(result)
        unwrapped = "".join(_output(result).split())
        assert "".join(str(user_lock).split()) in unwrapped, _evidence(result)
    assert user_lock.read_bytes() == before


def _with_second_dependency(project: _Project) -> None:
    """Keep a dependency after the writer under test removes ``../other``."""
    other = project.isolated.work_root / "other"
    (other / ".apm" / "instructions").mkdir(parents=True)
    (other / "apm.yml").write_text("name: other\nversion: 1.0.0\n", encoding="utf-8")
    (other / ".apm" / "instructions" / "other.instructions.md").write_text(_GUIDE, "utf-8")
    manifest = project.root / "apm.yml"
    manifest.write_text(manifest.read_text("utf-8") + "    - path: ../other\n", encoding="utf-8")


def _drop_dependency(project: _Project) -> None:
    manifest = project.root / "apm.yml"
    manifest.write_text(
        manifest.read_text("utf-8").replace("    - path: ../other\n", ""), encoding="utf-8"
    )


@pytest.mark.parametrize(
    ("label", "prepare", "args"),
    [
        ("uninstall", None, ("uninstall", "../other")),
        ("prune", _drop_dependency, ("prune",)),
        ("install-mcp", None, ("install", "--mcp", "foo", "--", "echo", "hi")),
    ],
)
def test_every_lockfile_writer_stamps_the_running_cli(
    tmp_path: Path, apm_binary_path: Path, label: str, prepare, args: tuple[str, ...]
) -> None:
    """A writer that changes an older APM's lockfile records this APM, so older CLIs refuse it."""
    project = _Project(tmp_path, apm_binary_path)
    _with_second_dependency(project)
    project.seed()
    current = project.stamped()
    project.stamp("0.1.0")
    before = project.lock.read_bytes()
    if prepare is not None:
        prepare(project)

    result = project.run(*args)
    assert result.returncode == 0, _evidence(result)
    assert project.lock.read_bytes() != before, label
    assert project.stamped() == current, label


def test_fresh_bundle_install_stamps_the_running_cli(tmp_path: Path, apm_binary_path: Path) -> None:
    """``apm install <bundle>`` creates a lockfile that records the APM that wrote it."""
    from tests.integration._dependency_graph_world import NO_POLICY, World

    world = World.create(tmp_path, apm_binary_path)
    world.publish("tool")
    world.manifest(["test/tool"])
    world.apm("install", NO_POLICY)
    current = re.search(r"(?m)^apm_version: (.*)$", world.lock_bytes().decode()).group(1)
    world.apm("pack")
    bundle = next(iter(sorted((world.project / "build").iterdir())))
    world.project = world.isolated.work_root / "consumer"
    world.project.mkdir()
    world.manifest([])

    world.apm("install", str(bundle), NO_POLICY)
    stamp = re.search(r"(?m)^apm_version: (.*)$", world.lock_bytes().decode())
    assert stamp is not None and stamp.group(1) == current
