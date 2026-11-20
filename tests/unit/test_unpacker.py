"""Unit tests for apm_cli.bundle.unpacker."""

import zipfile
from pathlib import Path

import pytest

from apm_cli.deps.lockfile import LockedDependency, LockFile
from tests.utils.unpack_admission import unpack_bundle


def _build_bundle_dir(tmp_path: Path, deployed_files: list[str]) -> Path:
    """Create a bundle directory with an enriched lockfile and the listed files."""
    bundle = tmp_path / "bundle" / "test-pkg-1.0.0"
    bundle.mkdir(parents=True)

    for fpath in deployed_files:
        full = bundle / fpath
        if fpath.endswith("/"):
            full.mkdir(parents=True, exist_ok=True)
        else:
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_text(f"content of {fpath}", encoding="utf-8")

    lockfile = LockFile()
    dep = LockedDependency(
        repo_url="owner/repo",
        resolved_commit="abc123",
        deployed_files=deployed_files,
    )
    lockfile.add_dependency(dep)
    lockfile.write(bundle / "apm.lock.yaml")
    return bundle


def _archive_bundle(bundle_dir: Path, dest: Path) -> Path:
    """Create a .zip from a bundle directory."""
    archive_path = dest / f"{bundle_dir.name}.zip"
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for fp in sorted(bundle_dir.rglob("*")):
            if fp.is_symlink() or not fp.is_file():
                continue
            zf.write(fp, arcname=f"{bundle_dir.name}/{fp.relative_to(bundle_dir).as_posix()}")
    return archive_path


