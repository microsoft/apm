"""Documentation drift guards for the #2005 and #2034 TLS trust contracts.

The additive bundle applies to package-management HTTPS. Execution children
retain their existing trust configuration.
"""

from __future__ import annotations

from pathlib import Path


def _repo_root() -> Path:
    current = Path(__file__).resolve().parent
    for parent in (current, *current.parents):
        if (parent / "pyproject.toml").is_file():
            return parent
    raise RuntimeError("Cannot locate repository root")


def _changelog_entry(changelog: str, marker: str) -> str:
    """Return the changelog bullet containing marker, regardless of release section."""
    for entry in changelog.split("\n- "):
        bullet = entry.split("\n\n", 1)[0]
        if marker in bullet:
            return bullet
    raise AssertionError(f"CHANGELOG entry containing {marker} not found")


def test_changelog_scopes_os_trust_to_python_paths():
    changelog = (_repo_root() / "CHANGELOG.md").read_text(encoding="utf-8")
    entry = _changelog_entry(changelog, "#2005")

    assert "Python" in entry
    # The stale round-1 joint claim must be gone.
    assert "and `apm run` (child runtimes)" not in entry


def test_ssl_docs_scope_and_known_limitations():
    docs = (
        _repo_root() / "docs" / "src" / "content" / "docs" / "troubleshooting" / "ssl-issues.md"
    ).read_text(encoding="utf-8")

    assert "### Known limitations" in docs, "ssl-issues.md must have a Known limitations section"
    assert "APM_EXTRA_CA_BUNDLE" in docs
    assert "NODE_EXTRA_CA_CERTS" in docs
    assert "Rust-based Codex" in docs
    assert "runtime-owned trust configuration" in docs


def test_changelog_names_tls_precedence_controls():
    changelog = (_repo_root() / "CHANGELOG.md").read_text(encoding="utf-8")
    entry = _changelog_entry(changelog, "#2005")

    assert "`APM_DISABLE_TRUSTSTORE=1`" in entry
    assert "`REQUESTS_CA_BUNDLE`" in entry
    assert "`CURL_CA_BUNDLE`" in entry


def test_ssl_docs_runtime_scope_appears_early() -> None:
    docs = (
        _repo_root() / "docs" / "src" / "content" / "docs" / "troubleshooting" / "ssl-issues.md"
    ).read_text(encoding="utf-8")

    heading = "## Default behaviour: the OS trust store"
    start = docs.index(heading)
    configure = docs.index("## Configure trust")
    # Runtime coverage must be visible in the default-behaviour explanation,
    # before users reach configuration recipes.
    scope = docs.index("### Runtime coverage", start)
    assert scope < configure
    scope_region = docs[scope:configure]
    assert "Node/Copilot child" in scope_region
    assert "NODE_EXTRA_CA_CERTS" in scope_region
    assert "Rust/Codex" in scope_region
    assert "runtime's own trust settings" in scope_region


def test_ssl_docs_pip_cert_and_replaces_notes():
    docs = (
        _repo_root() / "docs" / "src" / "content" / "docs" / "troubleshooting" / "ssl-issues.md"
    ).read_text(encoding="utf-8")

    # M4-docs: the pip-own-cert caveat during runtime setup.
    assert "PIP_CERT" in docs
    # L1: REQUESTS_CA_BUNDLE replaces (not augments) the OS store, plus the
    # stale-bundle "still failing?" note.
    assert "*replaces*" in docs or "replaces" in docs
    assert "stale `REQUESTS_CA_BUNDLE`" in docs


def test_ssl_docs_describe_additive_validation_and_precedence() -> None:
    docs = (
        _repo_root() / "docs" / "src" / "content" / "docs" / "troubleshooting" / "ssl-issues.md"
    ).read_text(encoding="utf-8")

    assert "APM_EXTRA_CA_BUNDLE" in docs
    assert "retains native OS roots" in docs
    assert "no larger than 8 MiB" in docs
    assert "invalid selected bundle fails closed" in docs
    assert "`REQUESTS_CA_BUNDLE`, `CURL_CA_BUNDLE`, `APM_DISABLE_TRUSTSTORE`, " in docs


def test_enterprise_security_docs_transport_trust_model():
    security = (
        _repo_root() / "docs" / "src" / "content" / "docs" / "enterprise" / "security.md"
    ).read_text(encoding="utf-8")

    assert "## HTTPS transport trust" in security
    assert "APM_DISABLE_TRUSTSTORE" in security
    assert "REQUESTS_CA_BUNDLE" in security
    assert "CURL_CA_BUNDLE" in security
    assert "APM_EXTRA_CA_BUNDLE" in security
    assert "does not extend that bootstrap" in security
    assert ".pth" in security
    assert "Node" in security
    assert "Rust" in security


def test_enterprise_security_docs_do_not_claim_transport_aware_policy():
    security = (
        _repo_root() / "docs" / "src" / "content" / "docs" / "enterprise" / "security.md"
    ).read_text(encoding="utf-8")
    # Keep this contract about the guidance rather than Markdown presentation.
    normalized = " ".join(security.replace("**", "").split())

    assert "no dedicated `allow_insecure` field" in normalized
    assert "scheme-blind, host-blind canonical package identities" in normalized
    assert "owner/repo`, not `github.com/owner/repo" in normalized
    assert "non-default hosts are retained" not in normalized
    assert "cannot restrict host identity" in normalized
    assert "does not enforce HTTPS" in normalized
    assert "`registry_source.allow_non_registry`" in normalized
    assert "Forbid `allow_insecure: true` via the policy allow list" not in normalized


def test_ssl_docs_verify_apm_path_and_shipped_scope() -> None:
    docs = (
        _repo_root() / "docs" / "src" / "content" / "docs" / "troubleshooting" / "ssl-issues.md"
    ).read_text(encoding="utf-8")

    assert "export APM_EXTRA_CA_BUNDLE=/path/to/corporate-ca.pem" in docs
    assert "export NODE_EXTRA_CA_CERTS=/path/to/node-ca-bundle.pem" in docs
    assert 'python -c "import requests' not in docs
    assert "APM_LOG_LEVEL=DEBUG apm install" in docs
    assert "schannel" not in docs.lower()


def test_changelog_scopes_additive_bundle_to_package_management() -> None:
    changelog = (_repo_root() / "CHANGELOG.md").read_text(encoding="utf-8")
    entry = _changelog_entry(changelog, "#2741")
    assert "`APM_EXTRA_CA_BUNDLE`" in entry
    assert "package-management HTTPS" in entry
    assert "retaining default trust roots" in entry
    assert "Node" not in entry
