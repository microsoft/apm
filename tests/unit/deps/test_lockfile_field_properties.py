"""Generated/metamorphic round-trip properties for every lockfile-consumed field.

Complements ``test_lockfile_consumer_contract.py`` (fixed literal examples) with
hypothesis-generated coverage: field-complete round trips, permutation/order
determinism, omission-when-falsy behavior, and fail-closed negative cases for
malformed or conflicting lock state.

Canonical-owner law (unchanged by these tests):
- ``LockedDependency`` (``apm_cli.deps.lockfile``) owns persisted lock state.
- ``DependencyReference`` / ``ProviderCoordinateMixin`` own transient
  provider-coordinate derivation (Azure DevOps ``ado_organization`` /
  ``ado_project`` / ``ado_repo``) -- these never persist in ``to_dict()``.

Every property below drives the real production functions (``to_dict``,
``from_dict``, ``to_dependency_ref``, ``canonical_ado_coordinates``,
``with_derived_provider_coordinates``, ``validate_provider_coordinates``);
none of them re-implement URL parsing or normalization.
"""

from __future__ import annotations

import string
from dataclasses import fields
from typing import Any

import pytest
from hypothesis import given, seed, settings
from hypothesis import strategies as st

from apm_cli.core.host_providers import accepted_host_types
from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.models.dependency.provider_coordinates import ProviderCoordinateMixin
from apm_cli.models.dependency.reference import DependencyReference

pytestmark = pytest.mark.unit

PROPERTY_SEED = 0xA9C2027
PROPERTY_PROFILE = settings(max_examples=25, deadline=None, database=None, print_blob=True)
SINGLE_EXAMPLE_PROFILE = settings(max_examples=1, deadline=None, database=None, print_blob=True)

_TOKEN = st.text(alphabet=string.ascii_lowercase + string.digits, min_size=2, max_size=8)
_SHA = st.text(alphabet="0123456789abcdef", min_size=40, max_size=40)

_ALLOWED_EXEC_STATUS = ("deployed", "gated_pending_approval", "denied", "absent")
# Sourced from the real provider registry (not a hand-maintained literal) so this
# strategy can never drift from what ``_normalize_lockfile_host_type`` accepts.
_ALLOWED_HOST_TYPES = tuple(accepted_host_types())
_DERIVED_PROVIDER_FIELDS = ("ado_organization", "ado_project", "ado_repo")

# Optional string-or-None fields whose to_dict() contract is "omitted when
# falsy, present with its exact value otherwise". Named directly (as the
# sibling contract test does) -- this states the persisted-field vocabulary,
# not any parsing/normalization behavior.
_OPTIONAL_STRING_FIELDS = (
    "host",
    "host_type",
    "registry_prefix",
    "resolved_commit",
    "resolved_ref",
    "version",
    "virtual_path",
    "resolved_by",
    "package_type",
    "source",
    "local_path",
    "declaring_parent",
    "anchored_local_path",
    "content_hash",
    "discovered_via",
    "marketplace_plugin_name",
    "source_url",
    "source_digest",
    "resolved_url",
    "resolved_hash",
    "constraint",
    "resolved_tag",
    "resolved_at",
    "declared_license",
    "exec_status",
    "name",
)


def _repo_url(draw: st.DrawFn) -> str:
    return "/".join(draw(st.lists(_TOKEN, min_size=2, max_size=2)))


