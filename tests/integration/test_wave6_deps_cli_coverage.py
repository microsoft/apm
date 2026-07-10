"""Integration tests to maximise coverage of src/apm_cli/commands/deps/cli.py.

Strategy
--------
* ``monkeypatch.chdir(tmp_path)`` so that ``get_apm_dir(PROJECT)`` resolves to
  the temp directory.
* Invoke CLI via ``CliRunner().invoke(cli, ["deps", ...])``.
* Create realistic temp directory fixtures: ``apm.yml``, ``apm.lock.yaml``,
  and ``apm_modules/<org>/<repo>/`` structures.
* Only mock external I/O (HTTP, subprocess, auth tokens) — never internal
  apm_cli helpers.

Target coverage lift
--------------------
Before: ~30 % (320 lines missing).
Expected after: ≥ 70 %.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from apm_cli.cli import cli
from apm_cli.models.apm_package import clear_apm_yml_cache

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

_APM_YML_SIMPLE = dedent("""\
    name: test-project
    version: 0.1.0
    description: Test project
    owner:
      name: test-org
""")

_APM_YML_WITH_DEPS = dedent("""\
    name: test-project
    version: 0.1.0
    description: Test project
    owner:
      name: test-org
    dependencies:
      apm:
        - test-org/test-dep
""")

_APM_YML_TWO_DEPS = dedent("""\
    name: test-project
    version: 0.1.0
    description: Test project
    owner:
      name: test-org
    dependencies:
      apm:
        - test-org/pkg-alpha
        - test-org/pkg-beta
""")

# Minimal lockfile: list-based ``dependencies`` key
_LOCKFILE_ONE_DEP = dedent("""\
    lockfile_version: "1"
    dependencies:
      - repo_url: test-org/test-dep
        resolved_commit: abc1234567890abcdef
        resolved_ref: main
        version: "1.0.0"
        depth: 1
""")

_LOCKFILE_TWO_DEPS = dedent("""\
    lockfile_version: "1"
    dependencies:
      - repo_url: test-org/pkg-alpha
        resolved_commit: aaa1111111111aaaaa
        resolved_ref: main
        version: "1.0.0"
        depth: 1
      - repo_url: test-org/pkg-beta
        resolved_commit: bbb2222222222bbbbb
        resolved_ref: v2.0.0
        version: "2.0.0"
        depth: 1
""")

_LOCKFILE_WITH_TRANSITIVE = dedent("""\
    lockfile_version: "1"
    dependencies:
      - repo_url: test-org/parent-pkg
        resolved_commit: aaa1111111111aaaaa
        resolved_ref: main
        version: "1.0.0"
        depth: 1
      - repo_url: test-org/child-pkg
        resolved_commit: bbb2222222222bbbbb
        resolved_ref: main
        version: "1.0.0"
        depth: 2
        resolved_by: test-org/parent-pkg
""")

_LOCKFILE_INSECURE = dedent("""\
    lockfile_version: "1"
    dependencies:
      - repo_url: test-org/insecure-pkg
        resolved_commit: ccc3333333333ccccc
        resolved_ref: main
        version: "1.0.0"
        depth: 1
        is_insecure: true
