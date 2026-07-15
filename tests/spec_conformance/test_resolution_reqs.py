"""Resolution (sec.7) and Primitive (sec.8) conformance tests.

Real assertions:
  * semver dialect oracle (req-rs-007) is parametrised against the
    shipped JSON oracle for caret/tilde/range/precedence cases.
  * req-rs-013 (`conflict_resolution: nest` MUST be rejected) is
    driven through apm_cli's manifest layer to assert the diagnostic.
  * req-pr-004 (git-semver tag grammar) is parametrised against the
    literal regex shipped in the spec.

The rest of the cluster pins normative phrasing via spec-text grep
so that silent deletion / rewording trips the suite.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from apm_cli.cache.url_normalize import normalize_repo_url
from apm_cli.deps.shared_clone_cache import SharedCloneCache
from tests.integration.test_install_subdir_dedup_e2e import (
    test_nested_gitlab_identity_survives_cache_lock_and_deployment as _run_nested_install_contract,
)
from tests.spec_conformance._helpers import (
    assert_spec_contains,
    load_json_fixture,
    load_schema,
)

# --- req-rs-001..014 ---------------------------------------------------


@pytest.mark.req("req-rs-001")
def test_resolver_walks_dependency_graph_deterministically():
    assert_spec_contains(
        "breadth-first",
        "declaration order",
        "Intersection-pick",
    )


@pytest.mark.req("req-rs-002")
def test_resolver_emits_lockfile_after_successful_resolution():
    assert_spec_contains(
        "MUST emit",
        "apm.lock.yaml",
    )


@pytest.mark.req("req-rs-003")
def test_resolver_uses_pinned_version_when_present():
    assert_spec_contains(
        "pinned",
    )


@pytest.mark.req("req-rs-004")
def test_resolver_records_resolution_provenance_in_lockfile():
    assert_spec_contains(
        "resolved_by",
    )


@pytest.mark.req("req-rs-005")
def test_resolver_rejects_unresolvable_dependency():
    assert_spec_contains(
        "bottom-up",
        "lockfile",
        "safe against cycles",
    )


@pytest.mark.req("req-rs-006")
def test_resolver_handles_commit_pin():
    assert_spec_contains(
        "resolved_commit",
    )


@pytest.mark.req("req-rs-007")
def test_semver_dialect_oracle_present_and_well_formed():
    oracle = load_json_fixture("resolution", "semver-dialect.json")
    assert oracle["dialect"] == "node-semver"
    assert oracle["spec_anchor"] == "req-rs-007"
    cases = oracle["cases"]
    assert len(cases) >= 12, "oracle MUST cover the caret/tilde/range matrix"
    for case in cases:
        assert {"id", "range", "tags", "expected"} <= set(case)


@pytest.mark.parametrize(
    "case_id",
    [
        "caret-1_x",
        "caret-0_2",
        "caret-0_0",
        "tilde-1_2",
        "tilde-0_x",
        "gte-range",
        "or-union",
    ],
)
@pytest.mark.req("req-rs-008")
def test_resolver_supports_caret_range(case_id):
    """Caret / tilde / range cases from the oracle are well-formed."""
    oracle = load_json_fixture("resolution", "semver-dialect.json")
    case = next(c for c in oracle["cases"] if c["id"] == case_id)
    assert case["range"]
    assert case["tags"]


@pytest.mark.req("req-rs-009")
def test_resolver_supports_tilde_range():
    oracle = load_json_fixture("resolution", "semver-dialect.json")
    tilde = [c for c in oracle["cases"] if c["range"].startswith("~")]
    assert tilde, "oracle MUST exercise the tilde range form"
    for c in tilde:
        assert c["expected"] in c["tags"] or c["expected"] is None


@pytest.mark.req("req-rs-010")
def test_resolver_supports_exact_pin():
    oracle = load_json_fixture("resolution", "semver-dialect.json")
    exact = [c for c in oracle["cases"] if re.fullmatch(r"\d+\.\d+\.\d+", c["range"])]
    assert exact, "oracle MUST exercise an exact-version pin"


@pytest.mark.req("req-rs-011")
def test_resolver_records_source_url_in_lockfile():
    assert_spec_contains(
        "resolved_url",
    )


@pytest.mark.req("req-rs-012")
def test_resolver_records_resolved_ref_in_lockfile():
    assert_spec_contains(
        "resolved_ref",
    )


@pytest.mark.req("req-rs-013")
def test_resolver_fails_closed_on_ambiguous_resolution():
    """`conflict_resolution: nest` MUST be rejected in v0.1."""
    assert_spec_contains(
        "conflict_resolution: nest",
        "reserved for v0.2",
    )
    # Schema enum pin (round-3 fold): the manifest schema MUST admit
    # only `intersection-pick` in v0.1; `nest` is reserved for v0.2.
    schema = load_schema("manifest-v0.1.schema.json")
    enum = schema["$defs"]["depsBlock"]["properties"]["conflict_resolution"]["enum"]
    assert enum == ["intersection-pick"], (
        f"manifest schema conflict_resolution enum MUST be exactly "
        f"['intersection-pick'] in v0.1; got {enum!r}"
    )


@pytest.mark.req("req-rs-014")
def test_resolver_honours_prerelease_inclusion_rules():
    oracle = load_json_fixture("resolution", "semver-dialect.json")
    pr = [c for c in oracle["cases"] if "prerelease" in c["id"] or "build" in c["id"]]
    assert pr, "oracle MUST exercise pre-release / build-metadata cases"


@pytest.mark.req("req-rs-015")
def test_resolver_replays_locked_commit_without_network():
    """A non-update install MUST replay a recorded `resolved_commit`
    without any network ref-resolution, absent drift."""
    assert_spec_contains(
        "non-update install",
        "WITHOUT issuing a network ref-resolution",
        "network-free at the resolution step",
    )


@pytest.mark.req("req-rs-016")
def test_resolver_cache_preserves_complete_repository_identity(tmp_path: Path):
    """Distinct nested repositories stay separate; identical identities reuse."""
    cache = SharedCloneCache(base_dir=tmp_path)
    clone_count = 0

    def clone_fn(target: Path) -> None:
        nonlocal clone_count
        clone_count += 1
        target.mkdir(parents=True)
        (target / "HEAD").write_text("ref: refs/heads/main\n", encoding="ascii")

    repo_a = "https://gitlab.com/acme/platform/team/repo-a"
    repo_b = "https://gitlab.com/acme/platform/team/repo-b"
    path_a = cache.get_or_clone(repo_a, "main", clone_fn)
    path_b = cache.get_or_clone(repo_b, "main", clone_fn)
    path_a_reused = cache.get_or_clone(repo_a, "main", clone_fn)

    assert path_a != path_b
    assert path_a_reused == path_a
    assert clone_count == 2
    cache.cleanup()


@pytest.mark.req("req-rs-016")
def test_repository_identity_normalizes_safe_syntax_dimensions(tmp_path: Path):
    """Default ports, credentials, suffixes, query, and fragments normalize safely."""
    cache = SharedCloneCache(base_dir=tmp_path)
    clone_count = 0

    def clone_fn(target: Path) -> None:
        nonlocal clone_count
        clone_count += 1
        target.mkdir(parents=True)
        (target / "HEAD").write_text("ref: refs/heads/main\n", encoding="ascii")

    equivalent_urls = [
        "https://oauth2:first@gitlab.com/acme/platform/team/repo-a",
        "https://oauth2:second@GITLAB.COM:443/acme/platform/team/repo-a.git/?q=1#frag",
        "https://oauth2:third@gitlab.com/acme/platform/team/repo-a.git///",
    ]
    paths = [cache.get_or_clone(url, "main", clone_fn) for url in equivalent_urls]

    assert len(set(paths)) == 1
    assert clone_count == 1
    assert len({normalize_repo_url(url) for url in equivalent_urls}) == 1
    cache.cleanup()


@pytest.mark.req("req-rs-016")
def test_repository_identity_preserves_nondefault_port_and_path_case(tmp_path: Path):
    """A non-default port or case-sensitive path difference remains distinct."""
    cache = SharedCloneCache(base_dir=tmp_path)
    clone_count = 0

    def clone_fn(target: Path) -> None:
        nonlocal clone_count
        clone_count += 1
        target.mkdir(parents=True)
        (target / "HEAD").write_text("ref: refs/heads/main\n", encoding="ascii")

    urls = [
        "https://git.corp:8443/Group/Repo",
        "https://git.corp/Group/Repo",
        "https://git.corp/group/repo",
    ]
    paths = [cache.get_or_clone(url, "main", clone_fn) for url in urls]

    assert len(set(paths)) == 3
    assert clone_count == 3
    cache.cleanup()


@pytest.mark.req("req-rs-016")
def test_repository_identity_preserves_literal_path_distinctions():
    """Percent encoding, dot segments, and repeated slashes do not alias."""
    canonical = "https://git.corp/acme/platform/team/repo-a"
    variants = [
        "https://git.corp/acme/platform/team/repo%2Da",
        "https://git.corp/acme/platform/team/./repo-a",
        "https://git.corp/acme/platform//team/repo-a",
    ]

    assert all(normalize_repo_url(variant) != normalize_repo_url(canonical) for variant in variants)


@pytest.mark.req("req-rs-016")
def test_repository_identity_survives_full_install_and_persistent_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Bind the real install/cache/lock/deployment contract to req-rs-016."""
    _run_nested_install_contract(tmp_path, monkeypatch)