@st.composite
def locked_dependency_kwargs(draw: st.DrawFn) -> dict[str, Any]:
    """Generate a valid, field-complete kwargs dict for ``LockedDependency``.

    Order-sensitive fields (``deployed_files``, ``skill_subset``,
    ``target_subset``) are generated already deduped and sorted, matching
    what ``to_dict()``/``from_dict()`` always converge to -- see the
    dedicated permutation-invariance property for the order-determinism
    guarantee itself.
    """

    def maybe(default: Any, value: st.SearchStrategy[Any]) -> Any:
        return draw(st.one_of(st.just(default), value))

    kwargs: dict[str, Any] = {"repo_url": _repo_url(draw)}
    kwargs["host"] = maybe(None, st.sampled_from(["gitlab.example.invalid", "git.example.invalid"]))
    kwargs["host_type"] = maybe(None, st.just("gitlab"))
    kwargs["port"] = maybe(None, st.integers(min_value=1, max_value=65535))
    kwargs["registry_prefix"] = maybe(None, st.sampled_from(["registry/git", "artifactory/github"]))
    kwargs["resolved_commit"] = maybe(None, _SHA)
    kwargs["resolved_ref"] = maybe(None, _TOKEN)
    kwargs["version"] = maybe(None, st.sampled_from(["1.0.0", "2.3.4", "0.9.1"]))
    kwargs["virtual_path"] = maybe(
        None, st.sampled_from(["skills/alpha", "prompts/beta.prompt.md"])
    )
    kwargs["is_virtual"] = draw(st.booleans())
    kwargs["depth"] = draw(st.integers(min_value=0, max_value=5))
    kwargs["resolved_by"] = maybe(None, st.just("group/parent"))
    kwargs["package_type"] = maybe(None, st.sampled_from(["skill_bundle", "agent"]))
    kwargs["deployed_files"] = draw(
        st.one_of(
            st.just([]),
            st.lists(
                st.sampled_from(["a/one.md", "b/two.md", "c/three.md"]),
                min_size=1,
                max_size=3,
                unique=True,
            ).map(sorted),
        )
    )
    kwargs["deployed_file_hashes"] = draw(
        st.one_of(
            st.just({}),
            st.dictionaries(
                st.sampled_from(["a/one.md", "b/two.md"]),
                _SHA.map(lambda s: f"sha256:{s}"),
                min_size=1,
                max_size=2,
            ),
        )
    )
    kwargs["source"] = maybe(None, st.sampled_from(["local", "registry", "git"]))
    kwargs["local_path"] = maybe(None, st.just("../sibling"))
    kwargs["declaring_parent"] = maybe(None, st.just("group/parent"))
    kwargs["anchored_local_path"] = maybe(None, st.just("/workspace/sibling"))
    kwargs["content_hash"] = maybe(None, _SHA.map(lambda s: f"sha256:{s}"))
    kwargs["is_dev"] = draw(st.booleans())
    kwargs["discovered_via"] = maybe(None, st.just("fixture-marketplace"))
    kwargs["marketplace_plugin_name"] = maybe(None, st.just("fixture-plugin"))
    kwargs["source_url"] = maybe(None, st.just("https://registry.example.invalid/fixture.json"))
    kwargs["source_digest"] = maybe(None, _SHA.map(lambda s: f"sha256:{s}"))
    kwargs["is_insecure"] = draw(st.booleans())
    kwargs["allow_insecure"] = draw(st.booleans())
    kwargs["skill_subset"] = draw(
        st.one_of(
            st.just([]), st.permutations(["alpha", "beta", "gamma"]).map(lambda p: sorted(p[:2]))
        )
    )
    kwargs["target_subset"] = draw(
        st.one_of(st.just([]), st.permutations(["copilot", "claude"]).map(sorted))
    )
    kwargs["resolved_url"] = maybe(None, st.just("https://registry.example.invalid/pkg.tgz"))
    kwargs["resolved_hash"] = maybe(None, _SHA.map(lambda s: f"sha256:{s}"))
    kwargs["constraint"] = maybe(None, st.just("^1.0.0"))
    kwargs["resolved_tag"] = maybe(None, st.just("v1.2.3"))
    kwargs["resolved_at"] = maybe(None, st.just("2026-01-01T00:00:00+00:00"))
    kwargs["declared_license"] = maybe(None, st.just("MIT"))
    kwargs["exec_status"] = maybe(None, st.sampled_from(_ALLOWED_EXEC_STATUS))
    kwargs["name"] = maybe(None, st.just("consume-contract"))
    kwargs["_unknown_fields"] = draw(
        st.one_of(st.just({}), st.just({"future_consumer_field": {"enabled": True}}))
    )
    return kwargs


