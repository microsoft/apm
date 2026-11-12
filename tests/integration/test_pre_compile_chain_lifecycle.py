"""The ``pre-compile`` lifecycle (#3596) composes with compile determinism (#3379),
generated-output placement (#3539) and umask-derived modes (#3547) (#3649)."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from tests.integration._target_compile_kit import Scenario, output, tree_digest, write

pytestmark = [pytest.mark.integration, pytest.mark.requires_apm_binary]

_SCRIPT = """#!/bin/sh
cat > /dev/null
echo ran >> "$PWD/ran.log"
mkdir -p .apm/instructions
printf -- '---\\ndescription: gen\\napplyTo: "src/**/*.py"\\n---\\nGenerated rule.\\n' \\
  > .apm/instructions/gen.instructions.md
exit "${GEN_EXIT:-0}"
"""

_COMPILED = ("AGENTS.md", "CLAUDE.md", "GEMINI.md")


def _sparse_tree(project: Path) -> None:
    """Sparse subtrees so distributed placement has real decisions to make."""
    for module in range(5):
        for sub in range(4):
            if (module + sub) % 3 == 0:
                write(project / f"src/mod{module}/sub{sub}/f.py", "x = 1\n")
            if sub == 2:
                write(project / f"src/mod{module}/sub{sub}/deep/leaf/a.ts", "let a = 1\n")
    write(project / "docs/guide/index.md", "# Guide\n")
    write(project / "web/app/component.tsx", "x\n")
    for stem, apply_to in {
        "all": "**",
        "ts": "src/**/*.ts",
        "mod1": "src/mod1/**",
        "deep": "src/mod2/sub2/deep/**",
        "docs": "docs/**/*.md",
        "multi": "web/**/*.tsx,docs/**",
        "agentsmd": "**/AGENTS.md",
    }.items():
        write(
            project / f".apm/instructions/{stem}.instructions.md",
            f'---\ndescription: {stem}\napplyTo: "{apply_to}"\n---\nRule {stem}.\n',
        )


def _compiled(project: Path) -> dict[str, str]:
    return {
        path: digest
        for path, digest in tree_digest(project).items()
        if path.rsplit("/", 1)[-1] in _COMPILED
    }


def _ran(project: Path) -> int:
    log = project / "ran.log"
    return len(log.read_text("ascii").splitlines()) if log.exists() else 0


def test_pre_compile_trust_generation_and_suppressed_modes(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    scenario = Scenario.create(tmp_path, apm_binary_path)
    project = scenario.isolated.work_root / "proj"
    _sparse_tree(project)
    write(
        project / "apm.yml",
        "name: proj\nversion: 1.0.0\ntargets: [copilot, claude, codex, gemini]\n"
        'lifecycle:\n  pre-compile:\n    - type: command\n      run: "./scripts/gen.sh"\n',
    )
    write(project / "scripts/gen.sh", _SCRIPT)
    (project / "scripts/gen.sh").chmod(0o755)

    # Untrusted: skipped with guidance, compile still runs on existing sources.
    untrusted = scenario.run("compile", cwd=project)
    assert "apm lifecycle trust" in output(untrusted)
    assert _ran(project) == 0
    assert not (project / ".apm/instructions/gen.instructions.md").exists()
    baseline = _compiled(project)
    assert "AGENTS.md" in baseline

    scenario.run("lifecycle", "trust", cwd=project)

    # Modes documented not to fire pre-compile, each leaving outputs untouched.
    root_out = scenario.isolated.work_root / "root-out"
    root_out.mkdir()
    for args, env in (
        (("compile", "--dry-run"), None),
        (("compile", "--validate"), None),
        (("compile", "--root", str(root_out)), None),
        (("compile",), scenario.isolated.subprocess_env(overrides={"APM_NO_SCRIPTS": "1"})),
    ):
        scenario.run(*args, cwd=project, env=env)
        assert _ran(project) == 0, args
        assert _compiled(project) == baseline, args

    # Trusted: the script runs once and compile reads what it generated in the same run.
    scenario.run("compile", cwd=project)
    assert _ran(project) == 1
    agents = "".join(
        (project / path).read_text("ascii")
        for path in _compiled(project)
        if path.endswith("AGENTS.md")
    )
    assert "Generated rule." in agents
    first = _compiled(project)

    # Deterministic: a second trusted compile changes no compiled byte.
    scenario.run("compile", cwd=project)
    assert _ran(project) == 2
    assert _compiled(project) == first

    # A failing script is nonfatal; the warning names event, exit code and scripts.log.
    failing = scenario.run(
        "compile", cwd=project, env=scenario.isolated.subprocess_env(overrides={"GEN_EXIT": "3"})
    )
    text = output(failing)
    assert "pre-compile" in text and "code 3" in text and "scripts.log" in text, text
    assert _ran(project) == 3
    assert _compiled(project) == first

    # Editing the lifecycle block revokes trust.
    manifest = (project / "apm.yml").read_text("ascii")
    write(project / "apm.yml", manifest.replace("./scripts/gen.sh", "sh ./scripts/gen.sh"))
    scenario.run("compile", cwd=project)
    assert _ran(project) == 3


def test_user_pre_compile_does_not_fire_for_global_compile(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    scenario = Scenario.create(tmp_path, apm_binary_path)
    package = scenario.isolated.package_root / "pkg"
    write(package / "apm.yml", "name: pkg\nversion: 1.0.0\n")
    write(
        package / ".apm/instructions/g.instructions.md",
        "---\ndescription: g\n---\nGlobal.\n",
    )
    cwd = scenario.isolated.work_root / "cwd"
    cwd.mkdir()
    scenario.run("install", "-g", str(package), "--target", "codex", cwd=cwd)
    marker = scenario.isolated.root / "user-ran.log"
    user_manifest = scenario.isolated.config_root / "apm.yml"
    user_manifest.write_text(
        user_manifest.read_text("ascii")
        + f'lifecycle:\n  pre-compile:\n    - type: command\n      run: "echo ran >> {marker}"\n',
        encoding="ascii",
    )
    scenario.run("compile", "-g", cwd=cwd)
    assert not marker.exists()
    assert "Global." in (scenario.isolated.home / ".codex/AGENTS.md").read_text("ascii")


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_compiled_outputs_follow_umask_and_keep_existing_modes(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    scenario = Scenario.create(tmp_path, apm_binary_path)
    project = scenario.isolated.work_root / "proj"
    _sparse_tree(project)
    write(project / "apm.yml", "name: proj\nversion: 1.0.0\ntargets: [claude, codex]\n")

    def compile_with(umask: int) -> None:
        previous = os.umask(umask)
        try:
            scenario.run("compile", cwd=project)
        finally:
            os.umask(previous)

    compile_with(0o077)
    outputs = sorted(_compiled(project))
    assert outputs
    for relative in outputs:
        assert stat.S_IMODE((project / relative).stat().st_mode) == 0o600, relative

    (project / "AGENTS.md").chmod(0o640)
    write(project / ".apm/instructions/all.instructions.md", '---\napplyTo: "**"\n---\nNew.\n')
    compile_with(0o022)
    assert "New." in (project / "AGENTS.md").read_text("ascii")
    assert stat.S_IMODE((project / "AGENTS.md").stat().st_mode) == 0o640
