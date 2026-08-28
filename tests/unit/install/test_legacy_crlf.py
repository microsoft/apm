"""Tests for the apm#2619 legacy CRLF hash-domain migration helpers."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from apm_cli.install.legacy_crlf import (
    _crlf_expand,
    apm_authored_files,
    converge_apm_authored_files,
    legacy_crlf_hash,
)
from apm_cli.models.validation import PackageType
from apm_cli.utils.content_hash import (
    compute_package_hash,
    compute_package_hash_with_overrides,
)

# Load-bearing cross-platform regression contract (see
# .github/instructions/tests.instructions.md): the migration must behave
# identically on every OS.
pytestmark = pytest.mark.windows_compat


def _marketplace_tree(root: Path, *, inline_hooks: bool = True, crlf: bool = False) -> Path:
    """Build a minimal marketplace-plugin install tree by hand.

    ``crlf=True`` writes the APM-authored files the way a pre-fix Windows
    APM did (CRLF); otherwise LF (post-fix / POSIX domain).
    """
    nl = b"\r\n" if crlf else b"\n"
    root.mkdir(parents=True, exist_ok=True)
    hooks_field = (
        b'"hooks": {"PreToolUse": []}, ' if inline_hooks else b'"hooks": "my-hooks.json", '
    )
    (root / "plugin.json").write_bytes(
        b'{"name": "demo-plugin", ' + hooks_field + b'"description": "Demo"}\n'
    )
    (root / "apm.yml").write_bytes(nl.join([b"name: demo-plugin", b"version: 2c7ec5e", b""]))
    hooks_dir = root / ".apm" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    (hooks_dir / "hooks.json").write_bytes(nl.join([b"{", b'  "PreToolUse": []', b"}"]))
    # Upstream content: byte-copied from git, must NEVER be rewritten.
    skill = root / "skills" / "demo"
    skill.mkdir(parents=True, exist_ok=True)
    (skill / "SKILL.md").write_bytes(b"---\r\nname: demo\r\n---\r\n# upstream CRLF stays\r\n")
    return root


class TestApmAuthoredFiles:
    def test_marketplace_plugin_with_inline_hooks(self, tmp_path: Path) -> None:
        tree = _marketplace_tree(tmp_path / "pkg", inline_hooks=True)
        files = apm_authored_files(tree, PackageType.MARKETPLACE_PLUGIN, None)
        assert files == ["apm.yml", ".apm/hooks/hooks.json"]

    def test_marketplace_plugin_with_file_path_hooks_excludes_hooks_json(
        self, tmp_path: Path
    ) -> None:
        """hooks.json from a declared config FILE is a byte-copy of upstream
        content -- normalizing it would CREATE divergence vs a fresh install."""
        tree = _marketplace_tree(tmp_path / "pkg", inline_hooks=False)
        files = apm_authored_files(tree, PackageType.MARKETPLACE_PLUGIN, None)
        assert files == ["apm.yml"]

    def test_string_package_type_value_accepted(self, tmp_path: Path) -> None:
        tree = _marketplace_tree(tmp_path / "pkg", inline_hooks=False)
        assert apm_authored_files(tree, "marketplace_plugin", None) == ["apm.yml"]

    def test_virtual_file_dep_owns_its_manifest(self, tmp_path: Path) -> None:
        dep = SimpleNamespace(is_virtual=True, is_virtual_file=lambda: True)
        assert apm_authored_files(tmp_path, None, dep) == ["apm.yml"]

    def test_plain_apm_package_owns_nothing(self, tmp_path: Path) -> None:
        """A regular APM package's apm.yml is upstream content -- untouchable."""
        assert apm_authored_files(tmp_path, PackageType.APM_PACKAGE, None) == []

    def test_none_package_type_is_detected_from_tree(self, tmp_path: Path) -> None:
        """Several install paths carry PackageInfo without package_type
        (e.g. pre-download results); the type must then be detected from
        the on-disk plugin evidence so the migration still engages."""
        tree = _marketplace_tree(tmp_path / "pkg", inline_hooks=True)
        assert apm_authored_files(tree, None, None) == ["apm.yml", ".apm/hooks/hooks.json"]

    def test_none_package_type_without_plugin_evidence_owns_nothing(self, tmp_path: Path) -> None:
        """No plugin evidence on disk -> detection cannot claim an upstream
        apm.yml as APM-authored."""
        pkg = tmp_path / "plain"
        (pkg / ".apm" / "instructions").mkdir(parents=True)
        (pkg / "apm.yml").write_bytes(b"name: upstream\nversion: 1.0.0\n")
        (pkg / ".apm" / "instructions" / "a.instructions.md").write_bytes(b"# a\n")
        assert apm_authored_files(pkg, None, None) == []


class TestCrlfExpand:
    def test_expands_lf_and_is_idempotent(self) -> None:
        assert _crlf_expand(b"a\nb\n") == b"a\r\nb\r\n"
        assert _crlf_expand(b"a\r\nb\r\n") == b"a\r\nb\r\n"

    def test_mixed_domains_converge(self) -> None:
        assert _crlf_expand(b"a\r\nb\n") == b"a\r\nb\r\n"