class TestUnpackBundle:
    def test_unpack_directory(self, tmp_path):
        deployed = [".github/agents/a.md", ".github/instructions/b.md"]
        bundle = _build_bundle_dir(tmp_path, deployed)
        output = tmp_path / "target"
        output.mkdir()

        result = unpack_bundle(bundle, output)

        assert set(result.files) == set(deployed)
        assert result.verified
        for f in deployed:
            assert (output / f).exists()

    def test_unpack_archive(self, tmp_path):
        deployed = [".github/agents/a.md"]
        bundle = _build_bundle_dir(tmp_path, deployed)
        archive = _archive_bundle(bundle, tmp_path)
        output = tmp_path / "target"
        output.mkdir()

        result = unpack_bundle(archive, output)

        assert set(result.files) == set(deployed)
        assert result.verified
        assert (output / ".github" / "agents" / "a.md").exists()

    def test_unpack_verify_complete(self, tmp_path):
        deployed = [".github/agents/a.md", ".claude/commands/b.md"]
        bundle = _build_bundle_dir(tmp_path, deployed)
        output = tmp_path / "target"
        output.mkdir()

        result = unpack_bundle(bundle, output)

        assert result.verified

    def test_unpack_names_a_plugin_bundle_instead_of_missing_files(self, tmp_path):
        """The default plugin layout is routed to 'apm install' (#3994)."""
        bundle = _build_bundle_dir(tmp_path, [".github/agents/a.md"])
        (bundle / ".github" / "agents" / "a.md").unlink()
        (bundle / "plugin.json").write_text("{}", encoding="utf-8")
        output = tmp_path / "target"
        output.mkdir()

        with pytest.raises(ValueError, match=r"plugin-format bundle.*apm install"):
            unpack_bundle(bundle, output)

    def test_unpack_verify_missing_file(self, tmp_path):
        deployed = [".github/agents/a.md", ".github/agents/missing.md"]
        bundle_dir = tmp_path / "bundle" / "test-pkg-1.0.0"
        bundle_dir.mkdir(parents=True)

        # Only create one file on disk but claim two in lockfile
        (bundle_dir / ".github" / "agents").mkdir(parents=True)
        (bundle_dir / ".github" / "agents" / "a.md").write_text("ok")

        lockfile = LockFile()
        dep = LockedDependency(
            repo_url="owner/repo",
            deployed_files=deployed,
        )
        lockfile.add_dependency(dep)
        lockfile.write(bundle_dir / "apm.lock.yaml")

        output = tmp_path / "target"
        output.mkdir()

        with pytest.raises(ValueError, match="missing from the bundle"):
            unpack_bundle(bundle_dir, output)

    def test_unpack_skip_verify(self, tmp_path):
        deployed = [".github/agents/a.md", ".github/agents/missing.md"]
        bundle_dir = tmp_path / "bundle" / "test-pkg-1.0.0"
        bundle_dir.mkdir(parents=True)

        (bundle_dir / ".github" / "agents").mkdir(parents=True)
        (bundle_dir / ".github" / "agents" / "a.md").write_text("ok")

        lockfile = LockFile()
        dep = LockedDependency(
            repo_url="owner/repo",
            deployed_files=deployed,
        )
        lockfile.add_dependency(dep)
        lockfile.write(bundle_dir / "apm.lock.yaml")

        output = tmp_path / "target"
        output.mkdir()

        # skip_verify should bypass the missing-file check
        result = unpack_bundle(bundle_dir, output, skip_verify=True)
        assert not result.verified
        # a.md should still be copied
        assert (output / ".github" / "agents" / "a.md").exists()

    def test_unpack_dry_run(self, tmp_path):
        deployed = [".github/agents/a.md"]
        bundle = _build_bundle_dir(tmp_path, deployed)
        output = tmp_path / "target"
        output.mkdir()

        result = unpack_bundle(bundle, output, dry_run=True)

        assert result.files == deployed
        # Nothing written
        assert not (output / ".github").exists()

    def test_unpack_preserves_local_files(self, tmp_path):
        deployed = [".github/agents/a.md"]
        bundle = _build_bundle_dir(tmp_path, deployed)
        output = tmp_path / "target"
        output.mkdir()

        # Pre-existing local file
        local_file = output / ".github" / "instructions" / "my-local.md"
        local_file.parent.mkdir(parents=True)
        local_file.write_text("local content")

        unpack_bundle(bundle, output)

        # Local file untouched
        assert local_file.read_text() == "local content"
        # Bundle file present
        assert (output / ".github" / "agents" / "a.md").exists()

    def test_unpack_overwrites_bundle_files(self, tmp_path):
        deployed = [".github/agents/a.md"]
        bundle = _build_bundle_dir(tmp_path, deployed)
        output = tmp_path / "target"
        output.mkdir()

        # Pre-existing file with same path
        existing = output / ".github" / "agents" / "a.md"
        existing.parent.mkdir(parents=True)
        existing.write_text("old content")

        unpack_bundle(bundle, output)

        assert (
            output / ".github" / "agents" / "a.md"
        ).read_text() == "content of .github/agents/a.md"

    def test_unpack_lockfile_not_scattered(self, tmp_path):
        deployed = [".github/agents/a.md"]
        bundle = _build_bundle_dir(tmp_path, deployed)
        output = tmp_path / "target"
        output.mkdir()

        unpack_bundle(bundle, output)

        # lockfile should NOT be copied to the output root
        assert not (output / "apm.lock.yaml").exists()
        assert not (output / "apm.lock").exists()

    def test_unpack_rejects_absolute_path_in_deployed_files(self, tmp_path):
        """unpack_bundle must reject absolute paths from bundle lockfile."""
        bundle_dir = tmp_path / "bundle" / "test-pkg-1.0.0"
        bundle_dir.mkdir(parents=True)
        lockfile = LockFile()
        dep = LockedDependency(repo_url="owner/repo", deployed_files=["/etc/passwd"])
        lockfile.add_dependency(dep)
        lockfile.write(bundle_dir / "apm.lock.yaml")
        output = tmp_path / "target"
        output.mkdir()

        with pytest.raises(ValueError, match="unsafe path"):
            unpack_bundle(bundle_dir, output, skip_verify=True)

    def test_unpack_rejects_traversal_path_in_deployed_files(self, tmp_path):
        """unpack_bundle must reject path-traversal entries from bundle lockfile."""
        bundle_dir = tmp_path / "bundle" / "test-pkg-1.0.0"
        bundle_dir.mkdir(parents=True)
        lockfile = LockFile()
        dep = LockedDependency(repo_url="owner/repo", deployed_files=["../outside.txt"])
        lockfile.add_dependency(dep)
        lockfile.write(bundle_dir / "apm.lock.yaml")
        output = tmp_path / "target"
        output.mkdir()

        with pytest.raises(ValueError, match="unsafe path"):
            unpack_bundle(bundle_dir, output, skip_verify=True)

    def test_unpack_dependency_files_single_dep(self, tmp_path):
        """dependency_files maps repo_url to its deployed files."""
        deployed = [".github/agents/a.md", ".github/prompts/b.md"]
        bundle = _build_bundle_dir(tmp_path, deployed)
        output = tmp_path / "target"
        output.mkdir()

        result = unpack_bundle(bundle, output)

        assert "owner/repo" in result.dependency_files
        assert set(result.dependency_files["owner/repo"]) == set(deployed)

    def test_unpack_dependency_files_multiple_deps(self, tmp_path):
        """dependency_files tracks files per dependency when bundle has many."""
        bundle_dir = tmp_path / "bundle" / "multi-pkg"
        bundle_dir.mkdir(parents=True)

        files_a = [".github/agents/a.md"]
        files_b = [".github/prompts/b.md", ".github/instructions/c.md"]
        for f in files_a + files_b:
            p = bundle_dir / f
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(f"content of {f}")

        lockfile = LockFile()
        lockfile.add_dependency(LockedDependency(repo_url="org/repo-a", deployed_files=files_a))
        lockfile.add_dependency(LockedDependency(repo_url="org/repo-b", deployed_files=files_b))
        lockfile.write(bundle_dir / "apm.lock.yaml")

        output = tmp_path / "target"
        output.mkdir()
        result = unpack_bundle(bundle_dir, output)

        assert result.dependency_files["org/repo-a"] == files_a
        assert set(result.dependency_files["org/repo-b"]) == set(files_b)
        assert len(result.files) == 3

    def test_unpack_dependency_files_virtual_deps(self, tmp_path):
        """Virtual deps from the same repo are tracked separately."""
        bundle_dir = tmp_path / "bundle" / "virtual-pkg"
        bundle_dir.mkdir(parents=True)

        files_a = [".github/agents/a.md"]
        files_b = [".github/prompts/b.md"]
        for f in files_a + files_b:
            p = bundle_dir / f
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(f"content of {f}")

        lockfile = LockFile()
        lockfile.add_dependency(
            LockedDependency(
                repo_url="org/monorepo",
                virtual_path="packages/alpha",
                is_virtual=True,
                deployed_files=files_a,
            )
        )
        lockfile.add_dependency(
            LockedDependency(
                repo_url="org/monorepo",
                virtual_path="packages/beta",
                is_virtual=True,
                deployed_files=files_b,
            )
        )
        lockfile.write(bundle_dir / "apm.lock.yaml")

        output = tmp_path / "target"
        output.mkdir()
        result = unpack_bundle(bundle_dir, output)

        assert "org/monorepo/packages/alpha" in result.dependency_files
        assert "org/monorepo/packages/beta" in result.dependency_files
        assert result.dependency_files["org/monorepo/packages/alpha"] == files_a
        assert result.dependency_files["org/monorepo/packages/beta"] == files_b
        assert len(result.files) == 2

    def test_unpack_dependency_files_dry_run(self, tmp_path):
        """dependency_files is populated even in dry-run mode."""
        deployed = [".github/agents/a.md"]
        bundle = _build_bundle_dir(tmp_path, deployed)
        output = tmp_path / "target"
        output.mkdir()

        result = unpack_bundle(bundle, output, dry_run=True)

        assert "owner/repo" in result.dependency_files
        assert result.dependency_files["owner/repo"] == deployed

    def test_unpack_skipped_count(self, tmp_path):
        """skipped_count tracks files missing from bundle when skip_verify."""
        deployed = [".github/agents/a.md", ".github/agents/missing.md"]
        bundle_dir = tmp_path / "bundle" / "test-pkg"
        bundle_dir.mkdir(parents=True)

        (bundle_dir / ".github" / "agents").mkdir(parents=True)
        (bundle_dir / ".github" / "agents" / "a.md").write_text("ok")

        lockfile = LockFile()
        lockfile.add_dependency(LockedDependency(repo_url="owner/repo", deployed_files=deployed))
        lockfile.write(bundle_dir / "apm.lock.yaml")

        output = tmp_path / "target"
        output.mkdir()

        result = unpack_bundle(bundle_dir, output, skip_verify=True)

        assert result.skipped_count == 1
        assert (output / ".github" / "agents" / "a.md").exists()

    def test_unpack_skipped_count_zero_when_all_present(self, tmp_path):
        """skipped_count is zero when all files are present."""
        deployed = [".github/agents/a.md"]
        bundle = _build_bundle_dir(tmp_path, deployed)
        output = tmp_path / "target"
        output.mkdir()

        result = unpack_bundle(bundle, output)

        assert result.skipped_count == 0

    def test_unpack_legacy_lockfile_backward_compat(self, tmp_path):
        """Bundles with legacy apm.lock (no .yaml) are still readable."""
        deployed = [".github/agents/a.md"]
        bundle_dir = tmp_path / "bundle" / "legacy-pkg"
        bundle_dir.mkdir(parents=True)

        (bundle_dir / ".github" / "agents").mkdir(parents=True)
        (bundle_dir / ".github" / "agents" / "a.md").write_text("ok")

        lockfile = LockFile()
        lockfile.add_dependency(LockedDependency(repo_url="owner/repo", deployed_files=deployed))
        # Write using the legacy name to simulate an old bundle
        lockfile.write(bundle_dir / "apm.lock")

        output = tmp_path / "target"
        output.mkdir()

        result = unpack_bundle(bundle_dir, output)

        assert set(result.files) == set(deployed)
        assert (output / ".github" / "agents" / "a.md").exists()

    def test_unpack_returns_pack_meta(self, tmp_path):
        """Enriched bundles expose the pack: metadata in UnpackResult."""
        import yaml

        deployed = [".claude/skills/x/SKILL.md"]
        bundle_dir = _build_bundle_dir(tmp_path, deployed)

        # Prepend a pack: section to the lockfile (simulates apm pack output)
        lf_path = bundle_dir / "apm.lock.yaml"
        existing = lf_path.read_text(encoding="utf-8")
        pack_section = yaml.dump(
            {"pack": {"format": "apm", "target": "claude"}},
            default_flow_style=False,
        )
        lf_path.write_text(pack_section + existing, encoding="utf-8")

        output = tmp_path / "out"
        output.mkdir()
        result = unpack_bundle(bundle_dir, output)

        assert result.pack_meta.get("target") == "claude"
        assert result.pack_meta.get("format") == "apm"

    def test_unpack_pack_meta_empty_for_plain_bundles(self, tmp_path):
        """Bundles without pack: section return empty pack_meta."""
        deployed = [".github/agents/a.md"]
        bundle_dir = _build_bundle_dir(tmp_path, deployed)

        output = tmp_path / "out"
        output.mkdir()
        result = unpack_bundle(bundle_dir, output)

        assert result.pack_meta == {}


