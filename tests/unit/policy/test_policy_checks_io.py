"""Tests for the policy checks engine (``run_policy_checks`` and individual checks) (filesystem, process and CLI boundary cases)."""

from __future__ import annotations

import os
import textwrap  # noqa: F401
from pathlib import Path

import pytest
import yaml

from apm_cli.models.apm_package import clear_apm_yml_cache
from apm_cli.policy.inheritance import merge_policies
from apm_cli.policy.models import CheckResult, CIAuditResult  # noqa: F401
from apm_cli.policy.policy_checks import (
    _check_includes_explicit,  # noqa: F401
    _check_unmanaged_files,
    _load_raw_apm_yml,
    run_policy_checks,
)
from apm_cli.policy.schema import (
    ApmPolicy,
    CompilationPolicy,
    CompilationTargetPolicy,
    DependencyPolicy,
    IntegrityPolicy,
    ManifestPolicy,
    SecurityPolicy,
    UnmanagedFilesPolicy,
)

pytestmark = [
    pytest.mark.component,
    pytest.mark.promise("P-OWN-read-only-lockfile-path", "P-OWN-apm-package-manifest-construction"),
]

# -- Helpers --------------------------------------------------------


def _write_apm_yml(project: Path, data: dict) -> None:
    """Write apm.yml from a dict."""
    (project / "apm.yml").write_text(yaml.dump(data, default_flow_style=False), encoding="utf-8")


def _write_lockfile(project: Path, data: dict) -> None:
    """Write apm.lock.yaml from a dict."""
    (project / "apm.lock.yaml").write_text(
        yaml.dump(data, default_flow_style=False), encoding="utf-8"
    )


def _make_lockfile(deps_data: list[dict]):
    """Create a LockFile from a list of dependency dicts."""
    from apm_cli.deps.lockfile import LockedDependency, LockFile

    lock = LockFile()
    for d in deps_data:
        lock.add_dependency(LockedDependency.from_dict(d))
    return lock


# -- Fixtures -------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_cache():
    """Clear the APMPackage parse cache between tests."""
    clear_apm_yml_cache()
    yield
    clear_apm_yml_cache()


# -- Check 1: dependency-allowlist ----------------------------------


# -- Check 2: dependency-denylist -----------------------------------


# -- Check 3: required-packages -------------------------------------


# -- Check 4: required-packages-deployed ----------------------------


# -- Check 4b: required-executable-untrusted ------------------------


# -- Check 5: required-package-version ------------------------------


# -- Check 6: transitive-depth --------------------------------------


# -- Check 7: mcp-allowlist -----------------------------------------


# -- Check 8: mcp-denylist ------------------------------------------


# -- Check 9: mcp-transport -----------------------------------------


# -- Check 10: mcp-self-defined -------------------------------------


# -- Check 11: compilation-target -----------------------------------


def _target_policy(*, allow=None, enforce=None) -> CompilationPolicy:
    return CompilationPolicy(target=CompilationTargetPolicy(allow=allow, enforce=enforce))


class TestAuditCompilationTargetFromManifest:
    """``apm audit`` judges the targets apm.yml declares (#3732).

    The plural ``targets:`` cases are ported from #3739 by @MohammedAlkindi.
    """

    @staticmethod
    def _audit(tmp_path: Path, manifest_targets: dict, policy: CompilationPolicy) -> CheckResult:
        _write_apm_yml(tmp_path, {"name": "test", "version": "1.0.0", **manifest_targets})
        result = run_policy_checks(tmp_path, ApmPolicy(compilation=policy), fail_fast=False)
        return next(check for check in result.checks if check.name == "compilation-target")

    def test_targets_plural_enforce_match(self, tmp_path):
        """Plural targets list containing the enforced value passes."""
        check = self._audit(
            tmp_path, {"targets": ["claude", "copilot"]}, _target_policy(enforce="claude")
        )
        assert check.passed

    def test_targets_plural_enforce_mismatch(self, tmp_path):
        """Plural targets list missing the enforced value fails."""
        check = self._audit(
            tmp_path, {"targets": ["cursor", "copilot"]}, _target_policy(enforce="claude")
        )
        assert not check.passed
        assert "enforced" in check.details[0]

    def test_targets_plural_allow_match(self, tmp_path):
        """Plural targets list with all items allowed passes."""
        check = self._audit(
            tmp_path,
            {"targets": ["claude", "copilot"]},
            _target_policy(allow=["claude", "copilot", "cursor"]),
        )
        assert check.passed

    def test_targets_plural_allow_disallowed(self, tmp_path):
        """Plural targets list with disallowed target fails (the #3732 bypass)."""
        check = self._audit(
            tmp_path, {"targets": ["claude", "cursor"]}, _target_policy(allow=["claude", "copilot"])
        )
        assert not check.passed
        assert "cursor" in check.message
        assert "apm.yml" in check.details[0]

    def test_no_targets_declared_passes(self, tmp_path):
        """A manifest without a target declaration has nothing to judge."""
        check = self._audit(tmp_path, {}, _target_policy(enforce="vscode"))
        assert check.passed

    def test_csv_target_is_split(self, tmp_path):
        """A compliant CSV value no longer fails as one literal token."""
        check = self._audit(
            tmp_path, {"target": "claude,copilot"}, _target_policy(allow=["claude", "copilot"])
        )
        assert check.passed

    @pytest.mark.parametrize("declaration", [{"target": "vscode"}, {"targets": ["vscode"]}])
    def test_vscode_alias_is_compliant(self, tmp_path, declaration):
        check = self._audit(tmp_path, declaration, _target_policy(allow=["claude", "copilot"]))
        assert check.passed

    def test_plural_legacy_all_stays_compliant(self, tmp_path):
        """``targets: [all]`` folds to omitted, so it must not fail as literal 'all'."""
        check = self._audit(tmp_path, {"targets": ["all"]}, _target_policy(allow=["claude"]))
        assert check.passed


# -- Check 12: compilation-strategy ---------------------------------


# -- Check 13: source-attribution -----------------------------------


