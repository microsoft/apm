"""Behavioral receipts for OpenAPM lockfile requirements (req-lk-*).

Install-path receipts run the real `apm install` command in process with the
git transport stubbed at the downloader boundary (the same seam the git-semver
lifecycle suite uses), so the lock, apm_modules and deployed files are the
oracles. Registry receipts run the registry resolver against a loopback HTTP
server.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
import yaml

from apm_cli.deps.lockfile import LockFile
from apm_cli.deps.registry.extractor import HashMismatchError
from apm_cli.deps.registry.resolver import RegistryPackageResolver, RegistryResolutionError
from apm_cli.marketplace.ref_resolver import RemoteRef
from apm_cli.models.apm_package import clear_apm_yml_cache
from tests.integration.test_git_semver_install_e2e import (
    _DownloaderStub,
    _find_locked,
    _read_lockfile,
    _RefResolverCallRecorder,
    _run_install,
    _write_apm_yml,
)
from tests.integration.test_git_semver_install_e2e import runner as runner
from tests.unit.registry.test_resolver_e2e_http import (
    _build_apm_tarball,
    _make_dep,
    _running_server,
)

_REPO = "acme/widget"
_REFS = [
    RemoteRef(name="refs/heads/main", sha="0" * 40),
    RemoteRef(name="refs/tags/v1.0.0", sha="1" * 40),
    RemoteRef(name="refs/tags/v1.2.3", sha="2" * 40),
]


@pytest.fixture(autouse=True)
def _fresh_manifest_cache():
    clear_apm_yml_cache()
    yield
    clear_apm_yml_cache()


def _tree(root: Path) -> dict[str, bytes]:
    if not root.exists():
        return {}
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _semver_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runner):
    project = tmp_path / "consumer"
    _write_apm_yml(project, [f"{_REPO}#^1.0.0"])
    refs = _RefResolverCallRecorder({_REPO: list(_REFS)})
    downloads = _DownloaderStub({"v1.0.0": "1" * 40, "v1.2.3": "2" * 40})
    refs.install(monkeypatch)
    downloads.install(monkeypatch)
    first = _run_install(runner, project, monkeypatch)
    assert first.exit_code == 0, first.output
    clear_apm_yml_cache()
    return project, refs, downloads


def _downloads_of(downloads: _DownloaderStub) -> int:
    return sum(1 for repo, _ref in downloads.calls if repo == _REPO)


# --- req-lk-007: matching checkout skips the download, same end state ------


@pytest.mark.req("req-lk-007")
def test_reinstall_with_matching_checkout_skips_download_and_keeps_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runner
) -> None:
    """A second install over a checkout at the locked commit downloads nothing.

    The project tree after the skipped install is byte-identical to the tree a
    fresh install produced (lock, apm_modules and deployed files).
    """
    project, _refs, downloads = _semver_project(tmp_path, monkeypatch, runner)
    fresh = _tree(project)
    fresh_downloads = _downloads_of(downloads)
    assert fresh_downloads == 1
    assert _find_locked(_read_lockfile(project), _REPO)["resolved_commit"] == "2" * 40

    again = _run_install(runner, project, monkeypatch)

    assert again.exit_code == 0, again.output
    assert _downloads_of(downloads) == fresh_downloads
    assert _tree(project) == fresh


# --- req-lk-010: explicit update purges and re-downloads -------------------


@pytest.mark.req("req-lk-010")
def test_update_redownloads_direct_semver_dependency_when_tag_is_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runner
) -> None:
    """`apm install --update` re-runs the download even though v1.2.3 still wins.

    A stale file planted in the install path does not survive: the path was
    purged and re-materialised rather than trusted from cache.
    """
    project, refs, downloads = _semver_project(tmp_path, monkeypatch, runner)
    install_path = project / "apm_modules" / "acme" / "widget"
    stale = install_path / "stale-from-cache.txt"
    stale.write_text("cached bytes\n", encoding="utf-8")
    before = _downloads_of(downloads)

    updated = _run_install(runner, project, monkeypatch, args=["--update"])

    assert updated.exit_code == 0, updated.output
    assert refs.calls.count(_REPO) == 2
    assert _downloads_of(downloads) > before
    assert not stale.exists()
    locked = _find_locked(_read_lockfile(project), _REPO)
    assert (locked["resolved_tag"], locked["resolved_commit"]) == ("v1.2.3", "2" * 40)


# --- req-lk-013: registry hash verified before extraction ------------------


@pytest.mark.req("req-lk-013")
def test_lockfile_replay_hash_mismatch_fails_before_any_extraction(tmp_path: Path) -> None:
    """A locked-hash mismatch names both digests and writes nothing to disk."""
    archive, actual = _build_apm_tarball()
    expected = hashlib.sha256(b"different bytes").hexdigest()
    download = "/v1/packages/acme/web/versions/1.0.0/download"
    target = tmp_path / "apm_modules" / "acme" / "web"

    with _running_server({download: lambda _h: (200, "application/gzip", archive)}) as (h, p):
        base = f"http://{h}:{p}"
        with pytest.raises((HashMismatchError, RegistryResolutionError)) as caught:
            RegistryPackageResolver({"corp": base}).download_from_lockfile(
                _make_dep(),
                target,
                resolved_url=f"{base}{download}",
                resolved_hash=f"sha256:{expected}",
                version="1.0.0",
            )

    message = str(caught.value)
    assert expected in message and actual in message
    assert _tree(target) == {}


@pytest.mark.req("req-lk-013")
def test_lockfile_replay_with_matching_hash_extracts(tmp_path: Path) -> None:
    """Control: the same replay with the true digest materialises the package."""
    archive, actual = _build_apm_tarball()
    download = "/v1/packages/acme/web/versions/1.0.0/download"
    target = tmp_path / "apm_modules" / "acme" / "web"

    with _running_server({download: lambda _h: (200, "application/gzip", archive)}) as (h, p):
        base = f"http://{h}:{p}"
        RegistryPackageResolver({"corp": base}).download_from_lockfile(
            _make_dep(),
            target,
            resolved_url=f"{base}{download}",
            resolved_hash=f"sha256:{actual}",
            version="1.0.0",
        )

    assert (target / "apm.yml").is_file()


# --- req-lk-014 / req-ext-001: x- keys survive a lockfile rewrite ----------

_LOCK_WITH_EXTENSIONS = """\
lockfile_version: '1'
apm_version: 0.37.0
x-acme-build-id: ci-12345
dependencies:
- repo_url: github.com/contoso/example
  resolved_commit: 7f3c9a4d2e1b8c7f0a9e6d5c4b3a2918f7e6d5c4
  resolved_ref: v1.2.0
  x-acme-attestation-id: att-9876
deployments: []
"""


def _rewrite(tmp_path: Path) -> dict:
    path = tmp_path / "apm.lock.yaml"
    path.write_text(_LOCK_WITH_EXTENSIONS, encoding="utf-8")
    LockFile.read(path).write(path)
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_lock_entry_vendor_keys_survive_rewrite(tmp_path: Path) -> None:
    """Per-entry clause of req-lk-014 (passing; the id reports the xfail below)."""
    (entry,) = _rewrite(tmp_path)["dependencies"]
    assert entry["x-acme-attestation-id"] == "att-9876"


@pytest.mark.req("req-lk-014", "req-ext-001")
def test_lock_top_level_vendor_keys_survive_rewrite(tmp_path: Path) -> None:
    rewritten = _rewrite(tmp_path)
    assert rewritten["x-acme-build-id"] == "ci-12345"
    assert rewritten["dependencies"][0]["x-acme-attestation-id"] == "att-9876"
