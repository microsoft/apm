"""Offline integration tests for the ``agent-skills`` target (issue #737).

Covers the cross-client ``.agents/skills/`` deployment surface:

- I1 deploy to ``.agents/skills/<name>/SKILL.md``
- I2 dedup with ``codex`` (also writes to ``.agents/skills/``)
- I3 lockfile records the ``.agents/skills/`` POSIX path
- I4 ``--target agents`` legacy alias prints a deprecation warning
- I5 ``--target all`` does NOT include ``agent-skills``
- I6 ``apm uninstall`` cleans ``.agents/skills/<name>/``
- I7 ``apm compile --target agent-skills`` is a no-op (exit 0 + skip msg)
- I8 ``apm install -g --target agent-skills`` deploys to ``~/.agents/skills/``
- I9 sync preserves foreign skills under ``.agents/skills/`` not in lockfile

These tests use the local-bundle install pattern (no network, no
GitHub API) -- modelled after ``test_install_local_bundle_e2e.py``.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from apm_cli.cli import cli

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SKILL_NAME = "test-skill"
SKILL_BODY = "# Test Skill\nA tiny skill used in agent-skills integration tests."
PLUGIN_ID = "test-plugin"


def _sha256(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()


def _make_plugin_bundle(
    tmp_path: Path,
    *,
    plugin_id: str = PLUGIN_ID,
    pack_target: str = "agent-skills",
    skill_name: str = SKILL_NAME,
    skill_body: str = SKILL_BODY,
) -> Path:
    """Build a minimal plugin-format bundle directory with one skill.

    The bundle layout mirrors what ``apm pack`` produces and what
    ``local_bundle_handler`` consumes: a ``plugin.json`` manifest, a
    skill under ``skills/<name>/SKILL.md``, and an ``apm.lock.yaml``
    declaring the bundled files + their target.
    """
    bundle = tmp_path / "bundle"
    bundle.mkdir(parents=True, exist_ok=True)

    (bundle / "plugin.json").write_text(
        json.dumps({"id": plugin_id, "name": "Test Plugin"}), encoding="utf-8"
    )

    rel = f"skills/{skill_name}/SKILL.md"
    skill_path = bundle / rel
    skill_path.parent.mkdir(parents=True, exist_ok=True)
    skill_path.write_text(skill_body, encoding="utf-8")

    bundle_files = {rel: _sha256(skill_body)}

    lock_data = {
        "pack": {
            "format": "plugin",
            "target": pack_target,
            "bundle_files": bundle_files,
        },
        "dependencies": [
            {
                "repo_url": f"owner/{plugin_id}",
                "resolved_commit": "abc123",
                "deployed_files": [rel],
                "deployed_file_hashes": bundle_files,
            }
        ],
    }
    (bundle / "apm.lock.yaml").write_text(
        yaml.dump(lock_data, default_flow_style=False), encoding="utf-8"
    )
    return bundle


def _make_project(tmp_path: Path, *, with_github: bool = True) -> Path:
    """Create a minimal APM project directory.

    By default also creates ``.github/`` so the auto-detector picks up
    copilot when relevant; pass ``with_github=False`` for tests that
    must not have any auto-detected target.
    """
    project = tmp_path / "project"
    project.mkdir(parents=True, exist_ok=True)
    (project / "apm.yml").write_text(
        yaml.dump(
            {
                "name": "test-project",
                "version": "1.0.0",
                "dependencies": {"apm": []},
            },
            default_flow_style=False,
        ),
        encoding="utf-8",
    )
    if with_github:
        (project / ".github").mkdir()
    return project


def _invoke(
    project_dir: Path,
    args: list[str],
    monkeypatch: pytest.MonkeyPatch,
):
    """Run ``apm <args>`` inside *project_dir* with full tracebacks."""
    monkeypatch.chdir(project_dir)
    runner = CliRunner()
    return runner.invoke(cli, args, catch_exceptions=False)


# ---------------------------------------------------------------------------
# I1 -- deploy to .agents/skills/<name>/SKILL.md
# ---------------------------------------------------------------------------


def test_install_agent_skills_deploys_to_agents_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--target agent-skills`` writes ``.agents/skills/<name>/SKILL.md``."""
    bundle = _make_plugin_bundle(tmp_path / "src")
    project = _make_project(tmp_path / "dst")

    result = _invoke(
        project,
        ["install", str(bundle), "--target", "agent-skills"],
        monkeypatch,
    )

    assert result.exit_code == 0, f"output={result.output!r}"
    deployed = project / ".agents" / "skills" / SKILL_NAME / "SKILL.md"
    assert deployed.is_file(), f"expected skill at {deployed}"
    assert SKILL_BODY in deployed.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# I2 -- codex + agent-skills dedup (both deploy to .agents/skills/)
