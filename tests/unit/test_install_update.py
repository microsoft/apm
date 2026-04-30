"""Tests for --update flag behavior in install command (Bug #190).

Verifies that `apm install --update` bypasses lockfile-pinned SHAs
and re-fetches the latest content, especially for subdirectory packages.

Also tests the drift detection helpers in ``apm_cli/drift.py``:
- ``detect_ref_change()`` covers all ref transition cases (None→value, etc.)
- ``detect_orphans()`` covers full vs partial install
- ``build_download_ref()`` validates locked-SHA vs manifest-ref selection
"""

from unittest.mock import Mock

from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.drift import build_download_ref, detect_config_drift, detect_orphans, detect_ref_change
from apm_cli.models.apm_package import DependencyReference


class TestSkipDownloadWithUpdateFlag:
    """Test that skip_download respects --update (update_refs=True).

    The skip_download condition must NOT skip when update_refs is True,
    even if the package was already resolved by the BFS callback.
    """

    def _build_skip_download(
        self, *, install_path_exists, is_cacheable, update_refs, already_resolved, lockfile_match
    ):
        """Reproduce the skip_download condition from cli.py.

        Note: ``already_resolved`` is intentionally NOT gated by ``update_refs``.
        When the BFS resolver callback downloads a package during this run it is
        always a fresh fetch (the callback itself skips lockfile overrides when
        ``update_refs=True``), so re-downloading would be redundant.
        """
        return install_path_exists and (
            (is_cacheable and not update_refs) or already_resolved or lockfile_match
        )

    def test_already_resolved_skips_without_update(self):
        """Without --update, already_resolved packages should be skipped."""
        assert (
            self._build_skip_download(
                install_path_exists=True,
                is_cacheable=False,
                update_refs=False,
                already_resolved=True,
                lockfile_match=False,
            )
            is True
        )

    def test_already_resolved_still_skips_with_update(self):
        """With --update, already_resolved packages are still skipped because
        the BFS callback already fetched them fresh in this run."""
        assert (
            self._build_skip_download(
                install_path_exists=True,
                is_cacheable=False,
                update_refs=True,
                already_resolved=True,
                lockfile_match=False,
            )
            is True
        )

    def test_cacheable_skips_without_update(self):
        """Without --update, cacheable (tag/commit) packages should be skipped."""
        assert (
            self._build_skip_download(
                install_path_exists=True,
                is_cacheable=True,
                update_refs=False,
                already_resolved=False,
                lockfile_match=False,
            )
            is True
        )

    def test_cacheable_does_not_skip_with_update(self):
        """With --update, cacheable packages must NOT be skipped."""
        assert (
            self._build_skip_download(
                install_path_exists=True,
                is_cacheable=True,
                update_refs=True,
                already_resolved=False,
                lockfile_match=False,
            )
            is False
        )

    def test_lockfile_match_always_skips(self):
        """lockfile_match should always skip (including under update_refs) because
        the lockfile_match check now handles both normal and update_refs modes."""
        assert (
            self._build_skip_download(
                install_path_exists=True,
                is_cacheable=False,
                update_refs=True,
                already_resolved=False,
                lockfile_match=True,
            )
            is True
        )

    def test_no_install_path_never_skips(self):
        """If install path doesn't exist, never skip regardless of other flags."""
        assert (
            self._build_skip_download(
                install_path_exists=False,
                is_cacheable=True,
                update_refs=False,
                already_resolved=True,
                lockfile_match=True,
            )
            is False
        )


