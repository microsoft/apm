"""Integration coverage for newly added APM modules.

Targets pure functions and data structures in:
- apm_cli.integration.copilot_app_db (slug helpers, URI translation, WorkflowRow, resolvers)
- apm_cli.integration.targets (PrimitiveMapping, TargetProfile, KNOWN_TARGETS, active_targets)
- apm_cli.integration.prompt_integrator (find_prompt_files, copy_prompt, get_target_filename)
- apm_cli.core.experimental (FLAGS, normalise/validate, is_enabled, overrides query helpers)

All tests are hermetic: no network I/O, no real SQLite DB, no real home directory access.
External I/O is isolated via monkeypatch or pytest tmp_path fixtures.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

# ===========================================================================
# copilot_app_db -- constants
# ===========================================================================


class TestCopilotAppDbConstants:
    """Verify that module-level constants have their documented values."""

    def test_uri_scheme_value(self) -> None:
        from apm_cli.integration.copilot_app_db import COPILOT_APP_URI_SCHEME

        assert COPILOT_APP_URI_SCHEME == "copilot-app-db://"

    def test_lockfile_prefix_value(self) -> None:
        from apm_cli.integration.copilot_app_db import COPILOT_APP_LOCKFILE_PREFIX

        assert COPILOT_APP_LOCKFILE_PREFIX == "copilot-app-db://workflows/"

    def test_namespace_prefix_value(self) -> None:
        from apm_cli.integration.copilot_app_db import _NAMESPACE_PREFIX

        assert _NAMESPACE_PREFIX == "apm--"

    def test_valid_intervals_contains_expected_values(self) -> None:
        from apm_cli.integration.copilot_app_db import _VALID_INTERVALS

        assert "manual" in _VALID_INTERVALS
        assert "hourly" in _VALID_INTERVALS
        assert "daily" in _VALID_INTERVALS
        assert "weekly" in _VALID_INTERVALS

    def test_valid_modes_contains_expected_values(self) -> None:
        from apm_cli.integration.copilot_app_db import _VALID_MODES

        assert "interactive" in _VALID_MODES
        assert "plan" in _VALID_MODES
        assert "autopilot" not in _VALID_MODES


# ===========================================================================
# copilot_app_db -- _slugify
# ===========================================================================


class TestSlugify:
    """_slugify reduces a token to safe ASCII-alphanumeric + hyphen/underscore."""

    def test_alphanumeric_passthrough(self) -> None:
        from apm_cli.integration.copilot_app_db import _slugify

        assert _slugify("hello123") == "hello123"

    def test_special_chars_replaced_with_hyphen(self) -> None:
        from apm_cli.integration.copilot_app_db import _slugify

        assert _slugify("hello world") == "hello-world"

    def test_at_sign_replaced(self) -> None:
        from apm_cli.integration.copilot_app_db import _slugify

        result = _slugify("@myorg")
        assert "@" not in result
        assert result in {"-myorg", "myorg"}

    def test_leading_trailing_hyphens_stripped(self) -> None:
        from apm_cli.integration.copilot_app_db import _slugify

        result = _slugify("!hello!")
        assert not result.startswith("-")
        assert not result.endswith("-")

    def test_empty_string_returns_unknown(self) -> None:
        from apm_cli.integration.copilot_app_db import _slugify

        assert _slugify("") == "unknown"

    def test_only_special_chars_returns_unknown(self) -> None:
        from apm_cli.integration.copilot_app_db import _slugify

        assert _slugify("!!!") == "unknown"

    def test_output_is_lowercased(self) -> None:
        from apm_cli.integration.copilot_app_db import _slugify

        assert _slugify("MyOrg") == "myorg"

    def test_underscores_preserved(self) -> None:
        from apm_cli.integration.copilot_app_db import _slugify

        assert _slugify("my_token") == "my_token"

    def test_hyphens_preserved(self) -> None:
        from apm_cli.integration.copilot_app_db import _slugify

        assert _slugify("my-token") == "my-token"


# ===========================================================================
# copilot_app_db -- namespaced_id
# ===========================================================================


class TestNamespacedId:
    """namespaced_id builds apm--<owner>--<pkg>--<prompt>."""

    def test_basic_format(self) -> None:
        from apm_cli.integration.copilot_app_db import namespaced_id

        result = namespaced_id("myorg", "mypkg", "myprompt")
        assert result == "apm--myorg--mypkg--myprompt"

    def test_starts_with_apm_prefix(self) -> None:
        from apm_cli.integration.copilot_app_db import namespaced_id

        result = namespaced_id("owner", "pkg", "prompt")
        assert result.startswith("apm--")

    def test_slugify_applied_to_segments(self) -> None:
        from apm_cli.integration.copilot_app_db import namespaced_id

        result = namespaced_id("My Org", "my pkg", "My Prompt")
        assert " " not in result
        assert result == "apm--my-org--my-pkg--my-prompt"

    def test_uppercase_lowercased(self) -> None:
        from apm_cli.integration.copilot_app_db import namespaced_id

        result = namespaced_id("OrgName", "PkgName", "PromptName")
        assert result == "apm--orgname--pkgname--promptname"

    def test_double_hyphen_separator(self) -> None:
        from apm_cli.integration.copilot_app_db import namespaced_id

        result = namespaced_id("a", "b", "c")
        parts = result.split("--")
        assert parts == ["apm", "a", "b", "c"]


# ===========================================================================
# copilot_app_db -- is_apm_managed_id
# ===========================================================================


class TestIsApmManagedId:
    """is_apm_managed_id checks for the apm-- prefix."""

    def test_returns_true_for_apm_prefixed_id(self) -> None:
        from apm_cli.integration.copilot_app_db import is_apm_managed_id

        assert is_apm_managed_id("apm--owner--pkg--prompt") is True

    def test_returns_false_for_non_apm_id(self) -> None:
        from apm_cli.integration.copilot_app_db import is_apm_managed_id

        assert is_apm_managed_id("user-created-workflow") is False

    def test_returns_false_for_partial_prefix(self) -> None:
        from apm_cli.integration.copilot_app_db import is_apm_managed_id

        assert is_apm_managed_id("apm-only-one-hyphen") is False

    def test_returns_false_for_empty_string(self) -> None:
        from apm_cli.integration.copilot_app_db import is_apm_managed_id

        assert is_apm_managed_id("") is False


# ===========================================================================
# copilot_app_db -- to_lockfile_uri
# ===========================================================================


class TestToLockfileUri:
    """to_lockfile_uri encodes a workflow id as a lockfile URI."""

    def test_produces_correct_uri(self) -> None:
        from apm_cli.integration.copilot_app_db import to_lockfile_uri

        result = to_lockfile_uri("apm--foo--bar--baz")
        assert result == "copilot-app-db://workflows/apm--foo--bar--baz"

    def test_uri_starts_with_lockfile_prefix(self) -> None:
        from apm_cli.integration.copilot_app_db import (
            COPILOT_APP_LOCKFILE_PREFIX,
            to_lockfile_uri,
        )

        result = to_lockfile_uri("apm--a--b--c")
        assert result.startswith(COPILOT_APP_LOCKFILE_PREFIX)

    def test_raises_for_non_apm_id(self) -> None:
        from apm_cli.integration.copilot_app_db import to_lockfile_uri

        with pytest.raises(ValueError, match="non-APM"):
            to_lockfile_uri("user-workflow")


# ===========================================================================
# copilot_app_db -- from_lockfile_uri
# ===========================================================================


class TestFromLockfileUri:
    """from_lockfile_uri decodes a copilot-app-db:// URI back to workflow id."""

    def test_round_trip_with_to_lockfile_uri(self) -> None:
        from apm_cli.integration.copilot_app_db import from_lockfile_uri, to_lockfile_uri

        wf_id = "apm--owner--pkg--prompt"
        assert from_lockfile_uri(to_lockfile_uri(wf_id)) == wf_id

    def test_raises_for_wrong_scheme(self) -> None:
        from apm_cli.integration.copilot_app_db import from_lockfile_uri

        with pytest.raises(ValueError, match="Not a copilot-app lockfile URI"):
            from_lockfile_uri("cowork://skills/apm--a--b--c")

    def test_raises_for_non_apm_id_in_valid_scheme(self) -> None:
        from apm_cli.integration.copilot_app_db import from_lockfile_uri

        with pytest.raises(ValueError, match="non-APM"):
            from_lockfile_uri("copilot-app-db://workflows/user-workflow")