# -- Check 14: required-manifest-fields -----------------------------


# -- Check 15: scripts-policy ---------------------------------------


# -- Check 16: unmanaged-files --------------------------------------


class TestUnmanagedFiles:
    def test_pass_ignore_action(self, tmp_path):
        policy = UnmanagedFilesPolicy(action="ignore")
        result = _check_unmanaged_files(tmp_path, None, policy)
        assert result.passed

    def test_pass_no_unmanaged(self, tmp_path):
        # Create a governance file that IS in the lockfile
        (tmp_path / ".github" / "prompts").mkdir(parents=True)
        (tmp_path / ".github" / "prompts" / "x.md").write_text("hi", encoding="utf-8")
        lock = _make_lockfile(
            [
                {
                    "repo_url": "org/pkg",
                    "deployed_files": [".github/prompts/x.md"],
                }
            ]
        )
        policy = UnmanagedFilesPolicy(action="deny", directories=[".github/prompts"])
        result = _check_unmanaged_files(tmp_path, lock, policy)
        assert result.passed

    def test_fail_deny_unmanaged(self, tmp_path):
        # Create a governance file NOT in lockfile
        (tmp_path / ".github" / "agents").mkdir(parents=True)
        (tmp_path / ".github" / "agents" / "rogue.md").write_text("rogue", encoding="utf-8")
        lock = _make_lockfile([{"repo_url": "org/pkg", "deployed_files": []}])
        policy = UnmanagedFilesPolicy(action="deny", directories=[".github/agents"])
        result = _check_unmanaged_files(tmp_path, lock, policy)
        assert not result.passed
        assert any(".github/agents/rogue.md" in d for d in result.details)

    def test_warn_unmanaged(self, tmp_path):
        (tmp_path / ".cursor" / "rules").mkdir(parents=True)
        (tmp_path / ".cursor" / "rules" / "extra.md").write_text("extra", encoding="utf-8")
        lock = _make_lockfile([{"repo_url": "org/pkg", "deployed_files": []}])
        policy = UnmanagedFilesPolicy(action="warn", directories=[".cursor/rules"])
        result = _check_unmanaged_files(tmp_path, lock, policy)
        assert result.passed
        assert len(result.details) > 0

    _LOCAL = ".claude/settings.local.json"
    _NOTE = (
        "Skipped .claude/settings.local.json: Claude Code's per-user local settings "
        "(excluded by default; name it in unmanaged_files.directories to govern it)"
    )

    @staticmethod
    def _write(root, *rels):
        for rel in rels:
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text("{}", encoding="utf-8")

    @staticmethod
    def _flagged(result):
        return [d.split(" ", 1)[0] for d in result.details[:-1]]

    @pytest.mark.parametrize("directories", [None, [".claude"], [".claude/../.claude"]])
    def test_deny_skips_harness_local_settings_only_at_their_exact_path(
        self, tmp_path, directories
    ):
        """#5472: Claude Code's per-user local settings file is skipped by default, under
        the default, a custom or an equivalent directory spelling, with one note; the
        same name elsewhere and a sibling .claude/settings.json are still governed."""
        self._write(
            tmp_path,
            self._LOCAL,
            ".claude/commands/settings.local.json",
            ".claude/settings.json",
        )
        policy = UnmanagedFilesPolicy(action="deny", directories=directories)
        result = _check_unmanaged_files(tmp_path, None, policy)
        assert not result.passed
        assert sorted(self._flagged(result)) == [
            ".claude/commands/settings.local.json",
            ".claude/settings.json",
        ], result.details
        assert result.notes == (self._NOTE,)

    @pytest.mark.parametrize(
        "directories",
        [
            [".claude/settings.local.json"],
            [".claude", ".claude/settings.local.json"],
            ["./.claude/../.claude/settings.local.json", ".claude"],
        ],
    )
    def test_deny_governs_harness_local_settings_the_policy_names(self, tmp_path, directories):
        """#5472 R1: the skip is a default; naming the file governs it, reported once."""
        self._write(tmp_path, self._LOCAL)
        policy = UnmanagedFilesPolicy(action="deny", directories=directories)
        result = _check_unmanaged_files(tmp_path, None, policy)
        assert not result.passed
        assert self._flagged(result) == [self._LOCAL], result.details
        assert result.notes == ()

    @pytest.mark.parametrize(
        "rel",
        [
            ".claude/Settings.Local.JSON",
            ".claude/settings.loc\u00e1l.json",
            ".claude/settings.loca\u0301l.json",
        ],
        ids=["case", "nfc", "nfd"],
    )
    def test_deny_governs_respelled_harness_local_names(self, tmp_path, rel):
        """#5472: the skip is byte-exact; case and Unicode respellings stay governed.

        The exempt name is ASCII, so NFC and NFD leave it unchanged and no
        normalized spelling of it can differ from it.
        """
        import unicodedata

        assert unicodedata.normalize("NFD", self._LOCAL) == self._LOCAL
        assert unicodedata.normalize("NFC", self._LOCAL) == self._LOCAL
        self._write(tmp_path, rel)
        result = _check_unmanaged_files(tmp_path, None, UnmanagedFilesPolicy(action="deny"))
        assert not result.passed
        assert len(self._flagged(result)) == 1, result.details
        assert self._flagged(result) != [self._LOCAL], result.details
        assert result.notes == ()

    def test_deny_never_relabels_a_path_through_a_symlinked_dir_as_exempt(self, tmp_path):
        """#5472: ``.claude/link/..`` walks ``.claude/commands`` when link points to
        ``commands/deep``; its settings.local.json is reported, never skipped as the
        exempt path (fail closed). POSIX keeps the as-walked label; Windows resolves
        the link while walking and reports the real path."""
        self._write(tmp_path, ".claude/commands/settings.local.json")
        (tmp_path / ".claude" / "commands" / "deep").mkdir()
        try:
            (tmp_path / ".claude" / "link").symlink_to(
                Path("commands") / "deep", target_is_directory=True
            )
        except OSError:
            pytest.skip("symlinks unavailable")
        policy = UnmanagedFilesPolicy(action="deny", directories=[".claude/link/.."])
        result = _check_unmanaged_files(tmp_path, None, policy)
        assert not result.passed
        [flagged] = self._flagged(result)
        assert flagged in {
            ".claude/link/../settings.local.json",
            ".claude/commands/settings.local.json",
        }, result.details
        assert flagged != self._LOCAL, result.details
        assert result.notes == ()

    @pytest.mark.parametrize("named", ["file", "dir"])
    def test_an_absolute_entry_inside_the_project_is_governed_as_relative(self, tmp_path, named):
        """#5472: an absolute entry inside the project means its project-relative path:
        naming the local settings file governs it; naming .claude keeps the default skip."""
        self._write(tmp_path, self._LOCAL)
        entry = tmp_path / (self._LOCAL if named == "file" else ".claude")
        policy = UnmanagedFilesPolicy(action="deny", directories=[str(entry)])
        result = _check_unmanaged_files(tmp_path, None, policy)
        if named == "file":
            assert not result.passed
            assert self._flagged(result) == [self._LOCAL], result.details
            assert result.notes == ()
        else:
            assert result.passed, result.details
            assert result.notes == (self._NOTE,)

    def test_naming_a_path_through_a_symlinked_dir_never_governs_the_root_local_file(
        self, tmp_path
    ):
        """#5472: ``.claude/link/../settings.local.json`` (link -> commands/deep) governs
        the file it physically names, reported once. POSIX follows the link before
        ``..`` (``commands/settings.local.json``), so the root local file stays
        skipped; Windows collapses ``..`` first, so the entry names the root local
        file and governs it. The filesystem decides which (``samefile``)."""
        self._write(tmp_path, self._LOCAL, ".claude/commands/settings.local.json")
        (tmp_path / ".claude" / "commands" / "deep").mkdir()
        try:
            (tmp_path / ".claude" / "link").symlink_to(
                Path("commands") / "deep", target_is_directory=True
            )
        except OSError:
            pytest.skip("symlinks unavailable")
        entry = ".claude/link/../settings.local.json"
        names_root = os.path.samefile(tmp_path / entry, tmp_path / self._LOCAL)
        policy = UnmanagedFilesPolicy(action="deny", directories=[".claude", entry])
        result = _check_unmanaged_files(tmp_path, None, policy)
        assert not result.passed
        flagged = self._flagged(result)
        assert len(flagged) == len(set(flagged)), result.details
        if names_root:
            assert sorted(flagged) == [".claude/commands/settings.local.json", self._LOCAL]
            assert result.notes == ()
        else:
            [only] = flagged
            assert only in {".claude/commands/settings.local.json", entry}, result.details
            assert result.notes == (self._NOTE,)

    @pytest.mark.parametrize("form", ["dotdot", "absolute", "link"])
    @pytest.mark.parametrize("action", ["deny", "warn"])
    def test_an_entry_outside_the_project_is_refused_by_name(self, tmp_path, form, action):
        """#5472: an entry outside the project (``..``, absolute, or a link out of it)
        is refused with the fix, never walked."""
        project = tmp_path / "proj"
        self._write(project, ".claude/commands/mine.md")
        self._write(tmp_path, "elsewhere/x.md")
        entry = {"dotdot": "../elsewhere", "absolute": str(tmp_path / "elsewhere")}.get(form, "out")
        if form == "link":
            try:
                (project / "out").symlink_to(tmp_path / "elsewhere", target_is_directory=True)
            except OSError:
                pytest.skip("symlinks unavailable")
        policy = UnmanagedFilesPolicy(action=action, directories=[entry])
        result = _check_unmanaged_files(project, None, policy)
        expected = (
            f"unmanaged_files.directories entry '{entry}' is outside the project; "
            "use a path relative to the project root"
        )
        assert result.passed is (action == "warn")
        assert result.message == expected
        assert result.details == [expected]

    @pytest.mark.parametrize("action", ["deny", "warn"])
    def test_an_outside_entry_is_refused_while_the_other_entries_are_scanned(
        self, tmp_path, action
    ):
        """#5472: only the outside entry is refused; the remaining entries still scan."""
        project = tmp_path / "proj"
        self._write(project, ".claude/commands/mine.md")
        self._write(tmp_path, "elsewhere/x.md")
        policy = UnmanagedFilesPolicy(action=action, directories=["../elsewhere", ".claude"])
        result = _check_unmanaged_files(project, None, policy)
        assert result.passed is (action == "warn")
        assert result.details[0] == (
            "unmanaged_files.directories entry '../elsewhere' is outside the project; "
            "use a path relative to the project root"
        )
        assert result.details[1].startswith(".claude/commands/mine.md -- not tracked")
        assert not any("x.md" in line for line in result.details), result.details

    @pytest.mark.parametrize("form", ["default", "named"])
    @pytest.mark.parametrize("action", ["deny", "warn"])
    def test_a_governance_dir_linked_out_of_the_project_is_refused(self, tmp_path, action, form):
        """#5472: ``.claude`` linking outside the project, as a default dir or a named
        entry, is refused (deny fails, as on main), never walked, and the refusal names
        only the entry, never the outside path."""
        project = tmp_path / "proj"
        project.mkdir()
        self._write(tmp_path, "elsewhere/commands/x.md")
        try:
            (project / ".claude").symlink_to(tmp_path / "elsewhere", target_is_directory=True)
        except OSError:
            pytest.skip("symlinks unavailable")
        directories = [".claude"] if form == "named" else None
        policy = UnmanagedFilesPolicy(action=action, directories=directories)
        result = _check_unmanaged_files(project, None, policy)
        refusal = (
            "unmanaged_files.directories entry '.claude' is outside the project; "
            "use a path relative to the project root"
            if form == "named"
            else "governance directory '.claude' links outside the project; "
            "replace the link with a directory inside the project"
        )
        assert result.passed is (action == "warn")
        assert result.details == [refusal]
        assert result.message == refusal
        text = " ".join([result.message, *result.details, *result.notes])
        assert "elsewhere" not in text and "x.md" not in text

    def test_deny_skips_only_the_link_path_of_a_symlinked_harness_local_file(self, tmp_path):
        """#5472: a symlinked local settings file skips its link path only; the governed
        file it points to is still reported at its own path."""
        self._write(tmp_path, ".claude/commands/mine.md")
        try:
            (tmp_path / self._LOCAL).symlink_to(Path("commands") / "mine.md")
        except OSError:
            pytest.skip("symlinks unavailable")
        result = _check_unmanaged_files(tmp_path, None, UnmanagedFilesPolicy(action="deny"))
        assert not result.passed
        assert self._flagged(result) == [".claude/commands/mine.md"], result.details
        assert result.notes == (self._NOTE,)

    def test_pass_empty_directory(self, tmp_path):
        policy = UnmanagedFilesPolicy(action="deny", directories=[".github/agents"])
        result = _check_unmanaged_files(tmp_path, None, policy)
        assert result.passed

    def test_pass_files_under_deployed_directory(self, tmp_path):
        """Files under a deployed directory (trailing /) are treated as managed."""
        skill_dir = tmp_path / ".github" / "skills" / "my-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("skill", encoding="utf-8")
        (skill_dir / "prompt.md").write_text("prompt", encoding="utf-8")
        lock = _make_lockfile(
            [
                {
                    "repo_url": "org/pkg",
                    "deployed_files": [".github/skills/my-skill/"],
                }
            ]
        )
        policy = UnmanagedFilesPolicy(action="deny", directories=[".github/skills"])
        result = _check_unmanaged_files(tmp_path, lock, policy)
        assert result.passed

    def test_rglob_cap_skips_check(self, tmp_path, monkeypatch):
        """When file count exceeds the safety cap, check passes with a warning."""
        from apm_cli.policy import unmanaged_scan

        monkeypatch.setattr(unmanaged_scan, "MAX_UNMANAGED_SCAN_FILES", 3)
        gov = tmp_path / ".github" / "agents"
        gov.mkdir(parents=True)
        for i in range(5):
            (gov / f"file{i}.md").write_text("x", encoding="utf-8")
        policy = UnmanagedFilesPolicy(action="deny", directories=[".github/agents"])
        result = _check_unmanaged_files(tmp_path, None, policy)
        assert result.passed
        assert "capped" in result.message.lower()

    # -- Regression tests for #1199 ------------------------------------

    def test_handrolled_instruction_flagged_as_unmanaged(self, tmp_path: Path) -> None:
        """Hand-rolled files in .github/instructions/ are flagged as unmanaged.

        Regression guard for #1199: a file that exists in the governance
        directory but is absent from every dependency's deployed_files must
        be reported as unmanaged when action='deny'.
        """
        instructions_dir = tmp_path / ".github" / "instructions"
        instructions_dir.mkdir(parents=True)
        # A file managed by a dependency.
        (instructions_dir / "managed.instructions.md").write_text("managed", encoding="utf-8")
        # A hand-rolled file not tracked by any dependency.
        (instructions_dir / "handrolled.instructions.md").write_text(
            "hand-rolled", encoding="utf-8"
        )
        lock = _make_lockfile(
            [
                {
                    "repo_url": "org/pkg",
                    "deployed_files": [".github/instructions/managed.instructions.md"],
                }
            ]
        )
        policy = UnmanagedFilesPolicy(action="deny", directories=[".github/instructions"])
        result = _check_unmanaged_files(tmp_path, lock, policy)
        assert not result.passed
        assert any(".github/instructions/handrolled.instructions.md" in d for d in result.details)

    def test_handrolled_file_not_masked_by_deployed_deps(self, tmp_path: Path) -> None:
        """A hand-rolled file is flagged even when sibling files are managed.

        Regression guard for #1199: the presence of dependency-deployed files
        in the same directory must not suppress the report for adjacent
        hand-rolled files.
        """
        instructions_dir = tmp_path / ".github" / "instructions"
        instructions_dir.mkdir(parents=True)
        # Two managed files deployed by the dependency.
        (instructions_dir / "a.instructions.md").write_text("a", encoding="utf-8")
        (instructions_dir / "b.instructions.md").write_text("b", encoding="utf-8")
        # One hand-rolled file alongside the managed ones.
        (instructions_dir / "rogue.instructions.md").write_text("rogue", encoding="utf-8")
        lock = _make_lockfile(
            [
                {
                    "repo_url": "org/team-pkg",
                    "deployed_files": [
                        ".github/instructions/a.instructions.md",
                        ".github/instructions/b.instructions.md",
                    ],
                }
            ]
        )
        policy = UnmanagedFilesPolicy(action="deny", directories=[".github/instructions"])
        result = _check_unmanaged_files(tmp_path, lock, policy)
        assert not result.passed
        assert any(".github/instructions/rogue.instructions.md" in d for d in result.details)
        # Managed files must NOT appear in the unmanaged details list.
        assert ".github/instructions/a.instructions.md" not in result.details
        assert ".github/instructions/b.instructions.md" not in result.details

    def test_handrolled_file_across_governance_dirs(self, tmp_path: Path) -> None:
        """Hand-rolled files in instructions/, hooks/, and agents/ are all flagged.

        Regression guard for #1199: the audit must behave consistently across
        every governance directory, not just .github/agents/.
        """
        for subdir in ("instructions", "hooks", "agents"):
            target = tmp_path / ".github" / subdir
            target.mkdir(parents=True)
            (target / f"handrolled.{subdir}.md").write_text("hand-rolled", encoding="utf-8")
        # No dependency deploys any of these files.
        lock = _make_lockfile([{"repo_url": "org/pkg", "deployed_files": []}])
        policy = UnmanagedFilesPolicy(
            action="deny",
            directories=[
                ".github/instructions",
                ".github/hooks",
                ".github/agents",
            ],
        )
        result = _check_unmanaged_files(tmp_path, lock, policy)
        assert not result.passed
        assert any(".github/instructions/handrolled.instructions.md" in d for d in result.details)
        assert any(".github/hooks/handrolled.hooks.md" in d for d in result.details)
        assert any(".github/agents/handrolled.agents.md" in d for d in result.details)

    def test_local_deployed_files_not_flagged(self, tmp_path: Path) -> None:
        """Files in local_deployed_files are treated as managed and not flagged.

        Regression guard for #1199: the virtual _SELF_KEY dependency that APM
        synthesises from local_deployed_files must exempt those paths from the
        unmanaged-files check.
        """
        from apm_cli.deps.lockfile import LockFile

        instructions_dir = tmp_path / ".github" / "instructions"
        instructions_dir.mkdir(parents=True)
        managed_path = ".github/instructions/managed.instructions.md"
        (tmp_path / managed_path).write_text("managed locally", encoding="utf-8")

        # Build a LockFile with local_deployed_files then round-trip through
        # YAML so the _SELF_KEY virtual entry is synthesised.
        lock = LockFile()
        lock.local_deployed_files = [managed_path]
        lock_file = tmp_path / "apm.lock.yaml"
        lock.write(lock_file)
        lock = LockFile.read(lock_file)

        policy = UnmanagedFilesPolicy(action="deny", directories=[".github/instructions"])
        result = _check_unmanaged_files(tmp_path, lock, policy)
        assert result.passed
        assert managed_path not in result.details

    def test_dir_prefix_does_not_mask_instructions(self, tmp_path: Path) -> None:
        """A trailing-slash directory entry only covers its own subtree.

        Regression guard for #1199: a deployed directory prefix such as
        '.github/skills/my-skill/' must not suppress unmanaged-file detection
        in a different governance directory (.github/instructions/).
        """
        skills_dir = tmp_path / ".github" / "skills" / "my-skill"
        skills_dir.mkdir(parents=True)
        (skills_dir / "SKILL.md").write_text("skill content", encoding="utf-8")

        instructions_dir = tmp_path / ".github" / "instructions"
        instructions_dir.mkdir(parents=True)
        (instructions_dir / "handrolled.instructions.md").write_text(
            "hand-rolled", encoding="utf-8"
        )

        lock = _make_lockfile(
            [
                {
                    "repo_url": "org/skills-pkg",
                    # Trailing slash marks an entire directory as managed.
                    "deployed_files": [".github/skills/my-skill/"],
                }
            ]
        )
        policy = UnmanagedFilesPolicy(
            action="deny",
            directories=[".github/skills", ".github/instructions"],
        )
        result = _check_unmanaged_files(tmp_path, lock, policy)
        assert not result.passed
        # The hand-rolled instruction file must be flagged...
        assert any(".github/instructions/handrolled.instructions.md" in d for d in result.details)
        # ...while files under the managed skill directory must NOT be flagged.
        assert ".github/skills/my-skill/SKILL.md" not in result.details

    def test_action_none_resolves_to_ignore(self, tmp_path):
        """action=None standalone: effective_action resolves to 'ignore', check passes."""
        policy = UnmanagedFilesPolicy(action=None)
        result = _check_unmanaged_files(tmp_path, None, policy)
        assert result.passed

    def test_action_none_inherits_parent_deny(self, tmp_path):
        """action=None + parent deny: merge propagates deny to child."""
        (tmp_path / ".github" / "agents").mkdir(parents=True)
        (tmp_path / ".github" / "agents" / "rogue.md").write_text("rogue", encoding="utf-8")
        parent = ApmPolicy(
            unmanaged_files=UnmanagedFilesPolicy(action="deny", directories=[".github/agents"])
        )
        child = ApmPolicy(unmanaged_files=UnmanagedFilesPolicy(action=None))
        merged = merge_policies(parent, child)
        lock = _make_lockfile([{"repo_url": "org/pkg", "deployed_files": []}])
        result = _check_unmanaged_files(tmp_path, lock, merged.unmanaged_files)
        assert not result.passed

    def test_rogue_file_caught_parent_deny_child_omits_block(self, tmp_path):
        """Rogue file is caught when parent says deny but child omits unmanaged_files block."""
        (tmp_path / ".github" / "agents").mkdir(parents=True)
        (tmp_path / ".github" / "agents" / "rogue.md").write_text("rogue", encoding="utf-8")
        parent = ApmPolicy(
            unmanaged_files=UnmanagedFilesPolicy(action="deny", directories=[".github/agents"])
        )
        child = ApmPolicy()  # child omits unmanaged_files entirely (defaults to action=None)
        merged = merge_policies(parent, child)
        lock = _make_lockfile([{"repo_url": "org/pkg", "deployed_files": []}])
        result = _check_unmanaged_files(tmp_path, lock, merged.unmanaged_files)
        assert not result.passed
        assert any(".github/agents/rogue.md" in d for d in result.details)

    def test_integration_chain_deny_propagates(self, tmp_path):
        """Integration chain: parent deny + child omits block -> merge -> check catches rogue file."""
        (tmp_path / ".github" / "agents").mkdir(parents=True)
        (tmp_path / ".github" / "agents" / "rogue.md").write_text("rogue", encoding="utf-8")
        parent_policy = ApmPolicy(
            unmanaged_files=UnmanagedFilesPolicy(action="deny", directories=[".github/agents"])
        )
        child_policy = ApmPolicy(unmanaged_files=UnmanagedFilesPolicy(action=None))
        merged = merge_policies(parent_policy, child_policy)
        lock = _make_lockfile([{"repo_url": "org/pkg", "deployed_files": []}])
        result = _check_unmanaged_files(tmp_path, lock, merged.unmanaged_files)
        assert not result.passed
        assert any(".github/agents/rogue.md" in d for d in result.details)