class TestDetectRefChange:
    """Tests for detect_ref_change()."""

    def test_insecure_transport_flip_from_https_to_http_is_drift(self):
        """Switching to insecure HTTP must force a re-download."""
        dep = DependencyReference(
            repo_url="owner/repo",
            host="gitlab.example.com",
            is_insecure=True,
        )
        locked = LockedDependency(
            repo_url="owner/repo",
            host="gitlab.example.com",
            is_insecure=False,
        )

        assert detect_ref_change(dep, locked) is True

    def test_insecure_transport_flip_from_http_to_https_is_drift(self):
        """Switching back to HTTPS must also force a re-download."""
        dep = DependencyReference(
            repo_url="owner/repo",
            host="gitlab.example.com",
            is_insecure=False,
        )
        locked = LockedDependency(
            repo_url="owner/repo",
            host="gitlab.example.com",
            is_insecure=True,
        )

        assert detect_ref_change(dep, locked) is True

    def test_same_transport_and_ref_is_not_drift(self):
        """Matching ref + transport must remain a no-drift case."""
        dep = DependencyReference(
            repo_url="owner/repo",
            host="gitlab.example.com",
            reference="main",
            is_insecure=False,
        )
        locked = LockedDependency(
            repo_url="owner/repo",
            host="gitlab.example.com",
            resolved_ref="main",
            is_insecure=False,
        )

        assert detect_ref_change(dep, locked) is False


class TestDownloadRefLockfileOverride:
    """Test that lockfile SHA override is gated behind `not update_refs`.

    When --update is used, the download ref should NOT be overridden with
    the lockfile's pinned SHA. The package should be fetched at its
    original reference (or default branch).
    """

    def _make_subdirectory_dep(self):
        return DependencyReference(
            repo_url="owner/monorepo",
            host="github.com",
            reference=None,
            virtual_path="packages/my-skill",
            is_virtual=True,
        )

    def _make_regular_dep(self):
        return DependencyReference(
            repo_url="owner/repo",
            host="github.com",
            reference="main",
        )

    def _mock_lockfile(self, dep_ref, resolved_commit="abc123def456"):
        lockfile = Mock()
        locked_dep = Mock()
        locked_dep.resolved_commit = resolved_commit
        locked_dep.registry_prefix = None
        lockfile.get_dependency = Mock(return_value=locked_dep)
        return lockfile

    def test_subdirectory_lockfile_override_without_update(self):
        """Without --update, subdirectory download ref uses locked SHA."""
        dep = self._make_subdirectory_dep()
        lockfile = self._mock_lockfile(dep)

        ref = build_download_ref(dep, lockfile, update_refs=False, ref_changed=False)
        assert ref.reference == "abc123def456"
        assert ref.repo_url == "owner/monorepo"
        assert ref.virtual_path == "packages/my-skill"
        assert ref.host == "github.com"

    def test_subdirectory_no_lockfile_override_with_update(self):
        """With --update, subdirectory download ref must NOT use locked SHA."""
        dep = self._make_subdirectory_dep()
        lockfile = self._mock_lockfile(dep)

        ref = build_download_ref(dep, lockfile, update_refs=True, ref_changed=False)
        assert ref.reference != "abc123def456"
        assert ref is dep

    def test_regular_lockfile_override_without_update(self):
        """Without --update, regular package download ref uses locked SHA."""
        dep = self._make_regular_dep()
        lockfile = self._mock_lockfile(dep)

        ref = build_download_ref(dep, lockfile, update_refs=False, ref_changed=False)
        assert ref.reference == "abc123def456"

    def test_regular_no_lockfile_override_with_update(self):
        """With --update, regular package download ref must NOT use locked SHA."""
        dep = self._make_regular_dep()
        lockfile = self._mock_lockfile(dep)

        ref = build_download_ref(dep, lockfile, update_refs=True, ref_changed=False)
        assert ref.reference != "abc123def456"

    def test_no_lockfile_returns_original_ref(self):
        """Without a lockfile, download ref is the original dependency."""
        dep = self._make_subdirectory_dep()
        ref = build_download_ref(dep, existing_lockfile=None, update_refs=False, ref_changed=False)
        assert ref is dep

    def test_cached_lockfile_entry_not_overridden(self):
        """Lockfile entries with resolved_commit='cached' should not override."""
        dep = self._make_subdirectory_dep()
        lockfile = self._mock_lockfile(dep, resolved_commit="cached")

        ref = build_download_ref(dep, lockfile, update_refs=False, ref_changed=False)
        assert ref is dep

    def test_ghe_custom_domain_host_preserved_in_locked_ref(self):
        """GHE custom domain host must be preserved in the locked download ref.

        Regression test: without the host, DependencyReference.parse()
        defaults to github.com and the clone fails for enterprise hosts.
        """
        dep = DependencyReference(
            repo_url="org/repo",
            host="github.example.com",
            reference=None,
        )
        lockfile = self._mock_lockfile(dep)

        ref = build_download_ref(dep, lockfile, update_refs=False, ref_changed=False)
        assert ref.host == "github.example.com"
        assert ref.repo_url == "org/repo"
        assert ref.reference == "abc123def456"

    def test_ghe_custom_domain_subdirectory_host_preserved(self):
        """GHE custom domain host must be preserved for virtual/subdirectory deps too."""
        dep = DependencyReference(
            repo_url="org/monorepo",
            host="git.corp.internal",
            reference=None,
            virtual_path="packages/my-skill",
            is_virtual=True,
        )
        lockfile = self._mock_lockfile(dep)

        ref = build_download_ref(dep, lockfile, update_refs=False, ref_changed=False)
        assert ref.host == "git.corp.internal"
        assert ref.repo_url == "org/monorepo"
        assert ref.virtual_path == "packages/my-skill"
        assert ref.reference == "abc123def456"

    def test_no_host_produces_plain_repo_url(self):
        """When host is None, download ref preserves plain repo_url."""
        dep = DependencyReference(
            repo_url="owner/repo",
            host=None,
            reference="main",
        )
        lockfile = self._mock_lockfile(dep)

        ref = build_download_ref(dep, lockfile, update_refs=False, ref_changed=False)
        assert ref.host is None
        assert ref.repo_url == "owner/repo"
        assert ref.reference == "abc123def456"

    def test_http_lockfile_restores_insecure_scheme(self):
        """HTTP deps should restore the locked insecure scheme on replay."""
        dep = DependencyReference(
            repo_url="acme/rules",
            host="git.company.internal",
            reference=None,
        )
        lockfile = LockFile()
        lockfile.add_dependency(
            LockedDependency(
                repo_url="acme/rules",
                host="git.company.internal",
                resolved_commit="abc123def456",
                is_insecure=True,
                allow_insecure=True,
            )
        )

        ref = build_download_ref(dep, lockfile, update_refs=False, ref_changed=False)
        assert ref.host == "git.company.internal"
        assert ref.reference == "abc123def456"
        assert ref.is_insecure is True
        assert ref.allow_insecure is True


