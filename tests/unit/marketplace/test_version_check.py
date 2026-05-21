"""Tests for the ``apm pack --check-versions`` release gate (Wave 4)."""

from __future__ import annotations

from pathlib import Path

import pytest

from apm_cli.marketplace.version_check import (
    VersionAlignmentReport,
    check_version_alignment,
)
from apm_cli.marketplace.yml_schema import load_marketplace_from_apm_yml


def _build_apm_yml(
    *,
    project_version: str = "1.0.0",
    market_version: str = "1.0.0",
    strategy: str | None = None,
    build_tag_pattern: str | None = None,
    packages: list[dict] | None = None,
) -> str:
    """Build a syntactically valid apm.yml string."""
    lines: list[str] = [
        "name: monorepo",
        f'version: "{project_version}"',
        "marketplace:",
        "  name: acme-tools",
        "  description: Acme tools",
        f'  version: "{market_version}"',
        "  owner:",
        "    name: Acme",
    ]
    if strategy is not None:
        lines.append("  versioning:")
        lines.append(f"    strategy: {strategy}")
    if build_tag_pattern is not None:
        lines.append("  build:")
        lines.append(f'    tagPattern: "{build_tag_pattern}"')
    lines.append("  outputs:")
    lines.append("    claude: {}")
    lines.append("  packages:")
    for pkg in packages or []:
        lines.append(f"    - name: {pkg['name']}")
        lines.append(f"      source: {pkg['source']}")
        if pkg.get("version"):
            lines.append(f'      version: "{pkg["version"]}"')
        if pkg.get("tag_pattern"):
            lines.append(f'      tag_pattern: "{pkg["tag_pattern"]}"')
    return "\n".join(lines) + "\n"