# -- Check 16b: enriched unmanaged-files (#1775) --------------------


def _detail_for(result, path: str) -> str | None:
    """Return the single enriched detail line that mentions *path*."""
    matches = [d for d in result.details if path in d]
    assert len(matches) == 1, f"{path} reported {len(matches)} time(s): {result.details}"
    return matches[0]


class TestUnmanagedEnrichment:
    """Each flagged file carries reason + type, surfaced in details."""

    def test_reason_not_tracked_present(self, tmp_path):
        (tmp_path / ".github" / "agents").mkdir(parents=True)
        (tmp_path / ".github" / "agents" / "rogue.agent.md").write_text("x", encoding="utf-8")
        lock = _make_lockfile([{"repo_url": "org/pkg", "deployed_files": []}])
        policy = UnmanagedFilesPolicy(action="deny", directories=[".github/agents"])
        result = _check_unmanaged_files(tmp_path, lock, policy)
        detail = _detail_for(result, ".github/agents/rogue.agent.md")
        assert "not tracked in apm.lock.yaml" in detail

    def test_type_present_in_detail(self, tmp_path):
        (tmp_path / ".github" / "agents").mkdir(parents=True)
        (tmp_path / ".github" / "agents" / "rogue.agent.md").write_text("x", encoding="utf-8")
        lock = _make_lockfile([{"repo_url": "org/pkg", "deployed_files": []}])
        policy = UnmanagedFilesPolicy(action="deny", directories=[".github/agents"])
        result = _check_unmanaged_files(tmp_path, lock, policy)
        detail = _detail_for(result, ".github/agents/rogue.agent.md")
        assert "[type: agent]" in detail

    def test_next_action_hint_present(self, tmp_path):
        # A non-empty report must carry exactly one actionable next-step hint
        # (track vs suppress) so a flagged file is self-resolving.
        (tmp_path / ".github" / "agents").mkdir(parents=True)
        (tmp_path / ".github" / "agents" / "rogue.agent.md").write_text("x", encoding="utf-8")
        lock = _make_lockfile([{"repo_url": "org/pkg", "deployed_files": []}])
        policy = UnmanagedFilesPolicy(action="deny", directories=[".github/agents"])
        result = _check_unmanaged_files(tmp_path, lock, policy)
        hints = [d for d in result.details if "unmanaged_files.exclude" in d and "apm install" in d]
        assert len(hints) == 1, f"expected one next-action hint, got: {result.details}"


