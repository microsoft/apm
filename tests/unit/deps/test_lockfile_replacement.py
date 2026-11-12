"""Unit tests for lockfile replacement serialization.

Tests that replacement rules round-trip through lock-file YAML and that
the ``replacements:`` section is properly written and read.
"""

from urllib.parse import urlparse

import pytest
import yaml

from apm_cli.models.dependency.replacement import (
    GitReplacementSource,
    LocalReplacementSource,
    ReplacementConfigError,
    ReplacementRule,
    ReplacementSelector,
    build_replacement_index,
    parse_replace_block,
    replacement_rule_to_dict,
    replacement_rules_from_dicts,
)


class TestLockfileReplacementSerialization:
    """Lock-file ``replacements:`` section serialization."""

    def _local_rule(self, repo="owner/repo", subpath=None, local_path="./local"):
        sel = ReplacementSelector(host="github.com", port=None, repo_url=repo, subpath=subpath)
        src = LocalReplacementSource(path=local_path)
        return ReplacementRule(selector=sel, source=src)

    def _git_rule(self, repo="owner/repo", subpath=None, fork_repo="fork/repo", ref="main"):
        sel = ReplacementSelector(host="github.com", port=None, repo_url=repo, subpath=subpath)
        src = GitReplacementSource(
            git=f"https://github.com/{fork_repo}.git",
            host="github.com",
            port=None,
            repo_url=fork_repo,
            subpath=subpath,
            ref=ref,
        )
        return ReplacementRule(selector=sel, source=src)

    def test_local_to_dict(self):
        rule = self._local_rule(local_path="../checkout")
        d = replacement_rule_to_dict(rule)
        assert urlparse(d["for"]["git"]) == urlparse("https://github.com/owner/repo.git")
        assert d["with"]["path"] == "../checkout"
        assert "git" not in d["with"]

    def test_local_with_subpath_to_dict(self):
        rule = self._local_rule(subpath="packages/base", local_path="./base")
        d = replacement_rule_to_dict(rule)
        assert d["for"]["path"] == "packages/base"
        assert d["with"]["path"] == "./base"

    def test_git_to_dict(self):
        rule = self._git_rule(fork_repo="fork-org/repo", ref="fix-branch")
        d = replacement_rule_to_dict(rule)
        assert "git" in d["for"]
        assert urlparse(d["with"]["git"]) == urlparse("https://github.com/fork-org/repo.git")
        assert d["with"]["ref"] == "fix-branch"

    def test_round_trip_yaml(self):
        """Rules survive YAML serialization and deserialization."""
        rules = [
            self._local_rule(repo="a/b", local_path="../b-local"),
            self._git_rule(repo="c/d", fork_repo="e/f", ref="hotfix"),
        ]
        dicts = [replacement_rule_to_dict(r) for r in rules]
        yaml_str = yaml.dump(dicts, default_flow_style=False)
        loaded = yaml.safe_load(yaml_str)
        restored = replacement_rules_from_dicts(loaded)
        assert len(restored) == 2
        assert restored[0].selector.repo_url == "a/b"
        assert isinstance(restored[0].source, LocalReplacementSource)
        assert restored[1].selector.repo_url == "c/d"
        assert isinstance(restored[1].source, GitReplacementSource)
        assert restored[1].source.ref == "hotfix"

    def test_dormant_rules_serialize(self):
        """Dormant (unused) rules are serialized for frozen comparison.

        Even if a rule was not matched during install, it must be recorded
        in the lock so frozen-install can detect rule additions/removals.
        """
        rules = [
            self._local_rule(repo="used/dep", local_path="./used"),
            self._local_rule(repo="unused/dep", local_path="./unused"),
        ]
        dicts = [replacement_rule_to_dict(r) for r in rules]
        assert len(dicts) == 2
        repos = {urlparse(d["for"]["git"]) for d in dicts}
        assert repos == {
            urlparse("https://github.com/used/dep.git"),
            urlparse("https://github.com/unused/dep.git"),
        }

    def test_empty_rules_list(self):
        """Empty rules list produces no serialized entries."""
        dicts = [replacement_rule_to_dict(r) for r in []]
        assert dicts == []
        restored = replacement_rules_from_dicts([])
        assert restored == []

    def test_deserialization_rejects_malformed(self):
        """Malformed lock-file entries are rejected with the same errors."""
        with pytest.raises(ReplacementConfigError):
            replacement_rules_from_dicts([{"for": {"git": "not-a-url"}, "with": {"path": "./x"}}])

    def test_custom_port_round_trips(self):
        """Custom port survives serialization."""
        sel = ReplacementSelector(
            host="ghe.corp.com", port=8443, repo_url="owner/repo", subpath=None
        )
        src = LocalReplacementSource(path="./local")
        rule = ReplacementRule(selector=sel, source=src)
        d = replacement_rule_to_dict(rule)
        assert urlparse(d["for"]["git"]).port == 8443
        restored = replacement_rules_from_dicts([d])
        assert restored[0].selector.port == 8443

    def test_non_github_host_round_trips(self):
        """Non-GitHub host round-trips correctly."""
        sel = ReplacementSelector(
            host="gitlab.com", port=None, repo_url="team/project", subpath=None
        )
        src = LocalReplacementSource(path="./local-project")
        rule = ReplacementRule(selector=sel, source=src)
        d = replacement_rule_to_dict(rule)
        parsed = urlparse(d["for"]["git"])
        assert parsed.hostname == "gitlab.com"
        restored = replacement_rules_from_dicts([d])
        assert restored[0].selector.host == "gitlab.com"


