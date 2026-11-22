"""Pure locked-selection proofs shared by marketplace install and audit."""

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from apm_cli.deps.apm_resolver import APMDependencyResolver
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


def _registry_locked(**changes: object) -> LockedDependency:
    return LockedDependency(
        **{
            "repo_url": "owner/package",
            "discovered_via": "catalog",
            "marketplace_plugin_name": "tool",
            "source": "registry",
            "resolved_ref": "1.2.3",
            "version": "1.2.3",
            "resolved_url": "https://registry.example/apm/v1/packages/owner/package/versions/1.2.3/download",
            "resolved_hash": "sha256:" + "c" * 64,
            **changes,
        }
    )


@pytest.mark.parametrize(
    ("spec", "changes", "valid"),
    [
        (None, {}, True),
        ("^1.0.0", {}, True),
        ("~1.2.0", {}, True),
        ("1.2.3", {}, True),
        ("^2.0.0", {}, False),
        ("1.2.4", {}, False),
        ("a" * 40, {}, False),
        ("a" * 40, {"version": "a" * 40, "resolved_ref": "a" * 40}, True),
        ("^1.0.0", {"version": "1.1.0-rc.1", "resolved_ref": "1.1.0-rc.1"}, False),
        ("^1.1.0-rc.1", {"version": "1.1.0-rc.1", "resolved_ref": "1.1.0-rc.1"}, True),
        ("^1.0.0", {"version": None}, False),
        ("^1.0.0", {"resolved_url": None}, False),
        ("^1.0.0", {"resolved_hash": None}, False),
        (None, {"resolved_hash": None}, False),
    ],
)
def test_registry_row_is_locked_evidence_without_a_commit(
    spec: str | None, changes: dict[str, object], valid: bool
) -> None:
    """A registry-routed catalog row replays on version, URL and hash (#4009)."""
    declaration = _selection(spec) if spec else _selection_without_version()
    locked = _registry_locked(**changes)
    lock = LockFile(dependencies={locked.get_unique_key(): locked})
    assert (marketplace_ref_mismatch(declaration, locked) is None) is valid
    assert detect_ref_change(declaration, locked) is not valid
    assert lockfile_satisfies_manifest(lock, [declaration])[0] is valid


@pytest.mark.parametrize(
    ("spec", "changes", "reason"),
    [
        (
            "^1.0.0",
            {"resolved_hash": None},
            "missing locked registry version, URL or hash evidence",
        ),
        ("^2.0.0", {}, "manifest version '^2.0.0' != lockfile registry version '1.2.3'"),
    ],
)
def test_registry_row_refusal_names_its_reason(
    spec: str, changes: dict[str, object], reason: str
) -> None:
    """An unusable registry row says why: missing pin evidence or a moved selector."""
    assert marketplace_ref_mismatch(_selection(spec), _registry_locked(**changes)) == reason


def _selection_without_version() -> DependencyReference:
    return DependencyReference.parse_from_dict({"name": "tool", "marketplace": "catalog"})


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


class _CatalogContacted(RuntimeError):
    pass


def _resolve_marketplace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    lock: LockFile,
    declaration: DependencyReference,
    **resolver_options: object,
) -> DependencyReference:
    def contacted(*_args: object, **_kwargs: object) -> None:
        raise _CatalogContacted

    monkeypatch.setattr("apm_cli.marketplace.resolver.resolve_marketplace_plugin", contacted)
    resolver = APMDependencyResolver(
        apm_modules_dir=tmp_path / "apm_modules", existing_lockfile=lock, **resolver_options
    )
    return resolver._resolve_marketplace_dep(declaration)


@pytest.mark.parametrize(
    ("version", "options", "replays"),
    [
        ("^1.0.0", {}, True),
        ("^1.0.0", {"frozen": True}, True),
        ("^2.0.0", {}, False),
        ("^1.0.0", {"update_refs": True}, False),
    ],
)
def test_resolver_replays_matching_lock_row_without_catalog(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    version: str,
    options: dict[str, object],
    replays: bool,
) -> None:
    """Only new, changed or explicitly updated declarations reach the catalog (#3911)."""
    locked = _locked()
    lock = LockFile(dependencies={locked.get_unique_key(): locked})
    if not replays:
        with pytest.raises(_CatalogContacted):
            _resolve_marketplace(monkeypatch, tmp_path, lock, _selection(version), **options)
        return
    replayed = _resolve_marketplace(monkeypatch, tmp_path, lock, _selection(version), **options)
    assert replayed.get_unique_key() == locked.get_unique_key()
    assert replayed.reference == locked.resolved_ref
    assert not replayed.is_marketplace