class TestUnmanagedDenyConflict:
    """Unmanaged files matching apm-policy deny rules are surfaced as conflicts."""

    def test_dependency_deny_conflict(self, tmp_path):
        (tmp_path / ".github" / "agents").mkdir(parents=True)
        (tmp_path / ".github" / "agents" / "rogue.agent.md").write_text("x", encoding="utf-8")
        lock = _make_lockfile([{"repo_url": "org/pkg", "deployed_files": []}])
        policy = UnmanagedFilesPolicy(action="deny", directories=[".github/agents"])
        result = _check_unmanaged_files(
            tmp_path,
            lock,
            policy,
            dependency_deny=("**/rogue*",),
        )
        detail = _detail_for(result, ".github/agents/rogue.agent.md")
        assert "matches deny rule" in detail

    def test_mcp_deny_conflict(self, tmp_path):
        (tmp_path / ".vscode").mkdir(parents=True)
        (tmp_path / ".vscode" / "mcp.json").write_text("{}", encoding="utf-8")
        lock = _make_lockfile([{"repo_url": "org/pkg", "deployed_files": []}])
        policy = UnmanagedFilesPolicy(action="deny", directories=[".vscode"])
        result = _check_unmanaged_files(
            tmp_path,
            lock,
            policy,
            mcp_deny=("*mcp.json",),
        )
        detail = _detail_for(result, ".vscode/mcp.json")
        assert "matches deny rule" in detail

    def test_no_deny_conflict_when_no_match(self, tmp_path):
        (tmp_path / ".github" / "agents").mkdir(parents=True)
        (tmp_path / ".github" / "agents" / "clean.agent.md").write_text("x", encoding="utf-8")
        lock = _make_lockfile([{"repo_url": "org/pkg", "deployed_files": []}])
        policy = UnmanagedFilesPolicy(action="deny", directories=[".github/agents"])
        result = _check_unmanaged_files(
            tmp_path,
            lock,
            policy,
            dependency_deny=("**/evil*",),
        )
        detail = _detail_for(result, ".github/agents/clean.agent.md")
        assert "matches deny rule" not in detail


