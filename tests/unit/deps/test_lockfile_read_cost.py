"""Lockfile read cost: one parse per text and an O(N) load, same results."""

from __future__ import annotations

import pytest
import yaml

from apm_cli.deps.lockfile import LockedDependency, LockFile

pytestmark = [pytest.mark.component, pytest.mark.promise("P-OWN-read-only-lockfile-path")]


def _lock_text_with_deployed_files(
    rows: dict[str, dict[str, str]], *, with_deployments: bool = True
) -> str:
    """Return lockfile YAML whose dependencies own *rows* (path -> hash)."""
    lock = LockFile()
    for repo_url, hashes in rows.items():
        lock.add_dependency(
            LockedDependency(
                repo_url=repo_url,
                resolved_commit="a" * 40,
                deployed_files=list(hashes),
                deployed_file_hashes={path: digest for path, digest in hashes.items() if digest},
            )
        )
    data = yaml.safe_load(lock.to_yaml())
    if not with_deployments:
        data.pop("deployments", None)
    return yaml.safe_dump(data, sort_keys=False)


def _ledger_snapshot(lock: LockFile) -> tuple:
    return (lock.to_yaml(), lock.deployment_ledger.records, lock._deployments_present)


_PROJECT_ROWS = {
    "owner/a": {".github/shared.md": "", ".github/a.md": ""},
    "owner/b": {".github/shared.md": "", ".github/b.md": ""},
}
_URI_ROWS = {
    "owner/a": {"copilot-mcp://srv": "sha256:" + "a" * 64},
    "owner/b": {"copilot-mcp://srv": "", ".github/b.md": ""},
    "owner/c": {"copilot-mcp://srv": "sha256:" + "c" * 64},
}


class TestLockFileReadCost:
    """One install reads the lockfile ~19 times; each read must stay O(N)."""

    def test_read_after_write_returns_new_data(self, tmp_path):
        lock_path = tmp_path / "apm.lock.yaml"
        first = LockFile()
        first.add_dependency(LockedDependency(repo_url="owner/first", resolved_commit="a" * 40))
        first.write(lock_path)
        assert LockFile.read(lock_path).has_dependency("owner/first")

        second = LockFile()
        second.add_dependency(LockedDependency(repo_url="owner/second", resolved_commit="b" * 40))
        second.write(lock_path)

        reread = LockFile.read(lock_path)
        assert reread.has_dependency("owner/second")
        assert not reread.has_dependency("owner/first")

    def test_mutating_a_read_lockfile_does_not_change_the_next_read(self, tmp_path):
        lock_path = tmp_path / "apm.lock.yaml"
        lock_path.write_text(
            _lock_text_with_deployed_files({"owner/repo": {".github/a.md": "sha256:" + "1" * 64}}),
            encoding="utf-8",
        )
        mutated = LockFile.read(lock_path)
        dep = mutated.get_dependency("owner/repo")
        dep.deployed_files.append(".github/injected.md")
        dep.deployed_file_hashes[".github/a.md"] = "sha256:" + "2" * 64
        mutated.mcp_servers.append("injected")
        mutated.dependencies.clear()

        fresh = LockFile.read(lock_path).get_dependency("owner/repo")
        assert fresh.deployed_files == [".github/a.md"]
        assert fresh.deployed_file_hashes == {".github/a.md": "sha256:" + "1" * 64}
        assert LockFile.read(lock_path).mcp_servers == []

    def test_same_text_is_parsed_once(self, monkeypatch):
        from apm_cli.utils import yaml_io

        text = _lock_text_with_deployed_files({"owner/memo-once": {".github/m.md": ""}})
        real_load = yaml_io.load_yaml_str
        calls = 0

        def counting_load(value):
            nonlocal calls
            calls += 1
            return real_load(value)

        monkeypatch.setattr(yaml_io, "load_yaml_str", counting_load)
        for _ in range(5):
            assert LockFile.from_yaml(text).has_dependency("owner/memo-once")
        assert calls == 1

    @pytest.mark.parametrize("with_deployments", [True, False])
    def test_ledger_projection_does_not_scale_with_rows(self, monkeypatch, with_deployments):
        from apm_cli.core.deployment_ledger import DeploymentLedgerCodec

        text = _lock_text_with_deployed_files(
            {f"owner/pkg-{index:02d}": {f".github/p{index}.md": ""} for index in range(30)},
            with_deployments=with_deployments,
        )
        real_from_lockfile = DeploymentLedgerCodec.from_lockfile
        calls = 0

        def counting_from_lockfile(lockfile):
            nonlocal calls
            calls += 1
            return real_from_lockfile(lockfile)

        monkeypatch.setattr(
            DeploymentLedgerCodec, "from_lockfile", staticmethod(counting_from_lockfile)
        )
        lock = LockFile.from_yaml(text)

        assert calls <= 1
        assert len(lock.deployment_ledger.records) == 30

    @pytest.mark.parametrize("with_deployments", [True, False])
    @pytest.mark.parametrize("rows", [_PROJECT_ROWS, _URI_ROWS], ids=["project", "uri"])
    def test_load_matches_per_row_projection(self, monkeypatch, rows, with_deployments):
        """The load equals the former per-row projection, URI receipts included."""
        text = _lock_text_with_deployed_files(rows, with_deployments=with_deployments)
        loaded = _ledger_snapshot(LockFile.from_yaml(text))

        real_add = LockFile.add_dependency

        def per_row_add(self, dep, *, project_ledger=True):
            real_add(self, dep, project_ledger=True)

        monkeypatch.setattr(LockFile, "add_dependency", per_row_add)
        assert loaded == _ledger_snapshot(LockFile.from_yaml(text))