@st.composite
def ado_host_and_segments(draw: st.DrawFn) -> tuple[str, str, str, str]:
    """Generate an Azure DevOps host with a valid 3-segment repo path."""
    host = draw(st.sampled_from(["dev.azure.com", "ssh.dev.azure.com", "myorg.visualstudio.com"]))
    org, project, repo = draw(st.lists(_TOKEN, min_size=3, max_size=3, unique=True))
    return host, org, project, repo


# --------------------------------------------------------------------------
# 1. Generic field-complete round trip + idempotent re-serialization
# --------------------------------------------------------------------------


@seed(PROPERTY_SEED)
@PROPERTY_PROFILE
@given(kwargs=locked_dependency_kwargs())
def test_locked_dependency_round_trips_every_generated_field_combination(
    kwargs: dict[str, Any],
) -> None:
    """``to_dict`` -> ``from_dict`` must reproduce every declared field."""
    declared_fields = {f.name for f in fields(LockedDependency)}
    assert set(kwargs) == declared_fields

    dependency = LockedDependency(**kwargs)
    persisted = dependency.to_dict()
    restored = LockedDependency.from_dict(persisted)

    assert restored == dependency
    assert restored.to_dict() == persisted, "re-serializing a restored entry must be a fixpoint"


@seed(PROPERTY_SEED)
@SINGLE_EXAMPLE_PROFILE
@given(kwargs=locked_dependency_kwargs())
def test_round_trip_property_breaks_if_from_dict_ignores_a_field(
    kwargs: dict[str, Any],
) -> None:
    """Negative twin: dropping one field in ``from_dict`` must break the round trip."""
    kwargs["declared_license"] = "MIT"
    dependency = LockedDependency(**kwargs)
    persisted = dependency.to_dict()
    original_from_dict = LockedDependency.from_dict

    def from_dict_ignoring_license(data: dict[str, Any]) -> LockedDependency:
        data = dict(data)
        data.pop("declared_license", None)
        return original_from_dict(data)

    restored = from_dict_ignoring_license(persisted)
    with pytest.raises(AssertionError):
        assert restored == dependency


# --------------------------------------------------------------------------
# 2. Optional-field omission-when-falsy contract
# --------------------------------------------------------------------------


@seed(PROPERTY_SEED)
@PROPERTY_PROFILE
@given(kwargs=locked_dependency_kwargs())
def test_optional_string_fields_are_present_iff_truthy(kwargs: dict[str, Any]) -> None:
    """Every optional string field is omitted when falsy, present when set."""
    dependency = LockedDependency(**kwargs)
    persisted = dependency.to_dict()

    for field_name in _OPTIONAL_STRING_FIELDS:
        value = kwargs[field_name]
        if value:
            assert persisted.get(field_name) == value, field_name
        else:
            assert field_name not in persisted, field_name


# --------------------------------------------------------------------------
# 3. Port defensive cast fails closed to None on malformed input
# --------------------------------------------------------------------------


@seed(PROPERTY_SEED)
@PROPERTY_PROFILE
@given(
    raw_port=st.one_of(
        st.text(min_size=1, max_size=6).filter(lambda s: not s.strip().lstrip("-").isdigit()),
        st.integers(max_value=0),
        st.integers(min_value=65536, max_value=1_000_000),
        st.none(),
    )
)
def test_malformed_port_fails_closed_to_none(raw_port: Any) -> None:
    """Any malformed/out-of-range port silently drops to ``None``, never raises."""
    restored = LockedDependency.from_dict({"repo_url": "acme/example", "port": raw_port})
    assert restored.port is None


@seed(PROPERTY_SEED)
@PROPERTY_PROFILE
@given(
    raw_port=st.one_of(
        st.integers(min_value=1, max_value=65535),
        st.integers(min_value=1, max_value=65535).map(str),
    )
)
def test_valid_port_survives_round_trip(raw_port: Any) -> None:
    """A well-formed port (int or numeric string) is preserved exactly."""
    restored = LockedDependency.from_dict({"repo_url": "acme/example", "port": raw_port})
    assert restored.port == int(raw_port)
    assert restored.to_dict()["port"] == int(raw_port)