class TestUnpackAdmission:
    """Admission (org policy and integrity) runs before a preview or a write."""

    @pytest.mark.parametrize("dry_run", [False, True])
    def test_refused_admission_writes_nothing(self, tmp_path, dry_run):
        bundle = _build_bundle_dir(tmp_path, [".github/agents/a.md"])
        output = tmp_path / "target"
        output.mkdir()

        def refuse(candidate):
            raise ValueError("refused")

        with pytest.raises(ValueError, match="refused"):
            unpack_bundle(bundle, output, dry_run=dry_run, admit=refuse)
        assert not any(output.iterdir())

    def test_admission_sees_every_file_it_would_write(self, tmp_path):
        bundle = _build_bundle_dir(tmp_path, [".github/skills/s/"])
        (bundle / ".github" / "skills" / "s" / "SKILL.md").write_text("x", encoding="utf-8")
        seen = []

        unpack_bundle(bundle, tmp_path / "target", admit=seen.append)

        assert ".github/skills/s/SKILL.md" in seen[0].files
        assert seen[0].lockfile.get_package_dependencies()[0].repo_url == "owner/repo"


class TestApmBundleIntegrity:
    """``--format apm`` bundles are checked against their per-file lock hashes."""

    @staticmethod
    def _check(bundle, logger):
        from apm_cli.install.bundle_admission import apm_bundle_integrity

        lockfile = LockFile.read(bundle / "apm.lock.yaml")
        return apm_bundle_integrity(bundle, lockfile, [".github/agents/a.md"], logger)

    @staticmethod
    def _hashed_bundle(tmp_path):
        from apm_cli.utils.content_hash import compute_file_hash

        bundle = _build_bundle_dir(tmp_path, [".github/agents/a.md"])
        lockfile = LockFile()
        lockfile.add_dependency(
            LockedDependency(
                repo_url="owner/repo",
                resolved_commit="abc123",
                deployed_files=[".github/agents/a.md"],
                deployed_file_hashes={
                    ".github/agents/a.md": compute_file_hash(bundle / ".github/agents/a.md")
                },
            )
        )
        lockfile.write(bundle / "apm.lock.yaml")
        return bundle

    def test_matching_hashes_pass_under_require_hashes(self, tmp_path):
        from unittest.mock import MagicMock

        logger = MagicMock()
        self._check(self._hashed_bundle(tmp_path), logger)(True)
        logger.warning.assert_not_called()

    def test_altered_file_is_refused_under_require_hashes(self, tmp_path):
        from unittest.mock import MagicMock

        bundle = self._hashed_bundle(tmp_path)
        (bundle / ".github/agents/a.md").write_text("altered", encoding="utf-8")
        with pytest.raises(ValueError, match=r"Hash mismatch: \.github/agents/a\.md"):
            self._check(bundle, MagicMock())(True)

    def test_altered_file_warns_without_require_hashes(self, tmp_path):
        from unittest.mock import MagicMock

        bundle = self._hashed_bundle(tmp_path)
        (bundle / ".github/agents/a.md").write_text("altered", encoding="utf-8")
        logger = MagicMock()
        self._check(bundle, logger)(False)
        assert ".github/agents/a.md" in logger.warning.call_args.args[0]

    def test_unhashed_file_is_refused_under_require_hashes(self, tmp_path):
        from unittest.mock import MagicMock

        bundle = _build_bundle_dir(tmp_path, [".github/agents/a.md"])
        with pytest.raises(ValueError, match="No locked hash"):
            self._check(bundle, MagicMock())(True)
        self._check(bundle, MagicMock())(False)