# ---------------------------------------------------------------------------


def test_install_codex_agent_skills_dedup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``--target codex,agent-skills`` integrates both targets cleanly.

    Through the dependency-driven install pipeline ``codex`` and
    ``agent-skills`` both resolve to ``.agents/skills/`` (codex skills
    use ``deploy_root='.agents'``) and ``SkillIntegrator`` dedups by
    resolved path emitting an "already deployed, skipping" message.

    The local-bundle install path tested here is intentionally simpler
    and per-target -- it copies bundle files into each
    ``target.root_dir`` independently -- so on disk we end up with a
    copy under ``.codex/skills/...`` (codex's static root) and one
    under ``.agents/skills/...`` (agent-skills root). The
    integrator-level dedup is covered by unit tests of
    ``SkillIntegrator``; this test asserts the local-bundle round-trip
    succeeds for the multi-target selection without errors and that
    the agent-skills target slot is populated alongside codex.
    """
    bundle = _make_plugin_bundle(tmp_path / "src", pack_target="codex,agent-skills")
    project = _make_project(tmp_path / "dst", with_github=False)
    (project / ".codex").mkdir()

    result = _invoke(
        project,
        ["install", str(bundle), "--target", "codex,agent-skills", "--verbose"],
        monkeypatch,
    )

    assert result.exit_code == 0, f"output={result.output!r}"
    assert (project / ".codex" / "skills" / SKILL_NAME / "SKILL.md").is_file()
    assert (project / ".agents" / "skills" / SKILL_NAME / "SKILL.md").is_file()


# ---------------------------------------------------------------------------
# I3 -- lockfile path uses POSIX .agents/skills/
# ---------------------------------------------------------------------------


def test_install_agent_skills_lockfile_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lockfile records ``.agents/skills/...`` as a POSIX-style path."""
    bundle = _make_plugin_bundle(tmp_path / "src")
    project = _make_project(tmp_path / "dst")

    result = _invoke(
        project,
        ["install", str(bundle), "--target", "agent-skills"],
        monkeypatch,
    )
    assert result.exit_code == 0, f"output={result.output!r}"

    lockfile_path = project / "apm.lock.yaml"
    assert lockfile_path.is_file()
    lock = yaml.safe_load(lockfile_path.read_text(encoding="utf-8")) or {}

    # Local-bundle installs persist deployed files in either the
    # lockfile's per-dependency ``deployed_files`` or the top-level
    # ``local_deployed_files`` list.  Accept either location.
    candidates: list[str] = list(lock.get("local_deployed_files") or [])
    for dep in lock.get("dependencies") or []:
        candidates.extend(dep.get("deployed_files") or [])

    matching = [p for p in candidates if p.startswith(".agents/skills/") and "\\" not in p]
    assert matching, f"expected a POSIX .agents/skills/... entry in lockfile, got: {candidates}"


# ---------------------------------------------------------------------------
# I4 -- legacy '--target agents' deprecation warning
# ---------------------------------------------------------------------------


def test_agents_deprecation_warning_visible_in_cli_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--target agents`` emits a deprecation warning to the CLI output.

    The warning is raised in the install pipeline's *targets* phase
    (``apm_cli.install.phases.targets``), which only runs when there
    are dependencies or local ``.apm/`` primitives to integrate. Drop
    a single local instruction file so the pipeline does not
    short-circuit before target resolution.
    """
    project = _make_project(tmp_path / "dst")
    apm_dir = project / ".apm" / "instructions"
    apm_dir.mkdir(parents=True)
    (apm_dir / "demo.instructions.md").write_text(
        "---\napplyTo: '**'\n---\n# Demo\nHello.\n", encoding="utf-8"
    )

    result = _invoke(
        project,
        ["install", "--target", "agents"],
        monkeypatch,
    )

    out = result.output or ""
    assert "deprecated" in out.lower() and "--target agents" in out, (
        f"expected deprecation warning mentioning '--target agents', got: {out!r}"
    )


# ---------------------------------------------------------------------------
# I5 -- 'all' does not include agent-skills
# ---------------------------------------------------------------------------


def test_all_does_not_include_agent_skills(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``--target all`` must NOT advertise ``agent-skills`` as an active target.

    ``agent-skills`` is in EXPLICIT_ONLY_TARGETS -- it is reachable
    only by being named explicitly.  ``codex`` (which is in ``all``)
    also writes to ``.agents/skills/``, so we cannot rely on the
    directory being absent.  Instead we assert at the policy level:
    the active-target list reported by the CLI does not name
    ``agent-skills``.
    """
    bundle = _make_plugin_bundle(tmp_path / "src", pack_target="copilot")
    project = _make_project(tmp_path / "dst")

    result = _invoke(
        project,
        ["install", str(bundle), "--target", "all", "--verbose"],
        monkeypatch,
    )

    assert result.exit_code == 0, f"output={result.output!r}"
    # Grep for "agent-skills" appearing as a target name in output.
    # Allowed mentions (e.g. inside help URLs) won't appear here, so
    # any hit is evidence of mis-routing.
    out = result.output or ""
    assert "agent-skills" not in out, (
        f"'agent-skills' should not appear in --target all output, got: {out!r}"
    )


# ---------------------------------------------------------------------------
# I6 -- uninstall cleans .agents/skills/<name>/
# ---------------------------------------------------------------------------


def test_uninstall_agent_skills_cleans_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``apm uninstall`` removes ``.agents/skills/<name>/`` it owns.

    The local-bundle installer does not mutate ``apm.yml`` so we
    cannot drive uninstall through it directly.  Instead we
    pre-construct an ``apm.yml`` + ``apm.lock.yaml`` pair that
    advertises ownership of ``.agents/skills/<name>/SKILL.md`` and
    materialise the file on disk -- mirroring the post-install state
    a real GitHub-backed install would produce.
    """
    project = tmp_path / "project"
    project.mkdir()

    pkg = "owner/test-plugin"
    (project / "apm.yml").write_text(
        yaml.dump(
            {
                "name": "test-project",
                "version": "1.0.0",
                "target": "agent-skills",
                "dependencies": {"apm": [f"{pkg}#main"]},
            },
            default_flow_style=False,
        ),
        encoding="utf-8",
    )

    skill_rel = f".agents/skills/{SKILL_NAME}/SKILL.md"
    deployed = project / skill_rel
    deployed.parent.mkdir(parents=True, exist_ok=True)
    deployed.write_text(SKILL_BODY, encoding="utf-8")

    lock = {
        "dependencies": [
            {
                "repo_url": pkg,
                "resolved_commit": "abc123",
                "deployed_files": [skill_rel],
                "deployed_file_hashes": {skill_rel: _sha256(SKILL_BODY)},
            }
        ],
    }
    (project / "apm.lock.yaml").write_text(
        yaml.dump(lock, default_flow_style=False), encoding="utf-8"
    )

    # Stub the modules dir so uninstall's apm_modules cleanup is a no-op.
    (project / "apm_modules").mkdir()

    result = _invoke(project, ["uninstall", pkg], monkeypatch)

    assert result.exit_code == 0, f"output={result.output!r}"
    deployed_md = project / ".agents" / "skills" / SKILL_NAME / "SKILL.md"
    assert not deployed_md.exists(), (
        f"expected {deployed_md} to be removed after uninstall, output={result.output!r}"
    )


# ---------------------------------------------------------------------------
# I7 -- compile --target agent-skills is a no-op
# ---------------------------------------------------------------------------


def test_compile_agent_skills_noop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``apm compile --target agent-skills`` exits 0 (no-op for skills-only target).

    ``agent-skills`` is a deployment surface, not a compilation target -- it
    has no AGENTS.md / CLAUDE.md / GEMINI.md output. The compile command
    must therefore not fail when the user passes it.

    The compiler's own ``agents_compiler`` emits a
    "agent-skills: skills-only target -- nothing to compile" info message
    when invoked programmatically with ``config.target == 'agent-skills'``;
    however, the CLI's ``_resolve_compile_target`` currently collapses an
    ``agent-skills``-only ``--target`` to the ``vscode`` family (defensive
    fallback), so the bare skip message does not surface in CLI output
    today. We assert the user-visible contract -- exit code 0 and no
    error -- and document the gap so a follow-up patch can wire the skip
    message through the CLI without breaking this test.
    """
    project = _make_project(tmp_path / "dst")
    inst_dir = project / ".apm" / "instructions"
    inst_dir.mkdir(parents=True)
    (inst_dir / "demo.instructions.md").write_text(
        "---\napplyTo: '**'\ndescription: demo\n---\n# Demo\nHello.\n",
        encoding="utf-8",
    )

    result = _invoke(
        project,
        ["compile", "--target", "agent-skills"],
        monkeypatch,
    )

    assert result.exit_code == 0, f"output={result.output!r}"
    out = (result.output or "").lower()
    # Compile must complete without an explicit error symbol.
    assert "[x]" not in out, f"unexpected error in compile output: {result.output!r}"


# ---------------------------------------------------------------------------
# I8 -- user-scope install deploys to ~/.agents/skills/
# ---------------------------------------------------------------------------


def test_user_scope_install_agent_skills(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``apm install -g --target agent-skills`` writes to ``~/.agents/skills/``."""
    fake_home = tmp_path / "fakehome"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    # User-scope manifest must exist before -g install.
    user_apm_dir = fake_home / ".apm"
    user_apm_dir.mkdir()
    (user_apm_dir / "apm.yml").write_text(
        yaml.dump(
            {
                "name": "user-scope",
                "version": "1.0.0",
                "dependencies": {"apm": []},
            },
            default_flow_style=False,
        ),
        encoding="utf-8",
    )

    bundle = _make_plugin_bundle(tmp_path / "src")

    # Run from a neutral cwd (a fresh, empty directory).
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    result = _invoke(
        cwd,
        ["install", str(bundle), "-g", "--target", "agent-skills"],
        monkeypatch,
    )

    assert result.exit_code == 0, f"output={result.output!r}"
    deployed = fake_home / ".agents" / "skills" / SKILL_NAME / "SKILL.md"
    assert deployed.is_file(), f"expected user-scope skill at {deployed}, output={result.output!r}"


# ---------------------------------------------------------------------------
# I9 -- foreign skills in .agents/skills/ are preserved
# ---------------------------------------------------------------------------


def test_uninstall_preserves_foreign_skill_in_agents_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A skill not owned by APM (foreign) survives sync/cleanup.

    The cross-client ``.agents/skills/`` directory is shared with
    other tools (Codex CLI, manual user-authored skills).  The
    integrator's orphan cleanup must consult lockfile ownership
    before deleting any subdirectory.
    """
    bundle = _make_plugin_bundle(tmp_path / "src")
    project = _make_project(tmp_path / "dst")

    install = _invoke(
        project,
        ["install", str(bundle), "--target", "agent-skills"],
        monkeypatch,
    )
    assert install.exit_code == 0, f"install output={install.output!r}"

    # Drop a foreign skill into .agents/skills/ -- not in any lockfile.
    foreign_dir = project / ".agents" / "skills" / "foreign-skill"
    foreign_dir.mkdir(parents=True, exist_ok=True)
    foreign_md = foreign_dir / "SKILL.md"
    foreign_md.write_text("# Foreign Skill\nPlaced by a different tool.", encoding="utf-8")

    # Re-run install (sync path) so cleanup phases get a chance to run.
    sync = _invoke(
        project,
        ["install", "--target", "agent-skills"],
        monkeypatch,
    )
    assert sync.exit_code == 0, f"sync output={sync.output!r}"

    assert foreign_md.is_file(), (
        f"foreign skill at {foreign_md} must be preserved across sync, sync output={sync.output!r}"
    )