# --------------------------------------------------------------------------
# 4. host_type / exec_status fail closed via exception (not silent drop)
# --------------------------------------------------------------------------


_BAD_EXEC_STATUS = st.text(
    alphabet=string.ascii_letters + string.digits, min_size=1, max_size=12
).filter(lambda s: s not in _ALLOWED_EXEC_STATUS)


def _assert_exec_status_fails_closed(bad_value: str) -> None:
    """Assert an invalid ``exec_status`` raises the fail-closed ValueError."""
    try:
        LockedDependency.from_dict({"repo_url": "acme/example", "exec_status": bad_value})
    except ValueError as exc:
        assert "Unsupported lockfile exec_status" in str(exc)
        return
    raise AssertionError("invalid exec_status did not raise")


@seed(PROPERTY_SEED)
@PROPERTY_PROFILE
@given(bad_value=_BAD_EXEC_STATUS)
def test_unsupported_exec_status_raises_instead_of_silently_coercing(bad_value: str) -> None:
    """Unlike ``port``, an invalid ``exec_status`` must fail loudly."""
    _assert_exec_status_fails_closed(bad_value)


@seed(PROPERTY_SEED)
@SINGLE_EXAMPLE_PROFILE
@given(bad_value=_BAD_EXEC_STATUS)
def test_exec_status_fail_closed_property_breaks_if_normalizer_is_bypassed(bad_value: str) -> None:
    """Negative twin: skipping ``_normalize_exec_status`` un-does the fail-closed guarantee."""
    import apm_cli.deps.lockfile as lockfile_module

    original = lockfile_module._normalize_exec_status
    try:
        lockfile_module._normalize_exec_status = lambda raw: raw
        with pytest.raises(AssertionError):
            _assert_exec_status_fails_closed(bad_value)
    finally:
        lockfile_module._normalize_exec_status = original


_BAD_HOST_TYPE = st.text(
    alphabet=string.ascii_letters + string.digits, min_size=1, max_size=12
).filter(lambda s: s.lower() not in _ALLOWED_HOST_TYPES)


def _assert_host_type_fails_closed(bad_value: str) -> None:
    """Assert an invalid ``host_type`` raises the fail-closed ValueError."""
    try:
        LockedDependency.from_dict({"repo_url": "acme/example", "host_type": bad_value})
    except ValueError as exc:
        assert "Unsupported lockfile host_type" in str(exc)
        return
    raise AssertionError("invalid host_type did not raise")


@seed(PROPERTY_SEED)
@PROPERTY_PROFILE
@given(bad_value=_BAD_HOST_TYPE)
def test_unsupported_host_type_raises_instead_of_silently_coercing(bad_value: str) -> None:
    """Unlike ``port``, an invalid ``host_type`` must fail loudly (not silently coerce)."""
    _assert_host_type_fails_closed(bad_value)


@seed(PROPERTY_SEED)
@SINGLE_EXAMPLE_PROFILE
@given(bad_value=_BAD_HOST_TYPE)
def test_host_type_fail_closed_property_breaks_if_normalizer_is_bypassed(bad_value: str) -> None:
    """Negative twin: skipping ``_normalize_lockfile_host_type`` un-does the fail-closed guarantee."""
    import apm_cli.deps.lockfile as lockfile_module

    original = lockfile_module._normalize_lockfile_host_type
    try:
        lockfile_module._normalize_lockfile_host_type = lambda raw: raw
        with pytest.raises(AssertionError):
            _assert_host_type_fails_closed(bad_value)
    finally:
        lockfile_module._normalize_lockfile_host_type = original