class TestUnpackCmdLogging:
    """Verify CLI output for the unpack command."""

    def test_unpack_cmd_logs_file_list(self, tmp_path):
        """unpack command outputs each file under its dependency name."""
        import os

        from click.testing import CliRunner

        from apm_cli.commands.pack import unpack_cmd

        deployed = [".github/agents/a.md", ".github/prompts/b.md"]
        bundle = _build_bundle_dir(tmp_path, deployed)
        output = tmp_path / "target"
        output.mkdir()

        runner = CliRunner()
        original_dir = os.getcwd()
        try:
            result = runner.invoke(
                unpack_cmd, [str(bundle), "-o", str(output)], catch_exceptions=False
            )
        finally:
            os.chdir(original_dir)

        assert result.exit_code == 0
        assert "Unpacking" in result.output
        assert "owner/repo" in result.output
        assert ".github/agents/a.md" in result.output
        assert ".github/prompts/b.md" in result.output
        assert "Unpacked 2 file(s)" in result.output

    def test_unpack_cmd_dry_run_logs_files(self, tmp_path):
        """Dry-run output includes per-dependency file listing."""
        import os

        from click.testing import CliRunner

        from apm_cli.commands.pack import unpack_cmd

        deployed = [".github/agents/a.md"]
        bundle = _build_bundle_dir(tmp_path, deployed)
        output = tmp_path / "target"
        output.mkdir()

        runner = CliRunner()
        original_dir = os.getcwd()
        try:
            result = runner.invoke(
                unpack_cmd,
                [str(bundle), "-o", str(output), "--dry-run"],
                catch_exceptions=False,
            )
        finally:
            os.chdir(original_dir)

        assert result.exit_code == 0
        assert "dry-run" in result.output
        assert "Would unpack 1 file(s)" in result.output
        assert ".github/agents/a.md" in result.output

    def test_unpack_cmd_logs_skipped_files(self, tmp_path):
        """Skipped files warning appears when skip_verify allows missing files."""
        import os

        from click.testing import CliRunner

        from apm_cli.commands.pack import unpack_cmd

        deployed = [".github/agents/a.md", ".github/agents/missing.md"]
        bundle_dir = tmp_path / "bundle" / "test-pkg"
        bundle_dir.mkdir(parents=True)

        (bundle_dir / ".github" / "agents").mkdir(parents=True)
        (bundle_dir / ".github" / "agents" / "a.md").write_text("ok")

        lockfile = LockFile()
        lockfile.add_dependency(LockedDependency(repo_url="owner/repo", deployed_files=deployed))
        lockfile.write(bundle_dir / "apm.lock.yaml")

        output = tmp_path / "target"
        output.mkdir()

        runner = CliRunner()
        original_dir = os.getcwd()
        try:
            result = runner.invoke(
                unpack_cmd,
                [str(bundle_dir), "-o", str(output), "--skip-verify"],
                catch_exceptions=False,
            )
        finally:
            os.chdir(original_dir)

        assert result.exit_code == 0
        assert "1 file(s) skipped" in result.output

    def test_unpack_cmd_multi_dep_logging(self, tmp_path):
        """Multiple dependencies are each logged with their file lists."""
        import os

        from click.testing import CliRunner

        from apm_cli.commands.pack import unpack_cmd

        bundle_dir = tmp_path / "bundle" / "multi-pkg"
        bundle_dir.mkdir(parents=True)

        files_a = [".github/agents/a.md"]
        files_b = [".github/prompts/b.md"]
        for f in files_a + files_b:
            p = bundle_dir / f
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(f"content of {f}")

        lockfile = LockFile()
        lockfile.add_dependency(LockedDependency(repo_url="org/repo-a", deployed_files=files_a))
        lockfile.add_dependency(LockedDependency(repo_url="org/repo-b", deployed_files=files_b))
        lockfile.write(bundle_dir / "apm.lock.yaml")

        output = tmp_path / "target"
        output.mkdir()

        runner = CliRunner()
        original_dir = os.getcwd()
        try:
            result = runner.invoke(
                unpack_cmd, [str(bundle_dir), "-o", str(output)], catch_exceptions=False
            )
        finally:
            os.chdir(original_dir)

        assert result.exit_code == 0
        assert "org/repo-a" in result.output
        assert "org/repo-b" in result.output
        assert ".github/agents/a.md" in result.output
        assert ".github/prompts/b.md" in result.output
        assert "Unpacked 2 file(s)" in result.output


