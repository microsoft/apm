"""Tests for Package URL (purl) identity + credential scrubbing (U5).

purl is the stable component identity in the SBOM. It is derived ONLY from
lockfile-recorded fields -- never by re-resolving or hashing at export time.
Credentials embedded in a recorded URL must never leak into SBOM output.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from apm_cli.deps.lockfile import LockedDependency
from apm_cli.export.purl import build_purl, scrub_url


def test_git_dep_uses_github_purl_with_commit():
    dep = LockedDependency(
        repo_url="github.com/acme/git-utils",
        host_type="github",
        resolved_commit="def789ghi012",
    )
    assert build_purl(dep) == "pkg:github/acme/git-utils@def789ghi012"


def test_git_dep_infers_github_from_repo_url_when_host_type_absent():
    # Real loaded lockfiles drop host_type for github (only gitlab is stored),
    # so the forge must be inferred from the repo_url host segment.
    dep = LockedDependency(
        repo_url="github.com/acme/git-utils",
        resolved_commit="def789ghi012",
    )
    assert build_purl(dep) == "pkg:github/acme/git-utils@def789ghi012"


def test_git_dep_infers_bitbucket_from_repo_url():
    dep = LockedDependency(
        repo_url="bitbucket.org/team/widget",
        resolved_commit="bbb222",
    )
    assert build_purl(dep) == "pkg:bitbucket/team/widget@bbb222"


def test_git_dep_unknown_forge_falls_back_to_generic():
    dep = LockedDependency(
        repo_url="git.example.com/acme/thing",
        resolved_commit="ccc333",
    )
    assert build_purl(dep) == "pkg:generic/thing@ccc333"


def test_local_dep_uses_generic_purl_with_content_hash():
    dep = LockedDependency(
        repo_url="github.com/acme/local-helper",
        source="local",
        local_path="./packages/local-helper",
        content_hash="sha256:abc123",
    )
    purl = build_purl(dep)
    assert purl.startswith("pkg:generic/local-helper@")
    assert "abc123" in purl


def test_local_dep_without_hash_omits_version():
    dep = LockedDependency(
        repo_url="github.com/acme/local-helper",
        source="local",
        local_path="./packages/local-helper",
    )
    assert build_purl(dep) == "pkg:generic/local-helper"


def test_oci_dep_uses_oci_purl():
    dep = LockedDependency(
        repo_url="github.com/acme/oci-tools",
        source="registry",
        resolved_url="oci://registry.example.com/acme/oci-tools@sha256:abc123",
        resolved_hash="sha256:abc123",
    )
    purl = build_purl(dep)
    assert purl.startswith("pkg:oci/")
    assert "oci-tools" in purl


def test_gitlab_host_type_maps_to_gitlab_purl():
    dep = LockedDependency(
        repo_url="gitlab.com/group/proj",
        host_type="gitlab",
        resolved_commit="aaa111",
    )
    assert build_purl(dep) == "pkg:gitlab/group/proj@aaa111"


def test_purl_is_deterministic():
    dep = LockedDependency(
        repo_url="github.com/acme/git-utils", host_type="github", resolved_commit="c0ffee"
    )
    assert build_purl(dep) == build_purl(dep)


def test_purl_percent_encodes_crafted_name_segments():
    # A crafted dependency name must not be able to inject purl-structural
    # characters (separators, '@', '#', '?', spaces) into the component
    # identity. Namespace/name segments are percent-encoded per the purl spec
    # so identity cannot be spoofed. Supply-chain hard line.
    dep = LockedDependency(
        repo_url="github.com/acme/git utils@evil",
        host_type="github",
        resolved_commit="def789ghi012",
    )
    purl = build_purl(dep)
    assert purl == "pkg:github/acme/git%20utils%40evil@def789ghi012"
    # The commit (after the LAST '@') stays the real version, not the injected one.
    assert purl.rsplit("@", 1)[1] == "def789ghi012"


def test_purl_encoding_is_noop_for_clean_slugs():
    # Normal forge slugs are already purl-safe; encoding must not alter them
    # (keeps existing identities and golden fixtures byte-stable).
    dep = LockedDependency(
        repo_url="github.com/acme/git-utils",
        host_type="github",
        resolved_commit="def789ghi012",
    )
    assert build_purl(dep) == "pkg:github/acme/git-utils@def789ghi012"


def test_scrub_url_removes_userinfo():
    scrubbed = scrub_url("https://user:secret@registry.example.com/path/x.tgz")
    parts = urlsplit(scrubbed)
    assert "secret" not in scrubbed
    assert "user" not in parts.netloc
    assert parts.hostname == "registry.example.com"
    assert parts.path == "/path/x.tgz"


def test_scrub_url_passes_through_clean_url():
    url = "https://registry.example.com/path/x.tgz"
    assert scrub_url(url) == url


def test_scrub_url_handles_oci_scheme():
    scrubbed = scrub_url("oci://user:tok@registry.example.com/acme/oci-tools@sha256:abc")
    parts = urlsplit(scrubbed)
    assert "tok" not in scrubbed
    assert "user" not in parts.netloc
    assert parts.hostname == "registry.example.com"


def test_scrub_url_strips_query_string_token():
    # Query-string credentials (deploy tokens, SAS signatures) must never reach
    # SBOM output, even when no userinfo is present. Supply-chain hard line.
    scrubbed = scrub_url("https://gitlab.example.com/acme/git-utils.git?access_token=DEADBEEF")
    parts = urlsplit(scrubbed)
    assert "DEADBEEF" not in scrubbed
    assert "access_token" not in scrubbed
    assert parts.query == ""
    assert parts.hostname == "gitlab.example.com"
    assert parts.path == "/acme/git-utils.git"


def test_scrub_url_strips_sas_signature_params():
    scrubbed = scrub_url("https://acct.blob.core.windows.net/c/x.tgz?se=2025&sp=r&sig=SECRETSIG")
    assert "SECRETSIG" not in scrubbed
    assert "sig=" not in scrubbed
    assert urlsplit(scrubbed).query == ""
