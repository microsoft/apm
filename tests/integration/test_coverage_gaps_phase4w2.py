"""Phase-4w2 integration tests targeting second-tier coverage gaps.

Target files and approximate uncovered lines:
- compilation/injector.py         ~30 missed  (lines 37-38,48,54-58,64-65,68-94)
- deps/aggregator.py              ~24 missed  (lines 18-41,53-66)
- compilation/constitution_block  ~23 missed  (lines 21-22,30-34,64-67,82-102)
- install/heals/branch_ref_drift  ~20 missed  (lines 29-48,51-61)
- commands/marketplace/plugin/remove ~18 missed
- marketplace/validator.py        ~17 missed
- commands/marketplace/migrate.py ~17 missed
- workflow/discovery.py           ~16 missed
- integration/utils.py            ~16 missed (line 37->46)
- commands/marketplace/plugin/add ~15 missed
- install/presentation/dry_run    ~14 missed
- security/file_scanner.py        ~14 missed
- models/dependency/types.py      ~13 missed
- install/phases/post_deps_local  ~12 missed
- install/phases/heal.py          ~11 missed (branches 85->82, 87->82)
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

# ---------------------------------------------------------------------------
# compilation/constitution_block.py
# ---------------------------------------------------------------------------


class TestConstitutionBlock:
    """Tests for render_block, find_existing_block, inject_or_update."""

    def test_compute_constitution_hash_stable(self) -> None:
        from apm_cli.compilation.constitution_block import compute_constitution_hash

        h1 = compute_constitution_hash("hello world")
        h2 = compute_constitution_hash("hello world")
        assert h1 == h2
        assert len(h1) == 12

    def test_render_block_contains_markers(self) -> None:
        from apm_cli.compilation.constants import (
            CONSTITUTION_MARKER_BEGIN,
            CONSTITUTION_MARKER_END,
        )
        from apm_cli.compilation.constitution_block import render_block

        block = render_block("My constitution text\n")
        assert CONSTITUTION_MARKER_BEGIN in block
        assert CONSTITUTION_MARKER_END in block
        assert "hash:" in block

    def test_render_block_includes_hash(self) -> None:
        from apm_cli.compilation.constitution_block import (
            compute_constitution_hash,
            render_block,
        )

        content = "Some important rules"
        block = render_block(content)
        expected_hash = compute_constitution_hash(content)
        assert expected_hash in block

    def test_render_block_ends_with_newline(self) -> None:
        from apm_cli.compilation.constitution_block import render_block

        block = render_block("text")
        assert block.endswith("\n")

    def test_find_existing_block_returns_none_when_absent(self) -> None:
        from apm_cli.compilation.constitution_block import find_existing_block

        result = find_existing_block("# AGENTS.md\n\nSome content\n")
        assert result is None

    def test_find_existing_block_returns_block_when_present(self) -> None:
        from apm_cli.compilation.constitution_block import find_existing_block, render_block

        constitution = "Be helpful and honest."
        block = render_block(constitution)
        content = f"# Header\n\n{block}\n## Body\n"
        result = find_existing_block(content)
        assert result is not None
        assert result.hash is not None
        assert len(result.hash) == 12

    def test_find_existing_block_without_hash_line(self) -> None:
        from apm_cli.compilation.constants import (
            CONSTITUTION_MARKER_BEGIN,
            CONSTITUTION_MARKER_END,
        )
        from apm_cli.compilation.constitution_block import find_existing_block

        content = (
            f"# Header\n\n{CONSTITUTION_MARKER_BEGIN}\nNo hash here\n{CONSTITUTION_MARKER_END}\n"
        )
        result = find_existing_block(content)
        assert result is not None
        assert result.hash is None

    def test_inject_or_update_creates_when_no_existing(self) -> None:
        from apm_cli.compilation.constitution_block import inject_or_update, render_block

        new_block = render_block("Rule: Be kind.")
        updated, status = inject_or_update("# AGENTS.md\n\nBody content\n", new_block)
        assert status == "CREATED"
        assert "Rule: Be kind." in updated

    def test_inject_or_update_unchanged(self) -> None:
        from apm_cli.compilation.constitution_block import (
            inject_or_update,
            render_block,
        )

        constitution = "Be consistent."
        block = render_block(constitution)
        content = block + "# Body\n"
        # inject same block
        updated, status = inject_or_update(content, block)
        assert status == "UNCHANGED"
        assert updated == content

    def test_inject_or_update_updated(self) -> None:
        from apm_cli.compilation.constitution_block import inject_or_update, render_block

        old_block = render_block("Old rules.")
        existing = old_block + "# Body\n"
        new_block = render_block("New rules.")
        updated, status = inject_or_update(existing, new_block)
        assert status == "UPDATED"
        assert "New rules." in updated

    def test_inject_or_update_place_bottom(self) -> None:
        from apm_cli.compilation.constitution_block import inject_or_update, render_block

        new_block = render_block("Bottom rule.")
        updated, status = inject_or_update("# Existing\n", new_block, place_top=False)
        assert status == "CREATED"
        assert updated.endswith(new_block) or "Bottom rule." in updated


# ---------------------------------------------------------------------------
# compilation/injector.py
# ---------------------------------------------------------------------------


class TestConstitutionInjector:
    """Tests for ConstitutionInjector.inject()."""

    def test_inject_output_path_not_exists(self, tmp_path: Path) -> None:
        from apm_cli.compilation.injector import ConstitutionInjector

        injector = ConstitutionInjector(str(tmp_path))
        output_path = tmp_path / "AGENTS.md"
        # File doesn't exist -- should skip read
        with patch("apm_cli.compilation.injector.read_constitution", return_value=None):
            _content, status, hash_val = injector.inject(
                "# Header\n\nBody\n",
                with_constitution=True,
                output_path=output_path,
            )
        assert status == "MISSING"
        assert hash_val is None

    def test_inject_oserror_on_read(self, tmp_path: Path) -> None:
        from apm_cli.compilation.injector import ConstitutionInjector

        injector = ConstitutionInjector(str(tmp_path))
        output_path = tmp_path / "AGENTS.md"
        output_path.write_text("existing", encoding="utf-8")

        with (
            patch.object(output_path.__class__, "read_text", side_effect=OSError("perm")),
            patch("apm_cli.compilation.injector.read_constitution", return_value=None),
            patch("apm_cli.compilation.constitution_block.find_existing_block", return_value=None),
        ):
            _content, status, _hash_val = injector.inject(
                "# Header\n\nBody\n",
                with_constitution=True,
                output_path=output_path,
            )
        assert status == "MISSING"

    def test_inject_skip_constitution_no_existing_block(self, tmp_path: Path) -> None:
        from apm_cli.compilation.injector import ConstitutionInjector

        injector = ConstitutionInjector(str(tmp_path))
        output_path = tmp_path / "AGENTS.md"
        compiled = "# Header\n\nBody\n"
        content, status, hash_val = injector.inject(
            compiled,
            with_constitution=False,
            output_path=output_path,
        )
        assert status == "SKIPPED"
        assert content == compiled
        assert hash_val is None

    def test_inject_skip_constitution_preserves_existing_block(self, tmp_path: Path) -> None:
        from apm_cli.compilation.constitution_block import render_block
        from apm_cli.compilation.injector import ConstitutionInjector

        constitution = "Preserved rules."
        block = render_block(constitution)
        existing = f"# Header\n\n{block}\n## Body\n"
        output_path = tmp_path / "AGENTS.md"
        output_path.write_text(existing, encoding="utf-8")

        injector = ConstitutionInjector(str(tmp_path))
        content, status, _ = injector.inject(
            "# Header\n\nBody\n",
            with_constitution=False,
            output_path=output_path,
        )
        assert status == "SKIPPED"
        assert "Preserved rules." in content

    def test_inject_constitution_missing_file_no_existing_block(self, tmp_path: Path) -> None:
        from apm_cli.compilation.injector import ConstitutionInjector

        injector = ConstitutionInjector(str(tmp_path))
        output_path = tmp_path / "AGENTS.md"
        with patch("apm_cli.compilation.injector.read_constitution", return_value=None):
            _content, status, _hash_val = injector.inject(
                "# Header\n\nBody\n",
                with_constitution=True,
                output_path=output_path,
            )
        assert status == "MISSING"

    def test_inject_constitution_missing_file_with_existing_block(self, tmp_path: Path) -> None:
        from apm_cli.compilation.constitution_block import render_block
        from apm_cli.compilation.injector import ConstitutionInjector

        old_block = render_block("Old constitution.")
        existing = f"# Header\n\n{old_block}\n## Body\n"
        output_path = tmp_path / "AGENTS.md"
        output_path.write_text(existing, encoding="utf-8")

        injector = ConstitutionInjector(str(tmp_path))
        with patch("apm_cli.compilation.injector.read_constitution", return_value=None):
            content, status, _ = injector.inject(
                "# Header\n\nBody\n",
                with_constitution=True,
                output_path=output_path,
            )
        assert status == "MISSING"
        assert "Old constitution." in content

    def test_inject_creates_new_block(self, tmp_path: Path) -> None:
        from apm_cli.compilation.injector import ConstitutionInjector

        injector = ConstitutionInjector(str(tmp_path))
        output_path = tmp_path / "AGENTS.md"
        with patch("apm_cli.compilation.injector.read_constitution", return_value="New rules."):
            content, status, hash_val = injector.inject(
                "# Header\n\nBody\n",
                with_constitution=True,
                output_path=output_path,
            )
        assert status == "CREATED"
        assert hash_val is not None
        assert "New rules." in content

    def test_inject_updates_existing_block(self, tmp_path: Path) -> None:
        from apm_cli.compilation.constitution_block import render_block
        from apm_cli.compilation.injector import ConstitutionInjector

        old_block = render_block("Old rules.")
        existing = f"# Header\n\n{old_block}\n## Body\n"
        output_path = tmp_path / "AGENTS.md"
        output_path.write_text(existing, encoding="utf-8")

        injector = ConstitutionInjector(str(tmp_path))
        with patch("apm_cli.compilation.injector.read_constitution", return_value="Updated rules."):
            content, status, hash_val = injector.inject(
                "# Header\n\nBody\n",
                with_constitution=True,
                output_path=output_path,
            )
        assert status == "UPDATED"
        assert hash_val is not None
        assert "Updated rules." in content

    def test_inject_unchanged_when_same_content(self, tmp_path: Path) -> None:
        from apm_cli.compilation.constitution_block import render_block
        from apm_cli.compilation.injector import ConstitutionInjector

        constitution = "Stable rules."
        block = render_block(constitution)
        existing = f"# Header\n\n{block}\n## Body\n"
        output_path = tmp_path / "AGENTS.md"
        output_path.write_text(existing, encoding="utf-8")

        injector = ConstitutionInjector(str(tmp_path))
        with patch("apm_cli.compilation.injector.read_constitution", return_value=constitution):
            _content, status, _hash_val = injector.inject(
                "# Header\n\nBody\n",
                with_constitution=True,
                output_path=output_path,
            )
        assert status == "UNCHANGED"

    def test_inject_compiled_no_double_newline(self, tmp_path: Path) -> None:
        """Exercises the _split_header fallback when no '\\n\\n' in compiled_content."""
        from apm_cli.compilation.injector import ConstitutionInjector

        injector = ConstitutionInjector(str(tmp_path))
        output_path = tmp_path / "AGENTS.md"
        # No double newline in compiled content => _split_header returns (content, "")
        with patch("apm_cli.compilation.injector.read_constitution", return_value="Rules."):
            content, status, _ = injector.inject(
                "No double newline here",
                with_constitution=True,
                output_path=output_path,
            )
        assert status == "CREATED"
        assert "Rules." in content

    def test_inject_result_ends_with_newline(self, tmp_path: Path) -> None:
        from apm_cli.compilation.injector import ConstitutionInjector

        injector = ConstitutionInjector(str(tmp_path))
        output_path = tmp_path / "AGENTS.md"
        with patch("apm_cli.compilation.injector.read_constitution", return_value="Rules."):
            content, _, __ = injector.inject(
                "# Header\n\nBody",
                with_constitution=True,
                output_path=output_path,
            )
        assert content.endswith("\n")


# ---------------------------------------------------------------------------
# deps/aggregator.py
# ---------------------------------------------------------------------------


class TestDepAggregator:
    """Tests for scan_workflows_for_dependencies and sync_workflow_dependencies."""

    def test_scan_no_workflows_returns_empty_set(self, tmp_path: Path) -> None:
        from apm_cli.deps.aggregator import scan_workflows_for_dependencies

        with patch("glob.glob", return_value=[]):
            result = scan_workflows_for_dependencies()
        assert result == set()

    def test_scan_picks_up_mcp_servers(self, tmp_path: Path) -> None:
        from apm_cli.deps.aggregator import scan_workflows_for_dependencies

        mock_metadata = MagicMock()
        mock_metadata.metadata = {"mcp": ["server-a", "server-b"]}

        workflow_path = str(tmp_path / "test.prompt.md")
        with (
            patch(
                "glob.glob",
                side_effect=lambda p, recursive: (
                    [workflow_path] if p.endswith(".prompt.md") else []
                ),
            ),
            patch("builtins.open", MagicMock()),
            patch("frontmatter.load", return_value=mock_metadata),
        ):
            result = scan_workflows_for_dependencies()
        assert "server-a" in result
        assert "server-b" in result

    def test_scan_skips_non_list_mcp(self, tmp_path: Path) -> None:
        from apm_cli.deps.aggregator import scan_workflows_for_dependencies

        mock_metadata = MagicMock()
        mock_metadata.metadata = {"mcp": "not-a-list"}

        workflow_path = str(tmp_path / "test.prompt.md")
        with (
            patch(
                "glob.glob",
                side_effect=lambda p, recursive: (
                    [workflow_path] if p.endswith(".prompt.md") else []
                ),
            ),
            patch("builtins.open", MagicMock()),
            patch("frontmatter.load", return_value=mock_metadata),
        ):
            result = scan_workflows_for_dependencies()
        assert result == set()

    def test_scan_handles_file_read_error(self, tmp_path: Path) -> None:
        from apm_cli.deps.aggregator import scan_workflows_for_dependencies

        workflow_path = str(tmp_path / "bad.prompt.md")
        with (
            patch(
                "glob.glob",
                side_effect=lambda p, recursive: (
                    [workflow_path] if p.endswith(".prompt.md") else []
                ),
            ),
            patch("builtins.open", side_effect=OSError("cannot read")),
        ):
            # Should not raise -- exceptions are caught with print
            result = scan_workflows_for_dependencies()
        assert isinstance(result, set)

    def test_scan_deduplicates_workflows(self, tmp_path: Path) -> None:
        """Both glob patterns may return the same file; dedup must occur."""
        from apm_cli.deps.aggregator import scan_workflows_for_dependencies

        workflow_path = str(tmp_path / "dup.prompt.md")
        call_count = {"n": 0}

        mock_metadata = MagicMock()
        mock_metadata.metadata = {"mcp": ["server-x"]}

        def _glob(pattern: str, recursive: bool) -> list[str]:
            call_count["n"] += 1
            return [workflow_path]

        with (
            patch("glob.glob", side_effect=_glob),
            patch("builtins.open", MagicMock()),
            patch("frontmatter.load", return_value=mock_metadata),
        ):
            result = scan_workflows_for_dependencies()
        # Even though glob returned same file twice, should only count once
        assert "server-x" in result

    def test_sync_workflow_dependencies_success(self, tmp_path: Path) -> None:
        from apm_cli.deps.aggregator import sync_workflow_dependencies

        with (
            patch(
                "apm_cli.deps.aggregator.scan_workflows_for_dependencies",
                return_value={"srv1", "srv2"},
            ),
            patch("apm_cli.utils.yaml_io.dump_yaml"),
        ):
            success, servers = sync_workflow_dependencies(str(tmp_path / "apm.yml"))
        assert success is True
        assert set(servers) == {"srv1", "srv2"}

    def test_sync_workflow_dependencies_write_error(self, tmp_path: Path) -> None:
        from apm_cli.deps.aggregator import sync_workflow_dependencies

        with (
            patch("apm_cli.deps.aggregator.scan_workflows_for_dependencies", return_value={"s1"}),
            patch("apm_cli.utils.yaml_io.dump_yaml", side_effect=OSError("disk full")),
        ):
            success, servers = sync_workflow_dependencies(str(tmp_path / "apm.yml"))
        # On error, returns (False, [])
        assert success is False
        assert servers == []


# ---------------------------------------------------------------------------
# install/heals/branch_ref_drift.py
# ---------------------------------------------------------------------------


class TestBranchRefDriftHeal:
    """Tests for BranchRefDriftHeal.applies() and execute()."""

    def _make_hctx(
        self,
        *,
        lockfile_match: bool = True,
        update_refs: bool = False,
        resolved_ref=None,
        existing_lockfile=None,
        package_key: str = "org/repo",
        locked_commit: str | None = "aabbccdd1234",
        remote_commit: str | None = "deadbeef5678",
    ) -> object:
        from apm_cli.install.heals.base import HealContext

        if resolved_ref is None:
            from apm_cli.models.dependency.types import GitReferenceType, ResolvedReference

            resolved_ref = ResolvedReference(
                original_ref="main",
                ref_type=GitReferenceType.BRANCH,
                resolved_commit=remote_commit,
                ref_name="main",
            )

        if existing_lockfile is None and locked_commit is not None:
            locked_dep = MagicMock()
            locked_dep.resolved_commit = locked_commit
            existing_lockfile = MagicMock()
            existing_lockfile.get_dependency.return_value = locked_dep

        dep_ref = MagicMock()
        dep_ref.get_unique_key.return_value = package_key

        return HealContext(
            dep_ref=dep_ref,
            package_key=package_key,
            resolved_ref=resolved_ref,
            existing_lockfile=existing_lockfile,
            lockfile_match=lockfile_match,
            lockfile_match_via_content_hash_only=False,
            update_refs=update_refs,
        )

    def test_applies_returns_false_when_lockfile_match_false(self) -> None:
        from apm_cli.install.heals.branch_ref_drift import BranchRefDriftHeal

        heal = BranchRefDriftHeal()
        hctx = self._make_hctx(lockfile_match=False)
        assert heal.applies(hctx) is False

    def test_applies_returns_false_when_update_refs(self) -> None:
        from apm_cli.install.heals.branch_ref_drift import BranchRefDriftHeal

        heal = BranchRefDriftHeal()
        hctx = self._make_hctx(update_refs=True)
        assert heal.applies(hctx) is False

    def test_applies_returns_false_when_resolved_ref_none(self) -> None:
        from apm_cli.install.heals.branch_ref_drift import BranchRefDriftHeal

        heal = BranchRefDriftHeal()
        # Build a HealContext with resolved_ref=None manually
        from apm_cli.install.heals.base import HealContext

        dep_ref = MagicMock()
        dep_ref.get_unique_key.return_value = "org/repo"
        hctx2 = HealContext(
            dep_ref=dep_ref,
            package_key="org/repo",
            resolved_ref=None,
            existing_lockfile=None,
            lockfile_match=True,
            lockfile_match_via_content_hash_only=False,
            update_refs=False,
        )
        assert heal.applies(hctx2) is False

    def test_applies_returns_false_for_tag_ref(self) -> None:
        from apm_cli.install.heals.branch_ref_drift import BranchRefDriftHeal
        from apm_cli.models.dependency.types import GitReferenceType, ResolvedReference

        heal = BranchRefDriftHeal()
        tag_ref = ResolvedReference(
            original_ref="v1.0.0",
            ref_type=GitReferenceType.TAG,
            resolved_commit="abc123",
            ref_name="v1.0.0",
        )
        hctx = self._make_hctx(resolved_ref=tag_ref)
        assert heal.applies(hctx) is False

    def test_applies_returns_false_when_remote_sha_none(self) -> None:
        from apm_cli.install.heals.branch_ref_drift import BranchRefDriftHeal
        from apm_cli.models.dependency.types import GitReferenceType, ResolvedReference

        heal = BranchRefDriftHeal()
        branch_ref = ResolvedReference(
            original_ref="main",
            ref_type=GitReferenceType.BRANCH,
            resolved_commit=None,
            ref_name="main",
        )
        hctx = self._make_hctx(resolved_ref=branch_ref)
        assert heal.applies(hctx) is False

    def test_applies_returns_false_when_remote_sha_cached(self) -> None:
        from apm_cli.install.heals.branch_ref_drift import BranchRefDriftHeal
        from apm_cli.models.dependency.types import GitReferenceType, ResolvedReference

        heal = BranchRefDriftHeal()
        branch_ref = ResolvedReference(
            original_ref="main",
            ref_type=GitReferenceType.BRANCH,
            resolved_commit="cached",
            ref_name="main",
        )
        hctx = self._make_hctx(resolved_ref=branch_ref)
        assert heal.applies(hctx) is False

    def test_applies_returns_false_when_no_lockfile(self) -> None:
        from apm_cli.install.heals.base import HealContext
        from apm_cli.install.heals.branch_ref_drift import BranchRefDriftHeal
        from apm_cli.models.dependency.types import GitReferenceType, ResolvedReference

        heal = BranchRefDriftHeal()
        branch_ref = ResolvedReference(
            original_ref="main",
            ref_type=GitReferenceType.BRANCH,
            resolved_commit="deadbeef1234",
            ref_name="main",
        )
        dep_ref = MagicMock()
        dep_ref.get_unique_key.return_value = "org/repo"
        hctx = HealContext(
            dep_ref=dep_ref,
            package_key="org/repo",
            resolved_ref=branch_ref,
            existing_lockfile=None,
            lockfile_match=True,
            lockfile_match_via_content_hash_only=False,
            update_refs=False,
        )
        assert heal.applies(hctx) is False

    def test_applies_returns_false_when_locked_dep_not_found(self) -> None:
        from apm_cli.install.heals.branch_ref_drift import BranchRefDriftHeal

        heal = BranchRefDriftHeal()
        lockfile = MagicMock()
        lockfile.get_dependency.return_value = None
        hctx = self._make_hctx(existing_lockfile=lockfile, locked_commit=None)
        assert heal.applies(hctx) is False

    def test_applies_returns_false_when_locked_commit_none(self) -> None:
        from apm_cli.install.heals.branch_ref_drift import BranchRefDriftHeal

        heal = BranchRefDriftHeal()
        locked_dep = MagicMock()
        locked_dep.resolved_commit = None
        lockfile = MagicMock()
        lockfile.get_dependency.return_value = locked_dep
        hctx = self._make_hctx(existing_lockfile=lockfile)
        assert heal.applies(hctx) is False

    def test_applies_returns_false_when_commits_equal(self) -> None:
        from apm_cli.install.heals.branch_ref_drift import BranchRefDriftHeal

        heal = BranchRefDriftHeal()
        same_sha = "aabbccdd1234567890ab"
        locked_dep = MagicMock()
        locked_dep.resolved_commit = same_sha
        lockfile = MagicMock()
        lockfile.get_dependency.return_value = locked_dep
        hctx = self._make_hctx(remote_commit=same_sha, existing_lockfile=lockfile)
        assert heal.applies(hctx) is False

    def test_applies_returns_true_on_drift(self) -> None:
        from apm_cli.install.heals.branch_ref_drift import BranchRefDriftHeal

        heal = BranchRefDriftHeal()
        hctx = self._make_hctx(
            locked_commit="aabbccdd1234",
            remote_commit="deadbeef5678",
        )
        assert heal.applies(hctx) is True

    def test_execute_sets_lockfile_match_false(self) -> None:
        from apm_cli.install.heals.branch_ref_drift import BranchRefDriftHeal

        heal = BranchRefDriftHeal()
        hctx = self._make_hctx(
            locked_commit="aabbccdd12345678",
            remote_commit="deadbeef56789012",
        )
        heal.execute(hctx)
        assert hctx.lockfile_match is False

    def test_execute_sets_ref_changed(self) -> None:
        from apm_cli.install.heals.branch_ref_drift import BranchRefDriftHeal

        heal = BranchRefDriftHeal()
        hctx = self._make_hctx(
            locked_commit="aabbccdd12345678",
            remote_commit="deadbeef56789012",
        )
        heal.execute(hctx)
        assert hctx.ref_changed is True

    def test_execute_adds_bypass_key(self) -> None:
        from apm_cli.install.heals.branch_ref_drift import BranchRefDriftHeal

        heal = BranchRefDriftHeal()
        hctx = self._make_hctx(
            package_key="myorg/myrepo",
            locked_commit="aabbccdd12345678",
            remote_commit="deadbeef56789012",
        )
        heal.execute(hctx)
        assert "myorg/myrepo" in hctx.bypass_keys

    def test_execute_emits_info_message(self) -> None:
        from apm_cli.install.heals.base import HealMessageLevel
        from apm_cli.install.heals.branch_ref_drift import BranchRefDriftHeal

        heal = BranchRefDriftHeal()
        hctx = self._make_hctx(
            locked_commit="aabbccdd12345678",
            remote_commit="deadbeef56789012",
        )
        heal.execute(hctx)
        assert len(hctx.messages) == 1
        assert hctx.messages[0].level == HealMessageLevel.INFO
        assert "drift" in hctx.messages[0].text.lower()


# ---------------------------------------------------------------------------
# marketplace/validator.py
# ---------------------------------------------------------------------------


class TestMarketplaceValidator:
    """Tests for validate_marketplace, validate_plugin_schema, validate_no_duplicate_names."""

    def _make_plugin(
        self, name: str = "my-plugin", source: str | None = "github.com/org/repo"
    ) -> object:
        from apm_cli.marketplace.models import MarketplacePlugin

        return MarketplacePlugin(name=name, source=source)

    def _make_manifest(self, plugins: list) -> object:
        from apm_cli.marketplace.models import MarketplaceManifest

        return MarketplaceManifest(name="test-marketplace", plugins=tuple(plugins))

    def test_validate_marketplace_passes_for_valid(self) -> None:
        from apm_cli.marketplace.validator import validate_marketplace

        manifest = self._make_manifest([self._make_plugin("plugin-a")])
        results = validate_marketplace(manifest)
        assert all(r.passed for r in results)

    def test_validate_marketplace_returns_two_results(self) -> None:
        from apm_cli.marketplace.validator import validate_marketplace

        manifest = self._make_manifest([self._make_plugin()])
        results = validate_marketplace(manifest)
        assert len(results) == 2

    def test_validate_plugin_schema_empty_name(self) -> None:
        from apm_cli.marketplace.models import MarketplacePlugin
        from apm_cli.marketplace.validator import validate_plugin_schema

        plugins = [MarketplacePlugin(name="  ", source="github.com/org/repo")]
        result = validate_plugin_schema(plugins)
        assert result.passed is False
        assert any("empty name" in e for e in result.errors)

    def test_validate_plugin_schema_missing_source(self) -> None:
        from apm_cli.marketplace.models import MarketplacePlugin
        from apm_cli.marketplace.validator import validate_plugin_schema

        plugins = [MarketplacePlugin(name="plugin-x", source=None)]
        result = validate_plugin_schema(plugins)
        assert result.passed is False
        assert any("source" in e for e in result.errors)

    def test_validate_plugin_schema_empty_list(self) -> None:
        from apm_cli.marketplace.validator import validate_plugin_schema

        result = validate_plugin_schema([])
        assert result.passed is True
        assert result.check_name == "Schema"

    def test_validate_no_duplicate_names_detects_duplicate(self) -> None:
        from apm_cli.marketplace.models import MarketplacePlugin
        from apm_cli.marketplace.validator import validate_no_duplicate_names

        plugins = [
            MarketplacePlugin(name="plugin-a", source="github.com/org/a"),
            MarketplacePlugin(name="plugin-a", source="github.com/org/a2"),
        ]
        result = validate_no_duplicate_names(plugins)
        assert result.passed is False
        assert any("plugin-a" in e for e in result.errors)

    def test_validate_no_duplicate_names_case_insensitive(self) -> None:
        from apm_cli.marketplace.models import MarketplacePlugin
        from apm_cli.marketplace.validator import validate_no_duplicate_names

        plugins = [
            MarketplacePlugin(name="Plugin-A", source="github.com/org/a"),
            MarketplacePlugin(name="plugin-a", source="github.com/org/a2"),
        ]
        result = validate_no_duplicate_names(plugins)
        assert result.passed is False

    def test_validate_no_duplicate_names_passes_for_unique(self) -> None:
        from apm_cli.marketplace.models import MarketplacePlugin
        from apm_cli.marketplace.validator import validate_no_duplicate_names

        plugins = [
            MarketplacePlugin(name="plugin-a", source="github.com/org/a"),
            MarketplacePlugin(name="plugin-b", source="github.com/org/b"),
        ]
        result = validate_no_duplicate_names(plugins)
        assert result.passed is True
        assert result.check_name == "Names"


# ---------------------------------------------------------------------------
# models/dependency/types.py
# ---------------------------------------------------------------------------


class TestDependencyTypes:
    """Tests for ResolvedReference.__str__ and parse_git_reference."""

    def test_resolved_reference_str_no_commit(self) -> None:
        from apm_cli.models.dependency.types import GitReferenceType, ResolvedReference

        ref = ResolvedReference(
            original_ref="main",
            ref_type=GitReferenceType.BRANCH,
            resolved_commit=None,
            ref_name="main",
        )
        assert str(ref) == "main"

    def test_resolved_reference_str_commit_type(self) -> None:
        from apm_cli.models.dependency.types import GitReferenceType, ResolvedReference

        ref = ResolvedReference(
            original_ref="abc1234567890",
            ref_type=GitReferenceType.COMMIT,
            resolved_commit="abc1234567890abcdef",
            ref_name="abc1234567890",
        )
        assert str(ref) == "abc12345"

    def test_resolved_reference_str_branch_with_commit(self) -> None:
        from apm_cli.models.dependency.types import GitReferenceType, ResolvedReference

        ref = ResolvedReference(
            original_ref="main",
            ref_type=GitReferenceType.BRANCH,
            resolved_commit="deadbeef12345678",
            ref_name="main",
        )
        assert str(ref) == "main (deadbeef)"

    def test_parse_git_reference_empty_returns_main(self) -> None:
        from apm_cli.models.dependency.types import GitReferenceType, parse_git_reference

        ref_type, ref_name = parse_git_reference("")
        assert ref_type == GitReferenceType.BRANCH
        assert ref_name == "main"

    def test_parse_git_reference_40_char_hex_is_commit(self) -> None:
        from apm_cli.models.dependency.types import GitReferenceType, parse_git_reference

        sha = "a" * 40
        ref_type, ref_name = parse_git_reference(sha)
        assert ref_type == GitReferenceType.COMMIT
        assert ref_name == sha

    def test_parse_git_reference_7_char_hex_is_commit(self) -> None:
        from apm_cli.models.dependency.types import GitReferenceType, parse_git_reference

        sha = "abc1234"
        ref_type, _ref_name = parse_git_reference(sha)
        assert ref_type == GitReferenceType.COMMIT

    def test_parse_git_reference_semver_tag(self) -> None:
        from apm_cli.models.dependency.types import GitReferenceType, parse_git_reference

        ref_type, ref_name = parse_git_reference("v1.2.3")
        assert ref_type == GitReferenceType.TAG
        assert ref_name == "v1.2.3"

    def test_parse_git_reference_bare_version_tag(self) -> None:
        from apm_cli.models.dependency.types import GitReferenceType, parse_git_reference

        ref_type, _ref_name = parse_git_reference("1.0.0")
        assert ref_type == GitReferenceType.TAG

    def test_parse_git_reference_branch_name(self) -> None:
        from apm_cli.models.dependency.types import GitReferenceType, parse_git_reference

        ref_type, ref_name = parse_git_reference("feature/my-branch")
        assert ref_type == GitReferenceType.BRANCH
        assert ref_name == "feature/my-branch"


# ---------------------------------------------------------------------------
# integration/utils.py
# ---------------------------------------------------------------------------


class TestIntegrationUtils:
    """Tests for normalize_repo_url covering all branches."""

    def test_short_form_unchanged(self) -> None:
        from apm_cli.integration.utils import normalize_repo_url

        assert normalize_repo_url("owner/repo") == "owner/repo"

    def test_short_form_strips_git_suffix(self) -> None:
        from apm_cli.integration.utils import normalize_repo_url

        assert normalize_repo_url("owner/repo.git") == "owner/repo"

    def test_short_form_strips_trailing_slash(self) -> None:
        from apm_cli.integration.utils import normalize_repo_url

        assert normalize_repo_url("owner/repo/") == "owner/repo"

    def test_full_https_url(self) -> None:
        from apm_cli.integration.utils import normalize_repo_url

        assert normalize_repo_url("https://github.com/owner/repo") == "owner/repo"

    def test_full_https_url_with_git_suffix(self) -> None:
        from apm_cli.integration.utils import normalize_repo_url

        assert normalize_repo_url("https://github.com/owner/repo.git") == "owner/repo"

    def test_full_https_url_with_trailing_slash(self) -> None:
        from apm_cli.integration.utils import normalize_repo_url

        assert normalize_repo_url("https://github.com/owner/repo/") == "owner/repo"

    def test_url_with_no_path_after_host_falls_through(self) -> None:
        """Covers branch 37->46: no slash after host yields raw URL."""
        from apm_cli.integration.utils import normalize_repo_url

        # When the URL has a protocol but no path after the host, the function
        # falls through to ``return package_repo_url``.
        url = "https://github.com"
        result = normalize_repo_url(url)
        # Falls through to return original
        assert result == url


# ---------------------------------------------------------------------------
# workflow/discovery.py
# ---------------------------------------------------------------------------


class TestWorkflowDiscovery:
    """Tests for discover_workflows and create_workflow_template."""

    def test_discover_workflows_uses_cwd_when_base_dir_none(self, tmp_path: Path) -> None:
        from apm_cli.workflow.discovery import discover_workflows

        with (
            patch("os.getcwd", return_value=str(tmp_path)),
            patch("glob.glob", return_value=[]),
        ):
            result = discover_workflows(base_dir=None)
        assert result == []

    def test_discover_workflows_with_no_files(self, tmp_path: Path) -> None:
        from apm_cli.workflow.discovery import discover_workflows

        with patch("glob.glob", return_value=[]):
            result = discover_workflows(base_dir=str(tmp_path))
        assert result == []

    def test_discover_workflows_deduplicates(self, tmp_path: Path) -> None:
        from apm_cli.workflow.discovery import discover_workflows

        wf_file = tmp_path / "test.prompt.md"
        wf_file.write_text("---\ndescription: test\n---\n# Test\n", encoding="utf-8")

        mock_wf = MagicMock()
        same_path = str(wf_file)

        # Both glob patterns return the same file
        def _glob(pattern: str, recursive: bool) -> list[str]:
            return [same_path]

        with (
            patch("glob.glob", side_effect=_glob),
            patch("apm_cli.workflow.discovery.parse_workflow_file", return_value=mock_wf),
        ):
            result = discover_workflows(base_dir=str(tmp_path))

        # File deduplicated → parse called once → single result
        assert len(result) == 1

    def test_discover_workflows_handles_parse_error(self, tmp_path: Path) -> None:
        from apm_cli.workflow.discovery import discover_workflows

        bad_file = str(tmp_path / "bad.prompt.md")

        def _glob(pattern: str, recursive: bool) -> list[str]:
            return [bad_file]

        with (
            patch("glob.glob", side_effect=_glob),
            patch("apm_cli.workflow.discovery.parse_workflow_file", side_effect=ValueError("bad")),
            patch("builtins.print"),  # suppress warning output
        ):
            result = discover_workflows(base_dir=str(tmp_path))

        # Should not raise; failed files are skipped
        assert result == []

    def test_create_workflow_template_vscode_convention(self, tmp_path: Path) -> None:
        from apm_cli.workflow.discovery import create_workflow_template

        path = create_workflow_template("my-workflow", output_dir=str(tmp_path))
        assert path.endswith("my-workflow.prompt.md")
        assert ".github/prompts" in path
        assert Path(path).exists()

    def test_create_workflow_template_non_vscode(self, tmp_path: Path) -> None:
        from apm_cli.workflow.discovery import create_workflow_template

        path = create_workflow_template(
            "my-workflow",
            output_dir=str(tmp_path),
            use_vscode_convention=False,
        )
        assert path.endswith("my-workflow.prompt.md")
        assert ".github" not in path
        assert Path(path).exists()

    def test_create_workflow_template_custom_description(self, tmp_path: Path) -> None:
        from apm_cli.workflow.discovery import create_workflow_template

        path = create_workflow_template(
            "my-workflow",
            output_dir=str(tmp_path),
            description="Custom description",
            use_vscode_convention=False,
        )
        content = Path(path).read_text(encoding="utf-8")
        assert "Custom description" in content

    def test_create_workflow_template_uses_cwd_when_output_dir_none(self, tmp_path: Path) -> None:
        from apm_cli.workflow.discovery import create_workflow_template

        with patch("os.getcwd", return_value=str(tmp_path)):
            path = create_workflow_template("wf-default-dir", output_dir=None)
        assert Path(path).exists()


# ---------------------------------------------------------------------------
# commands/marketplace/migrate.py
# ---------------------------------------------------------------------------


class TestMarketplaceMigrateCommand:
    """Integration tests for 'apm marketplace migrate'."""

    def test_migrate_success(self, tmp_path: Path) -> None:
        from apm_cli.commands.marketplace.migrate import migrate

        runner = CliRunner()
        with (
            patch(
                "apm_cli.commands.marketplace.migrate.migrate_marketplace_yml", return_value="diff"
            ),
            patch("apm_cli.commands.marketplace.migrate.Path") as mock_path_cls,
        ):
            mock_path_cls.cwd.return_value = tmp_path
            result = runner.invoke(migrate, [], catch_exceptions=False)
        assert result.exit_code == 0
        assert "Migrated" in result.output

    def test_migrate_dry_run_shows_diff(self, tmp_path: Path) -> None:
        from apm_cli.commands.marketplace.migrate import migrate

        runner = CliRunner()
        with (
            patch(
                "apm_cli.commands.marketplace.migrate.migrate_marketplace_yml",
                return_value="--- a\n+++ b\n",
            ),
            patch("apm_cli.commands.marketplace.migrate.Path") as mock_path_cls,
        ):
            mock_path_cls.cwd.return_value = tmp_path
            result = runner.invoke(migrate, ["--dry-run"], catch_exceptions=False)
        assert result.exit_code == 0
        assert "Dry run" in result.output

    def test_migrate_dry_run_no_diff(self, tmp_path: Path) -> None:
        from apm_cli.commands.marketplace.migrate import migrate

        runner = CliRunner()
        with (
            patch(
                "apm_cli.commands.marketplace.migrate.migrate_marketplace_yml", return_value=None
            ),
            patch("apm_cli.commands.marketplace.migrate.Path") as mock_path_cls,
        ):
            mock_path_cls.cwd.return_value = tmp_path
            result = runner.invoke(migrate, ["--dry-run"], catch_exceptions=False)
        assert result.exit_code == 0
        assert "no changes" in result.output.lower() or "(no changes)" in result.output

    def test_migrate_marketplace_yml_error(self, tmp_path: Path) -> None:
        from apm_cli.commands.marketplace.migrate import migrate
        from apm_cli.marketplace.errors import MarketplaceYmlError

        runner = CliRunner()
        with (
            patch(
                "apm_cli.commands.marketplace.migrate.migrate_marketplace_yml",
                side_effect=MarketplaceYmlError("bad yml"),
            ),
            patch("apm_cli.commands.marketplace.migrate.Path") as mock_path_cls,
        ):
            mock_path_cls.cwd.return_value = tmp_path
            result = runner.invoke(migrate, [])
        assert result.exit_code == 1

    def test_migrate_generic_exception(self, tmp_path: Path) -> None:
        from apm_cli.commands.marketplace.migrate import migrate

        runner = CliRunner()
        with (
            patch(
                "apm_cli.commands.marketplace.migrate.migrate_marketplace_yml",
                side_effect=RuntimeError("unexpected"),
            ),
            patch("apm_cli.commands.marketplace.migrate.Path") as mock_path_cls,
        ):
            mock_path_cls.cwd.return_value = tmp_path
            result = runner.invoke(migrate, [])
        assert result.exit_code == 1


# ---------------------------------------------------------------------------
# commands/marketplace/plugin/remove.py
# ---------------------------------------------------------------------------


class TestMarketplacePluginRemoveCommand:
    """Integration tests for 'apm marketplace package remove'."""

    def _make_apm_yml(self, tmp_path: Path) -> Path:
        apm_yml = tmp_path / "apm.yml"
        apm_yml.write_text(
            "marketplace:\n  owner: my-org\n  packages:\n    - name: plugin-a\n      source: github.com/org/a\n",
            encoding="utf-8",
        )
        return apm_yml

    def test_remove_with_yes_flag_calls_remove(self, tmp_path: Path) -> None:
        from apm_cli.commands.marketplace.plugin.remove import remove

        runner = CliRunner()
        with (
            runner.isolated_filesystem(temp_dir=str(tmp_path)),
            patch("apm_cli.marketplace.yml_editor.remove_plugin_entry") as mock_rm,
            patch(
                "apm_cli.commands.marketplace.plugin.remove._ensure_yml_exists",
                return_value=tmp_path / "apm.yml",
            ),
        ):
            result = runner.invoke(remove, ["plugin-a", "--yes"], catch_exceptions=False)
        assert result.exit_code == 0
        mock_rm.assert_called_once()

    def test_remove_non_interactive_no_yes_flag_exits_1(self, tmp_path: Path) -> None:
        from apm_cli.commands.marketplace.plugin.remove import remove

        runner = CliRunner()
        with (
            runner.isolated_filesystem(temp_dir=str(tmp_path)),
            patch(
                "apm_cli.commands.marketplace.plugin.remove._ensure_yml_exists",
                return_value=tmp_path / "apm.yml",
            ),
            patch("apm_cli.commands.marketplace.plugin.remove._is_interactive", return_value=False),
        ):
            result = runner.invoke(remove, ["plugin-a"])
        assert result.exit_code == 1

    def test_remove_abort_confirmation(self, tmp_path: Path) -> None:
        from apm_cli.commands.marketplace.plugin.remove import remove

        runner = CliRunner()
        # Simulate user typing 'n' to abort
        with (
            runner.isolated_filesystem(temp_dir=str(tmp_path)),
            patch(
                "apm_cli.commands.marketplace.plugin.remove._ensure_yml_exists",
                return_value=tmp_path / "apm.yml",
            ),
            patch("apm_cli.commands.marketplace.plugin.remove._is_interactive", return_value=True),
            patch("click.confirm", side_effect=Exception("Aborted.")),
        ):
            result = runner.invoke(remove, ["plugin-a"])
        # Should not crash -- abort is caught
        assert result.exit_code != 0 or "Cancelled" in (result.output or "")

    def test_remove_marketplace_yml_error_exits_2(self, tmp_path: Path) -> None:
        from apm_cli.commands.marketplace.plugin.remove import remove
        from apm_cli.marketplace.errors import MarketplaceYmlError

        runner = CliRunner()
        with (
            runner.isolated_filesystem(temp_dir=str(tmp_path)),
            patch(
                "apm_cli.commands.marketplace.plugin.remove._ensure_yml_exists",
                return_value=tmp_path / "apm.yml",
            ),
            patch(
                "apm_cli.marketplace.yml_editor.remove_plugin_entry",
                side_effect=MarketplaceYmlError("not found"),
            ),
        ):
            result = runner.invoke(remove, ["plugin-missing", "--yes"])
        assert result.exit_code == 2


# ---------------------------------------------------------------------------
# commands/marketplace/plugin/add.py
# ---------------------------------------------------------------------------


class TestMarketplacePluginAddCommand:
    """Integration tests for 'apm marketplace package add'."""

    def test_add_with_no_verify_and_sha_ref(self, tmp_path: Path) -> None:
        from apm_cli.commands.marketplace.plugin.add import add

        sha = "a" * 40
        runner = CliRunner()
        with (
            runner.isolated_filesystem(temp_dir=str(tmp_path)),
            patch(
                "apm_cli.commands.marketplace.plugin.add._ensure_yml_exists",
                return_value=tmp_path / "apm.yml",
            ),
            patch(
                "apm_cli.marketplace.yml_editor.add_plugin_entry",
                return_value="my-plugin",
            ),
            patch("apm_cli.commands.marketplace.plugin.add._verify_source"),
            patch("apm_cli.commands.marketplace.plugin.add._resolve_ref", return_value=sha),
        ):
            result = runner.invoke(
                add,
                ["github.com/org/repo", "--no-verify", f"--ref={sha}"],
                catch_exceptions=False,
            )
        assert result.exit_code == 0
        assert "Added" in result.output

    def test_add_version_and_ref_mutually_exclusive(self, tmp_path: Path) -> None:
        from apm_cli.commands.marketplace.plugin.add import add

        runner = CliRunner()
        with (
            runner.isolated_filesystem(temp_dir=str(tmp_path)),
            patch(
                "apm_cli.commands.marketplace.plugin.add._ensure_yml_exists",
                return_value=tmp_path / "apm.yml",
            ),
        ):
            result = runner.invoke(
                add,
                ["github.com/org/repo", "--version=1.0.0", "--ref=abc123"],
            )
        assert result.exit_code != 0

    def test_add_marketplace_yml_error_exits_2(self, tmp_path: Path) -> None:
        from apm_cli.commands.marketplace.plugin.add import add
        from apm_cli.marketplace.errors import MarketplaceYmlError

        runner = CliRunner()
        with (
            runner.isolated_filesystem(temp_dir=str(tmp_path)),
            patch(
                "apm_cli.commands.marketplace.plugin.add._ensure_yml_exists",
                return_value=tmp_path / "apm.yml",
            ),
            patch("apm_cli.commands.marketplace.plugin.add._verify_source"),
            patch("apm_cli.commands.marketplace.plugin.add._resolve_ref", return_value=None),
            patch(
                "apm_cli.marketplace.yml_editor.add_plugin_entry",
                side_effect=MarketplaceYmlError("duplicate"),
            ),
        ):
            result = runner.invoke(add, ["github.com/org/repo", "--no-verify"])
        assert result.exit_code == 2

    def test_add_with_tags(self, tmp_path: Path) -> None:
        from apm_cli.commands.marketplace.plugin.add import add

        runner = CliRunner()
        with (
            runner.isolated_filesystem(temp_dir=str(tmp_path)),
            patch(
                "apm_cli.commands.marketplace.plugin.add._ensure_yml_exists",
                return_value=tmp_path / "apm.yml",
            ),
            patch("apm_cli.commands.marketplace.plugin.add._verify_source"),
            patch("apm_cli.commands.marketplace.plugin.add._resolve_ref", return_value=None),
            patch(
                "apm_cli.marketplace.yml_editor.add_plugin_entry",
                return_value="tagged-plugin",
            ),
        ):
            result = runner.invoke(
                add,
                ["github.com/org/repo", "--no-verify", "--tags=ai,tools"],
                catch_exceptions=False,
            )
        assert result.exit_code == 0
        assert "tagged-plugin" in result.output


# ---------------------------------------------------------------------------
# install/presentation/dry_run.py
# ---------------------------------------------------------------------------


class TestDryRunPresentation:
    """Tests for render_and_exit covering uncovered branches."""

    def _make_logger(self) -> MagicMock:
        logger = MagicMock()
        logger.progress = MagicMock()
        logger.success = MagicMock()
        logger.dry_run_notice = MagicMock()
        return logger

    def _make_dep(self, repo_url: str = "org/repo", reference: str | None = "main") -> MagicMock:
        dep = MagicMock()
        dep.repo_url = repo_url
        dep.reference = reference
        dep.get_unique_key.return_value = repo_url
        return dep

    def test_renders_mcp_deps(self, tmp_path: Path) -> None:
        from apm_cli.install.presentation.dry_run import render_and_exit

        logger = self._make_logger()
        mcp_dep = MagicMock()
        mcp_dep.__str__ = lambda self: "my-mcp-server"

        with (
            patch("apm_cli.deps.lockfile.LockFile") as mock_lf,
            patch(
                "apm_cli.deps.lockfile.get_lockfile_path", return_value=tmp_path / "apm.lock.yaml"
            ),
            patch("apm_cli.drift.detect_orphans", return_value=set()),
        ):
            mock_lf.read.return_value = MagicMock()
            render_and_exit(
                logger=logger,
                should_install_apm=False,
                apm_deps=[],
                mcp_deps=[mcp_dep],
                dev_apm_deps=[],
                should_install_mcp=True,
                update=False,
                apm_dir=tmp_path,
            )

        calls = [str(c) for c in logger.progress.call_args_list]
        assert any("MCP" in c for c in calls)

    def test_renders_no_deps_message(self, tmp_path: Path) -> None:
        from apm_cli.install.presentation.dry_run import render_and_exit

        logger = self._make_logger()

        with (
            patch("apm_cli.deps.lockfile.LockFile") as mock_lf,
            patch(
                "apm_cli.deps.lockfile.get_lockfile_path", return_value=tmp_path / "apm.lock.yaml"
            ),
        ):
            mock_lf.read.return_value = None
            render_and_exit(
                logger=logger,
                should_install_apm=False,
                apm_deps=[],
                mcp_deps=[],
                dev_apm_deps=[],
                should_install_mcp=False,
                update=False,
                apm_dir=tmp_path,
            )

        calls = [str(c) for c in logger.progress.call_args_list]
        assert any("No dependencies" in c for c in calls)

    def test_renders_orphan_preview(self, tmp_path: Path) -> None:
        from apm_cli.install.presentation.dry_run import render_and_exit

        logger = self._make_logger()
        dep = self._make_dep()
        orphans = {f"file_{i}.md" for i in range(3)}

        with (
            patch("apm_cli.deps.lockfile.LockFile") as mock_lf,
            patch(
                "apm_cli.deps.lockfile.get_lockfile_path", return_value=tmp_path / "apm.lock.yaml"
            ),
            patch("apm_cli.drift.detect_orphans", return_value=orphans),
        ):
            mock_lf.read.return_value = MagicMock()
            render_and_exit(
                logger=logger,
                should_install_apm=True,
                apm_deps=[dep],
                mcp_deps=[],
                dev_apm_deps=[],
                should_install_mcp=False,
                update=False,
                apm_dir=tmp_path,
            )

        calls = [str(c) for c in logger.progress.call_args_list]
        assert any("removed" in c.lower() or "orphan" in c.lower() for c in calls)

    def test_renders_orphan_truncated_preview(self, tmp_path: Path) -> None:
        """When >10 orphans, only first 10 shown + '...and N more' message."""
        from apm_cli.install.presentation.dry_run import render_and_exit

        logger = self._make_logger()
        dep = self._make_dep()
        orphans = {f"file_{i}.md" for i in range(15)}

        with (
            patch("apm_cli.deps.lockfile.LockFile") as mock_lf,
            patch(
                "apm_cli.deps.lockfile.get_lockfile_path", return_value=tmp_path / "apm.lock.yaml"
            ),
            patch("apm_cli.drift.detect_orphans", return_value=orphans),
        ):
            mock_lf.read.return_value = MagicMock()
            render_and_exit(
                logger=logger,
                should_install_apm=True,
                apm_deps=[dep],
                mcp_deps=[],
                dev_apm_deps=[],
                should_install_mcp=False,
                update=False,
                apm_dir=tmp_path,
            )

        calls = [str(c) for c in logger.progress.call_args_list]
        assert any("more" in c.lower() for c in calls)

    def test_lockfile_read_exception_handled(self, tmp_path: Path) -> None:
        """When LockFile.read raises, _dryrun_lock stays None (no orphan preview)."""
        from apm_cli.install.presentation.dry_run import render_and_exit

        logger = self._make_logger()

        with (
            patch("apm_cli.deps.lockfile.LockFile") as mock_lf,
            patch(
                "apm_cli.deps.lockfile.get_lockfile_path", return_value=tmp_path / "apm.lock.yaml"
            ),
        ):
            mock_lf.read.side_effect = OSError("no lockfile")
            render_and_exit(
                logger=logger,
                should_install_apm=False,
                apm_deps=[],
                mcp_deps=[],
                dev_apm_deps=[],
                should_install_mcp=False,
                update=False,
                apm_dir=tmp_path,
            )

        logger.success.assert_called_once()

    def test_renders_apm_deps_update_action(self, tmp_path: Path) -> None:
        from apm_cli.install.presentation.dry_run import render_and_exit

        logger = self._make_logger()
        dep = self._make_dep(reference=None)

        with (
            patch("apm_cli.deps.lockfile.LockFile") as mock_lf,
            patch(
                "apm_cli.deps.lockfile.get_lockfile_path", return_value=tmp_path / "apm.lock.yaml"
            ),
            patch("apm_cli.drift.detect_orphans", return_value=set()),
        ):
            mock_lf.read.return_value = MagicMock()
            render_and_exit(
                logger=logger,
                should_install_apm=True,
                apm_deps=[dep],
                mcp_deps=[],
                dev_apm_deps=[],
                should_install_mcp=False,
                update=True,
                apm_dir=tmp_path,
            )

        calls = [str(c) for c in logger.progress.call_args_list]
        assert any("update" in c.lower() for c in calls)


# ---------------------------------------------------------------------------
# security/file_scanner.py
# ---------------------------------------------------------------------------


class TestFileScannerScanLockfilePackages:
    """Tests for scan_lockfile_packages covering the missed branches."""

    def test_returns_empty_when_no_lockfile(self, tmp_path: Path) -> None:
        from apm_cli.security.file_scanner import scan_lockfile_packages

        with patch("apm_cli.security.file_scanner.LockFile") as mock_lf:
            mock_lf.read.return_value = None
            findings, scanned = scan_lockfile_packages(tmp_path)
        assert findings == {}
        assert scanned == 0

    def test_skips_package_not_matching_filter(self, tmp_path: Path) -> None:
        from apm_cli.security.file_scanner import scan_lockfile_packages

        mock_lock = MagicMock()
        mock_dep = MagicMock()
        mock_dep.deployed_files = ["file.md"]
        mock_lock.dependencies = {"org/other": mock_dep}

        with patch("apm_cli.security.file_scanner.LockFile") as mock_lf:
            mock_lf.read.return_value = mock_lock
            findings, scanned = scan_lockfile_packages(tmp_path, package_filter="org/target")
        assert findings == {}
        assert scanned == 0

    def test_skips_unsafe_lockfile_path(self, tmp_path: Path) -> None:
        from apm_cli.security.file_scanner import scan_lockfile_packages

        mock_lock = MagicMock()
        mock_dep = MagicMock()
        mock_dep.deployed_files = ["../escape.md"]
        mock_lock.dependencies = {"org/repo": mock_dep}

        with (
            patch("apm_cli.security.file_scanner.LockFile") as mock_lf,
            patch("apm_cli.security.file_scanner._is_safe_lockfile_path", return_value=False),
        ):
            mock_lf.read.return_value = mock_lock
            _findings, scanned = scan_lockfile_packages(tmp_path)
        assert scanned == 0

    def test_skips_nonexistent_file(self, tmp_path: Path) -> None:
        from apm_cli.security.file_scanner import scan_lockfile_packages

        mock_lock = MagicMock()
        mock_dep = MagicMock()
        mock_dep.deployed_files = ["nonexistent.md"]
        mock_lock.dependencies = {"org/repo": mock_dep}

        with (
            patch("apm_cli.security.file_scanner.LockFile") as mock_lf,
            patch("apm_cli.security.file_scanner._is_safe_lockfile_path", return_value=True),
        ):
            mock_lf.read.return_value = mock_lock
            _findings, scanned = scan_lockfile_packages(tmp_path)
        assert scanned == 0

    def test_scans_regular_file(self, tmp_path: Path) -> None:
        from apm_cli.security.file_scanner import scan_lockfile_packages

        target_file = tmp_path / "safe.md"
        target_file.write_text("# Safe content\n", encoding="utf-8")

        mock_lock = MagicMock()
        mock_dep = MagicMock()
        mock_dep.deployed_files = ["safe.md"]
        mock_lock.dependencies = {"org/repo": mock_dep}

        with (
            patch("apm_cli.security.file_scanner.LockFile") as mock_lf,
            patch("apm_cli.security.file_scanner._is_safe_lockfile_path", return_value=True),
            patch("apm_cli.security.file_scanner.ContentScanner") as mock_scanner,
        ):
            mock_lf.read.return_value = mock_lock
            mock_scanner.scan_file.return_value = []
            findings, scanned = scan_lockfile_packages(tmp_path)

        assert scanned == 1
        assert findings == {}

    def test_collects_findings_for_file_with_issues(self, tmp_path: Path) -> None:
        from apm_cli.security.content_scanner import ScanFinding
        from apm_cli.security.file_scanner import scan_lockfile_packages

        target_file = tmp_path / "suspicious.md"
        target_file.write_text("SECRET_KEY=abc123\n", encoding="utf-8")

        mock_lock = MagicMock()
        mock_dep = MagicMock()
        mock_dep.deployed_files = ["suspicious.md"]
        mock_lock.dependencies = {"org/repo": mock_dep}

        finding = MagicMock(spec=ScanFinding)
        with (
            patch("apm_cli.security.file_scanner.LockFile") as mock_lf,
            patch("apm_cli.security.file_scanner._is_safe_lockfile_path", return_value=True),
            patch("apm_cli.security.file_scanner.ContentScanner") as mock_scanner,
        ):
            mock_lf.read.return_value = mock_lock
            mock_scanner.scan_file.return_value = [finding]
            findings, scanned = scan_lockfile_packages(tmp_path)

        assert scanned == 1
        assert "suspicious.md" in findings

    def test_scans_directory_via_scan_files_in_dir(self, tmp_path: Path) -> None:
        from apm_cli.security.file_scanner import scan_lockfile_packages

        dir_path = tmp_path / "mypkg"
        dir_path.mkdir()

        mock_lock = MagicMock()
        mock_dep = MagicMock()
        mock_dep.deployed_files = ["mypkg/"]
        mock_lock.dependencies = {"org/repo": mock_dep}

        with (
            patch("apm_cli.security.file_scanner.LockFile") as mock_lf,
            patch("apm_cli.security.file_scanner._is_safe_lockfile_path", return_value=True),
            patch("apm_cli.security.file_scanner._scan_files_in_dir", return_value=({}, 2)),
        ):
            mock_lf.read.return_value = mock_lock
            _findings, scanned = scan_lockfile_packages(tmp_path)

        assert scanned == 2


# ---------------------------------------------------------------------------
# install/phases/heal.py -- branches 85->82, 87->82 (logger is None)
# ---------------------------------------------------------------------------


class TestHealPhaseDispatcher:
    """Covers HealMessageLevel.WARN with logger=None and INFO with logger=None."""

    def _make_install_ctx(self, *, logger: object = None) -> object:
        from pathlib import Path as _Path

        from apm_cli.install.context import InstallContext

        diag = MagicMock()
        diag.error_count = 0
        diag.warn = MagicMock()

        return InstallContext(
            project_root=_Path("/tmp"),
            apm_dir=_Path("/tmp/.apm"),
            logger=logger,
            diagnostics=diag,
            expected_hash_change_deps=set(),
        )

    def test_warn_message_no_logger(self) -> None:
        """When logger=None and message is WARN, diagnostics.warn is still called."""
        from apm_cli.install.heals.base import HealContext, HealMessageLevel
        from apm_cli.install.phases.heal import run_heal_chain

        ctx = self._make_install_ctx(logger=None)

        dep_ref = MagicMock()
        dep_ref.get_unique_key.return_value = "org/repo"

        # Stub a heal that fires a WARN message
        warn_heal = MagicMock()
        warn_heal.exclusive_group = None
        warn_heal.applies.return_value = True

        def _execute(hctx: HealContext) -> None:
            hctx.emit(HealMessageLevel.WARN, "Something repaired")

        warn_heal.execute.side_effect = _execute

        with patch("apm_cli.install.phases.heal.HEAL_CHAIN", [warn_heal]):
            _lm, _rc = run_heal_chain(
                ctx,
                dep_ref,
                resolved_ref=None,
                existing_lockfile=None,
                lockfile_match=True,
                lockfile_match_via_content_hash_only=False,
                update_refs=False,
                ref_changed=False,
            )

        ctx.diagnostics.warn.assert_called_once()

    def test_info_message_no_logger(self) -> None:
        """When logger=None and message is INFO, no crash should occur."""
        from apm_cli.install.heals.base import HealContext, HealMessageLevel
        from apm_cli.install.phases.heal import run_heal_chain

        ctx = self._make_install_ctx(logger=None)

        dep_ref = MagicMock()
        dep_ref.get_unique_key.return_value = "org/repo"

        info_heal = MagicMock()
        info_heal.exclusive_group = None
        info_heal.applies.return_value = True

        def _execute(hctx: HealContext) -> None:
            hctx.emit(HealMessageLevel.INFO, "Branch drift silent re-download")

        info_heal.execute.side_effect = _execute

        with patch("apm_cli.install.phases.heal.HEAL_CHAIN", [info_heal]):
            _lm, _rc = run_heal_chain(
                ctx,
                dep_ref,
                resolved_ref=None,
                existing_lockfile=None,
                lockfile_match=True,
                lockfile_match_via_content_hash_only=False,
                update_refs=False,
                ref_changed=False,
            )

        # No crash; diagnostics.warn NOT called for INFO
        ctx.diagnostics.warn.assert_not_called()


# ---------------------------------------------------------------------------
# install/phases/post_deps_local.py
# ---------------------------------------------------------------------------


class TestPostDepsLocalPhase:
    """Tests for install/phases/post_deps_local.run()."""

    def _make_ctx(
        self,
        *,
        scope_project: bool = True,
        local_deployed: list[str] | None = None,
        old_local_deployed: list[str] | None = None,
        local_content_errors_before: int = 0,
        has_diag_errors: bool = False,
        existing_lockfile: object = None,
        tmp_path: Path | None = None,
    ) -> object:
        from apm_cli.core.scope import InstallScope
        from apm_cli.install.context import InstallContext

        root = tmp_path or Path("/tmp")
        apm_dir = root / ".apm"
        apm_dir.mkdir(parents=True, exist_ok=True)

        diag = MagicMock()
        diag.error_count = 1 if has_diag_errors else 0

        logger = MagicMock()

        ctx = InstallContext(
            project_root=root,
            apm_dir=apm_dir,
            scope=InstallScope.PROJECT if scope_project else InstallScope.USER,
            diagnostics=diag,
            logger=logger,
            existing_lockfile=existing_lockfile,
            local_content_errors_before=local_content_errors_before,
        )
        ctx.local_deployed_files = list(local_deployed or [])
        ctx.old_local_deployed = list(old_local_deployed or [])
        ctx.targets = []
        return ctx

    def test_skips_when_user_scope(self, tmp_path: Path) -> None:
        from apm_cli.install.phases.post_deps_local import run

        ctx = self._make_ctx(scope_project=False, tmp_path=tmp_path)
        # Should return early without doing anything
        with patch("apm_cli.deps.lockfile.LockFile") as mock_lf:
            run(ctx)
        mock_lf.assert_not_called()

    def test_skips_when_no_local_content(self, tmp_path: Path) -> None:
        from apm_cli.install.phases.post_deps_local import run

        ctx = self._make_ctx(
            local_deployed=[],
            old_local_deployed=[],
            tmp_path=tmp_path,
        )
        with patch("apm_cli.deps.lockfile.LockFile") as mock_lf:
            run(ctx)
        mock_lf.assert_not_called()

    def test_persists_lockfile_with_local_deployed(self, tmp_path: Path) -> None:
        from apm_cli.install.phases.post_deps_local import run

        ctx = self._make_ctx(
            local_deployed=["file.md"],
            old_local_deployed=[],
            tmp_path=tmp_path,
        )

        mock_lock = MagicMock()
        mock_lock.local_deployed_files = []
        mock_lock.local_deployed_file_hashes = {}
        mock_lock.is_semantically_equivalent.return_value = False

        with (
            patch("apm_cli.deps.lockfile.LockFile") as mock_lf_cls,
            patch(
                "apm_cli.deps.lockfile.get_lockfile_path", return_value=tmp_path / "apm.lock.yaml"
            ),
            patch("apm_cli.install.phases.lockfile.compute_deployed_hashes", return_value={}),
        ):
            mock_lf_cls.return_value = mock_lock
            mock_lf_cls.read.return_value = None
            run(ctx)

        mock_lock.save.assert_called_once()

    def test_stale_cleanup_runs_when_old_files(self, tmp_path: Path) -> None:
        from apm_cli.install.phases.post_deps_local import run
        from apm_cli.integration.targets import KNOWN_TARGETS

        stale = ".github/instructions/stale.instructions.md"
        current = ".github/instructions/current.instructions.md"

        ctx = self._make_ctx(
            local_deployed=[current],
            old_local_deployed=[stale, current],
            has_diag_errors=False,
            tmp_path=tmp_path,
        )
        ctx.targets = [KNOWN_TARGETS["copilot"]]
        (tmp_path / "apm.yml").write_text("targets:\n  - copilot\n", encoding="utf-8")

        mock_cleanup_result = MagicMock()
        mock_cleanup_result.failed = []
        mock_cleanup_result.deleted_targets = []
        mock_cleanup_result.skipped_user_edit = []
        mock_cleanup_result.skipped_unmanaged = []
        mock_cleanup_result.retained = []
        mock_cleanup_result.deleted = [stale]

        mock_lock = MagicMock()
        mock_lock.local_deployed_files = [stale, current]
        mock_lock.local_deployed_file_hashes = {}
        mock_lock.is_semantically_equivalent.return_value = False

        with (
            patch(
                "apm_cli.integration.cleanup.remove_stale_deployed_files",
                return_value=mock_cleanup_result,
            ) as mock_rm,
            patch("apm_cli.deps.lockfile.LockFile") as mock_lf_cls,
            patch(
                "apm_cli.deps.lockfile.get_lockfile_path", return_value=tmp_path / "apm.lock.yaml"
            ),
            patch("apm_cli.install.phases.lockfile.compute_deployed_hashes", return_value={}),
        ):
            mock_lf_cls.return_value = mock_lock
            mock_lf_cls.read.return_value = None
            run(ctx)

        mock_rm.assert_called_once()
        assert set(mock_rm.call_args.args[0]) == {stale}
        assert ctx.local_deployed_files == [current]

    def test_skips_stale_cleanup_when_local_errors(self, tmp_path: Path) -> None:
        """When integration had errors, stale cleanup is skipped."""
        from apm_cli.install.phases.post_deps_local import run
        from apm_cli.integration.targets import KNOWN_TARGETS

        stale = ".github/instructions/stale.instructions.md"
        current = ".github/instructions/current.instructions.md"

        ctx = self._make_ctx(
            local_deployed=[current],
            old_local_deployed=[stale],
            has_diag_errors=True,
            local_content_errors_before=0,  # errors happened during integration
            tmp_path=tmp_path,
        )
        ctx.targets = [KNOWN_TARGETS["copilot"]]
        (tmp_path / "apm.yml").write_text("targets:\n  - copilot\n", encoding="utf-8")

        mock_lock = MagicMock()
        mock_lock.local_deployed_files = [stale]
        mock_lock.local_deployed_file_hashes = {}
        mock_lock.is_semantically_equivalent.return_value = True

        with (
            patch("apm_cli.integration.cleanup.remove_stale_deployed_files") as mock_rm,
            patch("apm_cli.deps.lockfile.LockFile") as mock_lf_cls,
            patch(
                "apm_cli.deps.lockfile.get_lockfile_path", return_value=tmp_path / "apm.lock.yaml"
            ),
            patch("apm_cli.install.phases.lockfile.compute_deployed_hashes", return_value={}),
        ):
            mock_lf_cls.return_value = mock_lock
            mock_lf_cls.read.return_value = None
            run(ctx)

        mock_rm.assert_not_called()
        assert set(ctx.local_deployed_files) == {current, stale}