class TestUnpackCanvasTrust:
    """Canvas extensions are executable code: unpack must enforce the feature gate.

    The ``canvas`` experimental flag controls feature availability.
    When OFF, canvas paths are dropped.  When ON, unpack refuses them:
    only ``apm install <bundle>`` applies the executables gate.
    """

    @pytest.fixture(autouse=True)
    def _clear_config_cache(self):
        from apm_cli.config import _invalidate_config_cache

        _invalidate_config_cache()
        yield
        _invalidate_config_cache()

    @pytest.fixture
    def enable_canvas(self, monkeypatch):
        import apm_cli.config as _conf

        monkeypatch.setattr(_conf, "_config_cache", {"experimental": {"canvas": True}})

    def test_canvas_blocked_when_flag_off(self, tmp_path):
        """Canvas is silently dropped when the experimental flag is off."""
        deployed = [".github/agents/a.md", ".github/extensions/widget/extension.mjs"]
        bundle = _build_bundle_dir(tmp_path, deployed)
        output = tmp_path / "target"
        output.mkdir()

        result = unpack_bundle(bundle, output)

        assert result.canvas_blocked == 1
        assert ".github/extensions/widget/extension.mjs" not in result.files
        assert ".github/agents/a.md" in result.files
        assert not (output / ".github" / "extensions" / "widget").exists()
        assert (output / ".github" / "agents" / "a.md").exists()

    def test_canvas_refused_when_flag_on(self, tmp_path, enable_canvas):
        """With the flag on, canvas is executable content that unpack refuses."""
        deployed = [".github/agents/a.md", ".github/extensions/widget/extension.mjs"]
        bundle = _build_bundle_dir(tmp_path, deployed)
        output = tmp_path / "target"
        output.mkdir()

        with pytest.raises(ValueError, match="apm install"):
            unpack_bundle(bundle, output)

        assert list(output.iterdir()) == []

    def test_canvas_blocked_when_flag_off_no_trust(self, tmp_path):
        """When the experimental flag is off, canvas is always blocked regardless."""
        deployed = [".github/extensions/widget/extension.mjs"]
        bundle = _build_bundle_dir(tmp_path, deployed)
        output = tmp_path / "target"
        output.mkdir()

        result = unpack_bundle(bundle, output)

        assert result.canvas_blocked == 1
        assert ".github/extensions/widget/extension.mjs" not in result.files
        assert not (output / ".github" / "extensions").exists()

    def test_canvas_block_is_dry_run_visible(self, tmp_path):
        deployed = [".github/extensions/widget/extension.mjs"]
        bundle = _build_bundle_dir(tmp_path, deployed)
        output = tmp_path / "target"
        output.mkdir()

        result = unpack_bundle(bundle, output, dry_run=True)

        assert result.canvas_blocked == 1
        assert result.files == []
        assert not (output / ".github" / "extensions").exists()