class TestLegacyCrlfHash:
    def test_matches_hash_of_hand_built_crlf_tree(self, tmp_path: Path) -> None:
        """The computed CRLF-domain hash equals the hash of an actual tree
        whose APM-authored files were written CRLF -- the exact bytes a
        pre-fix Windows APM produced."""
        lf_tree = _marketplace_tree(tmp_path / "lf", crlf=False)
        crlf_tree = _marketplace_tree(tmp_path / "crlf", crlf=True)

        computed = legacy_crlf_hash(lf_tree, PackageType.MARKETPLACE_PLUGIN, None)

        assert computed == compute_package_hash(crlf_tree)
        assert computed != compute_package_hash(lf_tree)

    def test_returns_none_when_nothing_would_change(self, tmp_path: Path) -> None:
        """A tree whose authored files have no line endings at all offers no
        legacy domain to compare against."""
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (pkg / "apm.yml").write_bytes(b"name: x")  # no newline anywhere
        assert legacy_crlf_hash(pkg, PackageType.MARKETPLACE_PLUGIN, None) is None

    def test_returns_none_for_unowned_package_types(self, tmp_path: Path) -> None:
        tree = _marketplace_tree(tmp_path / "pkg")
        assert legacy_crlf_hash(tree, PackageType.APM_PACKAGE, None) is None


class TestConvergeApmAuthoredFiles:
    def test_converges_crlf_tree_to_lf_domain(self, tmp_path: Path) -> None:
        crlf_tree = _marketplace_tree(tmp_path / "crlf", crlf=True)
        lf_tree = _marketplace_tree(tmp_path / "lf", crlf=False)

        changed = converge_apm_authored_files(crlf_tree, PackageType.MARKETPLACE_PLUGIN, None)

        assert changed == ["apm.yml", ".apm/hooks/hooks.json"]
        assert b"\r" not in (crlf_tree / "apm.yml").read_bytes()
        assert b"\r" not in (crlf_tree / ".apm" / "hooks" / "hooks.json").read_bytes()
        # Upstream content is untouched -- its CRLF bytes stay hash-visible.
        assert b"\r\n" in (crlf_tree / "skills" / "demo" / "SKILL.md").read_bytes()
        # The converged tree now hashes identically to a post-fix tree.
        assert compute_package_hash(crlf_tree) == compute_package_hash(lf_tree)

    def test_noop_on_already_lf_tree(self, tmp_path: Path) -> None:
        tree = _marketplace_tree(tmp_path / "pkg", crlf=False)
        before = compute_package_hash(tree)
        assert converge_apm_authored_files(tree, PackageType.MARKETPLACE_PLUGIN, None) == []
        assert compute_package_hash(tree) == before

    def test_noop_for_unowned_package_types(self, tmp_path: Path) -> None:
        tree = _marketplace_tree(tmp_path / "pkg", crlf=True)
        before = compute_package_hash(tree)
        assert converge_apm_authored_files(tree, PackageType.APM_PACKAGE, None) == []
        assert compute_package_hash(tree) == before

    def test_missing_files_are_skipped(self, tmp_path: Path) -> None:
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        assert converge_apm_authored_files(pkg, PackageType.MARKETPLACE_PLUGIN, None) == []


class TestSymlinkedParentGuard:
    """apm#2619 round-3: authored paths behind symlinked directories are
    invisible to compute_package_hash (rglob does not descend symlinked
    dirs), so the migration must never read or rewrite through them --
    for a repo-shipped symlinked .apm the write would land OUTSIDE the
    package tree."""

    def _tree_with_symlinked_apm_dir(self, tmp_path: Path) -> tuple[Path, Path]:
        outside = tmp_path / "outside"
        (outside / "hooks").mkdir(parents=True)
        (outside / "hooks" / "hooks.json").write_bytes(b"{\r\n}\r\n")

        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (pkg / "plugin.json").write_bytes(
            b'{"name": "demo-plugin", "hooks": {"PreToolUse": []}, "description": "Demo"}\n'
        )
        (pkg / "apm.yml").write_bytes(b"name: demo-plugin\r\nversion: 2c7ec5e\r\n")
        try:
            (pkg / ".apm").symlink_to(outside, target_is_directory=True)
        except OSError:
            pytest.skip("Symlinks not supported on this platform")
        return pkg, outside

    def test_converge_never_writes_through_symlinked_parent(self, tmp_path: Path) -> None:
        pkg, outside = self._tree_with_symlinked_apm_dir(tmp_path)
        before = (outside / "hooks" / "hooks.json").read_bytes()

        changed = converge_apm_authored_files(pkg, PackageType.MARKETPLACE_PLUGIN, None)

        # apm.yml (real file at the root) converges; the symlinked
        # hooks.json is skipped and the out-of-tree file is untouched.
        assert changed == ["apm.yml"]
        assert (outside / "hooks" / "hooks.json").read_bytes() == before

    def test_legacy_hash_ignores_symlinked_parent(self, tmp_path: Path) -> None:
        pkg, outside = self._tree_with_symlinked_apm_dir(tmp_path)
        # Fresh-download scenario: the post-fix tree is LF. The symlinked
        # hooks.json is LF too, so an implementation without the parent
        # guard would read through the symlink and record an override for
        # it instead of skipping the path outright.
        (pkg / "apm.yml").write_bytes(b"name: demo-plugin\nversion: 2c7ec5e\n")
        (outside / "hooks" / "hooks.json").write_bytes(b"{\n}\n")

        computed = legacy_crlf_hash(pkg, PackageType.MARKETPLACE_PLUGIN, None)

        # apm.yml (real file at the root) contributes the only override;
        # the symlinked hooks.json must be skipped, not read through.
        assert computed is not None
        assert computed == compute_package_hash_with_overrides(
            pkg, {"apm.yml": b"name: demo-plugin\r\nversion: 2c7ec5e\r\n"}
        )