def test_empty_string_exec_status_fails_closed_with_its_own_message() -> None:
    """An empty ``exec_status`` is a distinct fail-closed branch from ``_BAD_EXEC_STATUS``.

    ``_BAD_EXEC_STATUS`` above only generates non-empty ASCII text, so it never
    exercises the ``not raw.strip()`` half of the guard. Mutating that guard's
    ``or`` to ``and`` still raises for an empty string (it falls through to the
    "unsupported value" branch instead) -- only the exact message distinguishes
    the two branches, so this asserts on it exactly.
    """
    with pytest.raises(ValueError) as exc_info:
        LockedDependency.from_dict({"repo_url": "acme/example", "exec_status": ""})

    assert str(exc_info.value) == "lockfile exec_status must be a non-empty string"


def test_empty_string_host_type_fails_closed_with_its_own_message() -> None:
    """An empty ``host_type`` is a distinct fail-closed branch from ``_BAD_HOST_TYPE``.

    Mirrors ``test_empty_string_exec_status_fails_closed_with_its_own_message``
    for the sibling normalizer -- see that test for why the empty-string case
    needs its own exact assertion.
    """
    with pytest.raises(ValueError) as exc_info:
        LockedDependency.from_dict({"repo_url": "acme/example", "host_type": ""})

    assert str(exc_info.value) == "lockfile host_type must be a non-empty string"


# --------------------------------------------------------------------------
# 5 & 6. ADO transient coordinate derivation: round trip, never persisted,
#         idempotent, and host-gated (not repo_url-shape-gated).
# --------------------------------------------------------------------------


@seed(PROPERTY_SEED)
@PROPERTY_PROFILE
@given(host_and_segments=ado_host_and_segments())
def test_ado_coordinates_derive_round_trip_and_never_persist(
    host_and_segments: tuple[str, str, str, str],
) -> None:
    """ADO coordinates derive from host+repo_url, round trip, and stay transient."""
    host, org, project, repo = host_and_segments
    repo_url = f"{org}/{project}/{repo}"

    parsed = DependencyReference(repo_url=repo_url, host=host)
    derived = parsed.with_derived_provider_coordinates()
    assert (derived.ado_organization, derived.ado_project, derived.ado_repo) == (org, project, repo)
    derived.validate_provider_coordinates()  # must not raise

    locked = LockedDependency.from_dependency_ref(
        derived, resolved_commit="a" * 40, depth=1, resolved_by=None
    )
    persisted = locked.to_dict()
    assert all(field_name not in persisted for field_name in _DERIVED_PROVIDER_FIELDS)

    restored = LockedDependency.from_dict(persisted)
    reconstructed = restored.to_dependency_ref()
    assert (reconstructed.ado_organization, reconstructed.ado_project, reconstructed.ado_repo) == (
        org,
        project,
        repo,
    )
    # Idempotence: deriving twice must be a fixpoint.
    twice = reconstructed.with_derived_provider_coordinates()
    assert (twice.ado_organization, twice.ado_project, twice.ado_repo) == (
        reconstructed.ado_organization,
        reconstructed.ado_project,
        reconstructed.ado_repo,
    )


@seed(PROPERTY_SEED)
@SINGLE_EXAMPLE_PROFILE
@given(host_and_segments=ado_host_and_segments())
def test_ado_derivation_property_breaks_if_canonical_coordinates_are_swapped(
    host_and_segments: tuple[str, str, str, str],
) -> None:
    """Negative twin: a project/repo swap bug must break the derivation property."""
    host, org, project, repo = host_and_segments
    repo_url = f"{org}/{project}/{repo}"
    original = DependencyReference.canonical_ado_coordinates.__func__

    def swapped(
        cls: type, host_arg: str | None, repo_url_arg: str
    ) -> tuple[str | None, str | None, str | None]:
        result = original(cls, host_arg, repo_url_arg)
        if result == (None, None, None):
            return result
        organization, proj, repository = result
        return organization, repository, proj  # bug: swap project/repo

    def _assert_derivation_matches_segments() -> None:
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(DependencyReference, "canonical_ado_coordinates", classmethod(swapped))
            parsed = DependencyReference(repo_url=repo_url, host=host)
            derived = parsed.with_derived_provider_coordinates()
            assert (derived.ado_organization, derived.ado_project, derived.ado_repo) == (
                org,
                project,
                repo,
            )

    with pytest.raises(AssertionError):
        _assert_derivation_matches_segments()