class TestUnmanagedExcludeSuppression:
    """Excluded paths are not reported."""

    def test_excluded_path_not_reported(self, tmp_path):
        agents = tmp_path / ".github" / "agents"
        agents.mkdir(parents=True)
        (agents / "harness.agent.md").write_text("x", encoding="utf-8")
        (agents / "rogue.agent.md").write_text("x", encoding="utf-8")
        lock = _make_lockfile([{"repo_url": "org/pkg", "deployed_files": []}])
        policy = UnmanagedFilesPolicy(
            action="deny",
            directories=[".github/agents"],
            exclude=[".github/agents/harness.agent.md"],
        )
        result = _check_unmanaged_files(tmp_path, lock, policy)
        assert not any(".github/agents/harness.agent.md" in d for d in result.details)
        assert any(".github/agents/rogue.agent.md" in d for d in result.details)

    def test_exclude_glob_suppresses_subtree(self, tmp_path):
        target = tmp_path / ".github" / "agents" / "vendor"
        target.mkdir(parents=True)
        (target / "a.agent.md").write_text("x", encoding="utf-8")
        lock = _make_lockfile([{"repo_url": "org/pkg", "deployed_files": []}])
        policy = UnmanagedFilesPolicy(
            action="deny",
            directories=[".github/agents"],
            exclude=[".github/agents/vendor/**"],
        )
        result = _check_unmanaged_files(tmp_path, lock, policy)
        assert result.passed
        assert not result.details


