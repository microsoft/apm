"""Drift replay served from render records never changes an audit verdict (#4940).

A package whose deployed files are byte copies of its sources is replayed from
its render record; every other package (here, hooks) replays live. The audit
output and exit code must be identical with records on and with
``APM_AUDIT_RECORDS=0``, on a clean project and after a hand edit.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.integration._install_audit_replay_kit import (
    ReplayWorld,
    apm_package,
    hooks_package,
    write,
)
from tests.lifecycle.known import KnownFailure
from tests.utils.apm_lifecycle_runner import CommandResult

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.promise("P-OWN-ci-audit-scratch-materialization"),
]


def _verbatim_package(root: Path, name: str) -> None:
    """Instructions, an agent, a prompt and a skill whose deployed files are byte copies."""
    write(root / "apm.yml", f"name: {name}\nversion: 1.0.0\n")
    write(root / ".apm/instructions/style.instructions.md", '---\napplyTo: "**"\n---\nBe brief.\n')
    write(
        root / ".apm/agents/plain-rev.agent.md",
        "---\nname: plain-rev\ndescription: Reviewer\n---\nReview.\n",
    )
    write(root / ".apm/prompts/plain-hello.prompt.md", "---\ndescription: Hello\n---\nSay hello.\n")
    write(
        root / ".apm/skills/plain-helper/SKILL.md",
        "---\nname: plain-helper\ndescription: Help\n---\nHelp.\n",
    )
    write(root / ".apm/skills/plain-helper/references/ref.md", "Ref.\n")


_SUMMARY = re.compile(r"^\[i\] drift replay: (\d+) package\(s\) from records, (\d+) live$", re.M)


def _audit(world: ReplayWorld, *args: str, records: bool) -> CommandResult:
    env = dict(world.env)
    env["APM_AUDIT_RECORDS"] = "1" if records else "0"
    return world.runner.run(("audit", *args), cwd=world.project, env=env)


def _same_verdict(world: ReplayWorld, *args: str) -> tuple[CommandResult, CommandResult]:
    served = _audit(world, *args, records=True)
    live = _audit(world, *args, records=False)
    evidence = f"served={served.stdout!r} {served.stderr!r}\nlive={live.stdout!r} {live.stderr!r}"
    assert served.returncode == live.returncode, evidence
    assert served.stdout == live.stdout, evidence
    # Default output is unchanged by records: the served/live line is --verbose only.
    assert _SUMMARY.search(served.stderr) is None, evidence
    assert _SUMMARY.search(live.stderr) is None, evidence
    assert served.stderr == live.stderr, evidence
    return served, live


def _replay_counts(world: ReplayWorld, *, records: bool) -> tuple[str, str]:
    """(served, live) package counts from the --verbose drift replay summary."""
    result = _audit(world, "--ci", "--no-fail-fast", "--verbose", records=records)
    found = _SUMMARY.search(result.stderr)
    assert found is not None, result.stdout + result.stderr
    return found.groups()


def test_record_served_replay_gives_the_live_verdict(tmp_path: Path, apm_binary_path: Path) -> None:
    world = ReplayWorld.create(tmp_path, apm_binary_path)
    # A verbatim package (served), a linking one (rendered, live) and hooks (live).
    deps = [
        world.publish("plain", _verbatim_package),
        world.publish("pkg", apm_package),
        world.publish("hk", hooks_package),
    ]
    listed = "".join(f"    - {dep}\n" for dep in deps)
    write(
        world.project / "apm.yml",
        f"name: proj\nversion: 1.0.0\ntargets: [copilot]\ndependencies:\n  apm:\n{listed}",
    )
    world.apm("install")

    served, _live = _same_verdict(world, "--ci", "--no-fail-fast")
    assert served.returncode == 0, served.stdout + served.stderr
    assert _replay_counts(world, records=True) == ("1", "2")
    assert _replay_counts(world, records=False) == ("0", "3")
    _same_verdict(world)

    edited = world.project / ".github" / "instructions" / "style.instructions.md"
    edited.write_text(edited.read_text(encoding="ascii") + "Hand edit.\n", encoding="ascii")

    served, _live = _same_verdict(world, "--ci", "--no-fail-fast")
    assert served.returncode != 0
    assert "style.instructions.md" in served.stdout
    _same_verdict(world)


def _clashing_hooks_package(root: Path, name: str) -> None:
    """A live (hooks) package that also deploys the served package's instruction file."""
    hooks_package(root, name)
    write(root / ".apm/instructions/style.instructions.md", '---\napplyTo: "**"\n---\nOther.\n')


