"""Real-CLI qualification of the Windows local-bundle audit report (#2927)."""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_git_repository import LocalGitRepositoryFactory
from tests.utils.local_package import LocalPackage, LocalPackageFactory

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.windows_compat,
    pytest.mark.lifecycle_smoke,
]

_AUDIT = ("audit", "--ci", "--no-policy", "--no-fail-fast", "--format", "json")
_INSTALL = ("--target", "opencode", "--no-policy", "--no-audit", "--parallel-downloads", "0")


def _author_skill(
    factory: LocalPackageFactory, package: LocalPackage, name: str, version: str
) -> dict[str, bytes]:
    """Author expected bytes independently of deployment and its ledger."""
    files = {
        "SKILL.md": (
            f"---\nname: {name}\ndescription: Local bundle audit fixture\n---\n"
            f"# {name}\n\nVersion {version}.\n"
        ).encode(),
        "agents/openai.yaml": b"interface:\n  display_name: Audit fixture\n",
        "references/guide.md": f"# Resource {name} {version}\n".encode(),
        "assets/data.bin": b"\x00\x01\x02\xff\r\n",
    }
    source = factory.add_skill(package, name, files["SKILL.md"].decode())
    for relative, content in files.items():
        path = source.parent / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    return {f".agents/skills/{name}/{relative}": content for relative, content in files.items()}


def _snapshot(root: Path, *, omit_temp: bool = False) -> dict[str, tuple[int, bytes]]:
    """Observe filesystem topology and bytes, without any product discovery helper."""
    observed: dict[str, tuple[int, bytes]] = {}
    for directory, names, files in os.walk(root, followlinks=False):
        if omit_temp and Path(directory) == root:
            names[:] = [name for name in names if name != "tmp"]
        for name in sorted((*names, *files)):
            path = Path(directory) / name
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode):
                content = os.readlink(path).encode()
            elif stat.S_ISREG(mode):
                content = path.read_bytes()
            else:
                content = b""
            observed[path.relative_to(root).as_posix()] = (mode, content)
    return observed


def _assert_deployed(project: Path, expected: dict[str, bytes]) -> None:
    """Assert the complete physical skill tree, not a replay/ledger-derived oracle."""
    actual = {
        path.relative_to(project).as_posix(): path.read_bytes()
        for path in (project / ".agents" / "skills").rglob("*")
        if path.is_file()
    }
    assert actual == expected
    assert not (project / ".claude").exists()
    assert not (project / ".github" / "skills").exists()


def _evidence(result: CommandResult) -> str:
    """Keep native subprocess diagnostics in failed test evidence."""
    return f"{result.command!r}\nexit={result.returncode}\n{result.stdout}\n{result.stderr}"


def test_opencode_local_bundle_pinned_skills_audit_lifecycle(
    tmp_path: Path,
    apm_engine_command: tuple[str, ...],
    capsys: pytest.CaptureFixture[str],
    request: pytest.FixtureRequest,
) -> None:
    """Install, reinstall, update and real drift preserve the isolated durable state."""
    isolated = IsolatedApmEnvironment.create(
        tmp_path / "local bundle audit", base_env=dict(os.environ)
    )
    appdata = isolated.home / "app-data"
    appdata.mkdir()
    environment = isolated.subprocess_env(
        overrides={"APPDATA": str(appdata), "PYTHONDONTWRITEBYTECODE": "1"}
    )
    factory = LocalPackageFactory(isolated.package_root)
    upstream = factory.create("pinned-skills")
    remote_expected: dict[str, bytes] = {}
    for name in ("remote-alpha", "remote-beta"):
        remote_expected.update(_author_skill(factory, upstream, name, "1"))
    repositories = LocalGitRepositoryFactory(isolated.repository_root, env=environment)
    repository = repositories.create("pinned-skills", source_tree=upstream.root)
    initial = repositories.commit(repository, message="seed pinned skills")
    remote = "https://github.com/apm-fixture-org/pinned-skills"
    environment = repositories.url_rewrite_subprocess_env(repository, remote)
    bundle = factory.create(
        "local-bundle",
        dependencies=tuple(
            f"apm-fixture-org/pinned-skills/skills/{name}#{initial.sha}"
            for name in ("remote-alpha", "remote-beta")
        ),
    )
    expected = dict(remote_expected)
    for name in ("local-alpha", "local-beta"):
        expected.update(_author_skill(factory, bundle, name, "1"))
    project = isolated.work_root / "empty-consumer"
    project.mkdir()
    harness = isolated.home / ".config" / "opencode" / "opencode.json"
    harness.parent.mkdir(parents=True)
    harness.write_bytes(b'{"theme":"user-owned"}\n')
    runner = ApmLifecycleRunner(apm_engine_command, timeout_seconds=180)

    for phase, command in (
        ("install", ("install", str(bundle.root), *_INSTALL)),
        ("reinstall", ("install", *_INSTALL)),
        ("update", ("install", "--update", *_INSTALL)),
    ):
        if phase == "update":
            expected.update(_author_skill(factory, bundle, "local-alpha", "2"))
            advanced = repository.worktree / "skills" / "remote-alpha" / "SKILL.md"
            advanced.write_bytes(b"# Unselected newer upstream revision\n")
            repositories.commit(repository, message="advance remote beyond the pinned revision")
        install = runner.run(command, scenario_id=phase, cwd=project, env=environment)
        assert install.returncode == 0, _evidence(install)
        _assert_deployed(project, expected)
        before = _snapshot(isolated.root, omit_temp=True)
        audit = runner.run(_AUDIT, scenario_id=f"{phase}-audit", cwd=project, env=environment)
        assert _snapshot(isolated.root, omit_temp=True) == before, _evidence(audit)
        assert audit.returncode == 0, _evidence(audit)
        report = json.loads(audit.stdout)
        assert report["passed"] is True
        assert report["drift"]["drift"] == []
        _assert_deployed(project, expected)

    for relative, kind in (
        (".agents/skills/local-alpha/SKILL.md", "modified"),
        (".agents/skills/remote-beta/references/guide.md", "unintegrated"),
    ):
        path = project / relative
        if kind == "modified":
            path.write_bytes(b"# Deliberately modified managed file\n")
        else:
            path.unlink()
        before = _snapshot(isolated.root, omit_temp=True)
        audit = runner.run(_AUDIT, scenario_id=kind, cwd=project, env=environment)
        assert _snapshot(isolated.root, omit_temp=True) == before, _evidence(audit)
        assert audit.returncode == 1, _evidence(audit)
        report = json.loads(audit.stdout)
        assert report["passed"] is False
        assert {(item["path"], item["kind"]) for item in report["drift"]["drift"]} == {
            (relative, kind)
        }, report
        path.write_bytes(expected[relative])
    _assert_deployed(project, expected)
    # xdist forwards worker stderr, but not uncaptured stdout.
    with capsys.disabled():
        print(f"[+] Completed {request.node.nodeid}", file=sys.stderr, flush=True)
