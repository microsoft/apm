"""Lockfile read cost: one parse per text and an O(N) load, same results."""

from __future__ import annotations

import json
import sys
import uuid
from collections.abc import Callable
from pathlib import Path
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


def _rich_lock(tag: str) -> LockFile:
    """A lockfile whose strings stress the dumper: quoting, folding, unicode."""
    lock = LockFile()
    lock.add_dependency(
        LockedDependency(
            repo_url=f"owner/rich-{tag}",
            resolved_commit="b" * 40,
            deployed_files=[".github/r.md", ".github/x y.md"],
            deployed_file_hashes={".github/r.md": "sha256:" + "d" * 64},
        )
    )
    lock.mcp_configs = {
        "kb": {
            "name": "kb",
            "registry": False,
            "transport": "stdio",
            "args": ["yes", "null", "0123", "a: b", "#c", " lead", "x\ny", "caf\u00e9"],
            "lookalikes": [
                "true",
                "False",
                "on",
                "~",
                "",
                "1.5",
                "1e3",
                ".inf",
                "0x1f",
                "0o17",
                "2026-01-01",
                "2026-01-01T00:00:00Z",
                "12:30",
                "-",
                "'q'",
                '"d"',
                "[x]",
                "{y}",
            ],
            "count": 3,
            "enabled": True,
            "missing": None,
            "env": {"LONG": "word " * 40},
        }
    }
    return lock


def _yaml_frames(action: Callable[[], object]) -> int:
    """Count Python frames inside the ``yaml`` package while *action* runs.

    Loader-agnostic: the pure-Python and libyaml loaders both construct
    through ``yaml/constructor.py``, so any parse shows up here.
    """
    package = str(Path(yaml.__file__).parent)
    calls = 0

    def profiler(frame, event, _arg):
        nonlocal calls
        if event == "call" and frame.f_code.co_filename.startswith(package):
            calls += 1

    previous = sys.getprofile()
    sys.setprofile(profiler)
    try:
        action()
    finally:
        sys.setprofile(previous)
    return calls


class TestLockFileWrittenTextIsNotReparsed:
    """A command that writes the lockfile reads its own bytes back without parsing them."""

    @pytest.mark.parametrize("staged", [True, False], ids=["staged", "on-disk"])
    def test_read_after_write_needs_no_parse(self, tmp_path, staged):
        from apm_cli.deps.lockfile_write_authority import LockfileWriter, lockfile_write_scope

        path = tmp_path / "apm.lock.yaml"
        lock = _rich_lock(uuid.uuid4().hex)
        reads: list[LockFile | None] = []

        if staged:
            with lockfile_write_scope(LockfileWriter.INSTALL):
                lock.write(path)
                parses = _yaml_frames(lambda: reads.append(LockFile.read(path)))
        else:
            lock.write(path)
            parses = _yaml_frames(lambda: reads.append(LockFile.read(path)))

        assert parses == 0
        assert reads[0] is not None
        assert reads[0].to_yaml() == path.read_text(encoding="utf-8")

    def test_recorded_document_equals_a_fresh_parse_in_key_order(self, tmp_path):
        from apm_cli.deps.lockfile_container import parse_lockfile_text

        path = tmp_path / "apm.lock.yaml"
        _rich_lock(uuid.uuid4().hex).write(path)
        text = path.read_text(encoding="utf-8")

        recorded = parse_lockfile_text(text)
        fresh = yaml_io.load_machine_yaml_str(text)

        assert recorded == fresh
        assert json.dumps(recorded) == json.dumps(fresh)

    def test_a_document_with_a_float_is_parsed_not_recorded(self):
        from apm_cli.deps.lockfile_container import parse_lockfile_text, remember_lockfile_text

        text = f"lockfile_version: '1'\nx-{uuid.uuid4().hex}: 1.5\n"
        remember_lockfile_text(text, {"lockfile_version": "1", "wrong": 1.5})

        assert parse_lockfile_text(text) == yaml.safe_load(text)

    def test_generator_gate_composes_each_text_once(self, tmp_path):
        from apm_cli.deps.lockfile_generator import assert_writable

        path = tmp_path / "apm.lock.yaml"
        _rich_lock(uuid.uuid4().hex).write(path)

        composes = _count_calls(
            yaml_io._top_level_scalar_values.__code__,
            lambda: [assert_writable(path) for _ in range(3)],
        )

        assert composes == 1


_ADVERSARIAL_SCALARS = [
    "yes",
    "on",
    "off",
    "null",
    "~",
    "1e3",
    "0o17",
    "0x1F",
    "2026-01-01",
    "12:30",
    "",
    " lead",
    "trail ",
    "a: b",
    "- x",
    "#c",
    "'q'",
    '"dq"',
    "\t",
    "multi\nline",
    "caf\u00e9",
    "&anchor",
    "*alias",
    "!tag",
    2**70,
    -1,
    0,
    True,
    False,
    None,
]


class TestRecordedParseAdmission:
    """QL rebase conditions: one admission gate and exact round-trip equality."""

    def test_a_dumped_document_loads_back_exactly(self):
        from apm_cli.deps.lockfile_container import _plain_copy

        document = {
            "lockfile_version": "1",
            "scalars": list(_ADVERSARIAL_SCALARS),
            "keys": {str(value): value for value in _ADVERSARIAL_SCALARS},
        }
        text = yaml_io.yaml_to_str(document)

        loaded = yaml_io.load_machine_yaml_str(text)

        assert _plain_copy(document) == loaded
        assert json.dumps(_plain_copy(document)) == json.dumps(loaded)

    def test_an_over_budget_text_is_not_seeded_and_reads_refuse_it(self):
        from apm_cli.deps.lockfile_container import (
            _parsed_texts,
            parse_key,
            parse_lockfile_text,
            remember_lockfile_text,
        )

        text = "lockfile_version: '1'\n#" + "x" * yaml_io._MAX_MACHINE_YAML_CHARS + "\n"

        remember_lockfile_text(text, {"lockfile_version": "1"})

        assert not yaml_io.machine_yaml_admits(text)
        assert parse_key(text) not in _parsed_texts
        with pytest.raises(yaml.YAMLError, match="character budget"):
            parse_lockfile_text(text)


@pytest.mark.parametrize("where", ["value", "key"])
@pytest.mark.parametrize("brk", ["\x85", "\u2028", "\u2029"], ids=["nel", "ls", "ps"])
def test_a_text_with_a_yaml_line_break_reads_like_a_fresh_parse(where, brk):
    from apm_cli.deps.lockfile_container import parse_lockfile_text, remember_lockfile_text

    odd = f"a{brk}b-{uuid.uuid4().hex}"
    document = (
        {"lockfile_version": "1", "x": odd}
        if where == "value"
        else {
            "lockfile_version": "1",
            odd: "v",
        }
    )
    text = yaml_io.yaml_to_str(document)

    remember_lockfile_text(text, document)

    assert parse_lockfile_text(text) == yaml_io.load_machine_yaml_str(text)