@seed(PROPERTY_SEED)
@PROPERTY_PROFILE
@given(
    host=st.sampled_from(["github.com", "gitlab.example.invalid", "bitbucket.example.invalid"]),
    org=_TOKEN,
    project=_TOKEN,
    repo=_TOKEN,
)
def test_non_ado_hosts_never_derive_ado_coordinates_regardless_of_repo_shape(
    host: str, org: str, project: str, repo: str
) -> None:
    """Non-ADO hosts yield ``(None, None, None)`` even with a 3-segment path."""
    repo_url = f"{org}/{project}/{repo}"
    coordinates = DependencyReference.canonical_ado_coordinates(host, repo_url)
    assert coordinates == (None, None, None)

    reconstructed = DependencyReference(
        repo_url=repo_url, host=host
    ).with_derived_provider_coordinates()
    assert (reconstructed.ado_organization, reconstructed.ado_project, reconstructed.ado_repo) == (
        None,
        None,
        None,
    )


# --------------------------------------------------------------------------
# 7. Mismatched explicit ado_* fields always fail closed (combinatorial)
# --------------------------------------------------------------------------


@seed(PROPERTY_SEED)
@PROPERTY_PROFILE
@given(
    host_and_segments=ado_host_and_segments(),
    corrupt_field=st.sampled_from(_DERIVED_PROVIDER_FIELDS),
    replacement=_TOKEN,
)
def test_any_single_mismatched_ado_field_fails_closed(
    host_and_segments: tuple[str, str, str, str],
    corrupt_field: str,
    replacement: str,
) -> None:
    """Corrupting any one of the three explicit ADO fields must raise."""
    host, org, project, repo = host_and_segments
    canonical = {"ado_organization": org, "ado_project": project, "ado_repo": repo}
    if replacement == canonical[corrupt_field]:
        replacement = replacement + "x"
    corrupted = dict(canonical)
    corrupted[corrupt_field] = replacement

    reference = DependencyReference(
        repo_url=f"{org}/{project}/{repo}",
        host=host,
        **corrupted,
    )
    with pytest.raises(ValueError, match=r"Run `apm install <original-ado-url>`"):
        reference.validate_provider_coordinates()


@seed(PROPERTY_SEED)
@PROPERTY_PROFILE
@given(host_and_segments=ado_host_and_segments())
def test_matching_explicit_ado_fields_never_raise(
    host_and_segments: tuple[str, str, str, str],
) -> None:
    """The canonical (matching) triple is always accepted -- no false positives."""
    host, org, project, repo = host_and_segments
    reference = DependencyReference(
        repo_url=f"{org}/{project}/{repo}",
        host=host,
        ado_organization=org,
        ado_project=project,
        ado_repo=repo,
    )
    reference.validate_provider_coordinates()  # must not raise


# --------------------------------------------------------------------------
# 8. Retired/foreign ado_* keys in raw lock data never resurrect
# --------------------------------------------------------------------------


@seed(PROPERTY_SEED)
@PROPERTY_PROFILE
@given(
    host_and_segments=ado_host_and_segments(),
    injected=st.fixed_dictionaries(
        {
            "ado_organization": _TOKEN,
            "ado_project": _TOKEN,
            "ado_repo": _TOKEN,
        }
    ),
)
def test_retired_ado_keys_in_raw_lock_data_never_resurrect(
    host_and_segments: tuple[str, str, str, str],
    injected: dict[str, str],
) -> None:
    """Whether or not injected ado_* keys match canonical identity, they never persist."""
    host, org, project, repo = host_and_segments
    raw = {
        "repo_url": f"{org}/{project}/{repo}",
        "host": host,
        "resolved_ref": "main",
        **injected,
    }

    locked = LockedDependency.from_dict(raw)
    reserialized = locked.to_dict()
    reconstructed = locked.to_dependency_ref()

    assert all(field_name not in reserialized for field_name in _DERIVED_PROVIDER_FIELDS)
    assert (reconstructed.ado_organization, reconstructed.ado_project, reconstructed.ado_repo) == (
        org,
        project,
        repo,
    )