def test_frozen_refuses_unlocked_marketplace_declaration_without_catalog(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """--frozen never asks the catalog for a declaration the lock cannot replay."""
    with pytest.raises(ValueError, match=r"--frozen replays marketplace dependencies"):
        _resolve_marketplace(monkeypatch, tmp_path, LockFile(), _selection(), frozen=True)


@pytest.mark.parametrize("frozen", [False, True])
@pytest.mark.parametrize(
    ("host", "provenance"),
    [
        (None, {"mcp_config_provenance": {"server": "tool"}}),
        (None, {"mcp_config_provenance": {"server": ["other", "tool"]}}),
        (None, {"lsp_config_provenance": {"server": "package:owner/package"}}),
        (
            "git.example.test",
            {"lsp_config_provenance": {"server": "package:git.example.test/owner/package"}},
        ),
        ("git.example.test", {"lsp_config_provenance": {"server": "package:owner/package"}}),
    ],
)
def test_catalog_defined_servers_replay_only_from_a_materialized_copy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    host: str | None,
    provenance: dict[str, dict],
    frozen: bool,
) -> None:
    """The lock cannot rebuild catalog-inline servers, so a cold copy re-resolves.

    LSP ownership is recorded for the lock key or, on a first install, the
    package's ``apm_modules``-relative path.
    """
    locked = _locked(name="tool", host=host)
    lock = LockFile(dependencies={locked.get_unique_key(): locked}, **provenance)
    with pytest.raises(_CatalogContacted):
        _resolve_marketplace(monkeypatch, tmp_path, lock, _selection(), frozen=frozen)
    replayed = locked.to_dependency_ref()
    replayed.get_install_path(tmp_path / "apm_modules").mkdir(parents=True)
    replayed = _resolve_marketplace(monkeypatch, tmp_path, lock, _selection(), frozen=frozen)
    assert replayed.get_unique_key() == locked.get_unique_key()


_PLUGIN_DIR = "/catalog/plugins/tool"


def _local_locked(**changes: object) -> LockedDependency:
    return LockedDependency(
        **{
            "repo_url": "_local/tool",
            "source": "local",
            "local_path": _PLUGIN_DIR,
            "content_hash": "sha256:" + "c" * 64,
            "discovered_via": "catalog",
            "marketplace_plugin_name": "tool",
            **changes,
        }
    )


def _plain_selection(**fields: str) -> DependencyReference:
    return DependencyReference.parse_from_dict({"name": "tool", "marketplace": "catalog", **fields})


@pytest.mark.parametrize(
    ("fields", "changes", "valid"),
    [
        ({}, {}, True),
        ({}, {"content_hash": None}, False),
        ({}, {"local_path": None}, False),
        ({"version": "^1.0.0"}, {}, False),
    ],
)
def test_local_catalog_rows_are_locked_by_content_not_commit(
    fields: dict[str, str], changes: dict[str, object], valid: bool
) -> None:
    """A local catalog plugin has no commit: its content evidence satisfies --frozen (#4639)."""
    declaration = _plain_selection(**fields)
    locked = _local_locked(**changes)
    lock = LockFile(dependencies={locked.get_unique_key(): locked})
    assert (marketplace_ref_mismatch(declaration, locked) is None) is valid
    assert detect_ref_change(declaration, locked) is not valid
    assert lockfile_satisfies_manifest(lock, [declaration])[0] is valid


@pytest.mark.parametrize("frozen", [False, True])
@pytest.mark.parametrize("selected", [_PLUGIN_DIR, "/catalog/plugins/moved"])
def test_local_catalog_rows_reread_the_catalog_and_frozen_keeps_the_lock(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, selected: str, frozen: bool
) -> None:
    """A local catalog stays the live source; --frozen refuses a changed selection (#4639)."""
    locked = _local_locked()
    lock = LockFile(dependencies={locked.get_unique_key(): locked})
    resolution = SimpleNamespace(
        canonical=selected,
        dependency_reference=None,
        plugin=SimpleNamespace(manifest=None),
        provenance=lambda *_args: {},
    )
    monkeypatch.setattr(
        "apm_cli.marketplace.resolver.resolve_marketplace_plugin", lambda *_a, **_k: resolution
    )
    monkeypatch.setattr(
        "apm_cli.marketplace.catalog_origin.require_locked_catalog_origin", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        "apm_cli.marketplace.dependency_confusion.admit_marketplace_resolution", lambda _r: None
    )
    resolver = APMDependencyResolver(
        apm_modules_dir=tmp_path / "apm_modules", existing_lockfile=lock, frozen=frozen
    )
    if frozen and selected != _PLUGIN_DIR:
        with pytest.raises(ValueError, match=r"declared source differs from apm\.lock\.yaml"):
            resolver._resolve_marketplace_dep(_plain_selection())
        return
    resolved = resolver._resolve_marketplace_dep(_plain_selection())
    assert resolved.is_local
    assert resolved.local_path == selected


def test_changed_local_catalog_selection_refuses_before_pre_install(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """#4639: a frozen local-catalog refusal comes before any lifecycle script."""
    from unittest.mock import MagicMock

    from apm_cli.install.errors import FrozenInstallError
    from apm_cli.install.request import InstallRequest
    from apm_cli.install.service import InstallService
    from apm_cli.models.apm_package import APMPackage

    runner = MagicMock()
    monkeypatch.setattr(
        "apm_cli.core.lifecycle_scripts.build_runner_from_context", lambda **_kw: runner
    )
    monkeypatch.setattr(
        "apm_cli.install.local_content_verdict.local_catalog_selection_reasons",
        lambda lockfile, deps: ["  - tool@catalog: the local catalog now selects other"],
    )
    LockFile().write(tmp_path / "apm.lock.yaml")
    package = APMPackage(name="demo", version="1.0.0", package_path=tmp_path)

    with pytest.raises(FrozenInstallError, match="local marketplace catalog") as refused:
        InstallService().run(InstallRequest(apm_package=package, frozen=True))
    assert "the local catalog now selects other" in str(refused.value.reasons)
    runner.fire.assert_not_called()