class TestLockedDependencyToDependencyRef:
    """Tests for LockedDependency.to_dependency_ref()."""

    def test_to_dependency_ref_preserves_install_path_fields(self):
        """Reconstructed refs keep the fields used by install path resolution."""
        locked = LockedDependency(
            repo_url="team/repo",
            host="gitlab.example.com",
            port=8443,
            registry_prefix="artifactory/github",
            resolved_ref="main",
            virtual_path="prompts/review.prompt.md",
            is_virtual=True,
            source="local",
            local_path="./packages/repo",
            is_insecure=True,
            allow_insecure=True,
        )

        dep_ref = locked.to_dependency_ref()

        assert dep_ref.repo_url == "team/repo"
        assert dep_ref.host == "gitlab.example.com"
        assert dep_ref.port == 8443
        assert dep_ref.reference == "main"
        assert dep_ref.virtual_path == "prompts/review.prompt.md"
        assert dep_ref.is_virtual is True
        assert dep_ref.artifactory_prefix == "artifactory/github"
        assert dep_ref.is_local is True
        assert dep_ref.local_path == "./packages/repo"
        assert dep_ref.is_insecure is True
        assert dep_ref.allow_insecure is True


class TestPreDownloadRefLockfileOverride:
    """Same as TestDownloadRefLockfileOverride but for the parallel pre-download path.

    Both paths now use ``build_download_ref()`` from ``drift.py``.
    """

    def _make_subdirectory_dep(self):
        return DependencyReference(
            repo_url="owner/monorepo",
            host="github.com",
            reference=None,
            virtual_path="packages/my-skill",
            is_virtual=True,
        )

    def _mock_lockfile(self, dep_ref, resolved_commit="abc123def456"):
        lockfile = Mock()
        locked_dep = Mock()
        locked_dep.resolved_commit = resolved_commit
        locked_dep.registry_prefix = None
        lockfile.get_dependency = Mock(return_value=locked_dep)
        return lockfile

    def test_pre_download_no_lockfile_override_with_update(self):
        """With --update, pre-download ref must NOT use locked SHA."""
        dep = self._make_subdirectory_dep()
        lockfile = self._mock_lockfile(dep)

        ref = build_download_ref(dep, lockfile, update_refs=True, ref_changed=False)
        assert ref.reference != "abc123def456"

    def test_pre_download_lockfile_override_without_update(self):
        """Without --update, pre-download ref uses locked SHA."""
        dep = self._make_subdirectory_dep()
        lockfile = self._mock_lockfile(dep)

        ref = build_download_ref(dep, lockfile, update_refs=False, ref_changed=False)
        assert ref.reference == "abc123def456"