# ===========================================================================
# copilot_app_db -- is_copilot_app_uri
# ===========================================================================


class TestIsCopilotAppUri:
    """is_copilot_app_uri checks for the copilot-app-db:// scheme."""

    def test_returns_true_for_matching_scheme(self) -> None:
        from apm_cli.integration.copilot_app_db import is_copilot_app_uri

        assert is_copilot_app_uri("copilot-app-db://workflows/apm--a--b--c") is True

    def test_returns_false_for_other_scheme(self) -> None:
        from apm_cli.integration.copilot_app_db import is_copilot_app_uri

        assert is_copilot_app_uri(".github/prompts/my-prompt.prompt.md") is False

    def test_returns_false_for_cowork_scheme(self) -> None:
        from apm_cli.integration.copilot_app_db import is_copilot_app_uri

        assert is_copilot_app_uri("cowork://skills/my-skill") is False

    def test_returns_false_for_empty_string(self) -> None:
        from apm_cli.integration.copilot_app_db import is_copilot_app_uri

        assert is_copilot_app_uri("") is False


# ===========================================================================
# copilot_app_db -- WorkflowRow defaults
# ===========================================================================


class TestWorkflowRowDefaults:
    """WorkflowRow dataclass default values match documented contract."""

    def test_required_fields_accepted(self) -> None:
        from apm_cli.integration.copilot_app_db import WorkflowRow

        row = WorkflowRow(id="apm--a--b--c", name="My Workflow", prompt="Do something")
        assert row.id == "apm--a--b--c"
        assert row.name == "My Workflow"
        assert row.prompt == "Do something"

    def test_default_interval_is_manual(self) -> None:
        from apm_cli.integration.copilot_app_db import WorkflowRow

        row = WorkflowRow(id="apm--a--b--c", name="N", prompt="P")
        assert row.interval == "manual"

    def test_default_enabled_is_zero(self) -> None:
        from apm_cli.integration.copilot_app_db import WorkflowRow

        row = WorkflowRow(id="apm--a--b--c", name="N", prompt="P")
        assert row.enabled == 0

    def test_default_schedule_hour(self) -> None:
        from apm_cli.integration.copilot_app_db import WorkflowRow

        row = WorkflowRow(id="apm--a--b--c", name="N", prompt="P")
        assert row.schedule_hour == 9

    def test_default_schedule_day(self) -> None:
        from apm_cli.integration.copilot_app_db import WorkflowRow

        row = WorkflowRow(id="apm--a--b--c", name="N", prompt="P")
        assert row.schedule_day == 1

    def test_optional_model_defaults_none(self) -> None:
        from apm_cli.integration.copilot_app_db import WorkflowRow

        row = WorkflowRow(id="apm--a--b--c", name="N", prompt="P")
        assert row.model is None
        assert row.reasoning_effort is None
        assert row.mode is None

    def test_custom_values_stored(self) -> None:
        from apm_cli.integration.copilot_app_db import WorkflowRow

        row = WorkflowRow(
            id="apm--o--p--q",
            name="Custom",
            prompt="Run audit",
            interval="daily",
            schedule_hour=6,
            schedule_day=3,
            enabled=0,
            model="gpt-4o",
            reasoning_effort="low",
            mode="plan",
        )
        assert row.interval == "daily"
        assert row.schedule_hour == 6
        assert row.schedule_day == 3
        assert row.model == "gpt-4o"
        assert row.mode == "plan"