@seed(PROPERTY_SEED)
@SINGLE_EXAMPLE_PROFILE
@given(host_and_segments=ado_host_and_segments())
def test_retired_key_property_breaks_if_transient_guard_is_disabled(
    host_and_segments: tuple[str, str, str, str],
) -> None:
    """Negative twin: disabling the transient-field guard lets ado_* resurrect."""
    host, org, project, repo = host_and_segments
    raw = {
        "repo_url": f"{org}/{project}/{repo}",
        "host": host,
        "resolved_ref": "main",
        "ado_organization": org,
        "ado_project": project,
        "ado_repo": repo,
    }

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            ProviderCoordinateMixin,
            "is_transient_provider_field",
            staticmethod(lambda field_name: False),
        )
        locked = LockedDependency.from_dict(raw)
        reserialized = locked.to_dict()
        with pytest.raises(AssertionError):
            assert all(field_name not in reserialized for field_name in _DERIVED_PROVIDER_FIELDS)


# --------------------------------------------------------------------------
# 9. alias is never part of persisted lock state (declared-manifest surface only)
# --------------------------------------------------------------------------


@seed(PROPERTY_SEED)
@PROPERTY_PROFILE
@given(
    alias=st.text(alphabet=string.ascii_letters + string.digits + "._-", min_size=1, max_size=12)
)
def test_alias_never_persists_into_lock_state(alias: str) -> None:
    """``alias`` is a declared-manifest concept; it must never reach the lockfile."""
    dep_ref = DependencyReference(
        repo_url="acme/example", host="gitlab.example.invalid", alias=alias
    )

    locked = LockedDependency.from_dependency_ref(
        dep_ref, resolved_commit="a" * 40, depth=1, resolved_by=None
    )
    persisted = locked.to_dict()

    assert "alias" not in persisted
    assert not any("alias" in key for key in persisted)
    reconstructed = locked.to_dependency_ref()
    assert reconstructed.alias is None


@seed(PROPERTY_SEED)
@SINGLE_EXAMPLE_PROFILE
@given(
    alias=st.text(alphabet=string.ascii_letters + string.digits + "._-", min_size=1, max_size=12)
)
def test_alias_never_persists_property_breaks_if_alias_leaks_into_unknown_fields(
    alias: str,
) -> None:
    """Negative twin: a leaked alias in ``_unknown_fields`` must break the guard."""
    dep_ref = DependencyReference(
        repo_url="acme/example", host="gitlab.example.invalid", alias=alias
    )
    original = LockedDependency.from_dependency_ref

    def leaking_from_dependency_ref(*args: Any, **kwargs: Any) -> LockedDependency:
        locked = original(*args, **kwargs)
        locked._unknown_fields = {**locked._unknown_fields, "alias": dep_ref.alias}
        return locked

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            LockedDependency,
            "from_dependency_ref",
            classmethod(lambda cls, *a, **k: leaking_from_dependency_ref(*a, **k)),
        )
        locked = LockedDependency.from_dependency_ref(
            dep_ref, resolved_commit="a" * 40, depth=1, resolved_by=None
        )
        persisted = locked.to_dict()
        with pytest.raises(AssertionError):
            assert "alias" not in persisted


# --------------------------------------------------------------------------
# 10. skill_subset / target_subset / deployed_files: permutation-invariant order
# --------------------------------------------------------------------------