class TestLockedDependencyHttpRoundTrip:
    """Tests for lockfile preservation of HTTP dependency metadata."""

    def test_to_yaml_round_trip_preserves_http_fields(self):
        lockfile = LockFile()
        lockfile.add_dependency(
            LockedDependency(
                repo_url="acme/rules",
                host="git.company.internal",
                resolved_commit="abc123def456",
                is_insecure=True,
                allow_insecure=True,
            )
        )

        parsed = LockFile.from_yaml(lockfile.to_yaml())
        dep = parsed.get_dependency("acme/rules")

        assert dep is not None
        assert dep.is_insecure is True
        assert dep.allow_insecure is True


class TestRefChangedDetection:
    """Tests for detect_ref_change() in drift.py.

    When the user changes the ref pin in apm.yml (e.g., from v1.0.0 to v2.0.0),
    apm install should detect the drift and re-download without --update.

    Key improvement over the old inline logic: handles all None transitions.
    """

    def _make_dep(self, reference):
        return DependencyReference(
            repo_url="owner/repo",
            host="github.com",
            reference=reference,
        )

    def _mock_locked_dep(self, resolved_ref, resolved_commit="abc123"):
        locked_dep = Mock()
        locked_dep.resolved_ref = resolved_ref
        locked_dep.resolved_commit = resolved_commit
        return locked_dep

    def test_no_drift_when_refs_match(self):
        """No drift when manifest ref matches lockfile resolved_ref."""
        dep = self._make_dep("v1.0.0")
        locked = self._mock_locked_dep("v1.0.0")
        assert detect_ref_change(dep, locked) is False

    def test_drift_when_ref_changed(self):
        """Drift detected when manifest ref changed from v1.0.0 to v2.0.0."""
        dep = self._make_dep("v2.0.0")
        locked = self._mock_locked_dep("v1.0.0")
        assert detect_ref_change(dep, locked) is True

    def test_drift_when_ref_added(self):
        """Drift detected when ref added (None → 'v1.0.0').

        This was a false-negative in the old inline logic because of the
        ``and locked_dep.resolved_ref`` guard. drift.py removes that guard.
        """
        dep = self._make_dep("v1.0.0")
        locked = self._mock_locked_dep(None)  # package was installed without a ref
        assert detect_ref_change(dep, locked) is True

    def test_drift_when_ref_removed(self):
        """Drift detected when ref removed ('main' → None)."""
        dep = self._make_dep(None)
        locked = self._mock_locked_dep("main")
        assert detect_ref_change(dep, locked) is True

    def test_no_drift_when_both_refs_none(self):
        """No drift when both manifest and lockfile have no ref."""
        dep = self._make_dep(None)
        locked = self._mock_locked_dep(None)
        assert detect_ref_change(dep, locked) is False

    def test_no_drift_when_no_locked_dep(self):
        """No drift when locked_dep is None (new package, first install)."""
        dep = self._make_dep("v1.0.0")
        assert detect_ref_change(dep, None) is False

    def test_no_drift_when_update_refs(self):
        """With update_refs=True, always returns False (--update mode)."""
        dep = self._make_dep("v2.0.0")
        locked = self._mock_locked_dep("v1.0.0")
        assert detect_ref_change(dep, locked, update_refs=True) is False

    def test_build_download_ref_uses_new_ref_when_changed(self):
        """When ref changed, build_download_ref does NOT use locked commit SHA."""
        dep = self._make_dep("v2.0.0")
        lockfile = Mock()
        locked_dep = self._mock_locked_dep("v1.0.0", "abc123")
        lockfile.get_dependency = Mock(return_value=locked_dep)
        ref_changed = detect_ref_change(dep, locked_dep)
        assert ref_changed is True
        download_ref = build_download_ref(dep, lockfile, update_refs=False, ref_changed=ref_changed)
        assert download_ref.reference != "abc123"

    def test_build_download_ref_uses_locked_sha_when_no_change(self):
        """When ref unchanged, build_download_ref uses the locked commit SHA."""
        dep = self._make_dep("v1.0.0")
        lockfile = Mock()
        locked_dep = self._mock_locked_dep("v1.0.0", "abc123")
        lockfile.get_dependency = Mock(return_value=locked_dep)
        ref_changed = detect_ref_change(dep, locked_dep)
        assert ref_changed is False
        download_ref = build_download_ref(dep, lockfile, update_refs=False, ref_changed=ref_changed)
        assert download_ref.reference == "abc123"