class TestUnmanagedSymlinkGuard:
    """Symlinks pointing outside the workspace are not followed."""

    def test_symlink_outside_project_not_followed(self, tmp_path):
        outside = tmp_path.parent / "outside_target.md"
        outside.write_text("secret", encoding="utf-8")
        agents = tmp_path / ".github" / "agents"
        agents.mkdir(parents=True)
        link = agents / "escape.agent.md"
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks not supported on this platform")
        lock = _make_lockfile([{"repo_url": "org/pkg", "deployed_files": []}])
        policy = UnmanagedFilesPolicy(action="deny", directories=[".github/agents"])
        result = _check_unmanaged_files(tmp_path, lock, policy)
        # The traversal must not follow the symlink out of the workspace.
        assert not any("escape.agent.md" in d for d in result.details)

    def test_symlinked_directory_outside_not_recursed(self, tmp_path):
        # A symlinked *directory* resolving outside the workspace must not be
        # recursed into -- its contents must never surface. rglob() follows
        # directory symlinks, so the scan must use a non-following walk.
        outside = tmp_path.parent / "outside_tree"
        outside.mkdir()
        (outside / "secret.agent.md").write_text("secret", encoding="utf-8")
        agents = tmp_path / ".github" / "agents"
        agents.mkdir(parents=True)
        link = agents / "linked"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks not supported on this platform")
        lock = _make_lockfile([{"repo_url": "org/pkg", "deployed_files": []}])
        policy = UnmanagedFilesPolicy(action="deny", directories=[".github/agents"])
        result = _check_unmanaged_files(tmp_path, lock, policy)
        assert not any("secret.agent.md" in d for d in result.details)
        assert not any("linked" in d for d in result.details)


