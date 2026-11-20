"""Real-command proof that cleanup messages identify dependencies, not source deletions."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.artifact_snapshot import ArtifactSnapshot, assert_unchanged
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_package import LocalPackageFactory

pytestmark = [pytest.mark.e2e, pytest.mark.integration]


@pytest.mark.parametrize("operation", ["reinstall", "uninstall"])
@pytest.mark.parametrize("count", [1, 2], ids=["one-skill", "two-skills"])
def test_cleanup_output_names_dependency(
    tmp_path: Path, apm_engine_command: tuple[str, ...], operation: str, count: int
) -> None:
    """Both real logger consumers report deployed deletions without changing source bytes."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "cleanup", base_env=os.environ)
    env = isolated.subprocess_env(overrides={"COLUMNS": "1000", "NO_COLOR": "1", "TERM": "dumb"})
    factory = LocalPackageFactory(isolated.package_root)
    dependency = factory.create("cleanup-source", targets=("agent-skills",))
    source_skills = [
        factory.add_skill(
            dependency,
            f"stale-{index}",
            f"---\nname: stale-{index}\ndescription: Cleanup message fixture\n---\n# Stale\n",
        )
        for index in range(count)
    ]
    if operation == "reinstall":
        factory.add_skill(
            dependency,
            "retained",
            "---\nname: retained\ndescription: Remaining skill\n---\n# Retained\n",
        )
    (dependency.root / ".gitignore").write_text("generated.txt\n", encoding="utf-8")
    (dependency.root / "generated.txt").write_bytes(b"source-only generated content\n")
    before_install = ArtifactSnapshot.capture(dependency.root)
    runner = ApmLifecycleRunner(apm_engine_command)
    installed = runner.run(
        (
            "install",
            "--global",
            "--target",
            "agent-skills",
            "--no-policy",
            "--parallel-downloads",
            "0",
            str(dependency.root),
        ),
        scenario_id=f"cleanup-{operation}-install",
        cwd=isolated.work_root,
        env=env,
    )
    assert installed.returncode == 0, installed.stdout + installed.stderr
    assert_unchanged(before_install, ArtifactSnapshot.capture(dependency.root))
    lock = load_yaml(isolated.config_root / "apm.lock.yaml")
    entries = [entry for entry in lock["dependencies"] if entry.get("source") == "local"]
    assert len(entries) == 1
    deployed = entries[0]["deployed_files"]
    stale_paths = [
        isolated.home / path
        for path in deployed
        if any(
            f"/stale-{index}/" in path or path.endswith(f"/stale-{index}") for index in range(count)
        )
    ]
    # Existing cleanup counts the recorded skill directory and its SKILL.md.
    assert len(stale_paths) == 2 * count, deployed
    assert all(path.exists() for path in stale_paths)
    retained_paths = [isolated.home / path for path in deployed if "/retained/" in path]
    if operation == "reinstall":
        assert len(retained_paths) == 1
        for source_skill in source_skills:
            source_skill.unlink()
            source_skill.parent.rmdir()
        args = ("install", "--global", "--no-policy", "--parallel-downloads", "0")
    else:
        args = ("uninstall", "--global", "_local/cleanup-source")
    source_before_cleanup = ArtifactSnapshot.capture(dependency.root)

    cleaned = runner.run(
        args,
        scenario_id=f"cleanup-{operation}-{count}",
        cwd=isolated.work_root,
        env=env,
    )

    output = cleaned.stdout + cleaned.stderr
    assert cleaned.returncode == 0, output
    noun = "path" if count == 1 else "paths"
    expected = f"Cleaned {count} stale deployed {noun} for {dependency.root}:"
    assert expected in output, output
    _, listed = _announced_removals(cleaned.stdout)
    assert listed == [f"~/.agents/skills/stale-{index}/" for index in range(count)], output
    assert f"Cleaned {2 * count} " not in output
    assert all(not path.exists() for path in stale_paths)
    assert all(path.is_file() for path in retained_paths)
    assert_unchanged(source_before_cleanup, ArtifactSnapshot.capture(dependency.root))


def _announced_removals(stdout: str) -> tuple[list[str], list[str]]:
    """Return the cleanup header lines and the paths listed under them."""
    lines = stdout.splitlines()
    headers = [line for line in lines if line.startswith("[i] Cleaned ")]
    listed = [line[len("  - ") :] for line in lines if line.startswith("  - ")]
    return headers, listed


def _assert_each_removal_named(before: ArtifactSnapshot, after: ArtifactSnapshot, stdout: str):
    """Every deleted deployed file is named, directly or by its listed directory."""
    kinds = {entry.relative_path: entry.kind for entry in before.entries}
    removed = {
        path
        for path in before.diff(after).removed
        if kinds[path] == "file" and path.startswith((".agents/", ".claude/", ".github/"))
    }
    headers, listed = _announced_removals(stdout)
    assert removed, stdout
    for path in removed:
        assert any(path == item or path.startswith(item) for item in listed if item), (
            f"{path} was removed but not named\n{stdout}"
        )
    assert len(listed) == len(set(listed)), stdout
    counts = sum(int(header.split()[2]) for header in headers)
    assert counts == len(listed), f"header counts do not match the listed paths\n{stdout}"
    return listed


@pytest.mark.requires_apm_binary
@pytest.mark.parametrize("operation", ["uninstall", "prune", "target-removal"])
def test_removal_commands_name_each_deleted_path(
    tmp_path: Path, apm_binary_path: Path, operation: str
) -> None:
    """Uninstall, prune and target removal name every deployed path they delete (#4193)."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "isolated", base_env=os.environ)
    env = isolated.subprocess_env(overrides={"COLUMNS": "80"})
    factory = LocalPackageFactory(isolated.package_root)
    kit = factory.create("named-kit")
    factory.add_skill(kit, "named", "---\nname: named\ndescription: Named\n---\n# Named\n")
    factory.add_instruction(kit, "named", "---\napplyTo: '**'\n---\n# Named rule\n")
    consumer = factory.create("consumer", targets=("claude", "copilot"))
    factory.add_relative_dependency(consumer, kit)
    runner = ApmLifecycleRunner((str(apm_binary_path),))

    def apm(*args: str) -> str:
        result = runner.run(args, cwd=consumer.root, env=env)
        assert result.returncode == 0, result.stdout + result.stderr
        for stream in (result.stdout, result.stderr):
            assert stream.isascii(), stream
            status = [line for line in stream.splitlines() if line.startswith("[")]
            assert len(status) == len(set(status)), stream
        return result.stdout

    apm("install")
    if operation == "uninstall":
        command = ("uninstall", "../named-kit")
    elif operation == "prune":
        assert factory.remove_apm_dependency(consumer, {"path": "../named-kit"})
        command = ("prune",)
    else:
        factory.set_targets(consumer, ("copilot",))
        command = ("install",)
    before = ArtifactSnapshot.capture(consumer.root)

    stdout = apm(*command)

    after = ArtifactSnapshot.capture(consumer.root)
    listed = _assert_each_removal_named(before, after, stdout)
    assert ".claude/skills/named/" in listed, stdout
    assert ".claude/rules/named.md" in listed, stdout
    assert not (consumer.root / ".claude/skills/named").exists()
    if operation == "target-removal":
        assert (consumer.root / ".agents/skills/named/SKILL.md").is_file()
    else:
        assert ".agents/skills/named/" in listed, stdout