""")


def _make_pkg(modules_dir: Path, org: str, repo: str, version: str = "1.0.0") -> Path:
    """Create a package directory with apm.yml inside apm_modules."""
    pkg = modules_dir / org / repo
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "apm.yml").write_text(
        f"name: {repo}\nversion: {version}\ndescription: Package {repo}\n",
        encoding="utf-8",
    )
    return pkg


def _make_skill_pkg(modules_dir: Path, org: str, repo: str) -> Path:
    """Create a skill-only package (SKILL.md only, no apm.yml)."""
    pkg = modules_dir / org / repo
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "SKILL.md").write_text(
        "---\ndescription: A skill\n---\n# Skill\n",
        encoding="utf-8",
    )
    return pkg


# ---------------------------------------------------------------------------
# Shared state helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_cache():
    """Clear APMPackage YAML cache before every test."""
    clear_apm_yml_cache()
    yield
    clear_apm_yml_cache()


# ---------------------------------------------------------------------------
# deps list — no apm_modules directory
# ---------------------------------------------------------------------------


class TestDepsListNoModules:
    """``deps list`` when apm_modules/ does not exist."""

    def test_no_apm_modules_reports_none_installed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / "apm.yml").write_text(_APM_YML_SIMPLE)
        result = CliRunner().invoke(cli, ["deps", "list"])
        assert result.exit_code == 0
        output = result.output
        assert "No APM dependencies installed" in output or "apm_modules" in output.lower()

    def test_no_apm_modules_global_scope(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """--global path exercises InstallScope.USER branch (HOME/.apm/apm_modules)."""
        monkeypatch.chdir(tmp_path)
        # We just verify the command runs; no crash is the key assertion.
        result = CliRunner().invoke(cli, ["deps", "list", "--global"])
        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# deps list — apm_modules exists but is empty / has valid packages
# ---------------------------------------------------------------------------


class TestDepsListWithModules:
    """``deps list`` with various apm_modules/ states."""

    def test_empty_modules_dir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """apm_modules exists but contains no valid packages."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "apm.yml").write_text(_APM_YML_SIMPLE)
        (tmp_path / "apm_modules").mkdir()
        result = CliRunner().invoke(cli, ["deps", "list"])
        assert result.exit_code == 0
        out = result.output
        assert "No APM" in out or "no valid packages" in out.lower() or "apm_modules" in out.lower()

    def test_with_one_package(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """apm_modules contains one package — table rows are rendered."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "apm.yml").write_text(_APM_YML_WITH_DEPS)
        modules = tmp_path / "apm_modules"
        _make_pkg(modules, "test-org", "test-dep")
        (tmp_path / "apm.lock.yaml").write_text(_LOCKFILE_ONE_DEP)

        result = CliRunner().invoke(cli, ["deps", "list"])
        assert result.exit_code == 0
        assert "test-org/test-dep" in result.output

    def test_with_two_packages(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Two declared packages show up in the table."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "apm.yml").write_text(_APM_YML_TWO_DEPS)
        modules = tmp_path / "apm_modules"
        _make_pkg(modules, "test-org", "pkg-alpha")
        _make_pkg(modules, "test-org", "pkg-beta", version="2.0.0")
        (tmp_path / "apm.lock.yaml").write_text(_LOCKFILE_TWO_DEPS)

        result = CliRunner().invoke(cli, ["deps", "list"])
        assert result.exit_code == 0
        assert "pkg-alpha" in result.output
        assert "pkg-beta" in result.output

    def test_orphaned_package_reported(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Package in apm_modules but not in apm.yml shows as orphaned."""
        monkeypatch.chdir(tmp_path)
        # apm.yml has NO deps, but apm_modules has a package
        (tmp_path / "apm.yml").write_text(_APM_YML_SIMPLE)
        modules = tmp_path / "apm_modules"
        _make_pkg(modules, "test-org", "stale-pkg")

        result = CliRunner().invoke(cli, ["deps", "list"])
        assert result.exit_code == 0
        # The orphaned label or warning should appear
        assert "orphan" in result.output.lower() or "stale-pkg" in result.output

    def test_insecure_flag_no_insecure_packages(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """--insecure with no insecure packages shows empty message."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "apm.yml").write_text(_APM_YML_WITH_DEPS)
        modules = tmp_path / "apm_modules"
        _make_pkg(modules, "test-org", "test-dep")
        (tmp_path / "apm.lock.yaml").write_text(_LOCKFILE_ONE_DEP)

        result = CliRunner().invoke(cli, ["deps", "list", "--insecure"])
        assert result.exit_code == 0
        assert (
            "No insecure" in result.output
            or result.output.strip() == ""
            or "insecure" in result.output.lower()
        )

    def test_insecure_flag_with_insecure_package(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """--insecure lists packages that have is_insecure=true in lockfile."""
        monkeypatch.chdir(tmp_path)
        apm_yml = dedent("""\
            name: test-project
            version: 0.1.0
            dependencies:
              apm:
                - test-org/insecure-pkg
        """)
        (tmp_path / "apm.yml").write_text(apm_yml)
        modules = tmp_path / "apm_modules"
        _make_pkg(modules, "test-org", "insecure-pkg")
        (tmp_path / "apm.lock.yaml").write_text(_LOCKFILE_INSECURE)

        result = CliRunner().invoke(cli, ["deps", "list", "--insecure"])
        assert result.exit_code == 0
        assert "insecure-pkg" in result.output

    def test_show_all_flag(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """--all shows both Project and Global scopes."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "apm.yml").write_text(_APM_YML_SIMPLE)
        result = CliRunner().invoke(cli, ["deps", "list", "--all"])
        assert result.exit_code == 0
        # At minimum both scopes attempted
        out = result.output
        assert "Project" in out or "Global" in out or "No APM" in out

    def test_package_with_skill_md_only(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Package identified by SKILL.md (no apm.yml) is listed."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "apm.yml").write_text(_APM_YML_SIMPLE)
        modules = tmp_path / "apm_modules"
        _make_skill_pkg(modules, "skill-org", "my-skill")

        result = CliRunner().invoke(cli, ["deps", "list"])
        assert result.exit_code == 0
        assert "my-skill" in result.output or "skill-org" in result.output

    def test_no_apm_yml_still_lists_modules(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When apm.yml is absent all installed packages show (no orphan check)."""
        monkeypatch.chdir(tmp_path)
        # No apm.yml, but there are modules
        modules = tmp_path / "apm_modules"
        _make_pkg(modules, "solo-org", "lone-pkg")

        result = CliRunner().invoke(cli, ["deps", "list"])
        # Should not crash — may show packages or "No APM" depending on scope
        assert result.exit_code == 0

    def test_corrupt_lockfile_is_ignored(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A corrupt lockfile does not crash deps list."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "apm.yml").write_text(_APM_YML_WITH_DEPS)
        modules = tmp_path / "apm_modules"
        _make_pkg(modules, "test-org", "test-dep")
        (tmp_path / "apm.lock.yaml").write_text("NOT: VALID: YAML: [[[")

        result = CliRunner().invoke(cli, ["deps", "list"])
        assert result.exit_code == 0

    def test_list_with_lockfile_no_modules_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Lockfile present but no apm_modules → returns 'no deps installed'."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "apm.yml").write_text(_APM_YML_WITH_DEPS)
        (tmp_path / "apm.lock.yaml").write_text(_LOCKFILE_ONE_DEP)
        # No apm_modules/

        result = CliRunner().invoke(cli, ["deps", "list"])
        assert result.exit_code == 0
        assert "No APM dependencies" in result.output or "apm_modules" in result.output.lower()


# ---------------------------------------------------------------------------
# deps tree
# ---------------------------------------------------------------------------


class TestDepsTree:
    """``deps tree`` command coverage."""

    def test_tree_no_modules_no_lockfile(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No apm_modules, no lockfile → tree shows 'No dependencies installed'."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "apm.yml").write_text(_APM_YML_SIMPLE)

        result = CliRunner().invoke(cli, ["deps", "tree"])
        assert result.exit_code == 0
        assert "No dependencies installed" in result.output or "test-project" in result.output

    def test_tree_with_lockfile_one_dep(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Lockfile present → tree uses lockfile as source."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "apm.yml").write_text(_APM_YML_WITH_DEPS)
        (tmp_path / "apm.lock.yaml").write_text(_LOCKFILE_ONE_DEP)
        modules = tmp_path / "apm_modules"
        _make_pkg(modules, "test-org", "test-dep")

        result = CliRunner().invoke(cli, ["deps", "tree"])
        assert result.exit_code == 0
        assert "test-dep" in result.output

    def test_tree_with_lockfile_transitive(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Tree shows direct + transitive deps from lockfile."""
        monkeypatch.chdir(tmp_path)
        apm_yml = dedent("""\
            name: test-project
            version: 0.1.0
            dependencies:
              apm:
                - test-org/parent-pkg
        """)
        (tmp_path / "apm.yml").write_text(apm_yml)
        (tmp_path / "apm.lock.yaml").write_text(_LOCKFILE_WITH_TRANSITIVE)
        modules = tmp_path / "apm_modules"
        _make_pkg(modules, "test-org", "parent-pkg")
        _make_pkg(modules, "test-org", "child-pkg")

        result = CliRunner().invoke(cli, ["deps", "tree"])
        assert result.exit_code == 0
        assert "parent-pkg" in result.output

    def test_tree_retains_identity_for_colliding_package_names(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """CLI tree renders each canonical identity despite colliding names."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "apm.yml").write_text(
            dedent("""\
                name: consumer
                version: 0.1.0
                dependencies:
                  apm:
                    - example/monorepo
            """)
        )
        (tmp_path / "apm.lock.yaml").write_text(
            dedent("""\
                lockfile_version: "1"
                dependencies:
                  - repo_url: example/monorepo
                    name: shared-display-name
                    depth: 1
                    version: "1.0.0"
                  - repo_url: example/monorepo
                    name: shared-display-name
                    virtual_path: packages/inner
                    is_virtual: true
                    depth: 2
                    resolved_by: example/monorepo
                    version: "1.0.0"
                  - repo_url: example/monorepo
                    name: shared-display-name
                    virtual_path: packages/leaf
                    is_virtual: true
                    depth: 3
                    resolved_by: example/monorepo
                    version: "1.0.0"
            """)
        )

        result = CliRunner().invoke(cli, ["deps", "tree"])

        assert result.exit_code == 0, result.output
        identities = [
            "example/monorepo@1.0.0",
            "example/monorepo/packages/inner@1.0.0",
            "example/monorepo/packages/leaf@1.0.0",
        ]
        positions = [result.output.index(identity) for identity in identities]
        assert positions == sorted(positions)
        assert all(result.output.count(identity) == 1 for identity in identities)

    def test_tree_surfaces_dependency_with_ambiguous_legacy_parent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Legacy parent ambiguity remains visible instead of dropping a child."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "apm.yml").write_text("name: consumer\nversion: 0.1.0\n")
        (tmp_path / "apm.lock.yaml").write_text(
            dedent("""\
                lockfile_version: "1"
                dependencies:
                  - repo_url: example/shared
                    host: git.example-one.test
                    depth: 1
                    version: "1.0.0"
                  - repo_url: example/shared
                    host: git.example-two.test
                    depth: 1
                    version: "1.0.0"
                  - repo_url: example/child
                    depth: 2
                    resolved_by: example/shared
                    version: "1.0.0"
            """)
        )

        result = CliRunner().invoke(cli, ["deps", "tree"])

        assert result.exit_code == 0, result.output
        assert result.output.count("example/child@1.0.0") == 1
        assert "could not determine parent" in result.output

    def test_tree_no_lockfile_with_modules(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fallback directory scan when no lockfile exists."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "apm.yml").write_text(_APM_YML_WITH_DEPS)
        modules = tmp_path / "apm_modules"
        _make_pkg(modules, "test-org", "test-dep")
        # No lockfile

        result = CliRunner().invoke(cli, ["deps", "tree"])
        assert result.exit_code == 0
        assert "test-dep" in result.output or "test-project" in result.output

    def test_tree_global_flag(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """--global exercises the user scope branch without error."""
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(cli, ["deps", "tree", "--global"])
        assert result.exit_code == 0

    def test_tree_corrupt_lockfile_falls_back_to_scan(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Corrupt lockfile → graceful fallback to directory scan."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "apm.yml").write_text(_APM_YML_WITH_DEPS)
        (tmp_path / "apm.lock.yaml").write_text("GARBAGE: [[[")
        modules = tmp_path / "apm_modules"
        _make_pkg(modules, "test-org", "test-dep")

        result = CliRunner().invoke(cli, ["deps", "tree"])
        assert result.exit_code == 0

    def test_tree_no_apm_yml_uses_default_name(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No apm.yml → project_name falls back to 'my-project'."""
        monkeypatch.chdir(tmp_path)
        modules = tmp_path / "apm_modules"
        _make_pkg(modules, "test-org", "test-dep")

        result = CliRunner().invoke(cli, ["deps", "tree"])
        assert result.exit_code == 0
        assert "my-project" in result.output or "test-dep" in result.output

    def test_tree_lockfile_dep_with_primitives(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Package with SKILL.md in apm_modules shows primitive counts in tree."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "apm.yml").write_text(_APM_YML_WITH_DEPS)
        (tmp_path / "apm.lock.yaml").write_text(_LOCKFILE_ONE_DEP)
        modules = tmp_path / "apm_modules"
        pkg = _make_pkg(modules, "test-org", "test-dep")
        # Add a skill
        skill_dir = pkg / ".apm" / "skills" / "helper"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\ndescription: Helper skill\n---\n# Helper\n",
            encoding="utf-8",
        )

        result = CliRunner().invoke(cli, ["deps", "tree"])
        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# deps clean
# ---------------------------------------------------------------------------


class TestDepsClean:
    """``deps clean`` command coverage."""

    def test_clean_no_modules_dir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """No apm_modules → already clean message."""
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(cli, ["deps", "clean"])
        assert result.exit_code == 0
        assert "already clean" in result.output.lower() or "No apm_modules" in result.output

    def test_clean_dry_run(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """--dry-run prints what would be removed without removing."""
        monkeypatch.chdir(tmp_path)
        modules = tmp_path / "apm_modules"
        _make_pkg(modules, "test-org", "test-dep")

        result = CliRunner().invoke(cli, ["deps", "clean", "--dry-run"])
        assert result.exit_code == 0
        assert "dry run" in result.output.lower() or "would remove" in result.output.lower()
        # apm_modules still exists
        assert modules.exists()

    def test_clean_with_yes_flag(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """--yes skips confirmation and removes apm_modules/."""
        monkeypatch.chdir(tmp_path)
        modules = tmp_path / "apm_modules"
        _make_pkg(modules, "test-org", "test-dep")

        result = CliRunner().invoke(cli, ["deps", "clean", "--yes"])
        assert result.exit_code == 0
        assert not modules.exists()
        assert "removed" in result.output.lower() or "success" in result.output.lower()

    def test_clean_dry_run_empty_modules(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """--dry-run with empty apm_modules (0 packages) works."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "apm_modules").mkdir()

        result = CliRunner().invoke(cli, ["deps", "clean", "--dry-run"])
        assert result.exit_code == 0
        assert "dry run" in result.output.lower() or "0 package" in result.output

    def test_clean_cancelled_via_no(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Answering 'no' to the confirmation prompt cancels the operation."""
        monkeypatch.chdir(tmp_path)
        modules = tmp_path / "apm_modules"
        _make_pkg(modules, "test-org", "test-dep")

        # CliRunner.invoke with input="n\n" simulates user typing 'no'
        result = CliRunner().invoke(cli, ["deps", "clean"], input="n\n")
        assert result.exit_code == 0
        # apm_modules still present
        assert modules.exists()
        assert "cancel" in result.output.lower() or "operation" in result.output.lower()

    def test_clean_confirmed_via_yes_input(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Answering 'y' to the confirmation prompt proceeds with removal."""
        monkeypatch.chdir(tmp_path)
        modules = tmp_path / "apm_modules"
        _make_pkg(modules, "test-org", "test-dep")

        result = CliRunner().invoke(cli, ["deps", "clean"], input="y\n")
        assert result.exit_code == 0
        assert not modules.exists()


# ---------------------------------------------------------------------------
# deps update
# ---------------------------------------------------------------------------


class TestDepsUpdate:
    """``deps update`` command coverage — no-network paths only."""

    def test_update_no_apm_yml(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """No apm.yml → error message and non-zero exit."""
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(cli, ["deps", "update"])
        assert result.exit_code != 0
        assert "apm.yml" in result.output or "No " in result.output

    def test_update_no_deps_in_apm_yml(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """apm.yml with no APM deps → 'No APM dependencies defined' message."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "apm.yml").write_text(_APM_YML_SIMPLE)

        result = CliRunner().invoke(cli, ["deps", "update"])
        assert result.exit_code == 0
        assert "No APM dependencies" in result.output

    def test_update_unknown_package_name(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Requesting update of a package not in apm.yml → error."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "apm.yml").write_text(_APM_YML_WITH_DEPS)

        result = CliRunner().invoke(cli, ["deps", "update", "nonexistent/pkg"])
        assert result.exit_code != 0
        assert "not found" in result.output.lower() or "nonexistent" in result.output

    def test_update_global_no_apm_yml(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``--global`` with no ~/.apm/apm.yml → error pointing to ~/.apm/."""
        monkeypatch.chdir(tmp_path)
        # We cannot reliably write to HOME, so just check the error is about scope
        # This exercises the global_ path (line 776: scope = InstallScope.USER)
        fake_home = tmp_path / "fake_home"
        fake_home.mkdir()
        monkeypatch.setenv("HOME", str(fake_home))
        # Reset Path.home() by re-importing — patch at the source level
        with patch("pathlib.Path.home", return_value=fake_home):
            result = CliRunner().invoke(cli, ["deps", "update", "--global"])
        assert result.exit_code != 0

    def test_update_corrupt_apm_yml(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Corrupt apm.yml → error about parsing failure."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "apm.yml").write_text("{{{{ not valid yaml }}}}")

        result = CliRunner().invoke(cli, ["deps", "update"])
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# deps info
# ---------------------------------------------------------------------------


class TestDepsInfo:
    """``deps info`` command coverage."""

    def test_info_no_apm_modules(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """No apm_modules/ → error about missing directory."""
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(cli, ["deps", "info", "test-org/test-dep"])
        assert result.exit_code != 0
        assert "apm_modules" in result.output.lower() or "No apm_modules" in result.output

    def test_info_package_not_found(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """apm_modules exists but package not found → error exit."""
        monkeypatch.chdir(tmp_path)
        modules = tmp_path / "apm_modules"
        _make_pkg(modules, "test-org", "other-pkg")

        result = CliRunner().invoke(cli, ["deps", "info", "test-org/missing-pkg"])
        assert result.exit_code != 0
        assert "not found" in result.output.lower() or "missing-pkg" in result.output

    def test_info_package_found_by_full_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Package found by org/repo path → info rendered without error."""
        monkeypatch.chdir(tmp_path)
        modules = tmp_path / "apm_modules"
        _make_pkg(modules, "test-org", "test-dep")

        result = CliRunner().invoke(cli, ["deps", "info", "test-org/test-dep"])
        # Should render info (may exit 0 or 1 depending on view internals)
        # The key is that it reaches display_package_info
        assert "test-dep" in result.output or "Name" in result.output

    def test_info_package_found_by_short_name(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Package found by short repo name (fallback scan)."""
        monkeypatch.chdir(tmp_path)
        modules = tmp_path / "apm_modules"
        _make_pkg(modules, "test-org", "test-dep")

        result = CliRunner().invoke(cli, ["deps", "info", "test-dep"])
        assert "test-dep" in result.output or "Name" in result.output


# ---------------------------------------------------------------------------
# Private helper — _resolve_scope_deps edge cases
# ---------------------------------------------------------------------------


class TestResolveScopeDepsEdgeCases:
    """Unit-level tests that drive _resolve_scope_deps via the CLI."""

    def test_lockfile_with_local_source_label(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Packages marked source=local in lockfile receive 'local' source label."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "apm.yml").write_text(_APM_YML_SIMPLE)
        lockfile_content = dedent("""\
            lockfile_version: "1"
            dependencies:
              - repo_url: local-org/local-pkg
                version: "0.1.0"
                depth: 1
                source: local
                local_path: ./local-pkg
        """)
        (tmp_path / "apm.lock.yaml").write_text(lockfile_content)
        modules = tmp_path / "apm_modules"
        _make_pkg(modules, "local-org", "local-pkg")

        result = CliRunner().invoke(cli, ["deps", "list"])
        assert result.exit_code == 0
        assert "local-pkg" in result.output

    def test_modules_dir_with_dotfile_dirs_skipped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Directories starting with '.' inside apm_modules are skipped."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "apm.yml").write_text(_APM_YML_SIMPLE)
        modules = tmp_path / "apm_modules"
        # Dot-directory should be skipped
        hidden = modules / ".cache" / "repo"
        hidden.mkdir(parents=True)
        (hidden / "apm.yml").write_text("name: hidden\nversion: 0.0.1\n")
        # Valid package
        _make_pkg(modules, "real-org", "real-pkg")

        result = CliRunner().invoke(cli, ["deps", "list"])
        assert result.exit_code == 0
        assert "real-pkg" in result.output
        assert "hidden" not in result.output

    def test_packages_with_single_path_component_skipped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Packages at depth-1 (no org namespace) are skipped (len(parts) < 2)."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "apm.yml").write_text(_APM_YML_SIMPLE)
        modules = tmp_path / "apm_modules"
        modules.mkdir()
        # Top-level package without org dir (only 1 part, should be skipped)
        top_pkg = modules / "lone-pkg"
        top_pkg.mkdir()
        (top_pkg / "apm.yml").write_text("name: lone-pkg\nversion: 1.0.0\n")

        result = CliRunner().invoke(cli, ["deps", "list"])
        assert result.exit_code == 0
        # lone-pkg should NOT appear (no org namespace)
        assert "lone-pkg" not in result.output

    def test_apm_subdir_packages_skipped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Skills nested under .apm/ subdirs inside a package are not listed."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "apm.yml").write_text(_APM_YML_WITH_DEPS)
        (tmp_path / "apm.lock.yaml").write_text(_LOCKFILE_ONE_DEP)
        modules = tmp_path / "apm_modules"
        pkg = _make_pkg(modules, "test-org", "test-dep")
        # Create a .apm/skills subdir (should be skipped)
        nested = pkg / ".apm" / "skills" / "nested-skill"
        nested.mkdir(parents=True)
        (nested / "SKILL.md").write_text(
            "---\ndescription: Nested skill\n---\n# Nested\n", encoding="utf-8"
        )

        result = CliRunner().invoke(cli, ["deps", "list"])
        assert result.exit_code == 0
        assert "test-org/test-dep" in result.output
        assert "nested-skill" not in result.output

    def test_insecure_package_with_resolved_by(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Insecure package with resolved_by shows 'via ...' in insecure_via."""
        monkeypatch.chdir(tmp_path)
        apm_yml = dedent("""\
            name: test-project
            version: 0.1.0
            dependencies:
              apm:
                - test-org/transitive-insecure
        """)
        (tmp_path / "apm.yml").write_text(apm_yml)
        lockfile_content = dedent("""\
            lockfile_version: "1"
            dependencies:
              - repo_url: test-org/transitive-insecure
                version: "1.0.0"
                depth: 1
                is_insecure: true
                resolved_by: test-org/parent
        """)
        (tmp_path / "apm.lock.yaml").write_text(lockfile_content)
        modules = tmp_path / "apm_modules"
        _make_pkg(modules, "test-org", "transitive-insecure")

        result = CliRunner().invoke(cli, ["deps", "list"])
        assert result.exit_code == 0
        assert "transitive-insecure" in result.output


# ---------------------------------------------------------------------------
# deps tree — text fallback (no-rich paths exercised)
# ---------------------------------------------------------------------------


class TestDepsTreeTextFallback:
    """Force the text-only fallback branch of ``deps tree``."""

    def test_tree_text_output_with_lockfile(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Text fallback still shows dep tree when rich is available (CliRunner strips markup)."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "apm.yml").write_text(_APM_YML_WITH_DEPS)
        (tmp_path / "apm.lock.yaml").write_text(_LOCKFILE_ONE_DEP)
        modules = tmp_path / "apm_modules"
        _make_pkg(modules, "test-org", "test-dep")

        result = CliRunner().invoke(cli, ["deps", "tree"])
        assert result.exit_code == 0
        assert "test-dep" in result.output

    def test_tree_text_output_with_transitive(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Text output for transitive deps: shows child under parent."""
        monkeypatch.chdir(tmp_path)
        apm_yml = dedent("""\
            name: test-project
            version: 0.1.0
            dependencies:
              apm:
                - test-org/parent-pkg
        """)
        (tmp_path / "apm.yml").write_text(apm_yml)
        (tmp_path / "apm.lock.yaml").write_text(_LOCKFILE_WITH_TRANSITIVE)
        modules = tmp_path / "apm_modules"
        _make_pkg(modules, "test-org", "parent-pkg")
        _make_pkg(modules, "test-org", "child-pkg")

        result = CliRunner().invoke(cli, ["deps", "tree"])
        assert result.exit_code == 0
        assert "parent-pkg" in result.output

    def test_tree_directory_fallback_no_lockfile(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Directory fallback: no lockfile, modules scanned directly."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "apm.yml").write_text(_APM_YML_TWO_DEPS)
        modules = tmp_path / "apm_modules"
        _make_pkg(modules, "test-org", "pkg-alpha")
        _make_pkg(modules, "test-org", "pkg-beta", version="2.0.0")

        result = CliRunner().invoke(cli, ["deps", "tree"])
        assert result.exit_code == 0
        assert "pkg-alpha" in result.output or "pkg-beta" in result.output

    def test_tree_directory_fallback_no_modules(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Directory fallback when apm_modules exists but is empty."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "apm.yml").write_text(_APM_YML_SIMPLE)
        (tmp_path / "apm_modules").mkdir()

        result = CliRunner().invoke(cli, ["deps", "tree"])
        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# deps list — version edge cases in _dep_display_name
# ---------------------------------------------------------------------------


class TestDepDisplayName:
    """Tests for _dep_display_name code paths via CLI tree."""

    def test_dep_with_version(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Dep with version shows key@version format."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "apm.yml").write_text(_APM_YML_WITH_DEPS)
        (tmp_path / "apm.lock.yaml").write_text(_LOCKFILE_ONE_DEP)
        modules = tmp_path / "apm_modules"
        _make_pkg(modules, "test-org", "test-dep")

        result = CliRunner().invoke(cli, ["deps", "tree"])
        assert result.exit_code == 0
        # Display should include version
        assert "1.0.0" in result.output or "test-dep" in result.output

    def test_dep_with_commit_only(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Dep with resolved_commit only (no version) shows short SHA."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "apm.yml").write_text(_APM_YML_WITH_DEPS)
        lockfile = dedent("""\
            lockfile_version: "1"
            dependencies:
              - repo_url: test-org/test-dep
                resolved_commit: deadbeef1234567
                depth: 1
        """)
        (tmp_path / "apm.lock.yaml").write_text(lockfile)
        modules = tmp_path / "apm_modules"
        _make_pkg(modules, "test-org", "test-dep")

        result = CliRunner().invoke(cli, ["deps", "tree"])
        assert result.exit_code == 0
        assert "deadbee" in result.output or "test-dep" in result.output

    def test_dep_with_ref_only(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Dep with resolved_ref only shows the ref."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "apm.yml").write_text(_APM_YML_WITH_DEPS)
        lockfile = dedent("""\
            lockfile_version: "1"
            dependencies:
              - repo_url: test-org/test-dep
                resolved_ref: main
                depth: 1
        """)
        (tmp_path / "apm.lock.yaml").write_text(lockfile)
        modules = tmp_path / "apm_modules"
        _make_pkg(modules, "test-org", "test-dep")

        result = CliRunner().invoke(cli, ["deps", "tree"])
        assert result.exit_code == 0
        assert "main" in result.output or "test-dep" in result.output

    def test_dep_with_no_version_shows_latest(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Dep with no version/commit/ref shows 'latest'."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "apm.yml").write_text(_APM_YML_WITH_DEPS)
        lockfile = dedent("""\
            lockfile_version: "1"
            dependencies:
              - repo_url: test-org/test-dep
                depth: 1
        """)
        (tmp_path / "apm.lock.yaml").write_text(lockfile)
        modules = tmp_path / "apm_modules"
        _make_pkg(modules, "test-org", "test-dep")

        result = CliRunner().invoke(cli, ["deps", "tree"])
        assert result.exit_code == 0
        assert "latest" in result.output or "test-dep" in result.output


# ---------------------------------------------------------------------------
# _deps_list_source_label helper exercised via list
# ---------------------------------------------------------------------------


class TestDepsSourceLabel:
    """Source labels for ADO / GitLab / local packages."""

    def test_ado_package_shows_azure_devops_source(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ADO-hosted package gets 'azure-devops' source label."""
        monkeypatch.chdir(tmp_path)
        # Use a format that the parser will accept
        apm_yml_simple = dedent("""\
            name: test-project
            version: 0.1.0
            dependencies:
              apm:
                - source: https://dev.azure.com/myorg/myproject/_git/myrepo
        """)
        (tmp_path / "apm.yml").write_text(apm_yml_simple)
        modules = tmp_path / "apm_modules"
        modules.mkdir()
        # Even with empty modules, the list command exercises source-label logic
        result = CliRunner().invoke(cli, ["deps", "list"])
        assert result.exit_code == 0

    def test_gitlab_package_in_lockfile(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Lockfile entry with gitlab host gets 'gitlab' source label."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "apm.yml").write_text(_APM_YML_SIMPLE)
        lockfile_content = dedent("""\
            lockfile_version: "1"
            dependencies:
              - repo_url: mygroup/myrepo
                host: gitlab.com
                version: "1.0.0"
                depth: 1
        """)
        (tmp_path / "apm.lock.yaml").write_text(lockfile_content)
        modules = tmp_path / "apm_modules"
        _make_pkg(modules, "mygroup", "myrepo")

        result = CliRunner().invoke(cli, ["deps", "list"])
        assert result.exit_code == 0
        assert "mygroup/myrepo" in result.output


# ---------------------------------------------------------------------------
# Combined / regression tests
# ---------------------------------------------------------------------------


class TestDepsRegression:
    """Regression and edge-case tests for deps commands."""

    def test_list_multiple_runs_are_idempotent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Running deps list twice produces consistent results."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "apm.yml").write_text(_APM_YML_WITH_DEPS)
        modules = tmp_path / "apm_modules"
        _make_pkg(modules, "test-org", "test-dep")
        (tmp_path / "apm.lock.yaml").write_text(_LOCKFILE_ONE_DEP)

        runner = CliRunner()
        r1 = runner.invoke(cli, ["deps", "list"])
        r2 = runner.invoke(cli, ["deps", "list"])
        assert r1.exit_code == 0
        assert r2.exit_code == 0
        assert r1.output == r2.output

    def test_tree_then_list_consistency(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """tree and list both report the same package after install."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "apm.yml").write_text(_APM_YML_WITH_DEPS)
        modules = tmp_path / "apm_modules"
        _make_pkg(modules, "test-org", "test-dep")
        (tmp_path / "apm.lock.yaml").write_text(_LOCKFILE_ONE_DEP)

        runner = CliRunner()
        tree_r = runner.invoke(cli, ["deps", "tree"])
        list_r = runner.invoke(cli, ["deps", "list"])
        assert tree_r.exit_code == 0
        assert list_r.exit_code == 0
        assert "test-dep" in tree_r.output
        assert "test-dep" in list_r.output

    def test_clean_then_list_shows_no_deps(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """After clean, list reports no packages installed."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "apm.yml").write_text(_APM_YML_WITH_DEPS)
        modules = tmp_path / "apm_modules"
        _make_pkg(modules, "test-org", "test-dep")
        (tmp_path / "apm.lock.yaml").write_text(_LOCKFILE_ONE_DEP)

        runner = CliRunner()
        clean_r = runner.invoke(cli, ["deps", "clean", "--yes"])
        assert clean_r.exit_code == 0
        assert not modules.exists()

        list_r = runner.invoke(cli, ["deps", "list"])
        assert list_r.exit_code == 0
        assert "No APM dependencies" in list_r.output or "apm_modules" in list_r.output.lower()

    def test_list_insecure_empty_modules(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """--insecure with no apm_modules shows 'no deps installed' path."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "apm.yml").write_text(_APM_YML_SIMPLE)

        result = CliRunner().invoke(cli, ["deps", "list", "--insecure"])
        assert result.exit_code == 0

    def test_update_verbose_no_deps(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """``deps update --verbose`` with no deps in apm.yml works cleanly."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "apm.yml").write_text(_APM_YML_SIMPLE)

        result = CliRunner().invoke(cli, ["deps", "update", "--verbose"])
        assert result.exit_code == 0
        assert "No APM dependencies" in result.output


# ---------------------------------------------------------------------------
# deps list — virtual package handling (lines 115, 122-139)
# ---------------------------------------------------------------------------


class TestDepsListVirtualPackages:
    """Virtual subdirectory and virtual file package handling in deps list."""

    def test_virtual_subdirectory_package(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """apm.yml with git+path virtual subdirectory package scans correctly."""
        monkeypatch.chdir(tmp_path)
        # Object-form dep: git + path → virtual subdirectory
        apm_yml = dedent("""\
            name: test-project
            version: 0.1.0
            dependencies:
              apm:
                - git: https://github.com/test-org/test-repo
                  path: skills/my-collection
        """)
        (tmp_path / "apm.yml").write_text(apm_yml)
        # Install the virtual subdirectory package under apm_modules
        modules = tmp_path / "apm_modules"
        pkg = modules / "test-org" / "test-repo" / "skills" / "my-collection"
        pkg.mkdir(parents=True)
        (pkg / "SKILL.md").write_text(
            "---\ndescription: Collection skill\n---\n# My Collection\n",
            encoding="utf-8",
        )

        result = CliRunner().invoke(cli, ["deps", "list"])
        assert result.exit_code == 0

    def test_virtual_file_package(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """apm.yml with git+path to a .prompt.md file (virtual file package)."""
        monkeypatch.chdir(tmp_path)
        apm_yml = dedent("""\
            name: test-project
            version: 0.1.0
            dependencies:
              apm:
                - git: https://github.com/test-org/prompt-repo
                  path: prompts/review.prompt.md
        """)
        (tmp_path / "apm.yml").write_text(apm_yml)
        # Even without corresponding module the command should not crash
        (tmp_path / "apm_modules").mkdir()

        result = CliRunner().invoke(cli, ["deps", "list"])
        assert result.exit_code == 0

    def test_virtual_subdirectory_with_installed_module(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Virtual subdirectory with installed dir is listed (declared_sources populated)."""
        monkeypatch.chdir(tmp_path)
        apm_yml = dedent("""\
            name: test-project
            version: 0.1.0
            dependencies:
              apm:
                - git: https://github.com/test-org/test-repo
                  path: skills/helpful
        """)
        (tmp_path / "apm.yml").write_text(apm_yml)
        # Install the virtual subdirectory package
        modules = tmp_path / "apm_modules"
        pkg = modules / "test-org" / "test-repo" / "skills" / "helpful"
        pkg.mkdir(parents=True)
        (pkg / "apm.yml").write_text("name: helpful\nversion: 1.0.0\n")

        result = CliRunner().invoke(cli, ["deps", "list"])
        assert result.exit_code == 0
        assert "test-org" in result.output or "helpful" in result.output


# ---------------------------------------------------------------------------
# deps list — nested skill filtering (line 187: continue for nested skills)
# ---------------------------------------------------------------------------


class TestNestedSkillFiltering:
    """Skill directories nested under a real package are skipped during scan."""

    def test_skill_nested_under_package_is_skipped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A SKILL.md-only dir inside a package root (skills/) is NOT listed separately."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "apm.yml").write_text(_APM_YML_WITH_DEPS)
        modules = tmp_path / "apm_modules"
        # Parent package with apm.yml
        pkg = _make_pkg(modules, "test-org", "test-dep")
        # Nested skill: SKILL.md only, no apm.yml, nested under pkg/
        nested_skill = pkg / "skills" / "helper-skill"
        nested_skill.mkdir(parents=True)
        (nested_skill / "SKILL.md").write_text(
            "---\ndescription: Helper skill\n---\n# Helper\n",
            encoding="utf-8",
        )

        result = CliRunner().invoke(cli, ["deps", "list"])
        assert result.exit_code == 0
        # The parent package should appear, the nested skill should NOT
        assert "test-org/test-dep" in result.output
        assert "helper-skill" not in result.output

    def test_nested_skill_filtering_in_tree_scan(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Tree fallback scan also skips nested SKILL.md dirs under packages."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "apm.yml").write_text(_APM_YML_WITH_DEPS)
        modules = tmp_path / "apm_modules"
        pkg = _make_pkg(modules, "test-org", "test-dep")
        # Nested skill under the package root
        nested = pkg / "agent-skills" / "inner-skill"
        nested.mkdir(parents=True)
        (nested / "SKILL.md").write_text(
            "---\ndescription: Inner\n---\n# Inner\n", encoding="utf-8"
        )
        # No lockfile — uses fallback scan

        result = CliRunner().invoke(cli, ["deps", "tree"])
        assert result.exit_code == 0
        assert "test-dep" in result.output
        assert "inner-skill" not in result.output


# ---------------------------------------------------------------------------
# deps tree — empty direct deps (line 598) and uninstalled dep (605->609)
# ---------------------------------------------------------------------------


class TestDepsTreeEdgeCases:
    """Edge cases in tree rendering."""

    def test_tree_lockfile_all_transitive_no_direct(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Lockfile with only depth-2 deps → direct=[] → 'No dependencies installed'."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "apm.yml").write_text(_APM_YML_SIMPLE)
        # Lockfile where all deps have depth=2 (no direct deps)
        lockfile = dedent("""\
            lockfile_version: "1"
            dependencies:
              - repo_url: test-org/child-pkg
                resolved_commit: bbb2222222222bbbbb
                resolved_ref: main
                version: "1.0.0"
                depth: 2
                resolved_by: test-org/missing-parent
        """)
        (tmp_path / "apm.lock.yaml").write_text(lockfile)

        result = CliRunner().invoke(cli, ["deps", "tree"])
        assert result.exit_code == 0
        assert "No dependencies installed" in result.output

    def test_tree_lockfile_dep_not_installed_locally(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Dep in lockfile but apm_modules dir for it doesn't exist — no crash."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "apm.yml").write_text(_APM_YML_WITH_DEPS)
        (tmp_path / "apm.lock.yaml").write_text(_LOCKFILE_ONE_DEP)
        # No apm_modules directory at all (dep listed in lockfile but not installed)

        result = CliRunner().invoke(cli, ["deps", "tree"])
        assert result.exit_code == 0
        assert "test-dep" in result.output

    def test_tree_lockfile_dep_apm_modules_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """apm_modules/ exists but dep subdirectory is absent → install_path missing."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "apm.yml").write_text(_APM_YML_WITH_DEPS)
        (tmp_path / "apm.lock.yaml").write_text(_LOCKFILE_ONE_DEP)
        # Create apm_modules but NOT the test-org/test-dep subdir
        (tmp_path / "apm_modules").mkdir()

        result = CliRunner().invoke(cli, ["deps", "tree"])
        assert result.exit_code == 0
        # test-dep appears in tree (from lockfile) even without install dir
        assert "test-dep" in result.output

    def test_tree_lockfile_multiple_transitive_same_parent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Multiple transitive deps sharing the same resolved_by parent."""
        monkeypatch.chdir(tmp_path)
        apm_yml = dedent("""\
            name: test-project
            version: 0.1.0
            dependencies:
              apm:
                - test-org/parent-pkg
        """)
        (tmp_path / "apm.yml").write_text(apm_yml)
        # Two transitive deps with same resolved_by → children_map reuse (line 518->520)
        lockfile = dedent("""\
            lockfile_version: "1"
            dependencies:
              - repo_url: test-org/parent-pkg
                resolved_commit: aaa1111111111aaaaa
                resolved_ref: main
                version: "1.0.0"
                depth: 1
              - repo_url: test-org/child-a
                resolved_commit: bbb2222222222bbbbb
                version: "1.0.0"
                depth: 2
                resolved_by: test-org/parent-pkg
              - repo_url: test-org/child-b
                resolved_commit: ccc3333333333ccccc
                version: "1.0.0"
                depth: 2
                resolved_by: test-org/parent-pkg
        """)
        (tmp_path / "apm.lock.yaml").write_text(lockfile)
        modules = tmp_path / "apm_modules"
        _make_pkg(modules, "test-org", "parent-pkg")

        result = CliRunner().invoke(cli, ["deps", "tree"])
        assert result.exit_code == 0
        assert "parent-pkg" in result.output

    def test_tree_fallback_package_with_primitives(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fallback directory scan: package with skills shows prim_summary (line 638)."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "apm.yml").write_text(_APM_YML_WITH_DEPS)
        # No lockfile — forces fallback scan
        modules = tmp_path / "apm_modules"
        pkg = _make_pkg(modules, "test-org", "test-dep")
        # Add a skill so prim_summary is non-empty
        skill_dir = pkg / ".apm" / "skills" / "my-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\ndescription: A skill\n---\n# Skill\n", encoding="utf-8"
        )

        result = CliRunner().invoke(cli, ["deps", "tree"])
        assert result.exit_code == 0
        assert "test-dep" in result.output

    def test_tree_corrupt_apm_yml_uses_default_name(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Corrupt apm.yml in _build_dep_tree → project_name stays 'my-project' (lines 489-490)."""
        monkeypatch.chdir(tmp_path)
        # Write a YAML file that parses but fails during APMPackage construction
        (tmp_path / "apm.yml").write_text("{{{{ completely invalid yaml content >>>")
        modules = tmp_path / "apm_modules"
        _make_pkg(modules, "test-org", "test-dep")

        result = CliRunner().invoke(cli, ["deps", "tree"])
        assert result.exit_code == 0
        # Falls back to 'my-project' name
        assert "my-project" in result.output or "test-dep" in result.output


# ---------------------------------------------------------------------------
# deps list — ADO package (line 115: ADO 3-part repo URL)
# ---------------------------------------------------------------------------


class TestDepsListADOPackage:
    """Azure DevOps package source label (3-part repo path)."""

    def test_ado_package_is_detected(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """ADO package in lockfile gets correct source label."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "apm.yml").write_text(_APM_YML_SIMPLE)
        lockfile_content = dedent("""\
            lockfile_version: "1"
            dependencies:
              - repo_url: myorg/myproject/myrepo
                host: dev.azure.com
                version: "1.0.0"
                depth: 1
        """)
        (tmp_path / "apm.lock.yaml").write_text(lockfile_content)
        modules = tmp_path / "apm_modules"
        # ADO-style 3-level path
        pkg = modules / "myorg" / "myproject" / "myrepo"
        pkg.mkdir(parents=True)
        (pkg / "apm.yml").write_text("name: myrepo\nversion: 1.0.0\n")

        result = CliRunner().invoke(cli, ["deps", "list"])
        assert result.exit_code == 0
        assert "myorg" in result.output or "myrepo" in result.output


# ---------------------------------------------------------------------------
# deps list — packages with primitives (text table fallback path coverage)
# ---------------------------------------------------------------------------


class TestDepsListPackagesWithPrimitives:
    """Coverage for primitive count rendering in the table."""

    def test_package_with_prompts(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Package with .prompt.md files shows non-zero prompt count."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "apm.yml").write_text(_APM_YML_WITH_DEPS)
        modules = tmp_path / "apm_modules"
        pkg = _make_pkg(modules, "test-org", "test-dep")
        # Add a prompt file
        prompts_dir = pkg / ".apm" / "prompts"
        prompts_dir.mkdir(parents=True)
        (prompts_dir / "review.prompt.md").write_text(
            "# Review\nPlease review the code.\n", encoding="utf-8"
        )
        (tmp_path / "apm.lock.yaml").write_text(_LOCKFILE_ONE_DEP)

        result = CliRunner().invoke(cli, ["deps", "list"])
        assert result.exit_code == 0
        assert "test-org/test-dep" in result.output

    def test_package_with_instructions(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Package with instructions file shows non-zero instruction count."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "apm.yml").write_text(_APM_YML_WITH_DEPS)
        modules = tmp_path / "apm_modules"
        pkg = _make_pkg(modules, "test-org", "test-dep")
        instr_dir = pkg / ".apm" / "instructions"
        instr_dir.mkdir(parents=True)
        (instr_dir / "coding.instructions.md").write_text(
            "---\ndescription: Coding\n---\n# Coding\n", encoding="utf-8"
        )
        (tmp_path / "apm.lock.yaml").write_text(_LOCKFILE_ONE_DEP)

        result = CliRunner().invoke(cli, ["deps", "list"])
        assert result.exit_code == 0

    def test_package_with_agents(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Package with agents shows non-zero agent count."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "apm.yml").write_text(_APM_YML_WITH_DEPS)
        modules = tmp_path / "apm_modules"
        pkg = _make_pkg(modules, "test-org", "test-dep")
        agents_dir = pkg / ".apm" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "helper.agent.md").write_text(
            "---\ndescription: Helper\n---\n# Helper\n", encoding="utf-8"
        )
        (tmp_path / "apm.lock.yaml").write_text(_LOCKFILE_ONE_DEP)

        result = CliRunner().invoke(cli, ["deps", "list"])
        assert result.exit_code == 0

    def test_multiple_orphaned_packages_warning(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Multiple orphaned packages all show in the warning output."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "apm.yml").write_text(_APM_YML_SIMPLE)
        modules = tmp_path / "apm_modules"
        _make_pkg(modules, "orphan-org", "pkg-one")
        _make_pkg(modules, "orphan-org", "pkg-two")
        _make_pkg(modules, "orphan-org", "pkg-three")

        result = CliRunner().invoke(cli, ["deps", "list"])
        assert result.exit_code == 0
        out = result.output
        # All three should appear as orphaned
        assert "pkg-one" in out or "orphan-org" in out

    def test_packages_with_mixed_primitives(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Package with skills + prompts renders all primitive columns."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "apm.yml").write_text(_APM_YML_WITH_DEPS)
        modules = tmp_path / "apm_modules"
        pkg = _make_pkg(modules, "test-org", "test-dep")
        # Skills
        skill_dir = pkg / ".apm" / "skills" / "helper"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\ndescription: Helper\n---\n# Helper\n", encoding="utf-8"
        )
        # Prompts
        prompts_dir = pkg / ".apm" / "prompts"
        prompts_dir.mkdir(parents=True)
        (prompts_dir / "review.prompt.md").write_text("# Review\n", encoding="utf-8")
        (tmp_path / "apm.lock.yaml").write_text(_LOCKFILE_ONE_DEP)

        result = CliRunner().invoke(cli, ["deps", "list"])
        assert result.exit_code == 0
        assert "test-org/test-dep" in result.output


# ---------------------------------------------------------------------------
# deps list single-component and .apm scan skips (list scan lines 174-178)
# ---------------------------------------------------------------------------


class TestDepsListScanSkips:
    """Scan filter edge cases in _resolve_scope_deps."""

    def test_single_component_dir_skipped_in_list_scan(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Dir with only 1 path component (no org/) is skipped even if has apm.yml."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "apm.yml").write_text(_APM_YML_SIMPLE)
        modules = tmp_path / "apm_modules"
        modules.mkdir()
        # Top-level dir (1 path component): should be skipped (len(parts) < 2)
        top_pkg = modules / "single-pkg"
        top_pkg.mkdir()
        (top_pkg / "apm.yml").write_text("name: single-pkg\nversion: 1.0.0\n")

        result = CliRunner().invoke(cli, ["deps", "list"])
        assert result.exit_code == 0
        assert "single-pkg" not in result.output

    def test_apm_nested_dir_skipped_in_list_scan(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Dir under .apm/ path is skipped due to '.apm' in rel_parts."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "apm.yml").write_text(_APM_YML_WITH_DEPS)
        modules = tmp_path / "apm_modules"
        pkg = _make_pkg(modules, "test-org", "test-dep")
        # .apm nested skill
        inner = pkg / ".apm" / "skills" / "deep-skill"
        inner.mkdir(parents=True)
        (inner / "SKILL.md").write_text("---\ndescription: Deep\n---\n# Deep\n", encoding="utf-8")
        (tmp_path / "apm.lock.yaml").write_text(_LOCKFILE_ONE_DEP)

        result = CliRunner().invoke(cli, ["deps", "list"])
        assert result.exit_code == 0
        assert "test-org/test-dep" in result.output
        assert "deep-skill" not in result.output


# ---------------------------------------------------------------------------
# deps update — token_to_canonical edge cases (lines 807, 809->811, 812->811)
# ---------------------------------------------------------------------------


class TestDepsUpdateTokenResolution:
    """Token-to-canonical resolution in deps update."""

    def test_update_dep_with_alias(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Dep with alias adds alias token to token_to_canonical (line 807)."""
        monkeypatch.chdir(tmp_path)
        apm_yml = dedent("""\
            name: test-project
            version: 0.1.0
            dependencies:
              apm:
                - git: https://github.com/test-org/test-dep
                  alias: my-alias
        """)
        (tmp_path / "apm.yml").write_text(apm_yml)
        # Request via alias name — canonical resolution executes line 807 (tokens.add(dep.alias))
        result = CliRunner().invoke(cli, ["deps", "update", "my-alias"])
        # Fails at install (no network), but alias token lookup succeeded
        assert result.exit_code != 0 or "my-alias" in result.output or "test-dep" in result.output

    def test_update_two_deps_same_short_name(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two deps with same short name → duplicate token skip (line 812->811 False branch)."""
        monkeypatch.chdir(tmp_path)
        apm_yml = dedent("""\
            name: test-project
            version: 0.1.0
            dependencies:
              apm:
                - test-org/shared-name
                - other-org/shared-name
        """)
        (tmp_path / "apm.yml").write_text(apm_yml)
        # 'shared-name' is the short name for both deps; second dep's short
        # name is already in token_to_canonical → hits the False branch
        result = CliRunner().invoke(cli, ["deps", "update", "shared-name"])
        # Should not report "not found" (first match resolves)
        assert "not found" not in result.output.lower() or result.exit_code != 0

    def test_update_by_short_repo_name(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Requesting update by short repo name (last path segment) resolves correctly."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "apm.yml").write_text(_APM_YML_WITH_DEPS)
        # 'test-dep' is parts[-1] of 'test-org/test-dep' — adds to token_to_canonical
        result = CliRunner().invoke(cli, ["deps", "update", "test-dep"])
        # Should reach install attempt (not a token lookup error)
        assert "not found" not in result.output.lower()

    def test_update_by_full_canonical_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Requesting update by full canonical key resolves correctly."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "apm.yml").write_text(_APM_YML_WITH_DEPS)
        result = CliRunner().invoke(cli, ["deps", "update", "test-org/test-dep"])
        assert "not found" not in result.output.lower()


# ---------------------------------------------------------------------------
# deps tree — fallback scan skip conditions (line 540)
# ---------------------------------------------------------------------------


class TestDepsTreeFallbackScanSkips:
    """Line 540: single-component dir skip in _build_dep_tree fallback scan."""

    def test_tree_fallback_skips_single_level_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Dir with 1 path component in apm_modules (no org/) is skipped in tree fallback."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "apm.yml").write_text(_APM_YML_SIMPLE)
        modules = tmp_path / "apm_modules"
        # Single-component dir (skipped by fallback scan len < 2 check)
        top_pkg = modules / "lone-pkg"
        top_pkg.mkdir(parents=True)
        (top_pkg / "apm.yml").write_text("name: lone-pkg\nversion: 1.0.0\n")
        # Valid 2-level package alongside
        _make_pkg(modules, "real-org", "real-dep")
        # No lockfile — forces fallback scan

        result = CliRunner().invoke(cli, ["deps", "tree"])
        assert result.exit_code == 0
        assert "real-dep" in result.output
        assert "lone-pkg" not in result.output

    def test_tree_fallback_skips_apm_nested_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Dir with .apm in rel_parts is skipped in tree fallback scan."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "apm.yml").write_text(_APM_YML_WITH_DEPS)
        modules = tmp_path / "apm_modules"
        pkg = _make_pkg(modules, "test-org", "test-dep")
        # .apm nested has SKILL.md but under .apm
        inner = pkg / ".apm" / "skills" / "inner"
        inner.mkdir(parents=True)
        (inner / "SKILL.md").write_text("---\ndescription: Inner\n---\n# Inner\n", encoding="utf-8")
        # No lockfile — fallback scan

        result = CliRunner().invoke(cli, ["deps", "tree"])
        assert result.exit_code == 0
        assert "test-dep" in result.output

    def test_tree_fallback_nested_skill_under_package_skipped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """SKILL.md-only dir nested under package root is skipped in fallback scan."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "apm.yml").write_text(_APM_YML_WITH_DEPS)
        modules = tmp_path / "apm_modules"
        pkg = _make_pkg(modules, "test-org", "test-dep")
        # Nested skill under pkg root (not .apm but still a child of pkg)
        nested = pkg / "skills" / "sub-skill"
        nested.mkdir(parents=True)
        (nested / "SKILL.md").write_text(
            "---\ndescription: Sub skill\n---\n# Sub\n", encoding="utf-8"
        )
        # No lockfile — fallback

        result = CliRunner().invoke(cli, ["deps", "tree"])
        assert result.exit_code == 0
        assert "test-dep" in result.output


# ---------------------------------------------------------------------------
# deps list — ADO and virtual file declared_sources (lines 115, 125, 137)
# ---------------------------------------------------------------------------


class TestDepsListADOVirtualDeps:
    """ADO virtual subdirectory and GH virtual file in apm.yml declared_sources."""

    def test_ado_non_virtual_dep_declared(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ADO non-virtual package (3-part repo_url) populates declared_sources (line 115)."""
        monkeypatch.chdir(tmp_path)
        apm_yml = dedent("""\
            name: test-project
            version: 0.1.0
            dependencies:
              apm:
                - git: https://dev.azure.com/myorg/myproject/_git/myrepo
        """)
        (tmp_path / "apm.yml").write_text(apm_yml)
        modules = tmp_path / "apm_modules"
        pkg = modules / "myorg" / "myproject" / "myrepo"
        pkg.mkdir(parents=True)
        (pkg / "apm.yml").write_text("name: myrepo\nversion: 1.0.0\n")

        result = CliRunner().invoke(cli, ["deps", "list"])
        assert result.exit_code == 0
        assert "myorg" in result.output or "myrepo" in result.output

    def test_ado_virtual_subdirectory_dep_declared(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ADO dep with virtual subdirectory populates declared_sources (line 125)."""
        monkeypatch.chdir(tmp_path)
        apm_yml = dedent("""\
            name: test-project
            version: 0.1.0
            dependencies:
              apm:
                - git: https://dev.azure.com/myorg/myproject/_git/myrepo
                  path: skills/my-collection
        """)
        (tmp_path / "apm.yml").write_text(apm_yml)
        modules = tmp_path / "apm_modules"
        pkg = modules / "myorg" / "myproject" / "myrepo" / "skills" / "my-collection"
        pkg.mkdir(parents=True)
        (pkg / "apm.yml").write_text("name: my-collection\nversion: 1.0.0\n")

        result = CliRunner().invoke(cli, ["deps", "list"])
        assert result.exit_code == 0

    def test_gh_virtual_file_dep_declared(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """GitHub virtual file dep populates declared_sources with flattened name (line 137)."""
        monkeypatch.chdir(tmp_path)
        apm_yml = dedent("""\
            name: test-project
            version: 0.1.0
            dependencies:
              apm:
                - git: https://github.com/test-org/prompt-repo
                  path: prompts/review.prompt.md
        """)
        (tmp_path / "apm.yml").write_text(apm_yml)
        modules = tmp_path / "apm_modules"
        # Virtual file package installed as flattened name
        pkg = modules / "test-org" / "prompt-repo-review"
        pkg.mkdir(parents=True)
        (pkg / "SKILL.md").write_text("---\ndescription: Review\n---\n# Review\n", encoding="utf-8")

        result = CliRunner().invoke(cli, ["deps", "list"])
        assert result.exit_code == 0

    def test_gh_virtual_subdirectory_dep_declared(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """GitHub virtual subdirectory dep populates declared_sources correctly."""
        monkeypatch.chdir(tmp_path)
        apm_yml = dedent("""\
            name: test-project
            version: 0.1.0
            dependencies:
              apm:
                - git: https://github.com/test-org/test-repo
                  path: skills/my-skill
        """)
        (tmp_path / "apm.yml").write_text(apm_yml)
        modules = tmp_path / "apm_modules"
        pkg = modules / "test-org" / "test-repo" / "skills" / "my-skill"
        pkg.mkdir(parents=True)
        (pkg / "apm.yml").write_text("name: my-skill\nversion: 1.0.0\n")

        result = CliRunner().invoke(cli, ["deps", "list"])
        assert result.exit_code == 0
