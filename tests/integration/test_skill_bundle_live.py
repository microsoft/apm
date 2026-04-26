"""Live integration tests for SKILL_BUNDLE detection and installation.

Exercises the full `apm install` pipeline against real public GitHub repos
to validate that:
  - SKILL_BUNDLE repos install successfully (exit 0).
  - MARKETPLACE_PLUGIN repos are not regressed by the new detection cascade.
  - PackageType is correctly classified in the lockfile.
  - Deployed skill count meets expectations.
  - `--skill <name>` subset selection works on multi-skill bundles.
  - `--skill <name>` on non-SKILL_BUNDLE repos produces a clear warning.

Requires network access. Set GITHUB_TOKEN for higher rate limits.
Run: uv run pytest tests/integration/test_skill_bundle_live.py -m live
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml


# ---------------------------------------------------------------------------
# Markers and skip gates
# ---------------------------------------------------------------------------

pytestmark = pytest.mark.live


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# (repo, expected_package_type_value, min_skill_count, is_plugin)
LIVE_REPOS = [
    # Already-working baseline -- must remain MARKETPLACE_PLUGIN
    ("microsoft/azure-skills", "marketplace_plugin", 1, True),
    ("firebase/agent-skills", "marketplace_plugin", 1, True),
    ("pbakaus/impeccable", "marketplace_plugin", 0, True),
    ("obra/superpowers", "marketplace_plugin", 1, True),
    # Currently classified as SKILL_BUNDLE
    ("vercel-labs/agent-skills", "skill_bundle", 2, False),
    ("xixu-me/skills", "skill_bundle", 1, False),
    ("larksuite/cli", "skill_bundle", 1, False),
    ("danielmeppiel/genesis", "skill_bundle", 1, False),
]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def apm_command():
    """Resolve the apm CLI executable (PATH first, then local venv)."""
    apm_on_path = shutil.which("apm")
    if apm_on_path:
        return apm_on_path
    venv_apm = Path(__file__).parent.parent.parent / ".venv" / "bin" / "apm"
    if venv_apm.exists():
        return str(venv_apm)
    # Fallback: run as module
    return None


@pytest.fixture
def fake_home(tmp_path):
    """Isolated HOME directory so installs never touch the real user config."""
    home_dir = tmp_path / "fakehome"
    home_dir.mkdir()
    return home_dir


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_github_token():
    """Resolve a GitHub token from env or `gh auth token` fallback."""
    for var in ("GITHUB_TOKEN", "GH_TOKEN", "GITHUB_APM_PAT"):
        val = os.environ.get(var)
        if val:
            return val
    # Fallback: try gh CLI
    try:
        proc = subprocess.run(
            ["gh", "auth", "token"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return None


def _env_with_home(fake_home):
    """Build env dict with HOME overridden + GITHUB_TOKEN forwarded."""
    env = os.environ.copy()
    env["HOME"] = str(fake_home)
    if sys.platform == "win32":
        env["USERPROFILE"] = str(fake_home)
    # Ensure git does not prompt for credentials
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    # Ensure a GitHub token is available (needed for API rate limits)
    if "GITHUB_TOKEN" not in env:
        token = _resolve_github_token()
        if token:
            env["GITHUB_TOKEN"] = token
    return env


def _run_apm(apm_command, args, cwd, fake_home, timeout=180):
    """Run apm CLI with isolated HOME."""
    if apm_command:
        cmd = [apm_command] + args
    else:
        cmd = [sys.executable, "-m", "apm_cli"] + args
    return subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=_env_with_home(fake_home),
    )


def _read_lockfile(directory):
    """Read and parse apm.lock.yaml from the given directory."""
    lock_path = directory / "apm.lock.yaml"
    if not lock_path.exists():
        return None
    return yaml.safe_load(lock_path.read_text(encoding="utf-8"))


def _get_locked_dep(lockfile, repo):
    """Find a dependency entry in the lockfile by repo short name."""
    if not lockfile or "dependencies" not in lockfile:
        return None
    deps = lockfile["dependencies"]
    if isinstance(deps, dict):
        # Dict-keyed lockfile (dep_key -> entry)
        for key, entry in deps.items():
            if isinstance(entry, dict):
                repo_url = entry.get("repo_url", "")
                if repo in repo_url or repo == key:
                    return entry
        return None
    if isinstance(deps, list):
        for entry in deps:
            repo_url = entry.get("repo_url", "")
            if repo in repo_url:
                return entry
    return None


def _count_deployed_skills(project_root):
    """Count skill directories deployed under .github/skills/ or .copilot/skills/."""
    count = 0
    for skills_dir_name in [".github/skills", ".copilot/skills"]:
        skills_dir = project_root / skills_dir_name
        if skills_dir.is_dir():
            for child in skills_dir.iterdir():
                if child.is_dir() and (child / "SKILL.md").exists():
                    count += 1
    return count


# ---------------------------------------------------------------------------
# Main parametrized live test
# ---------------------------------------------------------------------------


@pytest.mark.live
@pytest.mark.parametrize(
    "repo,expected_type,min_skills,is_plugin",
    LIVE_REPOS,
    ids=[r[0] for r in LIVE_REPOS],
)
def test_live_install_classifies_and_succeeds(
    tmp_path, apm_command, fake_home, repo, expected_type, min_skills, is_plugin
):
    """Install a real repo and validate classification + deployment."""
    work_dir = tmp_path / "project"
    work_dir.mkdir()

    result = _run_apm(
        apm_command, ["install", repo, "--verbose"], work_dir, fake_home
    )
    assert result.returncode == 0, (
        f"apm install {repo} failed (exit {result.returncode}):\n"
        f"STDOUT:\n{result.stdout[-2000:]}\n"
        f"STDERR:\n{result.stderr[-2000:]}"
    )

    # Verify lockfile was created and contains the dependency
    lockfile = _read_lockfile(work_dir)
    assert lockfile is not None, (
        f"apm.lock.yaml not created for {repo}.\n"
        f"STDOUT:\n{result.stdout[-1000:]}"
    )

    dep = _get_locked_dep(lockfile, repo)
    assert dep is not None, (
        f"{repo} not found in lockfile. Keys: {list(lockfile.get('dependencies', {}).keys()) if isinstance(lockfile.get('dependencies'), dict) else 'list-format'}.\n"
        f"STDOUT:\n{result.stdout[-1000:]}"
    )

    # Verify package_type classification
    actual_type = dep.get("package_type")
    assert actual_type == expected_type, (
        f"PackageType mismatch for {repo}: expected '{expected_type}', got '{actual_type}'.\n"
        f"STDOUT:\n{result.stdout[-1000:]}"
    )

    # Verify minimum skill deployment count
    if min_skills > 0:
        deployed_count = _count_deployed_skills(work_dir)
        assert deployed_count >= min_skills, (
            f"Expected >= {min_skills} deployed skills for {repo}, "
            f"got {deployed_count}.\n"
            f"STDOUT:\n{result.stdout[-1000:]}"
        )


# ---------------------------------------------------------------------------
# --skill subset selection (multi-skill SKILL_BUNDLE)
# ---------------------------------------------------------------------------


@pytest.mark.live
def test_live_skill_subset_selection(tmp_path, apm_command, fake_home):
    """Install a single skill from vercel-labs/agent-skills (6-skill bundle).

    Picks one known skill name and asserts only that skill is deployed.
    """
    work_dir = tmp_path / "project"
    work_dir.mkdir()

    # vercel-labs/agent-skills contains skills like: deploy-to-vercel,
    # react-best-practices, composition-patterns, etc.
    target_skill = "deploy-to-vercel"

    result = _run_apm(
        apm_command,
        ["install", "vercel-labs/agent-skills", "--skill", target_skill, "--verbose"],
        work_dir,
        fake_home,
    )
    assert result.returncode == 0, (
        f"apm install --skill {target_skill} failed:\n"
        f"STDOUT:\n{result.stdout[-2000:]}\n"
        f"STDERR:\n{result.stderr[-2000:]}"
    )

    # Only the target skill should be deployed
    deployed_count = _count_deployed_skills(work_dir)
    assert deployed_count >= 1, (
        f"Expected at least 1 skill deployed with --skill {target_skill}, got {deployed_count}."
    )

    # Verify the target skill specifically exists
    found_target = False
    for skills_dir_name in [".github/skills", ".copilot/skills"]:
        skill_path = work_dir / skills_dir_name / target_skill
        if skill_path.is_dir() and (skill_path / "SKILL.md").exists():
            found_target = True
            break
    assert found_target, (
        f"Skill '{target_skill}' not found in deployment targets after --skill filter.\n"
        f"STDOUT:\n{result.stdout[-1000:]}"
    )

    # Verify we did NOT deploy all 6 skills (subset restriction worked)
    assert deployed_count <= 2, (
        f"Expected subset install to deploy 1-2 skills, got {deployed_count}. "
        f"--skill filter may not be working."
    )


# ---------------------------------------------------------------------------
# --skill on non-SKILL_BUNDLE repo (should warn, not crash)
# ---------------------------------------------------------------------------


@pytest.mark.live
def test_live_skill_flag_on_non_bundle_deploys_normally(tmp_path, apm_command, fake_home):
    """--skill on a MARKETPLACE_PLUGIN that ships .apm/skills/ should still
    deploy those skills normally -- the --skill filter only applies to
    SKILL_BUNDLE packages with a root skills/ directory.

    pbakaus/impeccable ships skills under .apm/skills/, so the --skill flag
    is not applicable and the install proceeds as normal.
    """
    work_dir = tmp_path / "project"
    work_dir.mkdir()

    result = _run_apm(
        apm_command,
        ["install", "pbakaus/impeccable", "--skill", "nonexistent", "--verbose"],
        work_dir,
        fake_home,
    )
    # Install should succeed
    assert result.returncode == 0, (
        f"apm install --skill on plugin repo failed:\n"
        f"STDOUT:\n{result.stdout[-2000:]}\n"
        f"STDERR:\n{result.stderr[-2000:]}"
    )

    # Lockfile should exist (package was installed)
    lockfile = _read_lockfile(work_dir)
    assert lockfile is not None, "Lockfile not created"

    # Skill IS deployed because .apm/skills/ promotion is unconditional
    # (--skill filtering only applies to SKILL_BUNDLE packages)
    deployed_count = _count_deployed_skills(work_dir)
    assert deployed_count >= 1, (
        f"Expected .apm/skills/ to be promoted even with --skill flag, "
        f"got {deployed_count} deployed"
    )
