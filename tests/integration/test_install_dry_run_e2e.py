"""End-to-end integration tests for `apm install --dry-run`.

Covers gap G2: presentation/dry_run.py (extracted in PR #764) was not
exercised against the binary. This test exists in part because a latent
NameError on the orphan-preview path slipped through review until it was
hardened.

Uses the real `microsoft/apm-sample-package` from GitHub. Requires
GITHUB_APM_PAT or GITHUB_TOKEN for API access.
"""

import hashlib
import os
import subprocess
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.requires_github_token


@pytest.fixture
def temp_project(tmp_path):
    """Temp APM project. Target is set explicitly in apm.yml so that target
    detection does not depend on .github/copilot-instructions.md existing
    (whose absence is asserted by _assert_no_install_artifacts after the
    dry-run)."""
    project_dir = tmp_path / "dry-run-test"
    project_dir.mkdir()
    return project_dir


def _run_apm(apm_binary_path, args, cwd, timeout=180, env=None):
    return subprocess.run(
        [apm_binary_path] + args,  # noqa: RUF005
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )


def _isolated_env(home: Path, **extra: str) -> dict[str, str]:
    """The caller's credentials with HOME, APM_HOME and caches under *home*."""
    home.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, HOME=str(home), APM_HOME=str(home / ".apm"), **extra)
    for name in ("APM_CACHE_DIR", "APM_NO_CACHE", "XDG_CACHE_HOME"):
        env.pop(name, None)
    return env


def _tree_hashes(*roots: Path) -> dict[str, str]:
    """Path -> sha256 (or ``<dir>``) for every entry under *roots*."""
    state: dict[str, str] = {}
    for root in roots:
        for path in sorted(root.rglob("*")):
            key = path.as_posix()
            state[key] = "<dir>" if path.is_dir() else hashlib.sha256(path.read_bytes()).hexdigest()
    return state


def _write_apm_yml(project_dir, apm_packages, mcp_packages=None):
    config = {
        "name": "dry-run-test",
        "version": "1.0.0",
        "targets": ["copilot"],
        "dependencies": {
            "apm": apm_packages,
            "mcp": mcp_packages or [],
        },
    }
    (project_dir / "apm.yml").write_text(
        yaml.dump(config, default_flow_style=False), encoding="utf-8"
    )


def _assert_no_install_artifacts(project_dir):
    """Dry-run must not create lockfile or deploy any files."""
    assert not (project_dir / "apm.lock.yaml").exists(), "Dry-run created apm.lock.yaml"
    assert not (project_dir / "apm.lock").exists(), "Dry-run created legacy apm.lock"
    assert not (project_dir / "apm_modules").exists(), "Dry-run populated apm_modules/"
    copilot_instructions = project_dir / ".github" / "copilot-instructions.md"
    assert not copilot_instructions.exists(), "Dry-run wrote .github/copilot-instructions.md"