# ===========================================================================
# copilot_app_db -- resolve_copilot_app_db_path
# ===========================================================================


class TestResolveCopilotAppDbPath:
    """resolve_copilot_app_db_path resolves the DB path via env var or home fallback."""

    def test_env_var_pointing_to_existing_file_returns_path(self, tmp_path: Path) -> None:
        from apm_cli.integration.copilot_app_db import resolve_copilot_app_db_path

        db = tmp_path / "data.db"
        db.touch()
        with patch.dict("os.environ", {"APM_COPILOT_APP_DB": str(db)}):
            result = resolve_copilot_app_db_path()
        assert result == db

    def test_env_var_pointing_to_missing_file_returns_none(self, tmp_path: Path) -> None:
        from apm_cli.integration.copilot_app_db import resolve_copilot_app_db_path

        missing = tmp_path / "nonexistent.db"
        with patch.dict("os.environ", {"APM_COPILOT_APP_DB": str(missing)}):
            result = resolve_copilot_app_db_path()
        assert result is None

    def test_home_fallback_returns_path_when_db_exists(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from apm_cli.integration.copilot_app_db import resolve_copilot_app_db_path

        copilot_dir = tmp_path / ".copilot"
        copilot_dir.mkdir()
        db = copilot_dir / "data.db"
        db.touch()

        monkeypatch.delenv("APM_COPILOT_APP_DB", raising=False)
        with patch("apm_cli.integration.copilot_app_db.Path.home", return_value=tmp_path):
            result = resolve_copilot_app_db_path()
        assert result == db

    def test_home_fallback_returns_none_when_db_absent(self, tmp_path: Path, monkeypatch) -> None:
        from apm_cli.integration.copilot_app_db import resolve_copilot_app_db_path

        # No APM_COPILOT_APP_DB in env, home dir does not have .copilot/data.db
        monkeypatch.delenv("APM_COPILOT_APP_DB", raising=False)
        with patch("apm_cli.integration.copilot_app_db.Path") as mock_path_cls:
            fake_candidate = tmp_path / ".copilot" / "data.db"
            mock_path_cls.home.return_value.__truediv__ = lambda s, o: fake_candidate
            mock_path_cls.home.return_value = tmp_path
            # Make is_file() return False
            type(
                "FakePath",
                (),
                {"is_file": lambda self: False, "__str__": lambda self: str(fake_candidate)},
            )()
            result = resolve_copilot_app_db_path()
        assert result is None


class TestResolveCopilotAppDbPathSimple:
    """Simpler hermetic tests using only the env-var code path."""

    def test_no_env_var_and_no_db_file_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When home dir lacks .copilot/data.db and no env var is set, result is None."""
        from apm_cli.integration.copilot_app_db import resolve_copilot_app_db_path

        monkeypatch.delenv("APM_COPILOT_APP_DB", raising=False)
        # Point home to a tmp dir that has no .copilot/data.db.
        with patch(
            "apm_cli.integration.copilot_app_db.Path.home",
            return_value=Path("/nonexistent_apm_test_dir"),
        ):
            result = resolve_copilot_app_db_path()
        assert result is None

    def test_env_var_existing_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from apm_cli.integration.copilot_app_db import resolve_copilot_app_db_path

        db = tmp_path / "data.db"
        db.touch()
        monkeypatch.setenv("APM_COPILOT_APP_DB", str(db))
        result = resolve_copilot_app_db_path()
        assert result == db

    def test_env_var_missing_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from apm_cli.integration.copilot_app_db import resolve_copilot_app_db_path

        monkeypatch.setenv("APM_COPILOT_APP_DB", str(tmp_path / "missing.db"))
        result = resolve_copilot_app_db_path()
        assert result is None


# ===========================================================================
# copilot_app_db -- resolve_copilot_app_root
# ===========================================================================


class TestResolveCopilotAppRoot:
    """resolve_copilot_app_root returns the parent of the DB path or None."""

    def test_returns_parent_dir_when_db_exists(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from apm_cli.integration.copilot_app_db import resolve_copilot_app_root

        copilot_dir = tmp_path / ".copilot"
        copilot_dir.mkdir()
        db = copilot_dir / "data.db"
        db.touch()
        monkeypatch.setenv("APM_COPILOT_APP_DB", str(db))
        result = resolve_copilot_app_root()
        assert result == copilot_dir

    def test_returns_none_when_db_absent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from apm_cli.integration.copilot_app_db import resolve_copilot_app_root

        monkeypatch.setenv("APM_COPILOT_APP_DB", str(tmp_path / "missing.db"))
        result = resolve_copilot_app_root()
        assert result is None


# ===========================================================================
# targets -- PrimitiveMapping
# ===========================================================================


class TestPrimitiveMapping:
    """PrimitiveMapping is a frozen dataclass for deployment specs."""

    def test_required_fields_stored(self) -> None:
        from apm_cli.integration.targets import PrimitiveMapping

        pm = PrimitiveMapping(
            subdir="rules", extension=".md", format_id="cursor_rules", output_compare=True
        )
        assert pm.subdir == "rules"
        assert pm.extension == ".md"
        assert pm.format_id == "cursor_rules"

    def test_deploy_root_defaults_to_none(self) -> None:
        from apm_cli.integration.targets import PrimitiveMapping

        pm = PrimitiveMapping(subdir="skills", extension="/SKILL.md", format_id="skill_standard")
        assert pm.deploy_root is None

    def test_deploy_root_can_be_set(self) -> None:
        from apm_cli.integration.targets import PrimitiveMapping

        pm = PrimitiveMapping(
            subdir="skills",
            extension="/SKILL.md",
            format_id="skill_standard",
            deploy_root=".agents",
        )
        assert pm.deploy_root == ".agents"

    def test_frozen_raises_on_mutation(self) -> None:
        from apm_cli.integration.targets import PrimitiveMapping

        pm = PrimitiveMapping(subdir="rules", extension=".md", format_id="x")
        with pytest.raises(AttributeError):
            pm.subdir = "other"  # type: ignore[misc]


# ===========================================================================
# targets -- TargetProfile
# ===========================================================================


class TestTargetProfile:
    """TargetProfile dataclass behaviour: properties and methods."""

    def _make_profile(self, **kwargs):
        from apm_cli.integration.targets import PrimitiveMapping, TargetProfile

        defaults = dict(
            name="test",
            root_dir=".test",
            primitives={
                "instructions": PrimitiveMapping("rules", ".md", "test_rules"),
            },
        )
        defaults.update(kwargs)
        return TargetProfile(**defaults)

    def test_prefix_property(self) -> None:
        profile = self._make_profile(root_dir=".github")
        assert profile.prefix == ".github/"

    def test_supports_known_primitive(self) -> None:
        profile = self._make_profile()
        assert profile.supports("instructions") is True

    def test_supports_unknown_primitive_false(self) -> None:
        profile = self._make_profile()
        assert profile.supports("hooks") is False

    def test_effective_pack_prefixes_falls_back_to_prefix(self) -> None:
        profile = self._make_profile(root_dir=".cursor")
        assert ".cursor/" in profile.effective_pack_prefixes

    def test_effective_pack_prefixes_uses_override_when_set(self) -> None:
        from apm_cli.integration.targets import TargetProfile

        profile = TargetProfile(
            name="codex",
            root_dir=".codex",
            primitives={},
            pack_prefixes=(".codex/", ".agents/"),
        )
        assert ".codex/" in profile.effective_pack_prefixes
        assert ".agents/" in profile.effective_pack_prefixes

    def test_deploy_path_standard(self, tmp_path: Path) -> None:
        profile = self._make_profile(root_dir=".github")
        result = profile.deploy_path(tmp_path, "prompts")
        assert result == tmp_path / ".github" / "prompts"

    def test_deploy_path_with_resolved_root(self, tmp_path: Path) -> None:
        from dataclasses import replace

        profile = self._make_profile()
        resolved_root = tmp_path / "custom_root"
        profile_with_root = replace(profile, resolved_deploy_root=resolved_root)
        result = profile_with_root.deploy_path(tmp_path, "skills")
        assert result == resolved_root / "skills"

    def test_effective_root_project_scope(self) -> None:
        profile = self._make_profile(root_dir=".github", user_root_dir=".copilot")
        assert profile.effective_root(user_scope=False) == ".github"

    def test_effective_root_user_scope(self) -> None:
        profile = self._make_profile(root_dir=".github", user_root_dir=".copilot")
        assert profile.effective_root(user_scope=True) == ".copilot"

    def test_supports_at_user_scope_false_when_not_user_supported(self) -> None:
        profile = self._make_profile(user_supported=False)
        assert profile.supports_at_user_scope("instructions") is False

    def test_supports_at_user_scope_true(self) -> None:
        profile = self._make_profile(user_supported=True)
        assert profile.supports_at_user_scope("instructions") is True

    def test_supports_at_user_scope_excluded_primitive(self) -> None:
        profile = self._make_profile(
            user_supported=True,
            unsupported_user_primitives=("instructions",),
        )
        assert profile.supports_at_user_scope("instructions") is False

    def test_for_scope_false_returns_self(self) -> None:
        profile = self._make_profile()
        assert profile.for_scope(user_scope=False) is profile

    def test_for_scope_user_unsupported_returns_none(self) -> None:
        profile = self._make_profile(user_supported=False)
        assert profile.for_scope(user_scope=True) is None


# ===========================================================================
# targets -- KNOWN_TARGETS
# ===========================================================================


class TestKnownTargets:
    """KNOWN_TARGETS dict has the expected entries with required fields."""

    def test_known_target_names_present(self) -> None:
        from apm_cli.integration.targets import KNOWN_TARGETS

        expected = {
            "copilot",
            "claude",
            "cursor",
            "opencode",
            "gemini",
            "codex",
            "windsurf",
            "agent-skills",
            "copilot-cowork",
            "copilot-app",
        }
        assert expected.issubset(set(KNOWN_TARGETS.keys()))

    def test_all_entries_have_name_field(self) -> None:
        from apm_cli.integration.targets import KNOWN_TARGETS

        for key, profile in KNOWN_TARGETS.items():
            assert profile.name == key, f"Profile key {key!r} has name {profile.name!r}"

    def test_all_entries_have_root_dir(self) -> None:
        from apm_cli.integration.targets import KNOWN_TARGETS

        for key, profile in KNOWN_TARGETS.items():
            assert isinstance(profile.root_dir, str), f"{key}: root_dir must be str"
            assert len(profile.root_dir) > 0, f"{key}: root_dir must not be empty"

    def test_all_entries_have_primitives_dict(self) -> None:
        from apm_cli.integration.targets import KNOWN_TARGETS

        for key, profile in KNOWN_TARGETS.items():
            assert isinstance(profile.primitives, dict), f"{key}: primitives must be dict"

    def test_copilot_has_instructions_and_prompts(self) -> None:
        from apm_cli.integration.targets import KNOWN_TARGETS

        copilot = KNOWN_TARGETS["copilot"]
        assert "instructions" in copilot.primitives
        assert "prompts" in copilot.primitives

    def test_claude_has_rules_extension(self) -> None:
        from apm_cli.integration.targets import KNOWN_TARGETS

        claude = KNOWN_TARGETS["claude"]
        assert "instructions" in claude.primitives
        assert claude.primitives["instructions"].extension == ".md"

    def test_copilot_app_has_prompts_primitive(self) -> None:
        from apm_cli.integration.targets import KNOWN_TARGETS

        app = KNOWN_TARGETS["copilot-app"]
        assert "prompts" in app.primitives

    def test_copilot_app_requires_flag(self) -> None:
        from apm_cli.integration.targets import KNOWN_TARGETS

        app = KNOWN_TARGETS["copilot-app"]
        assert app.requires_flag == "copilot_app"

    def test_copilot_app_scope_invariant_resolver(self) -> None:
        from apm_cli.integration.targets import KNOWN_TARGETS

        app = KNOWN_TARGETS["copilot-app"]
        assert app.scope_invariant_resolver is True

    def test_codex_has_custom_pack_prefixes(self) -> None:
        from apm_cli.integration.targets import KNOWN_TARGETS

        codex = KNOWN_TARGETS["codex"]
        assert ".codex/" in codex.effective_pack_prefixes
        assert ".agents/" in codex.effective_pack_prefixes


# ===========================================================================
# targets -- active_targets
# ===========================================================================


class TestActiveTargets:
    """active_targets resolves the correct set of TargetProfile instances."""

    def test_explicit_target_returns_that_profile(self, tmp_path: Path) -> None:
        from apm_cli.integration.targets import active_targets

        result = active_targets(tmp_path, explicit_target="claude")
        assert len(result) == 1
        assert result[0].name == "claude"

    def test_explicit_list_of_targets(self, tmp_path: Path) -> None:
        from apm_cli.integration.targets import active_targets

        result = active_targets(tmp_path, explicit_target=["claude", "cursor"])
        names = {p.name for p in result}
        assert "claude" in names
        assert "cursor" in names

    def test_directory_detection_triggers_profile(self, tmp_path: Path) -> None:
        from apm_cli.integration.targets import active_targets

        (tmp_path / ".claude").mkdir()
        result = active_targets(tmp_path)
        names = {p.name for p in result}
        assert "claude" in names

    def test_fallback_to_copilot_when_no_dir_matches(self, tmp_path: Path) -> None:
        from apm_cli.integration.targets import active_targets

        # tmp_path has no known target directories
        result = active_targets(tmp_path)
        assert any(p.name == "copilot" for p in result)

    def test_explicit_unknown_returns_empty(self, tmp_path: Path) -> None:
        from apm_cli.integration.targets import active_targets

        result = active_targets(tmp_path, explicit_target="totally-unknown-target-xyz")
        assert result == []

    def test_runtime_alias_vscode_maps_to_copilot(self, tmp_path: Path) -> None:
        from apm_cli.integration.targets import active_targets

        result = active_targets(tmp_path, explicit_target="vscode")
        assert len(result) == 1
        assert result[0].name == "copilot"

    def test_string_explicit_target_equivalent_to_list(self, tmp_path: Path) -> None:
        from apm_cli.integration.targets import active_targets

        result_str = active_targets(tmp_path, explicit_target="claude")
        result_list = active_targets(tmp_path, explicit_target=["claude"])
        assert [p.name for p in result_str] == [p.name for p in result_list]


# ===========================================================================
# targets -- active_targets_user_scope
# ===========================================================================


class TestActiveTargetsUserScope:
    """active_targets_user_scope resolves user-scope capable profiles."""

    def test_explicit_user_supported_target(self) -> None:
        from apm_cli.integration.targets import active_targets_user_scope

        result = active_targets_user_scope(explicit_target="claude")
        assert any(p.name == "claude" for p in result)

    def test_explicit_non_user_supported_returns_empty(self) -> None:
        from apm_cli.integration.targets import active_targets_user_scope

        # codex is user_supported="partial" with no user_root_dir set; let's pick
        # a target that is user_supported=False -- but in KNOWN_TARGETS all have
        # some form of user support.  Use an unknown name: should just return [].
        result = active_targets_user_scope(explicit_target="totally-unknown-target-xyz")
        assert result == []

    def test_fallback_returns_copilot(self, tmp_path: Path) -> None:
        from apm_cli.integration.targets import active_targets_user_scope

        # Make Path.home() return a temp dir with no known target dirs so
        # auto-detect finds nothing and falls back to copilot.
        with patch("pathlib.Path.home", return_value=tmp_path):
            result = active_targets_user_scope()
        assert any(p.name == "copilot" for p in result)


# ===========================================================================
# targets -- should_use_legacy_skill_paths
# ===========================================================================


class TestShouldUseLegacySkillPaths:
    """should_use_legacy_skill_paths reads APM_LEGACY_SKILL_PATHS env var."""

    def test_unset_returns_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from apm_cli.integration.targets import should_use_legacy_skill_paths

        monkeypatch.delenv("APM_LEGACY_SKILL_PATHS", raising=False)
        assert should_use_legacy_skill_paths() is False

    def test_set_to_1_returns_true(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from apm_cli.integration.targets import should_use_legacy_skill_paths

        monkeypatch.setenv("APM_LEGACY_SKILL_PATHS", "1")
        assert should_use_legacy_skill_paths() is True

    def test_set_to_true_string_returns_true(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from apm_cli.integration.targets import should_use_legacy_skill_paths

        monkeypatch.setenv("APM_LEGACY_SKILL_PATHS", "true")
        assert should_use_legacy_skill_paths() is True

    def test_set_to_yes_returns_true(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from apm_cli.integration.targets import should_use_legacy_skill_paths

        monkeypatch.setenv("APM_LEGACY_SKILL_PATHS", "yes")
        assert should_use_legacy_skill_paths() is True

    def test_set_to_zero_returns_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from apm_cli.integration.targets import should_use_legacy_skill_paths

        monkeypatch.setenv("APM_LEGACY_SKILL_PATHS", "0")
        assert should_use_legacy_skill_paths() is False


# ===========================================================================
# targets -- apply_legacy_skill_paths
# ===========================================================================


class TestApplyLegacySkillPaths:
    """apply_legacy_skill_paths resets deploy_root on skills primitives."""

    def test_clears_deploy_root_on_skills(self) -> None:
        from apm_cli.integration.targets import KNOWN_TARGETS, apply_legacy_skill_paths

        copilot = KNOWN_TARGETS["copilot"]
        assert copilot.primitives["skills"].deploy_root == ".agents"

        result = apply_legacy_skill_paths([copilot])
        assert result[0].primitives["skills"].deploy_root is None

    def test_does_not_mutate_known_targets(self) -> None:
        from apm_cli.integration.targets import KNOWN_TARGETS, apply_legacy_skill_paths

        copilot = KNOWN_TARGETS["copilot"]
        apply_legacy_skill_paths([copilot])
        # Original must be unchanged
        assert KNOWN_TARGETS["copilot"].primitives["skills"].deploy_root == ".agents"

    def test_profile_without_skills_unchanged(self) -> None:
        from apm_cli.integration.targets import KNOWN_TARGETS, apply_legacy_skill_paths

        gemini = KNOWN_TARGETS["gemini"]
        result = apply_legacy_skill_paths([gemini])
        assert result[0] is not None
        assert result[0].name == "gemini"


# ===========================================================================
# prompt_integrator -- find_prompt_files
# ===========================================================================


class TestPromptIntegratorFindFiles:
    """PromptIntegrator.find_prompt_files discovers .prompt.md files."""

    def _make_integrator(self):
        from apm_cli.integration.prompt_integrator import PromptIntegrator

        return PromptIntegrator()

    def test_finds_prompt_md_at_package_root(self, tmp_path: Path) -> None:
        (tmp_path / "my-task.prompt.md").write_text("# My Task", encoding="utf-8")
        integrator = self._make_integrator()
        result = integrator.find_prompt_files(tmp_path)
        names = [f.name for f in result]
        assert "my-task.prompt.md" in names

    def test_finds_prompt_md_in_apm_prompts_subdir(self, tmp_path: Path) -> None:
        subdir = tmp_path / ".apm" / "prompts"
        subdir.mkdir(parents=True)
        (subdir / "sub-task.prompt.md").write_text("# Sub Task", encoding="utf-8")
        integrator = self._make_integrator()
        result = integrator.find_prompt_files(tmp_path)
        names = [f.name for f in result]
        assert "sub-task.prompt.md" in names

    def test_ignores_files_without_prompt_md_suffix(self, tmp_path: Path) -> None:
        (tmp_path / "readme.md").write_text("# Readme", encoding="utf-8")
        (tmp_path / "my-task.prompt.md").write_text("# Task", encoding="utf-8")
        integrator = self._make_integrator()
        result = integrator.find_prompt_files(tmp_path)
        names = [f.name for f in result]
        assert "readme.md" not in names
        assert "my-task.prompt.md" in names

    def test_empty_package_returns_empty_list(self, tmp_path: Path) -> None:
        integrator = self._make_integrator()
        result = integrator.find_prompt_files(tmp_path)
        assert result == []

    def test_returns_list_of_paths(self, tmp_path: Path) -> None:
        (tmp_path / "task.prompt.md").write_text("# Task", encoding="utf-8")
        integrator = self._make_integrator()
        result = integrator.find_prompt_files(tmp_path)
        assert isinstance(result, list)
        assert all(isinstance(p, Path) for p in result)


# ===========================================================================
# prompt_integrator -- copy_prompt
# ===========================================================================


class TestPromptIntegratorCopyPrompt:
    """PromptIntegrator.copy_prompt copies file content to target path."""

    def _make_integrator(self):
        from apm_cli.integration.prompt_integrator import PromptIntegrator

        return PromptIntegrator()

    def test_copies_content_verbatim(self, tmp_path: Path) -> None:
        src = tmp_path / "source.prompt.md"
        src.write_text("Hello prompt content", encoding="utf-8")
        dst = tmp_path / "dest" / "source.prompt.md"
        dst.parent.mkdir(parents=True)

        integrator = self._make_integrator()
        integrator.copy_prompt(src, dst)
        assert dst.read_text(encoding="utf-8") == "Hello prompt content"

    def test_returns_links_resolved_count_zero_for_plain_content(self, tmp_path: Path) -> None:
        src = tmp_path / "plain.prompt.md"
        src.write_text("No links here", encoding="utf-8")
        dst = tmp_path / "out" / "plain.prompt.md"
        dst.parent.mkdir(parents=True)

        integrator = self._make_integrator()
        links_resolved = integrator.copy_prompt(src, dst)
        assert links_resolved == 0

    def test_rejects_symlink_source(self, tmp_path: Path) -> None:
        real = tmp_path / "real.prompt.md"
        real.write_text("Real content", encoding="utf-8")
        link = tmp_path / "link.prompt.md"
        link.symlink_to(real)
        dst = tmp_path / "out.prompt.md"

        integrator = self._make_integrator()
        with pytest.raises(ValueError, match="symlink"):
            integrator.copy_prompt(link, dst)


# ===========================================================================
# prompt_integrator -- get_target_filename
# ===========================================================================


class TestPromptIntegratorGetTargetFilename:
    """PromptIntegrator.get_target_filename returns the original filename."""

    def _make_integrator(self):
        from apm_cli.integration.prompt_integrator import PromptIntegrator

        return PromptIntegrator()

    def test_returns_original_filename(self, tmp_path: Path) -> None:
        src = tmp_path / "accessibility-audit.prompt.md"
        src.touch()
        integrator = self._make_integrator()
        result = integrator.get_target_filename(src, "my-package")
        assert result == "accessibility-audit.prompt.md"

    def test_does_not_add_apm_suffix(self, tmp_path: Path) -> None:
        src = tmp_path / "my-task.prompt.md"
        src.touch()
        integrator = self._make_integrator()
        result = integrator.get_target_filename(src, "any-pkg")
        assert "-apm" not in result

    def test_package_name_ignored_in_naming(self, tmp_path: Path) -> None:
        src = tmp_path / "task.prompt.md"
        src.touch()
        integrator = self._make_integrator()
        assert integrator.get_target_filename(src, "pkg-a") == integrator.get_target_filename(
            src, "pkg-b"
        )


# ===========================================================================
# experimental -- FLAGS registry
# ===========================================================================


class TestExperimentalFlags:
    """FLAGS dict contains required entries with correct structure."""

    def test_flags_contains_verbose_version(self) -> None:
        from apm_cli.core.experimental import FLAGS

        assert "verbose_version" in FLAGS

    def test_flags_contains_copilot_cowork(self) -> None:
        from apm_cli.core.experimental import FLAGS

        assert "copilot_cowork" in FLAGS

    def test_flags_contains_copilot_app(self) -> None:
        from apm_cli.core.experimental import FLAGS

        assert "copilot_app" in FLAGS

    def test_all_flags_default_to_false(self) -> None:
        from apm_cli.core.experimental import FLAGS

        for name, flag in FLAGS.items():
            assert flag.default is False, f"Flag {name!r} must default to False"

    def test_flag_name_matches_dict_key(self) -> None:
        from apm_cli.core.experimental import FLAGS

        for key, flag in FLAGS.items():
            assert flag.name == key, f"Flag key {key!r} has name {flag.name!r}"

    def test_description_is_non_empty_string(self) -> None:
        from apm_cli.core.experimental import FLAGS

        for key, flag in FLAGS.items():
            assert isinstance(flag.description, str) and flag.description, (
                f"Flag {key!r} has empty description"
            )


# ===========================================================================
# experimental -- normalise_flag_name
# ===========================================================================


class TestNormaliseFlagName:
    """normalise_flag_name converts kebab-case to snake_case."""

    def test_hyphen_converted_to_underscore(self) -> None:
        from apm_cli.core.experimental import normalise_flag_name

        assert normalise_flag_name("verbose-version") == "verbose_version"

    def test_already_snake_case_unchanged(self) -> None:
        from apm_cli.core.experimental import normalise_flag_name

        assert normalise_flag_name("verbose_version") == "verbose_version"

    def test_uppercased_input_lowercased(self) -> None:
        from apm_cli.core.experimental import normalise_flag_name

        assert normalise_flag_name("Verbose_Version") == "verbose_version"


# ===========================================================================
# experimental -- display_name
# ===========================================================================


class TestDisplayName:
    """display_name converts snake_case to kebab-case for display."""

    def test_underscore_converted_to_hyphen(self) -> None:
        from apm_cli.core.experimental import display_name

        assert display_name("verbose_version") == "verbose-version"

    def test_already_hyphen_unchanged(self) -> None:
        from apm_cli.core.experimental import display_name

        assert display_name("verbose-version") == "verbose-version"


# ===========================================================================
# experimental -- validate_flag_name
# ===========================================================================


class TestValidateFlagName:
    """validate_flag_name raises ValueError for unknown flags."""

    def test_valid_snake_case_flag_accepted(self) -> None:
        from apm_cli.core.experimental import validate_flag_name

        result = validate_flag_name("verbose_version")
        assert result == "verbose_version"

    def test_valid_kebab_case_flag_accepted(self) -> None:
        from apm_cli.core.experimental import validate_flag_name

        result = validate_flag_name("verbose-version")
        assert result == "verbose_version"

    def test_unknown_flag_raises_value_error(self) -> None:
        from apm_cli.core.experimental import validate_flag_name

        with pytest.raises(ValueError):
            validate_flag_name("totally-unknown-flag-xyz")

    def test_error_message_includes_flag_name(self) -> None:
        from apm_cli.core.experimental import validate_flag_name

        with pytest.raises(ValueError) as exc_info:
            validate_flag_name("totally-unknown-xyz")
        assert "totally-unknown-xyz" in str(exc_info.value)


# ===========================================================================
# experimental -- is_enabled
# ===========================================================================


class TestIsEnabled:
    """is_enabled checks the experimental section of config."""

    def test_enabled_flag_returns_true(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from apm_cli.core.experimental import is_enabled

        with patch(
            "apm_cli.core.experimental._get_experimental_section",
            return_value={"verbose_version": True},
        ):
            assert is_enabled("verbose_version") is True

    def test_disabled_flag_returns_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from apm_cli.core.experimental import is_enabled

        with patch(
            "apm_cli.core.experimental._get_experimental_section",
            return_value={"verbose_version": False},
        ):
            assert is_enabled("verbose_version") is False

    def test_missing_flag_in_config_returns_registry_default(self) -> None:
        from apm_cli.core.experimental import is_enabled

        with patch(
            "apm_cli.core.experimental._get_experimental_section",
            return_value={},
        ):
            assert is_enabled("verbose_version") is False

    def test_non_bool_value_falls_back_to_registry_default(self) -> None:
        from apm_cli.core.experimental import is_enabled

        with patch(
            "apm_cli.core.experimental._get_experimental_section",
            return_value={"verbose_version": "yes"},
        ):
            assert is_enabled("verbose_version") is False

    def test_unknown_flag_raises_value_error(self) -> None:
        from apm_cli.core.experimental import is_enabled

        with pytest.raises(ValueError, match="Unknown experimental flag"):
            is_enabled("nonexistent_flag_xyz")


# ===========================================================================
# experimental -- get_overridden_flags
# ===========================================================================


class TestGetOverriddenFlags:
    """get_overridden_flags returns only registered bool overrides."""

    def test_returns_registered_bool_flags(self) -> None:
        from apm_cli.core.experimental import get_overridden_flags

        with patch(
            "apm_cli.core.experimental._get_experimental_section",
            return_value={"verbose_version": True, "copilot_cowork": False},
        ):
            result = get_overridden_flags()
        assert result["verbose_version"] is True
        assert result["copilot_cowork"] is False

    def test_excludes_non_registered_keys(self) -> None:
        from apm_cli.core.experimental import get_overridden_flags

        with patch(
            "apm_cli.core.experimental._get_experimental_section",
            return_value={"verbose_version": True, "removed_old_flag": True},
        ):
            result = get_overridden_flags()
        assert "removed_old_flag" not in result

    def test_excludes_non_bool_values(self) -> None:
        from apm_cli.core.experimental import get_overridden_flags

        with patch(
            "apm_cli.core.experimental._get_experimental_section",
            return_value={"verbose_version": "yes"},
        ):
            result = get_overridden_flags()
        assert "verbose_version" not in result


# ===========================================================================
# experimental -- get_stale_config_keys
# ===========================================================================


class TestGetStaleConfigKeys:
    """get_stale_config_keys returns keys not in FLAGS."""

    def test_identifies_removed_flag_as_stale(self) -> None:
        from apm_cli.core.experimental import get_stale_config_keys

        with patch(
            "apm_cli.core.experimental._get_experimental_section",
            return_value={"verbose_version": True, "old_removed_flag": True},
        ):
            result = get_stale_config_keys()
        assert "old_removed_flag" in result
        assert "verbose_version" not in result

    def test_empty_config_returns_empty_list(self) -> None:
        from apm_cli.core.experimental import get_stale_config_keys

        with patch(
            "apm_cli.core.experimental._get_experimental_section",
            return_value={},
        ):
            result = get_stale_config_keys()
        assert result == []


# ===========================================================================
# experimental -- get_malformed_flag_keys
# ===========================================================================


class TestGetMalformedFlagKeys:
    """get_malformed_flag_keys returns known flag names with non-bool values."""

    def test_identifies_string_value_as_malformed(self) -> None:
        from apm_cli.core.experimental import get_malformed_flag_keys

        with patch(
            "apm_cli.core.experimental._get_experimental_section",
            return_value={"verbose_version": "yes"},
        ):
            result = get_malformed_flag_keys()
        assert "verbose_version" in result

    def test_correct_bool_value_is_not_malformed(self) -> None:
        from apm_cli.core.experimental import get_malformed_flag_keys

        with patch(
            "apm_cli.core.experimental._get_experimental_section",
            return_value={"verbose_version": True},
        ):
            result = get_malformed_flag_keys()
        assert "verbose_version" not in result

    def test_unregistered_key_not_included(self) -> None:
        from apm_cli.core.experimental import get_malformed_flag_keys

        with patch(
            "apm_cli.core.experimental._get_experimental_section",
            return_value={"old_removed_flag": "true"},
        ):
            result = get_malformed_flag_keys()
        assert "old_removed_flag" not in result


# ===========================================================================
# experimental -- ExperimentalFlag dataclass
# ===========================================================================


class TestExperimentalFlagDataclass:
    """ExperimentalFlag is a frozen dataclass with expected fields."""

    def test_fields_stored_correctly(self) -> None:
        from apm_cli.core.experimental import ExperimentalFlag

        flag = ExperimentalFlag(
            name="my_flag",
            description="A test flag.",
            default=False,
            hint="Enable with apm experimental enable my-flag.",
        )
        assert flag.name == "my_flag"
        assert flag.description == "A test flag."
        assert flag.default is False
        assert "Enable" in flag.hint  # type: ignore[operator]

    def test_hint_defaults_to_none(self) -> None:
        from apm_cli.core.experimental import ExperimentalFlag

        flag = ExperimentalFlag(name="x", description="desc", default=False)
        assert flag.hint is None

    def test_frozen_raises_on_mutation(self) -> None:
        from apm_cli.core.experimental import ExperimentalFlag

        flag = ExperimentalFlag(name="x", description="desc", default=False)
        with pytest.raises(AttributeError):
            flag.name = "y"  # type: ignore[misc]
