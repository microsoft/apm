"""Behavioral receipts for OpenAPM manifest requirements (req-mf-*).

Each test drives an APM manifest owner (or the installed CLI) and asserts an
observable outcome: a parsed value, a rejection naming the offending token, or
manifest bytes on disk. Spec-text checks live in test_manifest_reqs.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from apm_cli.core.errors import UnknownTargetError
from apm_cli.core.target_detection import accepted_target_values
from apm_cli.integration.targets import KNOWN_TARGETS
from apm_cli.marketplace.errors import MarketplaceYmlError
from apm_cli.marketplace.yml_schema import load_marketplace_from_apm_yml
from apm_cli.models.apm_package import APMPackage
from apm_cli.models.dependency.reference import DependencyReference
from apm_cli.policy.project_config import (
    ProjectPolicyConfigError,
    parse_project_policy_hash_pin,
)
from apm_cli.utils.github_host import manifest_default_host_scope

_V01_SCHEMA = "https://microsoft.github.io/apm/specs/schemas/manifest-v0.1.schema.json"


def _load(tmp_path: Path, data: dict) -> APMPackage:
    return APMPackage.from_mapping(data, package_path=tmp_path, create_config=False)


@pytest.fixture
def _registries_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    import apm_cli.deps.registry.feature_gate as gate

    monkeypatch.setattr(gate, "is_package_registry_enabled", lambda **_kw: True)


# --- req-mf-002 / req-mf-003 / req-mf-004: producer identity ------------


@pytest.mark.req("req-mf-002", "req-mf-003")
@pytest.mark.parametrize(
    ("data", "field"),
    [
        ({"version": "1.0.0"}, "name"),
        ({"name": "", "version": "1.0.0"}, "name"),
        ({"name": "   ", "version": "1.0.0"}, "name"),
        ({"name": 7, "version": "1.0.0"}, "name"),
        ({"name": "demo"}, "version"),
        ({"name": "demo", "version": 1.0}, "version"),
        ({"name": "demo", "version": 2}, "version"),
    ],
    ids=["no-name", "empty-name", "blank-name", "int-name", "no-version", "float", "int"],
)
def test_manifest_without_string_identity_is_refused(
    tmp_path: Path, data: dict, field: str
) -> None:
    """A manifest whose name/version is missing or not a string never loads."""
    with pytest.raises(ValueError, match=rf"'{field}'"):
        _load(tmp_path, data)


# --- req-mf-005: target vocabulary --------------------------------------


# Passing clause (a) of req-mf-005. Unmarked so the requirement reports the
# xfail below until clause (b) holds.
@pytest.mark.parametrize("spelling", ["target", "targets"])
def test_unknown_target_token_is_rejected_naming_the_token(tmp_path: Path, spelling: str) -> None:
    """Canonical names (and the singular alias) load; an unknown token fails naming itself."""
    canonical = sorted(set(accepted_target_values()) & set(KNOWN_TARGETS))[:3]
    accepted = _load(tmp_path, {"name": "p", "version": "1.0.0", spelling: canonical})
    assert set(canonical) <= set(accepted.canonical_targets)
    if spelling == "target":
        aliased = _load(tmp_path, {"name": "p", "version": "1.0.0", spelling: ["agents"]})
        assert aliased.canonical_targets == ("copilot",)

    value = [canonical[0], "bogus-harness"]
    with pytest.raises((ValueError, UnknownTargetError), match="bogus-harness"):
        _load(tmp_path, {"name": "p", "version": "1.0.0", spelling: value})


@pytest.mark.req("req-mf-005")
def test_vendor_extension_target_identifier_is_accepted(tmp_path: Path) -> None:
    """The x-<vendor>-<name> form is accepted, unlike an arbitrary unknown token (#4251).

    APM registers no vendor handler, so the identifier is kept as a vendor
    target and never selects a deployable target.
    """
    package = _load(
        tmp_path, {"name": "p", "version": "1.0.0", "targets": ["copilot", "x-acme-foo"]}
    )
    assert package.vendor_targets == ("x-acme-foo",)
    assert package.canonical_targets == ("copilot",)


# --- req-mf-007: string-form dependency grammar -------------------------


@pytest.mark.req("req-mf-007")
@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        ("https://gitlab.example.com/acme/tools", ("gitlab.example.com", "acme/tools", None)),
        ("git@github.com:acme/tools.git#v1.2.0", ("github.com", "acme/tools", "v1.2.0")),
        ("ssh://git@git.example.com:2222/acme/tools.git", ("git.example.com", "acme/tools", None)),
        ("acme/tools#main", ("github.com", "acme/tools", "main")),
        ("gitlab.com/acme/tools", ("gitlab.com", "acme/tools", None)),
    ],
    ids=["https-url", "scp-url-ref", "ssh-port", "shorthand-ref", "shorthand-host"],
)
def test_string_dependency_productions_parse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: str, expected: tuple
) -> None:
    """url-form and shorthand-form entries parse to host, repo and ref."""
    monkeypatch.delenv("GITHUB_HOST", raising=False)
    package = _load(tmp_path, {"name": "p", "version": "1.0.0", "dependencies": {"apm": [entry]}})
    (dep,) = package.get_apm_dependencies()
    assert (dep.host, dep.repo_url, dep.reference) == expected


@pytest.mark.req("req-mf-007")
@pytest.mark.parametrize("entry", ["./packages/local", "../shared/pkg"])
def test_local_path_production_parses_as_local(tmp_path: Path, entry: str) -> None:
    """local-path-form entries parse as local dependencies, not remote ones."""
    package = _load(tmp_path, {"name": "p", "version": "1.0.0", "dependencies": {"apm": [entry]}})
    (dep,) = package.get_apm_dependencies()
    assert dep.is_local
    assert dep.local_path == entry


@pytest.mark.req("req-mf-007")
@pytest.mark.parametrize("entry", ["lonely", "a b/c", "https://"])
def test_string_outside_the_grammar_is_rejected_naming_the_entry(
    tmp_path: Path, entry: str
) -> None:
    """An entry matching no production fails the load and names the entry."""
    with pytest.raises(ValueError) as caught:
        _load(tmp_path, {"name": "p", "version": "1.0.0", "dependencies": {"apm": [entry]}})
    assert f"Invalid APM dependency '{entry}'" in str(caught.value)


# --- req-mf-009: canonical normalisation on rewrite ---------------------


@pytest.mark.req("req-mf-009")
@pytest.mark.parametrize(
    ("default_host", "entry", "written"),
    [
        (None, "git@github.com:acme/x.git", "acme/x"),
        (None, "https://github.com/acme/x.git", "acme/x"),
        (None, "https://github.com/acme/x#v1.0.0", "acme/x#v1.0.0"),
        (None, "git@gitlab.com:acme/x.git", "gitlab.com/acme/x"),
        (None, "https://gitlab.example.com/acme/x", "gitlab.example.com/acme/x"),
        ("gitlab.com", "git@gitlab.com:acme/x.git", "acme/x"),
        ("gitlab.com", "https://gitlab.com/acme/x", "acme/x"),
        ("gitlab.com", "git@github.com:acme/x.git", "github.com/acme/x"),
        ("gitlab.com", "https://github.com/acme/x", "github.com/acme/x"),
    ],
)
def test_writer_strips_only_the_selected_default_host(
    monkeypatch: pytest.MonkeyPatch, default_host: str | None, entry: str, written: str
) -> None:
    """SCP and https URLs on the selected host become owner/repo; others keep the FQDN.

    With ``default_host:`` omitted the implementation default (github.com) is
    the selected host; with ``default_host: gitlab.com`` github.com is not
    stripped, proving no host literal is hard-coded.
    """
    monkeypatch.delenv("GITHUB_HOST", raising=False)
    with manifest_default_host_scope(default_host):
        assert DependencyReference.parse(entry).to_apm_yml_entry() == written


# --- req-mf-015: registries.<name> key guard -----------------------------


@pytest.mark.req("req-mf-015")
@pytest.mark.xfail(
    strict=True,
    reason="#4252: registries.<name> accepts typo keys (v0.1) or rejects x- keys (default)",
)
@pytest.mark.parametrize("schema", [None, _V01_SCHEMA], ids=["default", "openapm-v0.1"])
@pytest.mark.usefixtures("_registries_enabled")
def test_registry_entry_rejects_typos_but_keeps_vendor_keys(tmp_path: Path, schema) -> None:
    """A typo key fails naming itself; an x- key loads under either contract."""
    base: dict = {"name": "p", "version": "1.0.0"}
    if schema:
        base["$schema"] = schema
    vendor = {**base, "registries": {"corp": {"url": "https://r.example.com", "x-acme-note": 1}}}
    assert _load(tmp_path, vendor).registries == {"corp": "https://r.example.com"}
    typo = {**base, "registries": {"corp": {"url": "https://r.example.com", "ulr": "x"}}}
    with pytest.raises(ValueError, match="ulr"):
        _load(tmp_path, typo)


# --- req-mf-017: marketplace package sources -----------------------------


def _marketplace_manifest(tmp_path: Path, source: str) -> Path:
    manifest = tmp_path / "apm.yml"
    manifest.write_text(
        "name: market\nversion: 1.0.0\ndescription: d\nmarketplace:\n"
        "  owner:\n    name: ACME\n  packages:\n"
        f"    - name: tool-a\n      source: '{source}'\n      ref: v1.0.0\n",
        encoding="utf-8",
    )
    return manifest


@pytest.mark.req("req-mf-017")
@pytest.mark.parametrize(
    "source",
    [
        "../escape",
        "./tools/../../escape",
        "https://user@example.com/acme/tool",
        "https://example.com:8443/acme/tool",
        "https://example.com/acme/tool?ref=main",
        "http://example.com/acme/tool",
        "ssh://example.com/acme/tool",
        "/abs/tools/a",
    ],
    ids=["dotdot", "nested-dotdot", "userinfo", "port", "query", "http", "ssh", "absolute-local"],
)
def test_marketplace_rejects_unsafe_package_source(tmp_path: Path, source: str) -> None:
    """Every forbidden source shape fails the marketplace parse."""
    with pytest.raises(MarketplaceYmlError, match=r"packages\[0\]\.source"):
        load_marketplace_from_apm_yml(_marketplace_manifest(tmp_path, source))


@pytest.mark.req("req-mf-017")
@pytest.mark.parametrize("source", ["./tools/a", "acme/tool", "https://example.com/acme/tool.git"])
def test_marketplace_accepts_safe_package_source(tmp_path: Path, source: str) -> None:
    """A ./ local source and a plain https remote source parse."""
    config = load_marketplace_from_apm_yml(_marketplace_manifest(tmp_path, source))
    assert [entry.name for entry in config.packages] == ["tool-a"]


# --- req-mf-018: policy.hash_algorithm allow-list ------------------------


# Passing allow-list clause of req-mf-018. Unmarked so the requirement reports
# the inference xfail below until it holds.
@pytest.mark.parametrize("algorithm", ["md5", "sha1", "sha224", "blake2b"])
def test_weak_or_unknown_policy_hash_algorithm_is_rejected(algorithm: str) -> None:
    with pytest.raises(ProjectPolicyConfigError, match=f"'{algorithm}' is not supported"):
        parse_project_policy_hash_pin({"hash_algorithm": algorithm, "hash": "a" * 64})


@pytest.mark.parametrize(("algorithm", "length"), [("sha256", 64), ("sha384", 96), ("sha512", 128)])
def test_strong_policy_hash_algorithms_are_accepted(algorithm: str, length: int) -> None:
    pin = parse_project_policy_hash_pin({"hash_algorithm": algorithm, "hash": "b" * length})
    assert (pin.algorithm, pin.digest) == (algorithm, "b" * length)


@pytest.mark.req("req-mf-018")
@pytest.mark.xfail(strict=True, reason="#4253: algorithm is not inferred from the digest prefix")
@pytest.mark.parametrize(("algorithm", "length"), [("sha384", 96), ("sha512", 128)])
def test_policy_hash_algorithm_is_inferred_from_digest_prefix(algorithm: str, length: int) -> None:
    pin = parse_project_policy_hash_pin({"hash": f"{algorithm}:" + "c" * length})
    assert (pin.algorithm, pin.digest) == (algorithm, "c" * length)