class TestOrphanDeployedFilesDetection:
    """Tests for detect_orphans() in drift.py.

    When packages are removed from apm.yml and apm install is run (full install),
    the deployed files should be identified for cleanup.
    """

    @staticmethod
    def _should_merge_lockfile_entry(
        dep_key, lockfile_dependencies, only_packages, intended_dep_keys
    ):
        """Reproduce the lockfile merge condition from install.py.

        Returns True if the dep_key should be merged into the new lockfile.
        Logic: only merge if (a) not already in new lockfile AND
               (b) either partial install OR package still in intended set.
        """
        if dep_key in lockfile_dependencies:
            return False  # already in new lockfile
        return bool(only_packages or dep_key in intended_dep_keys)

    def _mock_lockfile_with_deps(self, deps):
        """Build a mock lockfile with {dep_key: [deployed_files]} entries."""
        lockfile = Mock()
        dep_mocks = {}
        for dep_key, deployed_files in deps.items():
            dep = Mock()
            dep.deployed_files = deployed_files
            dep_mocks[dep_key] = dep
        lockfile.dependencies = dep_mocks
        return lockfile

    def test_no_orphans_when_all_packages_still_in_manifest(self):
        """No orphaned files when all lockfile packages are still in manifest."""
        lockfile = self._mock_lockfile_with_deps(
            {
                "owner/pkg-a": [".github/prompts/a.prompt.md"],
                "owner/pkg-b": [".github/prompts/b.prompt.md"],
            }
        )
        intended = {"owner/pkg-a", "owner/pkg-b"}
        orphans = detect_orphans(lockfile, intended, only_packages=None)
        assert orphans == set()

    def test_orphaned_files_when_package_removed(self):
        """Deployed files for removed package should be detected as orphans."""
        lockfile = self._mock_lockfile_with_deps(
            {
                "owner/pkg-a": [".github/prompts/a.prompt.md"],
                "owner/pkg-removed": [
                    ".github/prompts/removed.prompt.md",
                    ".github/instructions/removed.instructions.md",
                ],
            }
        )
        intended = {"owner/pkg-a"}  # pkg-removed not in new manifest
        orphans = detect_orphans(lockfile, intended, only_packages=None)
        assert orphans == {
            ".github/prompts/removed.prompt.md",
            ".github/instructions/removed.instructions.md",
        }

    def test_no_orphans_for_partial_install(self):
        """Orphan detection is skipped for partial installs (only_packages)."""
        lockfile = self._mock_lockfile_with_deps(
            {
                "owner/pkg-a": [".github/prompts/a.prompt.md"],
                "owner/pkg-removed": [".github/prompts/removed.prompt.md"],
            }
        )
        intended = {"owner/pkg-a"}
        orphans = detect_orphans(lockfile, intended, only_packages=["owner/pkg-a"])
        assert orphans == set()

    def test_no_orphans_when_no_lockfile(self):
        """No orphaned files when there is no existing lockfile."""
        orphans = detect_orphans(None, {"owner/pkg-a"}, only_packages=None)
        assert orphans == set()

    def test_lockfile_merge_drops_orphan_in_full_install(self):
        """In a full install, orphaned lockfile entries should NOT be merged."""
        # Simulate: new lockfile has pkg-a, old lockfile has pkg-a + pkg-removed
        new_lockfile_deps = {"owner/pkg-a"}
        intended = {"owner/pkg-a"}  # pkg-removed no longer in manifest

        # pkg-removed should NOT be merged (orphan)
        assert not self._should_merge_lockfile_entry(
            "owner/pkg-removed", new_lockfile_deps, only_packages=None, intended_dep_keys=intended
        )

    def test_lockfile_merge_preserves_failed_download_in_full_install(self):
        """In a full install, failed downloads (still in manifest) should be preserved."""
        new_lockfile_deps = {"owner/pkg-a"}  # pkg-b failed to download
        intended = {"owner/pkg-a", "owner/pkg-b"}  # both in manifest

        # pkg-b should be preserved (still in manifest, just failed)
        assert self._should_merge_lockfile_entry(
            "owner/pkg-b", new_lockfile_deps, only_packages=None, intended_dep_keys=intended
        )

    def test_lockfile_merge_preserves_all_for_partial_install(self):
        """For partial installs, ALL old lockfile entries should be preserved."""
        new_lockfile_deps = {"owner/pkg-a"}
        intended = {"owner/pkg-a"}  # pkg-removed not in new manifest

        # pkg-removed should STILL be preserved in a partial install
        assert self._should_merge_lockfile_entry(
            "owner/pkg-removed",
            new_lockfile_deps,
            only_packages=["owner/pkg-a"],
            intended_dep_keys=intended,
        )