def _write_apm_yml(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "apm.yml"
    p.write_text(content, encoding="utf-8")
    return p


def _write_pkg(root: Path, rel: str, version: str | None = "1.0.0") -> None:
    """Write a local package's apm.yml at ``<root>/<rel>/apm.yml``."""
    pkg_dir = root / rel
    pkg_dir.mkdir(parents=True, exist_ok=True)
    if version is None:
        body = 'name: pkg\ndescription: "no version"\n'
    else:
        body = f'name: pkg\ndescription: "x"\nversion: "{version}"\n'
    (pkg_dir / "apm.yml").write_text(body, encoding="utf-8")


def _load(tmp_path: Path) -> VersionAlignmentReport:
    config = load_marketplace_from_apm_yml(tmp_path / "apm.yml")
    return check_version_alignment(config, tmp_path)


# ---------------------------------------------------------------------------
# Lockstep strategy
# ---------------------------------------------------------------------------


class TestLockstepStrategy:
    """Default strategy: every local package version == marketplace.version."""

    def test_all_aligned_passes(self, tmp_path: Path):
        _write_pkg(tmp_path, "plugins/a", "1.0.0")
        _write_pkg(tmp_path, "plugins/b", "1.0.0")
        _write_apm_yml(
            tmp_path,
            _build_apm_yml(
                packages=[
                    {"name": "a", "source": "./plugins/a"},
                    {"name": "b", "source": "./plugins/b"},
                ],
            ),
        )
        report = _load(tmp_path)
        assert report.ok
        assert report.strategy == "lockstep"
        assert report.expected == "1.0.0"
        assert all(r.reason == "matches" for r in report.packages)
        assert [r.path for r in report.packages] == ["plugins/a", "plugins/b"]

    def test_one_misaligned_fails(self, tmp_path: Path):
        _write_pkg(tmp_path, "plugins/a", "1.0.0")
        _write_pkg(tmp_path, "plugins/b", "0.9.0")
        _write_apm_yml(
            tmp_path,
            _build_apm_yml(
                packages=[
                    {"name": "a", "source": "./plugins/a"},
                    {"name": "b", "source": "./plugins/b"},
                ],
            ),
        )
        report = _load(tmp_path)
        assert not report.ok
        bad = [r for r in report.packages if not r.ok]
        assert len(bad) == 1
        assert bad[0].path == "plugins/b"
        assert bad[0].reason == "drift:expected=1.0.0"

    def test_missing_version_field_fails(self, tmp_path: Path):
        _write_pkg(tmp_path, "plugins/a", None)
        _write_apm_yml(
            tmp_path,
            _build_apm_yml(packages=[{"name": "a", "source": "./plugins/a"}]),
        )
        report = _load(tmp_path)
        assert not report.ok
        assert report.packages[0].reason == "missing_version"

    def test_no_apm_yml_fails(self, tmp_path: Path):
        _write_apm_yml(
            tmp_path,
            _build_apm_yml(packages=[{"name": "a", "source": "./plugins/missing"}]),
        )
        report = _load(tmp_path)
        assert not report.ok
        assert report.packages[0].reason == "no_apm_yml"

    def test_remote_packages_skipped(self, tmp_path: Path):
        _write_pkg(tmp_path, "plugins/a", "1.0.0")
        _write_apm_yml(
            tmp_path,
            _build_apm_yml(
                packages=[
                    {"name": "a", "source": "./plugins/a"},
                    {
                        "name": "remote",
                        "source": "github/some-repo",
                        "version": ">=1.0.0",
                    },
                ],
            ),
        )
        report = _load(tmp_path)
        assert report.ok
        assert [r.path for r in report.packages] == ["plugins/a"]


# ---------------------------------------------------------------------------
# Tag pattern strategy
# ---------------------------------------------------------------------------


class TestTagPatternStrategy:
    """Strategy where each package version is required + tag uniqueness checked."""

    def test_unique_tags_pass(self, tmp_path: Path):
        _write_pkg(tmp_path, "plugins/a", "1.0.0")
        _write_pkg(tmp_path, "plugins/b", "2.0.0")
        _write_apm_yml(
            tmp_path,
            _build_apm_yml(
                market_version="9.9.9",
                project_version="9.9.9",
                strategy="tag_pattern",
                packages=[
                    {
                        "name": "a",
                        "source": "./plugins/a",
                        "tag_pattern": "{name}-v{version}",
                    },
                    {
                        "name": "b",
                        "source": "./plugins/b",
                        "tag_pattern": "{name}-v{version}",
                    },
                ],
            ),
        )
        report = _load(tmp_path)
        assert report.ok
        for row in report.packages:
            assert row.rendered_tag is not None
            assert row.reason == "matches"

    def test_duplicate_tag_fails(self, tmp_path: Path):
        _write_pkg(tmp_path, "plugins/a", "1.0.0")
        _write_pkg(tmp_path, "plugins/b", "1.0.0")
        _write_apm_yml(
            tmp_path,
            _build_apm_yml(
                market_version="9.9.9",
                project_version="9.9.9",
                strategy="tag_pattern",
                packages=[
                    {
                        "name": "a",
                        "source": "./plugins/a",
                        "tag_pattern": "v{version}",
                    },
                    {
                        "name": "b",
                        "source": "./plugins/b",
                        "tag_pattern": "v{version}",
                    },
                ],
            ),
        )
        report = _load(tmp_path)
        assert not report.ok
        bad = [r for r in report.packages if not r.ok]
        assert len(bad) == 2
        assert all(r.reason.startswith("duplicate_tag:other=") for r in bad)

    def test_three_way_collision_blames_nearest_sibling(self, tmp_path: Path):
        """With 3+ colliding packages, each blames its nearest previously-seen sibling."""
        _write_pkg(tmp_path, "plugins/a", "1.0.0")
        _write_pkg(tmp_path, "plugins/b", "1.0.0")
        _write_pkg(tmp_path, "plugins/c", "1.0.0")
        _write_apm_yml(
            tmp_path,
            _build_apm_yml(
                market_version="9.9.9",
                project_version="9.9.9",
                strategy="tag_pattern",
                packages=[
                    {"name": "a", "source": "./plugins/a", "tag_pattern": "v{version}"},
                    {"name": "b", "source": "./plugins/b", "tag_pattern": "v{version}"},
                    {"name": "c", "source": "./plugins/c", "tag_pattern": "v{version}"},
                ],
            ),
        )
        report = _load(tmp_path)
        assert not report.ok
        bad = {r.path: r for r in report.packages if not r.ok}
        assert len(bad) == 3
        # 'c' is the most recent; it blames its nearest previous collider 'b'
        # (not the original 'a'). 'a' and 'b' still blame each other.
        assert bad["plugins/c"].reason == "duplicate_tag:other=plugins/b"
        assert bad["plugins/b"].reason == "duplicate_tag:other=plugins/a"

    def test_missing_version_fails(self, tmp_path: Path):
        _write_pkg(tmp_path, "plugins/a", None)
        _write_apm_yml(
            tmp_path,
            _build_apm_yml(
                market_version="9.9.9",
                project_version="9.9.9",
                strategy="tag_pattern",
                packages=[
                    {
                        "name": "a",
                        "source": "./plugins/a",
                        "tag_pattern": "v{version}",
                    }
                ],
            ),
        )
        report = _load(tmp_path)
        assert not report.ok
        assert report.packages[0].reason == "missing_version"

    def test_invalid_yaml_distinct_from_missing_version(self, tmp_path: Path):
        """A malformed apm.yml surfaces as 'invalid_yaml', not 'missing_version'."""
        pkg_dir = tmp_path / "plugins" / "a"
        pkg_dir.mkdir(parents=True)
        # Unbalanced bracket -> yaml.YAMLError at safe_load time.
        (pkg_dir / "apm.yml").write_text("name: pkg\nversion: [1.0.0\n", encoding="utf-8")
        _write_apm_yml(
            tmp_path,
            _build_apm_yml(
                market_version="9.9.9",
                project_version="9.9.9",
                strategy="tag_pattern",
                packages=[{"name": "a", "source": "./plugins/a", "tag_pattern": "v{version}"}],
            ),
        )
        report = _load(tmp_path)
        assert not report.ok
        assert report.packages[0].reason == "invalid_yaml"
        # The human-readable error must point at the parse failure, not a missing key.
        msgs = report.error_messages()
        assert len(msgs) == 1
        assert "malformed YAML" in msgs[0]
        assert "missing 'version'" not in msgs[0]

    def test_default_pattern_inherited(self, tmp_path: Path):
        _write_pkg(tmp_path, "plugins/a", "1.0.0")
        _write_apm_yml(
            tmp_path,
            _build_apm_yml(
                market_version="9.9.9",
                project_version="9.9.9",
                strategy="tag_pattern",
                build_tag_pattern="v{version}-{name}",
                packages=[{"name": "a", "source": "./plugins/a"}],
            ),
        )
        report = _load(tmp_path)
        assert report.ok
        assert report.packages[0].rendered_tag == "v1.0.0-a"


# ---------------------------------------------------------------------------
# Per-package strategy
# ---------------------------------------------------------------------------


class TestPerPackageStrategy:
    """Most permissive: only requires that each local package declares a version."""

    def test_divergent_versions_allowed(self, tmp_path: Path):
        _write_pkg(tmp_path, "plugins/a", "1.0.0")
        _write_pkg(tmp_path, "plugins/b", "7.3.2")
        _write_apm_yml(
            tmp_path,
            _build_apm_yml(
                strategy="per_package",
                packages=[
                    {"name": "a", "source": "./plugins/a"},
                    {"name": "b", "source": "./plugins/b"},
                ],
            ),
        )
        report = _load(tmp_path)
        assert report.ok
        assert report.expected is None
        assert all(r.reason == "matches" for r in report.packages)

    def test_missing_version_still_fails(self, tmp_path: Path):
        _write_pkg(tmp_path, "plugins/a", None)
        _write_apm_yml(
            tmp_path,
            _build_apm_yml(
                strategy="per_package",
                packages=[{"name": "a", "source": "./plugins/a"}],
            ),
        )
        report = _load(tmp_path)
        assert not report.ok
        assert report.packages[0].reason == "missing_version"

    def test_error_messages_helper(self, tmp_path: Path):
        _write_pkg(tmp_path, "plugins/a", None)
        _write_apm_yml(
            tmp_path,
            _build_apm_yml(
                strategy="per_package",
                packages=[{"name": "a", "source": "./plugins/a"}],
            ),
        )
        report = _load(tmp_path)
        msgs = report.error_messages()
        assert len(msgs) == 1
        assert "plugins/a" in msgs[0]
        assert "missing" in msgs[0]


# ---------------------------------------------------------------------------
# JSON serialization
# ---------------------------------------------------------------------------


class TestJsonSerialization:
    def test_to_json_dict_shape(self, tmp_path: Path):
        _write_pkg(tmp_path, "plugins/a", "1.0.0")
        _write_apm_yml(
            tmp_path,
            _build_apm_yml(packages=[{"name": "a", "source": "./plugins/a"}]),
        )
        payload = _load(tmp_path).to_json_dict()
        assert set(payload.keys()) == {"strategy", "expected", "ok", "packages"}
        assert payload["strategy"] == "lockstep"
        assert payload["expected"] == "1.0.0"
        assert payload["ok"] is True
        assert payload["packages"][0]["path"] == "plugins/a"
        assert "ok" in payload["packages"][0]
        assert "reason" in payload["packages"][0]


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