class TestInstallDryRunE2E:
    """End-to-end coverage for `apm install --dry-run`."""

    def test_install_dry_run_lists_apm_dependencies_without_changes(
        self, temp_project, apm_binary_path
    ):
        """An uncached remote dep resolves into rollback staging for a complete preview.

        Nothing reaches apm_modules, the lockfile, deployed targets, APM_HOME
        or the cache: the project and home trees hash identically before and
        after.
        """
        _write_apm_yml(temp_project, ["microsoft/apm-sample-package"])
        home = temp_project.parent / "home"
        env = _isolated_env(home)
        _run_apm(apm_binary_path, ["--version"], temp_project, env=env)
        before = _tree_hashes(temp_project, home)

        result = _run_apm(apm_binary_path, ["install", "--dry-run"], temp_project, env=env)
        assert result.returncode == 0, (
            f"Dry-run failed:\nSTDOUT: {result.stdout}\nSTDERR: {result.stderr}"
        )

        out = result.stdout
        assert "Dry run mode" in out, f"Missing 'Dry run mode' banner:\n{out}"
        assert "Dry run completed: would install" in " ".join(out.split()), out
        assert "APM dependencies" in out, f"Missing APM dependencies header:\n{out}"
        assert "microsoft/apm-sample-package" in out, (
            f"Dep repo_url not mentioned in dry-run output:\n{out}"
        )
        assert _tree_hashes(temp_project, home) == before
        _assert_no_install_artifacts(temp_project)

    def test_install_dry_run_with_an_unreachable_remote_is_incomplete(
        self, temp_project, apm_binary_path
    ):
        """An unresolvable remote gives an incomplete preview that never claims it."""
        _write_apm_yml(temp_project, ["microsoft/apm-sample-package"])
        home = temp_project.parent / "home"
        dead_proxy = "http://127.0.0.1:9"
        env = _isolated_env(home, HTTPS_PROXY=dead_proxy, HTTP_PROXY=dead_proxy)
        _run_apm(apm_binary_path, ["--version"], temp_project, env=env)
        before = _tree_hashes(temp_project, home)

        result = _run_apm(apm_binary_path, ["install", "--dry-run"], temp_project, env=env)

        out = " ".join(result.stdout.split())
        assert result.returncode == 0, f"STDOUT: {result.stdout}\nSTDERR: {result.stderr}"
        assert "Dry run incomplete: 1 locally known APM dependency" in out, out
        assert "microsoft/apm-sample-package" in out, out
        assert "would install" not in out, out
        assert _tree_hashes(temp_project, home) == before
        _assert_no_install_artifacts(temp_project)

    def test_install_dry_run_with_only_packages_filter(self, temp_project, apm_binary_path):
        """`--only=apm` suppresses MCP-dependency listing in the dry-run preview."""
        _write_apm_yml(
            temp_project,
            apm_packages=["microsoft/apm-sample-package"],
            mcp_packages=["io.github.github/github-mcp-server"],
        )

        result = _run_apm(apm_binary_path, ["install", "--dry-run", "--only=apm"], temp_project)
        assert result.returncode == 0, (
            f"Filtered dry-run failed:\nSTDOUT: {result.stdout}\nSTDERR: {result.stderr}"
        )

        out = result.stdout
        assert "Dry run mode" in out
        assert "APM dependencies" in out, f"APM section missing under --only=apm:\n{out}"
        assert "microsoft/apm-sample-package" in out
        assert "MCP dependencies" not in out, (
            f"MCP section should be hidden under --only=apm:\n{out}"
        )
        assert "github-mcp-server" not in out, f"MCP dep leaked into --only=apm dry-run:\n{out}"

        _assert_no_install_artifacts(temp_project)

    def test_install_dry_run_previews_orphan_removals(self, temp_project, apm_binary_path):
        """After a real install, removing the dep + dry-run reports orphan files
        and keeps them on disk (the orphan-preview NameError regression test)."""
        _write_apm_yml(temp_project, ["microsoft/apm-sample-package"])
        real = _run_apm(apm_binary_path, ["install"], temp_project)
        assert real.returncode == 0, (
            f"Initial install failed:\nSTDOUT: {real.stdout}\nSTDERR: {real.stderr}"
        )

        lock_path = temp_project / "apm.lock.yaml"
        assert lock_path.exists(), "apm.lock.yaml not created by initial install"
        with open(lock_path, encoding="utf-8") as f:
            lockfile = yaml.safe_load(f)

        deployed_files = []
        for entry in lockfile.get("dependencies") or []:
            if entry.get("repo_url") == "microsoft/apm-sample-package":
                deployed_files = [
                    f for f in (entry.get("deployed_files") or []) if (temp_project / f).exists()
                ]
                break
        if not deployed_files:
            pytest.skip("apm-sample-package deployed no files; cannot verify orphans")

        _write_apm_yml(temp_project, [])

        result = _run_apm(apm_binary_path, ["install", "--dry-run"], temp_project)
        assert result.returncode == 0, (
            f"Orphan dry-run failed:\nSTDOUT: {result.stdout}\nSTDERR: {result.stderr}"
        )

        out = result.stdout
        assert "Dry run mode" in out
        assert "Dry run complete" in out
        assert "Files that would be removed" in out, f"Orphan-removal preview missing:\n{out}"

        for rel_path in deployed_files:
            full = temp_project / rel_path
            assert full.exists(), f"Dry-run unexpectedly deleted orphan file: {rel_path}"