class TestDetectConfigDrift:
    """Tests for detect_config_drift() in drift.py.

    Config drift means an already-installed item's serialized config
    in apm.yml differs from the stored baseline in the lockfile.
    """

    def test_no_drift_when_configs_match(self):
        """No drift when current config is identical to stored config."""
        current = {"name": "my-server", "url": "http://example.com"}
        stored = {"my-server": {"name": "my-server", "url": "http://example.com"}}
        assert detect_config_drift({"my-server": current}, stored) == set()

    def test_drift_when_config_changed(self):
        """Drift detected when config value changed."""
        current = {"name": "my-server", "url": "http://new.example.com"}
        stored = {"my-server": {"name": "my-server", "url": "http://old.example.com"}}
        assert detect_config_drift({"my-server": current}, stored) == {"my-server"}

    def test_no_drift_for_new_entry_without_baseline(self):
        """Brand-new entry without a stored baseline is NOT drift — it's a first install."""
        current = {"name": "brand-new", "url": "http://example.com"}
        assert detect_config_drift({"brand-new": current}, {}) == set()

    def test_drift_when_env_changed(self):
        """Drift detected when env variables change."""
        current = {"name": "s", "env": {"TOKEN": "new"}}
        stored = {"s": {"name": "s", "env": {"TOKEN": "old"}}}
        assert detect_config_drift({"s": current}, stored) == {"s"}

    def test_no_drift_when_stored_configs_empty(self):
        """No drift when no stored baseline exists (backward compat)."""
        current = {"name": "s", "url": "http://x.com"}
        assert detect_config_drift({"s": current}, {}) == set()

    def test_multiple_entries_partial_drift(self):
        """Only changed entries are reported."""
        current_configs = {
            "unchanged": {"url": "http://a.com"},
            "changed": {"url": "http://new.com"},
        }
        stored = {
            "unchanged": {"url": "http://a.com"},
            "changed": {"url": "http://old.com"},
        }
        assert detect_config_drift(current_configs, stored) == {"changed"}