class TestUnpackRefusesExecutables:
    """``apm unpack`` cannot apply the executables gate, so it deploys none (#4027)."""

    @pytest.mark.parametrize(
        "executable",
        [
            ".github/hooks/pkg-hooks.json",
            ".github/hooks/scripts/pkg/run.sh",
            ".claude/settings.json",
            ".cursor/hooks.json",
            ".vscode/mcp.json",
            ".codex/config.toml",
            ".github/skills/tool/bin/tool",
            ".agents/mcp_config.json",
            ".kiro/settings/mcp.json",
            ".claude/skills/apm-lsp/.claude-plugin/plugin.json",
            ".github/lsp.json",
        ],
    )
    @pytest.mark.parametrize("dry_run", [False, True])
    def test_executable_content_is_refused_before_any_write(self, tmp_path, executable, dry_run):
        bundle = _build_bundle_dir(tmp_path, [".github/agents/a.md", executable])
        output = tmp_path / "target"
        output.mkdir()

        with pytest.raises(ValueError, match=r"apm install .*test-pkg-1\.0\.0"):
            unpack_bundle(bundle, output, dry_run=dry_run)

        assert list(output.iterdir()) == []

    def test_executable_inside_a_directory_entry_is_refused(self, tmp_path):
        bundle = _build_bundle_dir(tmp_path, [".github/skills/tool/"])
        (bundle / ".github/skills/tool/bin").mkdir()
        (bundle / ".github/skills/tool/bin/run").write_text("x", encoding="utf-8")
        output = tmp_path / "target"
        output.mkdir()

        with pytest.raises(ValueError, match="bin executable"):
            unpack_bundle(bundle, output)

    def test_file_listed_with_its_directory_is_counted_once(self, tmp_path):
        bundle = _build_bundle_dir(
            tmp_path, [".github/skills/tool/", ".github/skills/tool/bin/run"]
        )
        output = tmp_path / "target"
        output.mkdir()

        with pytest.raises(ValueError, match=r"Refusing to unpack 1 executable file"):
            unpack_bundle(bundle, output)

    @pytest.mark.parametrize(
        "path",
        [
            "apm-hooks.json",
            ".github/hooks/apm-hooks.json",
            "lsp-config.json",
            "config.yaml",
            ".claude.json",
            ".opencode/agents/opencode.json",
        ],
    )
    def test_config_names_are_executable_content(self, path):
        from apm_cli.bundle.unpacker import executable_content_kind

        assert executable_content_kind(path) is not None

    def test_owner_catalog_paths_are_executable_content(self):
        from apm_cli.bundle.unpacker import executable_content_kind
        from apm_cli.core.plugin_manifest import PLUGIN_ECOSYSTEM_PATHS
        from apm_cli.integration.lsp_integrator import _LSP_TARGET_SPECS

        paths = ["/".join(spec.project_relative_path) for spec in _LSP_TARGET_SPECS.values()]
        paths += list(PLUGIN_ECOSYSTEM_PATHS.values())
        paths += [f".claude/skills/x/{p}" for p in PLUGIN_ECOSYSTEM_PATHS.values()]
        assert all(executable_content_kind(p) == "hook, MCP or LSP configuration" for p in paths)

    def test_client_settings_names_are_bounded_by_depth(self):
        from apm_cli.bundle.unpacker import executable_content_kind

        assert executable_content_kind(".github/skills/x/settings.json") is None

    def test_non_executable_content_still_unpacks(self, tmp_path):
        deployed = [
            ".github/agents/a.md",
            ".github/skills/tool/SKILL.md",
            ".github/skills/tool/scripts/helper.py",
            ".github/instructions/b.instructions.md",
        ]
        bundle = _build_bundle_dir(tmp_path, deployed)
        output = tmp_path / "target"
        output.mkdir()

        result = unpack_bundle(bundle, output)

        assert set(result.files) == set(deployed)
        assert all((output / f).exists() for f in deployed)