class TestUnmanagedNonDuplication:
    """A file is reported exactly once -- one unified unmanaged report."""

    def test_file_reported_once_even_with_deny_conflict(self, tmp_path):
        (tmp_path / ".github" / "agents").mkdir(parents=True)
        (tmp_path / ".github" / "agents" / "rogue.agent.md").write_text("x", encoding="utf-8")
        lock = _make_lockfile([{"repo_url": "org/pkg", "deployed_files": []}])
        policy = UnmanagedFilesPolicy(action="deny", directories=[".github/agents"])
        result = _check_unmanaged_files(
            tmp_path,
            lock,
            policy,
            dependency_deny=("**/rogue*",),
            mcp_deny=("**/rogue*",),
        )
        occurrences = [d for d in result.details if ".github/agents/rogue.agent.md" in d]
        assert len(occurrences) == 1


# -- Integration: run_policy_checks ---------------------------------
class TestRunPolicyChecks:
    def test_returns_all_22_checks(self, tmp_path):
        """Full run should produce exactly 22 checks."""
        _write_apm_yml(
            tmp_path,
            {
                "name": "test",
                "version": "1.0.0",
                "dependencies": {"apm": ["owner/repo"]},
            },
        )
        _write_lockfile(
            tmp_path,
            {
                "lockfile_version": "1",
                "generated_at": "2025-01-01T00:00:00Z",
                "dependencies": [
                    {"repo_url": "owner/repo", "deployed_files": [".github/prompts/x.md"]}
                ],
            },
        )
        (tmp_path / ".github" / "prompts").mkdir(parents=True)
        (tmp_path / ".github" / "prompts" / "x.md").write_text("ok", encoding="utf-8")

        policy = ApmPolicy()
        result = run_policy_checks(tmp_path, policy)
        assert len(result.checks) == 22
        # Default policy = all checks pass
        assert result.passed

    @pytest.mark.parametrize(
        ("enabled", "source", "expected_pass"),
        (
            (False, None, True),
            (True, "local", True),
            (True, None, False),
        ),
        ids=("disabled", "local-exempt", "non-local-missing"),
    )
    def test_dependency_content_hash_policy_uses_integrity_owner(
        self,
        tmp_path: Path,
        enabled: bool,
        source: str | None,
        expected_pass: bool,
    ) -> None:
        """Audit hash verdicts match the canonical install integrity rule."""
        _write_apm_yml(
            tmp_path,
            {
                "name": "test",
                "version": "1.0.0",
                "dependencies": {"apm": ["owner/repo#v1.0.0"]},
            },
        )
        dependency = {
            "repo_url": "owner/repo",
            "resolved_ref": "v1.0.0",
            "resolved_commit": "a" * 40,
        }
        if source is not None:
            dependency["source"] = source
        _write_lockfile(
            tmp_path,
            {
                "lockfile_version": "1",
                "generated_at": "2025-01-01T00:00:00Z",
                "dependencies": [dependency],
            },
        )
        policy = ApmPolicy(
            security=SecurityPolicy(
                integrity=IntegrityPolicy(require_hashes=enabled),
            )
        )

        result = run_policy_checks(tmp_path, policy, fail_fast=False)
        check = next(check for check in result.checks if check.name == "dependency-content-hashes")

        assert check.passed is expected_pass
        if not expected_pass:
            assert "require_hashes" in check.details[0]

    def test_mixed_pass_fail(self, tmp_path):
        """Denied dep should fail while other checks pass."""
        _write_apm_yml(
            tmp_path,
            {
                "name": "test",
                "version": "1.0.0",
                "dependencies": {"apm": ["evil/malware"]},
            },
        )
        _write_lockfile(
            tmp_path,
            {
                "lockfile_version": "1",
                "generated_at": "2025-01-01T00:00:00Z",
                "dependencies": [{"repo_url": "evil/malware", "deployed_files": ["x.md"]}],
            },
        )

        policy = ApmPolicy(dependencies=DependencyPolicy(deny=["evil/*"]))
        result = run_policy_checks(tmp_path, policy)
        assert not result.passed
        failed_names = {c.name for c in result.checks if not c.passed}
        assert "dependency-denylist" in failed_names

    def test_no_apm_yml_returns_empty(self, tmp_path):
        policy = ApmPolicy()
        result = run_policy_checks(tmp_path, policy)
        assert len(result.checks) == 0
        assert result.passed

    def test_multiple_failures(self, tmp_path):
        """Multiple policy violations accumulate."""
        _write_apm_yml(
            tmp_path,
            {
                "name": "test",
                "version": "1.0.0",
                "dependencies": {"apm": ["evil/pkg"]},
                "scripts": {"build": "make"},
            },
        )
        _write_lockfile(
            tmp_path,
            {
                "lockfile_version": "1",
                "generated_at": "2025-01-01T00:00:00Z",
                "dependencies": [{"repo_url": "evil/pkg", "deployed_files": ["x.md"]}],
            },
        )

        policy = ApmPolicy(
            dependencies=DependencyPolicy(deny=["evil/*"]),
            manifest=ManifestPolicy(scripts="deny", required_fields=["license"]),
        )
        result = run_policy_checks(tmp_path, policy, fail_fast=False)
        failed_names = {c.name for c in result.checks if not c.passed}
        assert "dependency-denylist" in failed_names
        assert "scripts-policy" in failed_names
        assert "required-manifest-fields" in failed_names