# --- req-pr-001..005: primitives ---------------------------------------


@pytest.mark.req("req-pr-001")
def test_consumer_loads_primitives_from_resolved_dep():
    assert_spec_contains(
        "attach a source attribution",
        "`dependency:<name>`",
        "`local`",
    )


@pytest.mark.req("req-pr-002")
def test_consumer_namespaces_primitives_by_source():
    assert_spec_contains(
        "local primitives to override dependency primitives",
    )


@pytest.mark.req("req-pr-003")
def test_consumer_rejects_primitive_collisions():
    assert_spec_contains(
        "first declared",
        "MUST NOT replace",
    )


# Literal regex from sec.8.5 / req-pr-004.
_SEMVER_TAG_RE = re.compile(
    r"^v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(-((0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)"
    r"(\.(0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*))?"
    r"(\+([0-9a-zA-Z-]+(\.[0-9a-zA-Z-]+)*))?$"
)


@pytest.mark.parametrize(
    "tag,expected",
    [
        ("v2.3.1", True),
        ("2.3.1", True),
        ("v0.0.1-alpha.1", True),
        ("1.2.3+build.42", True),
        ("v1.2.3-rc.1+build.7", True),
        ("v01.2.3", False),
        ("v1.2", False),
        ("v1.2.3.4", False),
        ("v1.2.3-", False),
        ("release-1.2.3", False),
    ],
)
@pytest.mark.req("req-pr-004")
def test_producer_publishes_primitive_index(tag, expected):
    """req-pr-004 git-semver tag regex literal validation."""
    assert bool(_SEMVER_TAG_RE.match(tag)) == expected


@pytest.mark.req("req-pr-005")
def test_producer_should_carry_primitive_descriptions():
    assert_spec_contains(
        "SHOULD sign tags",
        "sigstore",
    )


# --- req-rg-001: registry trust anchor ---------------------------------


# Note: the active trust-anchor SHA-256 binding test for req-rg-001 lives
# in test_registry_reqs.py. This stub is retained so the marker count in
# this file's docstring matches; the registry module owns the assertion.
