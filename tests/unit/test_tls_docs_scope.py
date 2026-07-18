"""T3: the #2005 docs/CHANGELOG must scope OS-trust honestly.

Round-1 shipped copy claiming ``apm run`` child runtimes (incl. ``codex``)
re-run the OS-trust bootstrap. That was a field no-op for the ``llm`` venv and
never true for the Node/Rust runtimes. These tests are the silent-drift guard
that keeps the prose scoped to what actually ships: ``apm install`` plus the
Python ``llm`` runtime, with Node (Copilot) / Rust (Codex) tracked in #2034.
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
    assert "#2034" in docs, "ssl-issues.md must reference the Node/Rust follow-up (#2034)"
    # Node (Copilot) / Rust (Codex) must be described as NOT covered.
    assert "not yet covered" in docs
    # The stale round-1 claim that codex re-runs the bootstrap must be gone.
    assert "the `llm` and `codex` CLIs) re-run the same OS-trust bootstrap" not in docs


def test_changelog_names_tls_precedence_controls():
    changelog = (_repo_root() / "CHANGELOG.md").read_text(encoding="utf-8")
    entry = _changelog_entry(changelog, "#2005")

    assert "`APM_DISABLE_TRUSTSTORE=1`" in entry
    assert "`REQUESTS_CA_BUNDLE`" in entry
    assert "`CURL_CA_BUNDLE`" in entry


def test_ssl_docs_node_caveat_appears_early():
    docs = (
        _repo_root() / "docs" / "src" / "content" / "docs" / "troubleshooting" / "ssl-issues.md"
    ).read_text(encoding="utf-8")

    heading = "## Default behaviour: the OS trust store"
    start = docs.index(heading)
    known_limits = docs.index("### Known limitations")
    # The Node/Codex caveat must surface EARLY -- inside the Default behaviour
    # section, well before the Known limitations block far below.
    caveat = docs.index("Scope caveat", start)
    assert caveat < known_limits, "Node/Codex caveat must appear before Known limitations"
    # And it must offer the workaround users can apply today.
    caveat_region = docs[start:known_limits]
    assert "NODE_EXTRA_CA_CERTS" in caveat_region


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


def test_ssl_docs_keep_planned_configuration_generic():
    docs = (
        _repo_root() / "docs" / "src" / "content" / "docs" / "troubleshooting" / "ssl-issues.md"
    ).read_text(encoding="utf-8")

    assert "APM_EXTRA_CA_BUNDLE" not in docs
    assert docs.count("#2034") == 1


def test_enterprise_security_docs_transport_trust_model():
    security = (
        _repo_root() / "docs" / "src" / "content" / "docs" / "enterprise" / "security.md"
    ).read_text(encoding="utf-8")

    assert "## HTTPS transport trust" in security
    assert "APM_DISABLE_TRUSTSTORE" in security
    assert "REQUESTS_CA_BUNDLE" in security
    assert "CURL_CA_BUNDLE" in security
    assert ".pth" in security
    assert "Node" in security
    assert "Rust" in security


def test_ssl_docs_verify_apm_path_and_mark_planned_scope():
    docs = (
        _repo_root() / "docs" / "src" / "content" / "docs" / "troubleshooting" / "ssl-issues.md"
    ).read_text(encoding="utf-8")

    assert ":::note[Planned]" in docs
    assert 'python -c "import requests' not in docs
    assert "APM_LOG_LEVEL=DEBUG apm install" in docs
    assert "schannel" not in docs.lower()