# ---------------------------------------------------------------------------
# ZIP extraction security tests (mirrors the tar.gz equivalents in
# tests/integration/test_wave4_pure_logic_coverage.py)
# ---------------------------------------------------------------------------


class TestUnpackZipSecurity:
    """Verify the ZIP extraction path in unpack_bundle rejects malicious entries."""

    def _make_zip_with_member(self, path, arcname: str, content: bytes = b"evil") -> None:
        """Write a zip to *path* containing one entry named *arcname*."""
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr(arcname, content)

    def test_zip_path_traversal_rejected(self, tmp_path):
        """Zip entries with ``..`` path segments are rejected."""
        zip_path = tmp_path / "evil.zip"
        self._make_zip_with_member(zip_path, "../escaped.txt")
        output = tmp_path / "output"
        output.mkdir()
        with pytest.raises((ValueError, FileNotFoundError)):
            unpack_bundle(zip_path, output_dir=output)

    def test_zip_absolute_path_rejected(self, tmp_path):
        """Zip entries with absolute paths are rejected."""
        zip_path = tmp_path / "evil.zip"
        self._make_zip_with_member(zip_path, "/etc/passwd")
        output = tmp_path / "output"
        output.mkdir()
        with pytest.raises((ValueError, FileNotFoundError)):
            unpack_bundle(zip_path, output_dir=output)

    def test_zip_symlink_entry_rejected(self, tmp_path):
        """Zip entries with Unix symlink bit set in external_attr are rejected."""
        zip_path = tmp_path / "symlink.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            info = zipfile.ZipInfo("link.txt")
            # Set external_attr to Unix symlink mode (S_IFLNK = 0o120000)
            info.external_attr = (0o120000 | 0o777) << 16
            zf.writestr(info, "/etc/passwd")
        output = tmp_path / "output"
        output.mkdir()
        with pytest.raises(ValueError, match="symlink"):
            unpack_bundle(zip_path, output_dir=output)

    def test_zip_bomb_too_many_entries_rejected(self, tmp_path, monkeypatch):
        """ZIP with more entries than _MAX_ZIP_ENTRIES is rejected before extraction."""
        from apm_cli.bundle import unpacker

        monkeypatch.setattr(unpacker, "_MAX_ZIP_ENTRIES", 3)
        zip_path = tmp_path / "bomb_entries.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            for i in range(4):
                zf.writestr(f"file_{i}.txt", "x")
        output = tmp_path / "output"
        output.mkdir()
        with pytest.raises(ValueError, match="entries"):
            unpack_bundle(zip_path, output_dir=output)

    def test_zip_bomb_uncompressed_size_rejected(self, tmp_path, monkeypatch):
        """ZIP whose total uncompressed size exceeds _MAX_ZIP_UNCOMPRESSED is rejected."""
        from apm_cli.bundle import unpacker

        monkeypatch.setattr(unpacker, "_MAX_ZIP_UNCOMPRESSED", 50)
        zip_path = tmp_path / "bomb_size.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("big.txt", "x" * 51)
        output = tmp_path / "output"
        output.mkdir()
        with pytest.raises(ValueError, match="uncompressed size"):
            unpack_bundle(zip_path, output_dir=output)