class TestReplacementFrozenComparison:
    """Frozen install compares full normalized rule tables."""

    def test_identical_tables_match(self):
        """Two indexes built from identical rules have identical keys."""
        raw = [
            {
                "for": {"git": "https://github.com/a/b.git"},
                "with": {"path": "./local-b"},
            },
            {
                "for": {"git": "https://github.com/c/d.git", "path": "pkg"},
                "with": {"git": "https://github.com/e/f.git", "ref": "main"},
            },
        ]
        rules1 = parse_replace_block(raw)
        rules2 = parse_replace_block(raw)
        idx1 = build_replacement_index(rules1)
        idx2 = build_replacement_index(rules2)
        # Compare rule serialization for frozen checks
        dicts1 = [replacement_rule_to_dict(r) for r in idx1.rules]
        dicts2 = [replacement_rule_to_dict(r) for r in idx2.rules]
        assert dicts1 == dicts2

    def test_added_rule_detected(self):
        """A rule added to the manifest produces a different serialization."""
        base = [
            {
                "for": {"git": "https://github.com/a/b.git"},
                "with": {"path": "./local-b"},
            }
        ]
        extended = [
            *base,
            {
                "for": {"git": "https://github.com/c/d.git"},
                "with": {"path": "./local-d"},
            },
        ]
        rules_base = parse_replace_block(base)
        rules_ext = parse_replace_block(extended)
        dicts_base = [replacement_rule_to_dict(r) for r in rules_base]
        dicts_ext = [replacement_rule_to_dict(r) for r in rules_ext]
        assert dicts_base != dicts_ext

    def test_removed_rule_detected(self):
        """A rule removed from the manifest produces a different serialization."""
        full = [
            {
                "for": {"git": "https://github.com/a/b.git"},
                "with": {"path": "./local-b"},
            },
            {
                "for": {"git": "https://github.com/c/d.git"},
                "with": {"path": "./local-d"},
            },
        ]
        partial = full[:1]
        rules_full = parse_replace_block(full)
        rules_partial = parse_replace_block(partial)
        dicts_full = [replacement_rule_to_dict(r) for r in rules_full]
        dicts_partial = [replacement_rule_to_dict(r) for r in rules_partial]
        assert dicts_full != dicts_partial

    def test_changed_source_detected(self):
        """Changed source path produces a different serialization."""
        raw1 = [
            {
                "for": {"git": "https://github.com/a/b.git"},
                "with": {"path": "./local-old"},
            }
        ]
        raw2 = [
            {
                "for": {"git": "https://github.com/a/b.git"},
                "with": {"path": "./local-new"},
            }
        ]
        rules1 = parse_replace_block(raw1)
        rules2 = parse_replace_block(raw2)
        dicts1 = [replacement_rule_to_dict(r) for r in rules1]
        dicts2 = [replacement_rule_to_dict(r) for r in rules2]
        assert dicts1 != dicts2


