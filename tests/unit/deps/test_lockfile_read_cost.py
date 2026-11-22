"""Lockfile read cost: one parse per text and an O(N) load, same results."""

from __future__ import annotations

import sys
import uuid
from collections.abc import Callable
from types import CodeType

import pytest
import yaml

from apm_cli.core.deployment_ledger import DeploymentLedgerCodec
from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.utils import yaml_io

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-OWN-read-only-lockfile-path")]


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


def _count_calls(code: CodeType, action: Callable[[], object]) -> int:
    """Count calls of *code* while *action* runs, observing without patching."""
    calls = 0

    def profiler(frame, event, _arg):
        nonlocal calls
        if event == "call" and frame.f_code is code:
            calls += 1

    previous = sys.getprofile()
    sys.setprofile(profiler)
    try:
        action()
    finally:
        sys.setprofile(previous)
    return calls


def _per_row_reference(text: str) -> tuple[dict, bool]:
    """The ledger the former load produced: per-row projection, then the final step."""
    data = yaml.safe_load(text)
    reference = LockFile()
    for row in data["dependencies"]:
        reference.add_dependency(LockedDependency.from_dict(row))
    if "deployments" in data:
        ledger = DeploymentLedgerCodec.from_rows(data["deployments"])
        return ledger.records, not data["deployments"] or bool(ledger.records)
    return DeploymentLedgerCodec.from_lockfile(reference).records, reference._deployments_present


_PROJECT_ROWS = {
    "owner/a": {".github/shared.md": "", ".github/a.md": ""},
    "owner/b": {".github/shared.md": "", ".github/b.md": ""},
}
# Three claimants of one URI receipt: a per-row rebuild keeps the first as owner.
_URI_ROWS = {
    "owner/a": {"copilot-mcp://srv": "sha256:" + "a" * 64},
    "owner/b": {"copilot-mcp://srv": "", ".github/b.md": ""},
    "owner/c": {"copilot-mcp://srv": "sha256:" + "c" * 64},
}


class TestLockFileReadCost:
    """One install reads the lockfile about 19 times; each read must stay O(N)."""

    def test_each_parse_of_a_text_is_a_private_copy(self):
        from apm_cli.deps.lockfile_container import parse_lockfile_text

        repo_url = f"owner/copy-{uuid.uuid4().hex}"
        text = _lock_text_with_deployed_files({repo_url: {".github/c.md": ""}})
        expected = yaml.safe_load(text)

        first = parse_lockfile_text(text)
        first["dependencies"][0]["deployed_files"].append(".github/injected.md")
        first["dependencies"].append({"repo_url": "owner/injected"})
        first["lockfile_version"] = "injected"

        assert parse_lockfile_text(text) == expected

    def test_private_copy_matches_deepcopy_and_keeps_aliases_shared(self):
        import copy
        import datetime

        from apm_cli.deps.lockfile_container import _private_copy

        shared = {"k": ["v", 1, 2.5, True, None, datetime.date(2026, 1, 2)]}
        cyclic: list = ["self"]
        cyclic.append(cyclic)
        tree = {"a": shared, "b": shared, "c": cyclic, "d": {1, 2}}

        copied = _private_copy(tree, {})

        assert copied["a"] == copy.deepcopy(tree)["a"]
        assert copied["a"] is copied["b"]
        assert copied["a"] is not shared
        assert copied["a"]["k"] is not shared["k"]
        assert copied["c"][1] is copied["c"]
        assert copied["d"] == {1, 2} and copied["d"] is not tree["d"]

    def test_private_copy_of_a_pure_alias_graph_stays_linear(self):
        from apm_cli.deps.lockfile_container import _private_copy

        level: list = ["leaf"]
        for _ in range(60):
            level = [level, level]

        copies = _count_calls(_private_copy.__code__, lambda: _private_copy(level, {}))

        # 2^60 expanded nodes, one call per distinct object and per reference.
        assert copies <= 3 * 61 + 1

    def test_same_text_is_parsed_once(self):
        repo_url = f"owner/memo-{uuid.uuid4().hex}"
        text = _lock_text_with_deployed_files({repo_url: {".github/m.md": ""}})
        loads: list[LockFile] = []

        parses = _count_calls(
            yaml_io._bounded_load.__code__,
            lambda: loads.extend(LockFile.from_yaml(text) for _ in range(5)),
        )

        assert parses == 1
        assert all(lock.has_dependency(repo_url) for lock in loads)

    @pytest.mark.parametrize("with_deployments", [True, False])
    def test_ledger_projection_does_not_scale_with_rows(self, with_deployments):
        text = _lock_text_with_deployed_files(
            {f"owner/pkg-{index:02d}": {f".github/p{index}.md": ""} for index in range(30)},
            with_deployments=with_deployments,
        )
        loads: list[LockFile] = []

        projections = _count_calls(
            DeploymentLedgerCodec.from_lockfile.__code__,
            lambda: loads.append(LockFile.from_yaml(text)),
        )

        assert projections <= 1
        assert len(loads[0].deployment_ledger.records) == 30

    @pytest.mark.parametrize("with_deployments", [True, False])
    @pytest.mark.parametrize("rows", [_PROJECT_ROWS, _URI_ROWS], ids=["project", "uri"])
    def test_load_matches_per_row_projection(self, rows, with_deployments):
        text = _lock_text_with_deployed_files(rows, with_deployments=with_deployments)

        loaded = LockFile.from_yaml(text)

        records, present = _per_row_reference(text)
        assert loaded.deployment_ledger.records == records
        assert loaded._deployments_present == present

    def test_uri_receipt_keeps_the_first_claimant_as_active_owner(self):
        text = _lock_text_with_deployed_files(_URI_ROWS, with_deployments=False)

        loaded = LockFile.from_yaml(text)

        owners = {
            record.locator.value: record.active_owner
            for record in loaded.deployment_ledger.records.values()
        }
        assert owners["copilot-mcp://srv"] == "owner/a"

    def test_reserved_owner_row_keeps_the_per_row_failure(self):
        """A row keyed like local-bundle provenance still fails the load as before."""
        text = yaml.safe_dump(
            {
                "lockfile_version": "1",
                "dependencies": [
                    {
                        "repo_url": "local-bundle",
                        "resolved_commit": "a" * 40,
                        "deployed_files": [".github/a.md"],
                    }
                ],
            }
        )
        with pytest.raises(ValueError, match="requires a canonical sha256"):
            LockFile.from_yaml(text)

    def test_merge_reordered_alias_chain_loads_like_a_fresh_parse(self):
        """A shallow alias chain the bounded loader accepts is never lost to a copy limit.

        The merge key puts ``deep`` first, so a deep copy enters the whole chain
        at once and exceeds the recursion limit; every read must still load.
        """
        depth = 1200
        lines = ["lockfile_version: '1'", "dependencies: []", "x-chain:", "  k0: &a0 [leaf]"]
        lines += [f"  k{index}: &a{index} [*a{index - 1}]" for index in range(1, depth)]
        lines.append(f"  <<: {{deep: *a{depth - 1}}}")
        text = "\n".join(lines) + "\n"

        loads = [LockFile.from_yaml(text) for _ in range(2)]

        for lock in loads:
            chain = lock._extensions["x-chain"]
            assert next(iter(chain)) == "deep"
            assert chain["k0"] == ["leaf"]