# -- Group 1: _load_raw_apm_yml malformed-manifest tests -----------


class TestLoadRawApmYml:
    """Tests for _load_raw_apm_yml with malformed content (fix #936)."""

    def test_malformed_yaml_returns_none_and_logs(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Malformed YAML returns None and logs a WARNING."""
        (tmp_path / "apm.yml").write_text(": :\n  bad: [yaml\n", encoding="utf-8")
        import logging

        with caplog.at_level(logging.WARNING, logger="apm_cli.policy.policy_checks"):
            result = _load_raw_apm_yml(tmp_path)
        assert result is None
        assert "Malformed YAML" in caplog.text

    def test_non_dict_yaml_returns_none_and_logs(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Non-mapping YAML (bare list) returns None and logs a WARNING."""
        (tmp_path / "apm.yml").write_text("- item1\n- item2\n", encoding="utf-8")
        import logging

        with caplog.at_level(logging.WARNING, logger="apm_cli.policy.policy_checks"):
            result = _load_raw_apm_yml(tmp_path)
        assert result is None
        assert "not a YAML mapping" in caplog.text

    def test_scalar_yaml_returns_none_and_logs(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Plain scalar YAML returns None and logs a WARNING."""
        (tmp_path / "apm.yml").write_text("just a string\n", encoding="utf-8")
        import logging

        with caplog.at_level(logging.WARNING, logger="apm_cli.policy.policy_checks"):
            result = _load_raw_apm_yml(tmp_path)
        assert result is None
        assert "not a YAML mapping" in caplog.text

    def test_empty_file_returns_none_and_logs(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Empty apm.yml (yaml.safe_load returns None) is not a dict."""
        (tmp_path / "apm.yml").write_text("", encoding="utf-8")
        import logging

        with caplog.at_level(logging.WARNING, logger="apm_cli.policy.policy_checks"):
            result = _load_raw_apm_yml(tmp_path)
        assert result is None
        assert "not a YAML mapping" in caplog.text

    def test_missing_file_returns_none_silently(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Missing apm.yml returns None without raising or logging."""
        import logging

        with caplog.at_level(logging.DEBUG, logger="apm_cli.policy.policy_checks"):
            result = _load_raw_apm_yml(tmp_path)
        assert result is None
        assert caplog.records == []

    def test_valid_yaml_returns_dict(self, tmp_path: Path) -> None:
        """Valid YAML mapping is returned as a dict unchanged."""
        (tmp_path / "apm.yml").write_text("name: test\nversion: '1.0'\n", encoding="utf-8")
        result = _load_raw_apm_yml(tmp_path)
        assert result == {"name": "test", "version": "1.0"}


# -- Group 2: run_policy_checks malformed-manifest tests -----------


class TestRunPolicyChecksMalformedManifest:
    """run_policy_checks must fail-closed on malformed apm.yml (fix #936)."""

    def test_malformed_yaml_produces_failing_check(self, tmp_path: Path) -> None:
        """Malformed YAML appends manifest-parse check with passed=False."""
        (tmp_path / "apm.yml").write_text(": :\n  bad: [yaml\n", encoding="utf-8")
        clear_apm_yml_cache()
        policy = ApmPolicy()
        result = run_policy_checks(tmp_path, policy)
        assert not result.passed
        parse_checks = [c for c in result.checks if c.name == "manifest-parse"]
        assert len(parse_checks) == 1
        assert not parse_checks[0].passed
        assert "Cannot parse apm.yml" in parse_checks[0].message

    def test_non_dict_yaml_produces_failing_check(self, tmp_path: Path) -> None:
        """Non-dict YAML (bare list) triggers a manifest-parse failing check."""
        (tmp_path / "apm.yml").write_text("- item1\n- item2\n", encoding="utf-8")
        clear_apm_yml_cache()
        policy = ApmPolicy()
        result = run_policy_checks(tmp_path, policy)
        assert not result.passed
        parse_checks = [c for c in result.checks if c.name == "manifest-parse"]
        assert len(parse_checks) == 1
        assert not parse_checks[0].passed

    def test_empty_file_produces_failing_check(self, tmp_path: Path) -> None:
        """Empty apm.yml triggers ValueError from from_apm_yml."""
        (tmp_path / "apm.yml").write_text("", encoding="utf-8")
        clear_apm_yml_cache()
        policy = ApmPolicy()
        result = run_policy_checks(tmp_path, policy)
        assert not result.passed
        parse_checks = [c for c in result.checks if c.name == "manifest-parse"]
        assert len(parse_checks) == 1
        assert not parse_checks[0].passed

    def test_missing_apm_yml_returns_empty_passing_result(self, tmp_path: Path) -> None:
        """No apm.yml means nothing to check -- result is empty and passes."""
        clear_apm_yml_cache()
        policy = ApmPolicy()
        result = run_policy_checks(tmp_path, policy)
        assert result.passed
        assert len(result.checks) == 0

    # -- Group 5 regression guard --

    def test_malformed_yaml_does_not_silently_pass(self, tmp_path: Path) -> None:
        """Regression: malformed YAML must NOT produce an empty passing result.

        Before fix #936, malformed apm.yml returned CIAuditResult() with
        no checks, which trivially passed (all([]) is True).
        """
        (tmp_path / "apm.yml").write_text("{{invalid yaml}}\n", encoding="utf-8")
        clear_apm_yml_cache()
        policy = ApmPolicy()
        result = run_policy_checks(tmp_path, policy)
        assert not result.passed, "Malformed apm.yml must not silently pass policy checks"