# ---------------------------------------------------------------------------
# LockFile v3 version and replacements integration
# ---------------------------------------------------------------------------


class TestLockFileReplacementsIntegration:
    """LockFile ``replacements:`` field and version-3 semantics."""

    def test_lockfile_v1_without_replacements(self):
        """Lockfile without replacements stays at v1."""
        from apm_cli.deps.lockfile import LockFile

        lock = LockFile()
        assert lock.replacements is None
        yaml_str = lock.to_yaml()
        assert "lockfile_version: '1'" in yaml_str or 'lockfile_version: "1"' in yaml_str

    def test_lockfile_v3_with_replacements(self):
        """Lockfile with replacements emits version 3."""
        from apm_cli.deps.lockfile import LockFile

        lock = LockFile()
        lock.replacements = [
            {
                "for": {"git": "https://github.com/a/b.git"},
                "with": {"path": "./local"},
            }
        ]
        yaml_str = lock.to_yaml()
        assert "lockfile_version: '3'" in yaml_str or 'lockfile_version: "3"' in yaml_str

    def test_lockfile_v3_with_empty_replacements(self):
        """Explicitly empty replacements list still bumps to v3.

        An empty list means 'replacements existed, now cleared'; old
        readers must still reject this lockfile.
        """
        from apm_cli.deps.lockfile import LockFile

        lock = LockFile()
        lock.replacements = []
        yaml_str = lock.to_yaml()
        assert "lockfile_version: '3'" in yaml_str or 'lockfile_version: "3"' in yaml_str

    def test_lockfile_replacements_round_trip(self):
        """Replacements survive to_yaml -> from_yaml."""
        from apm_cli.deps.lockfile import LockFile

        lock = LockFile()
        lock.replacements = [
            {
                "for": {"git": "https://github.com/org/lib.git"},
                "with": {"path": "../lib-fork"},
            },
            {
                "for": {"git": "https://github.com/org/util.git", "path": "pkg"},
                "with": {
                    "git": "https://github.com/fork/util.git",
                    "ref": "hotfix-branch",
                },
            },
        ]
        yaml_str = lock.to_yaml()
        restored = LockFile.from_yaml(yaml_str)
        assert restored.replacements == lock.replacements
        assert restored.lockfile_version == "3"

    def test_lockfile_no_replacements_key_in_yaml(self):
        """When replacements is None, no ``replacements:`` key in YAML."""
        from apm_cli.deps.lockfile import LockFile

        lock = LockFile()
        assert lock.replacements is None
        yaml_str = lock.to_yaml()
        assert "replacements:" not in yaml_str

    def test_lockfile_replacements_empty_list_in_yaml(self):
        """Explicitly empty list emits ``replacements: []`` in YAML."""
        from apm_cli.deps.lockfile import LockFile

        lock = LockFile()
        lock.replacements = []
        yaml_str = lock.to_yaml()
        assert "replacements:" in yaml_str
        restored = LockFile.from_yaml(yaml_str)
        assert restored.replacements == []

    def test_lockfile_v3_read_supported(self):
        """Version 3 lockfile is accepted by from_yaml."""
        from apm_cli.deps.lockfile import LockFile

        yaml_str = (
            "lockfile_version: '3'\n"
            "dependencies: []\n"
            "replacements:\n"
            "- for:\n"
            "    git: https://github.com/a/b.git\n"
            "  with:\n"
            "    path: ./local\n"
        )
        lock = LockFile.from_yaml(yaml_str)
        assert lock.lockfile_version == "3"
        assert len(lock.replacements) == 1

    def test_lockfile_v3_without_replacements_section(self):
        """Version 3 lockfile without replacements section reads as None."""
        from apm_cli.deps.lockfile import LockFile

        yaml_str = "lockfile_version: '3'\ndependencies: []\n"
        lock = LockFile.from_yaml(yaml_str)
        assert lock.lockfile_version == "3"
        assert lock.replacements is None

    def test_lockfile_replacements_malformed_rejects(self):
        """Non-list replacements value is rejected."""
        from apm_cli.deps.lockfile import LockFile, LockfileFormatError

        yaml_str = "lockfile_version: '3'\ndependencies: []\nreplacements: not-a-list\n"
        with pytest.raises(LockfileFormatError, match="must be a list"):
            LockFile.from_yaml(yaml_str)