def test_a_live_package_rendering_onto_a_served_file_replays_all_live(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world = ReplayWorld.create(tmp_path, apm_binary_path)
    # Replayed after "plain" (lock order), so the live package renders over a served file.
    deps = [
        world.publish("plain", _verbatim_package),
        world.publish("zclash", _clashing_hooks_package),
    ]
    listed = "".join(f"    - {dep}\n" for dep in deps)
    write(
        world.project / "apm.yml",
        f"name: proj\nversion: 1.0.0\ntargets: [copilot]\ndependencies:\n  apm:\n{listed}",
    )
    world.apm("install", expect=None)

    served, live = _same_verdict(world, "--ci", "--no-fail-fast")
    assert served.returncode == live.returncode
    assert _SUMMARY.search(live.stderr) is None
    # The clash sent the whole replay live: nothing is reported as served.
    assert _replay_counts(world, records=True) == ("0", "2")
    assert _replay_counts(world, records=False) == ("0", "2")
    _same_verdict(world)


@pytest.mark.parametrize(
    "prompt",
    [
        b"---\ndescription: Odd\n---\nBare\rreturn.\n",
        b"---\ndescription: Odd\n---\nNot \xff utf-8.\n",
    ],
    ids=["bare-cr", "not-utf8"],
)
def test_an_adopted_prompt_a_text_read_changes_gives_the_live_verdict(
    tmp_path: Path, apm_binary_path: Path, prompt: bytes
) -> None:
    world = ReplayWorld.create(tmp_path, apm_binary_path)

    def package(root: Path, name: str) -> None:
        _verbatim_package(root, name)
        (root / ".apm/prompts/odd.prompt.md").write_bytes(prompt)

    dep = world.publish("odd", package)
    # The deployed prompt already holds the source bytes, so install adopts it unchanged.
    existing = world.project / ".github" / "prompts" / "odd.prompt.md"
    existing.parent.mkdir(parents=True, exist_ok=True)
    existing.write_bytes(prompt)
    write(
        world.project / "apm.yml",
        f"name: proj\nversion: 1.0.0\ntargets: [copilot]\ndependencies:\n  apm:\n    - {dep}\n",
    )
    world.apm("install", expect=None)

    served, live = _same_verdict(world, "--ci", "--no-fail-fast")
    assert served.returncode == live.returncode
    _same_verdict(world)


# -- adversarial deployed destinations (#5151 cut hunt: link, path, encoding classes) --

_STYLE = Path(".github/instructions/style.instructions.md")


def _plain_world(tmp_path: Path, binary: Path, package=_verbatim_package) -> ReplayWorld:
    world = ReplayWorld.create(tmp_path, binary)
    dep = world.publish("plain", package)
    write(
        world.project / "apm.yml",
        f"name: proj\nversion: 1.0.0\ntargets: [copilot]\ndependencies:\n  apm:\n    - {dep}\n",
    )
    world.apm("install")
    return world


def _same_bytes_elsewhere(project: Path, target: Path) -> None:
    copy = project.parent / "elsewhere.md"
    copy.write_bytes(target.read_bytes())
    target.unlink()
    target.symlink_to(copy)


def _dangling(project: Path, target: Path) -> None:
    target.unlink()
    target.symlink_to(project.parent / "missing.md")


def _directory(project: Path, target: Path) -> None:
    target.unlink()
    target.mkdir()


def _linked_parent(project: Path, target: Path) -> None:
    parent = target.parent
    copy = project.parent / "instructions-copy"
    shutil.copytree(parent, copy)
    shutil.rmtree(parent)
    parent.symlink_to(copy, target_is_directory=True)


_ADVERSARIAL = {
    "link-to-same-bytes": (_same_bytes_elsewhere, None),
    "dangling-link": (_dangling, True),
    "directory": (_directory, True),
    "linked-parent-dir": (_linked_parent, None),
}


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX links")
@pytest.mark.parametrize("kind", sorted(_ADVERSARIAL))
def test_an_adversarial_deployed_destination_gives_the_live_verdict(
    tmp_path: Path, apm_binary_path: Path, kind: str
) -> None:
    """Whatever stands at a served file's deployed path (a link, a dangling link,
    a directory, a linked parent), the record-served audit gives the live verdict,
    finishes, and prints ASCII only; a destination that cannot hold the deployed bytes
    fails the audit (#5151)."""
    world = _plain_world(tmp_path, apm_binary_path)
    tamper, must_fail = _ADVERSARIAL[kind]
    tamper(world.project, world.project / _STYLE)

    served, _live = _same_verdict(world, "--ci", "--no-fail-fast")
    output = served.stdout + served.stderr
    assert output.isascii(), output
    if must_fail:
        assert served.returncode != 0, output
        assert "style.instructions.md" in output, output


_NEWLINE_SPLITS_THE_ENTRY = KnownFailure(
    "#5279",
    "the drift list prints a file name's newline verbatim",
    r"\n\s*\[\+\] all checks passed\.md  \[test/plain\]",
)


@pytest.mark.parametrize(
    "file_name",
    [
        pytest.param("r\u00e9f\u00e9rence.md", id="non-ascii"),
        pytest.param(
            "line\n[+] all checks passed.md",
            id="newline",
            marks=[
                pytest.mark.skipif(sys.platform == "win32", reason="POSIX file names"),
                _NEWLINE_SPLITS_THE_ENTRY.mark(),
            ],
        ),
    ],
)
def test_an_edited_file_with_an_unusual_name_is_reported_on_one_ascii_line(
    tmp_path: Path, apm_binary_path: Path, file_name: str
) -> None:
    """A served skill file named with non-ASCII text or a newline, then hand-edited, gets
    the live verdict, in ASCII, and its name cannot start a verdict line (#5151)."""

    def package(root: Path, name: str) -> None:
        _verbatim_package(root, name)
        write(root / ".apm/skills/plain-helper/references" / file_name, "Ref.\n")

    world = _plain_world(tmp_path, apm_binary_path, package)
    (deployed,) = [
        path
        for path in world.project.rglob("plain-helper/references/*")
        if path.name == file_name and "apm_modules" not in path.parts
    ]
    deployed.write_text(deployed.read_text(encoding="utf-8") + "Edit.\n", encoding="utf-8")

    served, _live = _same_verdict(world, "--ci", "--no-fail-fast")
    output = served.stdout + served.stderr
    assert served.returncode != 0, output
    assert output.isascii(), output
    assert not any(line.startswith("[+] all checks passed") for line in output.splitlines()), output


_FIFO_BLOCKS_THE_AUDIT = KnownFailure(
    "#5280",
    "audit --ci blocks reading a FIFO at a deployed path",
    r"timed out after",
    raises=subprocess.TimeoutExpired,
)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX FIFOs")
@_FIFO_BLOCKS_THE_AUDIT.mark()
def test_a_fifo_at_a_deployed_path_fails_the_audit_without_blocking(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """A FIFO standing at a served file's path is drift: the audit finishes, fails and
    names the path, instead of blocking on the read (#5151)."""
    world = _plain_world(tmp_path, apm_binary_path)
    target = world.project / _STYLE
    target.unlink()
    os.mkfifo(target)

    result = subprocess.run(
        (str(apm_binary_path), "audit", "--ci", "--no-fail-fast"),
        cwd=world.project,
        env=world.env,
        capture_output=True,
        text=True,
        timeout=60,
    )

    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert "style.instructions.md" in output, output
    assert output.isascii(), output
