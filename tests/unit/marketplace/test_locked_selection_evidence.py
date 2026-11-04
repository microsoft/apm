"""Pure locked-selection proofs shared by marketplace install and audit."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.drift import detect_ref_change, marketplace_ref_mismatch
from apm_cli.install.phases.lockfile import LockfileBuilder
from apm_cli.install.plan import lockfile_satisfies_manifest
from apm_cli.models.dependency import DependencyReference

pytestmark = pytest.mark.unit


def _selection(version: str = "^1.0.0") -> DependencyReference:
    return DependencyReference.parse_from_dict(
        {"name": "tool", "marketplace": "catalog", "version": version}
    )


def _locked(**changes: object) -> LockedDependency:
    return LockedDependency(
        **{
            "repo_url": "owner/package",
            "discovered_via": "catalog",
            "marketplace_plugin_name": "tool",
            "resolved_commit": "a" * 40,
            "resolved_ref": "arbitrary/tool@1.2.3",
            "resolved_tag": "arbitrary/tool@1.2.3",
            "version": "1.2.3",
            "constraint": "^1.0.0",
            "resolved_at": "2026-10-20T19:26:55+00:00",
            **changes,
        }
    )


@pytest.mark.parametrize(
    ("spec", "changes", "valid"),
    [
        ("^1.0.0", {}, True),
        ("~1.2.0", {}, True),
        ("1.2.3", {}, True),
        ("^2.0.0", {}, False),
        ("^1.0.0", {"version": "2.0.0"}, False),
        ("^1.0.0", {"version": None}, False),
        ("^1.0.0", {"constraint": None}, False),
        ("^1.0.0", {"resolved_tag": None}, False),
        ("^1.0.0", {"resolved_ref": "other-tag"}, False),
        ("^1.0.0", {"resolved_commit": "short"}, False),
        ("main", {"resolved_ref": "main"}, True),
        ("other", {"version": "other"}, False),
        ("a" * 40, {}, True),
        ("b" * 40, {"version": "b" * 40}, False),
    ],
)
def test_install_and_audit_share_locked_selection(
    spec: str, changes: dict[str, object], valid: bool
) -> None:
    """All consumers use the selected evidence, never the selector or inventory."""
    declaration = _selection(spec)
    locked = _locked(**changes)
    lock = LockFile(dependencies={locked.get_unique_key(): locked})
    assert (marketplace_ref_mismatch(declaration, locked) is None) is valid
    assert detect_ref_change(declaration, locked) is not valid
    assert lockfile_satisfies_manifest(lock, [declaration])[0] is valid


def test_lookup_refreshes_after_mutation_and_rejects_placeholder_bypass() -> None:
    """Mutable lockfiles cannot retain a stale unique provenance answer."""
    selected = _locked()
    key = _selection().get_unique_key()
    lock = LockFile(dependencies={selected.get_unique_key(): selected})
    assert lock.get_dependency(key) is selected
    extra = replace(selected, repo_url="owner/other")
    lock.dependencies[extra.get_unique_key()] = extra
    assert lock.get_dependency(key) is None
    del lock.dependencies[extra.get_unique_key()]
    assert lock.get_dependency(key) is selected
    lock.dependencies.clear()
    lock.dependencies[key] = LockedDependency(repo_url=key)
    assert lock.get_dependency(key) is None


@pytest.mark.parametrize("missing", [True, False])
def test_replay_diagnostic_names_declared_marketplace(missing: bool) -> None:
    """Frozen install and audit replay must not expose the internal lookup key."""
    locked = _locked(version="2.0.0")
    lock = LockFile(dependencies={} if missing else {locked.get_unique_key(): locked})
    satisfied, reasons = lockfile_satisfies_manifest(lock, [_selection()])
    assert not satisfied
    assert len(reasons) == 1
    assert "tool@catalog" in reasons[0]
    assert "_marketplace/" not in reasons[0]


@pytest.mark.parametrize("changed_field", [None, "resolved_commit", "resolved_ref"])
def test_rebuilt_lock_carries_selected_version_only_for_same_pin(
    changed_field: str | None,
) -> None:
    """Discovery can survive an update, but old selected-version proof cannot."""
    previous = _locked()
    current = replace(
        previous, constraint=None, resolved_tag=None, version="9.9.9", resolved_at=None
    )
    if changed_field:
        setattr(current, changed_field, "b" * 40 if changed_field == "resolved_commit" else "main")
    lock = LockFile(dependencies={current.get_unique_key(): current})
    context = SimpleNamespace(
        existing_lockfile=LockFile(dependencies={previous.get_unique_key(): previous}),
        marketplace_provenance=None,
    )
    LockfileBuilder(context)._attach_marketplace_provenance(lock)
    assert current.discovered_via == "catalog"
    assert current.version == ("9.9.9" if changed_field else "1.2.3")
    assert current.constraint == (None if changed_field else "^1.0.0")
    assert current.resolved_tag == (None if changed_field else previous.resolved_tag)
    assert current.resolved_at == (None if changed_field else previous.resolved_at)


@pytest.mark.parametrize(
    "changes",
    [
        {},
        {"port": 8443},
        {"resolved_commit": "b" * 40},
        {"resolved_ref": "arbitrary/tool@1.2.4"},
        {"resolved_tag": "arbitrary/tool@1.2.4"},
        {"constraint": "~1.2.0"},
        {"version": "1.2.4"},
        {"discovered_via": "other-catalog"},
        {"marketplace_plugin_name": "other-tool"},
        {"source_url": "https://catalog.example.test/marketplace.json"},
        {"source_digest": "sha256:" + "b" * 64},
        pytest.param({"source": "local"}, id="changed-source"),
        pytest.param({"host_type": "gitlab"}, id="changed-host-type"),
    ],
)
def test_marketplace_rediscovery_preserves_time_only_for_unchanged_selection(
    changes: dict[str, object],
) -> None:
    """A no-op discovery retains pin age, but changed selection evidence does not."""
    previous = _locked()
    current = replace(previous, **changes)
    refreshed_at = "2026-10-21T15:00:00+00:00"
    provenance = {
        field: getattr(current, field)
        for field in (
            "discovered_via",
            "marketplace_plugin_name",
            "source_url",
            "source_digest",
            "constraint",
            "resolved_tag",
        )
    }
    provenance.update(resolved_version=current.version, resolved_at=refreshed_at)
    lock = LockFile(dependencies={current.get_unique_key(): current})
    context = SimpleNamespace(
        existing_lockfile=LockFile(dependencies={previous.get_unique_key(): previous}),
        marketplace_provenance={current.get_unique_key(): provenance},
    )

    LockfileBuilder(context)._attach_marketplace_provenance(lock)

    assert current.resolved_at == (refreshed_at if changes else previous.resolved_at)
    assert previous.resolved_at == "2026-10-20T19:26:55+00:00"


@pytest.mark.parametrize("missing", ["resolved_commit", "resolved_at"])
def test_marketplace_rediscovery_does_not_reuse_incomplete_timestamp_evidence(
    missing: str,
) -> None:
    """An incomplete prior pin cannot preserve a historical resolution time."""
    previous = _locked(**{missing: None})
    current = replace(previous)
    refreshed_at = "2026-10-21T15:00:00+00:00"
    context = SimpleNamespace(
        existing_lockfile=LockFile(dependencies={previous.get_unique_key(): previous}),
        marketplace_provenance={
            current.get_unique_key(): {
                "discovered_via": current.discovered_via,
                "marketplace_plugin_name": current.marketplace_plugin_name,
                "constraint": current.constraint,
                "resolved_tag": current.resolved_tag,
                "resolved_version": current.version,
                "resolved_at": refreshed_at,
            }
        },
    )
    lock = LockFile(dependencies={current.get_unique_key(): current})

    LockfileBuilder(context)._attach_marketplace_provenance(lock)

    assert current.resolved_at == refreshed_at