class TestUpdateRefsShaComparison:
    """Tests for the perf optimization: skip download when resolved SHA matches lockfile.

    When ``update_refs=True``, the engine resolves refs to get the latest SHA,
    then compares against the lockfile SHA. If they match, the download is skipped.
    This avoids re-downloading packages that are already at their latest version.
    """

    @staticmethod
    def _build_lockfile_match_update(
        *, resolved_sha, lockfile_sha, install_exists, local_head_sha=None
    ):
        """Reproduce the update_refs lockfile_match check from install.py.

        In update mode, lockfile_match is True when the resolved remote SHA
        matches the lockfile SHA AND local HEAD matches (guarding against
        corrupted local checkouts).
        """
        resolved_ref = Mock() if resolved_sha else None
        if resolved_ref:
            resolved_ref.resolved_commit = resolved_sha

        locked_dep = Mock() if lockfile_sha else None
        if locked_dep:
            locked_dep.resolved_commit = lockfile_sha

        # Default: local HEAD matches lockfile when not explicitly set
        if local_head_sha is None:
            local_head_sha = lockfile_sha

        lockfile_match = False
        if install_exists and locked_dep:
            if locked_dep.resolved_commit and locked_dep.resolved_commit != "cached":
                # Update mode: compare resolved SHA with lockfile SHA,
                # then verify local checkout matches.
                if resolved_ref and resolved_ref.resolved_commit == locked_dep.resolved_commit:
                    if local_head_sha == locked_dep.resolved_commit:
                        lockfile_match = True
        return lockfile_match

    def test_matching_sha_skips_download(self):
        """When resolved SHA matches lockfile SHA, lockfile_match is True."""
        assert (
            self._build_lockfile_match_update(
                resolved_sha="abc123def456",
                lockfile_sha="abc123def456",
                install_exists=True,
            )
            is True
        )

    def test_different_sha_does_not_skip(self):
        """When resolved SHA differs, lockfile_match is False (download needed)."""
        assert (
            self._build_lockfile_match_update(
                resolved_sha="new_sha_789",
                lockfile_sha="abc123def456",
                install_exists=True,
            )
            is False
        )

    def test_no_resolved_ref_does_not_skip(self):
        """When resolution failed (None), lockfile_match is False."""
        assert (
            self._build_lockfile_match_update(
                resolved_sha=None,
                lockfile_sha="abc123def456",
                install_exists=True,
            )
            is False
        )

    def test_no_lockfile_entry_does_not_skip(self):
        """When no lockfile entry exists (new package), lockfile_match is False."""
        assert (
            self._build_lockfile_match_update(
                resolved_sha="abc123def456",
                lockfile_sha=None,
                install_exists=True,
            )
            is False
        )

    def test_cached_lockfile_entry_does_not_skip(self):
        """When lockfile has 'cached' placeholder, lockfile_match is False."""
        assert (
            self._build_lockfile_match_update(
                resolved_sha="abc123def456",
                lockfile_sha="cached",
                install_exists=True,
            )
            is False
        )

    def test_no_install_path_does_not_skip(self):
        """When install path doesn't exist, lockfile_match is False."""
        assert (
            self._build_lockfile_match_update(
                resolved_sha="abc123def456",
                lockfile_sha="abc123def456",
                install_exists=False,
            )
            is False
        )

    def test_corrupted_local_checkout_does_not_skip(self):
        """When local HEAD differs from lockfile SHA, lockfile_match is False
        even if resolved remote matches lockfile (guards against corrupt installs)."""
        assert (
            self._build_lockfile_match_update(
                resolved_sha="abc123def456",
                lockfile_sha="abc123def456",
                install_exists=True,
                local_head_sha="corrupted_different_sha",
            )
            is False
        )

    def test_skip_download_with_update_lockfile_match(self):
        """End-to-end: skip_download is True when update_refs lockfile_match is True."""
        install_path_exists = True
        is_cacheable = False
        update_refs = True
        already_resolved = False
        lockfile_match = True  # From the update_refs SHA comparison

        skip_download = install_path_exists and (
            (is_cacheable and not update_refs) or already_resolved or lockfile_match
        )
        assert skip_download is True

    def test_no_skip_when_sha_changed_during_update(self):
        """When remote SHA changed, skip_download is False (package must be fetched)."""
        install_path_exists = True
        is_cacheable = False
        update_refs = True
        already_resolved = False
        lockfile_match = False  # SHA comparison failed (remote changed)

        skip_download = install_path_exists and (
            (is_cacheable and not update_refs) or already_resolved or lockfile_match
        )
        assert skip_download is False