@seed(PROPERTY_SEED)
@PROPERTY_PROFILE
@given(
    skill_permutation=st.permutations(["alpha", "beta", "gamma"]),
    target_permutation=st.permutations(["copilot", "claude", "vscode"]),
)
def test_subset_persistence_is_permutation_invariant(
    skill_permutation: tuple[str, ...], target_permutation: tuple[str, ...]
) -> None:
    """Any input order of the same subset must persist identically (sorted)."""
    dep_ref = DependencyReference(
        repo_url="acme/example",
        host="gitlab.example.invalid",
        skill_subset=list(skill_permutation),
        target_subset=list(target_permutation),
    )

    locked = LockedDependency.from_dependency_ref(
        dep_ref, resolved_commit="a" * 40, depth=1, resolved_by=None
    )
    persisted = locked.to_dict()

    assert persisted["skill_subset"] == sorted(skill_permutation)
    assert persisted["target_subset"] == sorted(target_permutation)

    reconstructed = LockedDependency.from_dict(persisted).to_dependency_ref()
    assert reconstructed.skill_subset == sorted(skill_permutation)
    assert reconstructed.target_subset == sorted(target_permutation)


@pytest.mark.parametrize(
    "skill_permutation", [["gamma", "beta", "alpha"], ["beta", "gamma", "alpha"]]
)
def test_subset_permutation_invariance_breaks_if_sort_is_dropped(
    skill_permutation: list[str],
) -> None:
    """Negative twin: forgetting to sort in ``to_dict`` must break permutation invariance.

    ``to_dict`` is the true owner of the write-time canonical order (it
    unconditionally re-sorts, even if ``skill_subset`` was already sorted by
    ``from_dependency_ref``), so the mutation must target ``to_dict`` itself.
    """
    assert list(skill_permutation) != sorted(skill_permutation), (
        "fixture must be a non-sorted permutation"
    )
    locked = LockedDependency(
        repo_url="acme/example", resolved_commit="a" * 40, skill_subset=list(skill_permutation)
    )
    original_to_dict = LockedDependency.to_dict

    def unsorted_to_dict(self: LockedDependency) -> dict[str, Any]:
        result = original_to_dict(self)
        result["skill_subset"] = list(skill_permutation)  # bug: drop the sort
        return result

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(LockedDependency, "to_dict", unsorted_to_dict)
        persisted = locked.to_dict()
        with pytest.raises(AssertionError):
            assert persisted.get("skill_subset") == sorted(skill_permutation)


# --------------------------------------------------------------------------
# 11. Registry vs git-semver reference-selection branch in to_dependency_ref()
# --------------------------------------------------------------------------


@seed(PROPERTY_SEED)
@PROPERTY_PROFILE
@given(
    source=st.sampled_from([None, "git", "registry"]),
    resolved_ref=st.one_of(st.none(), _TOKEN),
    version=st.one_of(st.none(), st.sampled_from(["1.0.0", "2.0.0"])),
)
def test_reconstructed_reference_prefers_locked_version_only_for_registry_source(
    source: str | None, resolved_ref: str | None, version: str | None
) -> None:
    """Registry sources with a locked version use it; every other case uses resolved_ref."""
    locked = LockedDependency(
        repo_url="acme/example", source=source, resolved_ref=resolved_ref, version=version
    )
    reconstructed = locked.to_dependency_ref()

    if source == "registry" and version:
        assert reconstructed.reference == version
    else:
        assert reconstructed.reference == resolved_ref


# --------------------------------------------------------------------------
# 12. Sanity: LockFile-level real-YAML round trip stays lossless for a
#     generated dependency (guards the full write()/read() path, not just
#     the in-memory dict layer).
# --------------------------------------------------------------------------


@seed(PROPERTY_SEED)
@PROPERTY_PROFILE
@given(kwargs=locked_dependency_kwargs())
def test_lockfile_yaml_round_trip_preserves_a_generated_dependency(kwargs: dict[str, Any]) -> None:
    """A generated dependency survives a real YAML write/parse cycle unchanged."""
    dependency = LockedDependency(**kwargs)
    lockfile = LockFile(generated_at="2026-01-01T00:00:00+00:00")
    lockfile.add_dependency(dependency)

    restored = LockFile.from_yaml(lockfile.to_yaml())

    assert restored.get_dependency(dependency.get_unique_key()) == dependency