class TestPreDownloadUpdateRefsSkip:
    """Tests for the pre-download skip optimization when update_refs=True.

    When update_refs=True and the local HEAD matches the lockfile SHA,
    the pre-download section skips the package (defers to sequential
    resolution for remote SHA comparison).
    """

    @staticmethod
    def _should_skip_pre_download(*, update_refs, path_exists, lockfile_sha, local_head_sha):
        """Reproduce the pre-download skip condition for update_refs."""
        locked_dep = Mock() if lockfile_sha else None
        if locked_dep:
            locked_dep.resolved_commit = lockfile_sha

        if update_refs and path_exists and locked_dep:
            if locked_dep.resolved_commit and locked_dep.resolved_commit != "cached":
                if local_head_sha == locked_dep.resolved_commit:
                    return True
        return False

    def test_skip_when_head_matches_lockfile(self):
        """Skip pre-download when local HEAD matches lockfile SHA."""
        assert (
            self._should_skip_pre_download(
                update_refs=True,
                path_exists=True,
                lockfile_sha="abc123",
                local_head_sha="abc123",
            )
            is True
        )

    def test_no_skip_when_head_differs(self):
        """Don't skip pre-download when local HEAD differs (corrupted install)."""
        assert (
            self._should_skip_pre_download(
                update_refs=True,
                path_exists=True,
                lockfile_sha="abc123",
                local_head_sha="different",
            )
            is False
        )

    def test_no_skip_when_not_update_mode(self):
        """Don't use this optimization in normal install mode."""
        assert (
            self._should_skip_pre_download(
                update_refs=False,
                path_exists=True,
                lockfile_sha="abc123",
                local_head_sha="abc123",
            )
            is False
        )

    def test_no_skip_when_path_missing(self):
        """Don't skip when install path doesn't exist."""
        assert (
            self._should_skip_pre_download(
                update_refs=True,
                path_exists=False,
                lockfile_sha="abc123",
                local_head_sha="abc123",
            )
            is False
        )

    def test_no_skip_when_no_lockfile_entry(self):
        """Don't skip when there's no lockfile entry (new package)."""
        assert (
            self._should_skip_pre_download(
                update_refs=True,
                path_exists=True,
                lockfile_sha=None,
                local_head_sha="abc123",
            )
            is False
        )

    def test_no_skip_when_lockfile_sha_is_cached(self):
        """Don't skip when lockfile has 'cached' placeholder."""
        assert (
            self._should_skip_pre_download(
                update_refs=True,
                path_exists=True,
                lockfile_sha="cached",
                local_head_sha="cached",
            )
            is False
        )
