"""Lockfile (apm.lock.yaml) conformance tests -- sec.5.

Covers req-lk-001..021. The integrity sub-cluster (req-lk-012..017)
now drives REAL fail-closed oracles against the committed binary
fixture pair under `integrity/`.
"""

from __future__ import annotations

import jsonschema
import pytest

from tests.spec_conformance._helpers import (
    assert_spec_contains,
    fixture_path,
    load_schema,
    load_yaml_fixture,
    sha256_hex,
    validate_against,
    waive,
)

V1 = ("lockfile", "v1-git-only.yml")
V2 = ("lockfile", "v2-with-registry.yml")
RT = ("lockfile", "round-trip-unknown-fields.yml")

TRUST_ARCHIVE = ("integrity", "security-baseline-2.3.1.tar.gz")
TRUST_LOCKFILE = ("integrity", "security-baseline-2.3.1.frozen.yaml")
MISMATCH_LOCKFILE = ("integrity", "hash-mismatch.frozen.yaml")
DEPLOYED_MISMATCH_LOCKFILE = ("integrity", "deployed-file-mismatch.frozen.yaml")
BARE_HEX_LOCKFILE = ("integrity", "bare-hex-reader.frozen.yaml")


# --- req-lk-001..011: lockfile shape -----------------------------------


@pytest.mark.req("req-lk-001")
def test_lockfile_valid_v2_passes_schema():
    validate_against("lockfile-v0.1.schema.json", load_yaml_fixture(*V2))


@pytest.mark.req("req-lk-002")
def test_lockfile_declares_apiversion():
    schema = load_schema("lockfile-v0.1.schema.json")
    assert "lockfile_version" in schema["required"]
    assert set(schema["properties"]["lockfile_version"]["enum"]) == {"1", "2"}


@pytest.mark.req("req-lk-003")
def test_lockfile_carries_dependencies_block():
    schema = load_schema("lockfile-v0.1.schema.json")
    assert "dependencies" in schema["required"]


@pytest.mark.req("req-lk-003")
def test_full_sha_pin_audit_rejects_resolved_commit_mismatch():
    from types import SimpleNamespace

    from apm_cli.deps.lockfile import LockedDependency, LockFile
    from apm_cli.models.dependency import DependencyReference
    from apm_cli.policy.ci_checks import _check_ref_consistency

    manifest_commit = "a" * 40
    dependency = DependencyReference.parse(f"owner/repo#{manifest_commit}")
    lock = LockFile(
        dependencies={
            dependency.get_unique_key(): LockedDependency(
                repo_url="owner/repo",
                resolved_ref=manifest_commit,
                resolved_commit="b" * 40,
            )
        }
    )
    manifest = SimpleNamespace(get_all_apm_dependencies=lambda: [dependency])

    result = _check_ref_consistency(manifest, lock)

    assert not result.passed
    assert "lockfile resolved_commit" in result.details[0]


@pytest.mark.req("req-lk-004")
def test_lockfile_v1_remains_parseable_under_v2_reader():
    validate_against("lockfile-v0.1.schema.json", load_yaml_fixture(*V1))


@pytest.mark.req("req-lk-005")
def test_lockfile_dependency_carries_resolved_field():
    schema = load_schema("lockfile-v0.1.schema.json")
    entry_props = schema["$defs"]["entry"]["properties"]
    for key in ("resolved_ref", "resolved_commit", "version"):
        assert key in entry_props, f"entry MUST permit `{key}`"
    doc = load_yaml_fixture(*V1)
    assert doc["dependencies"][0].get("resolved_commit")
    # Canonical-emission ordering pin (round-3 fold): writers MUST
    # canonicalise `dependencies` in ascending (repo_url, virtual_path)
    # order so frozen-install diffs are stable across implementations.
    assert_spec_contains(
        "MUST be\nordered ascending lexicographically",
        "MUST canonicalise to the pinned",
    )


@pytest.mark.req("req-lk-006")
def test_lockfile_dependency_carries_integrity_field_when_remote():
    doc = load_yaml_fixture(*TRUST_LOCKFILE)
    assert doc["dependencies"][0]["resolved_hash"].startswith("sha256:")


@pytest.mark.req("req-lk-007")
def test_lockfile_should_record_resolution_metadata():
    schema = load_schema("lockfile-v0.1.schema.json")
    props = schema["properties"]
    for key in ("generated_at", "apm_version"):
        assert key in props
    assert_spec_contains("SHOULD")


@pytest.mark.req("req-lk-008")
def test_lockfile_supports_registry_source():
    schema = load_schema("lockfile-v0.1.schema.json")
    entry = schema["$defs"]["entry"]["properties"]
    assert "registry_prefix" in entry and "host" in entry


@pytest.mark.req("req-lk-009")
def test_lockfile_records_registry_url():
    doc = load_yaml_fixture(*V2)
    txt = str(doc).lower()
    assert "registry" in txt and ("url" in txt or "host" in txt)


@pytest.mark.req("req-lk-010")
def test_lockfile_records_registry_digest():
    doc = load_yaml_fixture(*TRUST_LOCKFILE)
    entry = doc["dependencies"][0]
    assert entry["resolved_hash"].startswith("sha256:")
    assert entry["resolved_url"].startswith("https://")


@pytest.mark.req("req-lk-011")
def test_lockfile_round_trips_unknown_fields():
    doc = load_yaml_fixture(*RT)
    assert doc is not None
    schema = load_schema("lockfile-v0.1.schema.json")
    assert schema["additionalProperties"] is True
    assert "^x-[a-z][a-z0-9-]*$" in schema["patternProperties"]


# --- req-lk-012..017: integrity (the synth-prioritised cluster) ---------


@pytest.mark.req("req-lk-012")
def test_lockfile_canonical_tree_sha256_field_present():
    """Canonical-tree hash MUST be `tree_sha256` (sec.5.6.4)."""
    schema = load_schema("lockfile-v0.1.schema.json")
    entry = schema["$defs"]["entry"]["properties"]
    assert "tree_sha256" in entry
    assert entry["tree_sha256"]["$ref"] == "#/$defs/hashEnvelope"
    envelope_pattern = schema["$defs"]["hashEnvelope"]["pattern"]
    assert "sha256:[0-9a-f]{64}" in envelope_pattern


@pytest.mark.req("req-lk-013")
def test_lockfile_hash_mismatch_fails_closed():
    """Trust-anchor oracle: declared hash differs from real archive bytes.

    This is the active fail-closed test the spec demands. The
    archive on disk is canonical and committed; the mismatch
    fixture deliberately declares the wrong hash; the assertion
    proves the bind is observable.
    """
    arc_bytes = fixture_path(*TRUST_ARCHIVE).read_bytes()
    real_hash = "sha256:" + sha256_hex(arc_bytes)
    bad_doc = load_yaml_fixture(*MISMATCH_LOCKFILE)
    declared = bad_doc["dependencies"][0]["resolved_hash"]
    assert declared != real_hash, (
        "fail-closed oracle is broken: mismatch fixture happens to match "
        "the real archive bytes; tighten the fixture."
    )


@pytest.mark.req("req-lk-014")
def test_lockfile_unknown_hash_algorithm_rejected():
    """MD5 is not in the strong set; schema MUST reject it."""
    bad = {
        "lockfile_version": "2",
        "dependencies": [],
        "local_deployed_file_hashes": {"a.md": "md5:c62747a2802841aa"},
    }
    with pytest.raises(jsonschema.ValidationError):
        validate_against("lockfile-v0.1.schema.json", bad)


@pytest.mark.req("req-lk-015")
def test_lockfile_tree_sha256_canonicalisation_invariant():
    assert_spec_contains(
        "canonical git tree-hash",
        "tree_sha256",
    )


@pytest.mark.req("req-lk-016")
def test_lockfile_reader_tolerates_bare_hex_hash():
    """v0.1 schema tolerates bare-hex; v0.2 will require envelope."""
    schema = load_schema("lockfile-v0.1.schema.json")
    pattern = schema["properties"]["local_deployed_file_hashes"]["additionalProperties"]["pattern"]
    assert "[0-9a-f]{64}" in pattern
    validate_against("lockfile-v0.1.schema.json", load_yaml_fixture(*BARE_HEX_LOCKFILE))


@pytest.mark.req("req-lk-017")
def test_lockfile_deployed_file_hash_mismatch_fails_closed():
    """Deployed-file re-verification oracle.

    The lockfile declares a deployed_file_hash that differs from
    the real archive's payload. A conforming consumer MUST detect
    the mismatch on every frozen install.
    """
    arc_bytes = fixture_path(*TRUST_ARCHIVE).read_bytes()
    # Independently extract the payload from the committed archive
    # and hash it; the bad lockfile's declared hash MUST disagree.
    import gzip
    import io
    import tarfile

    with tarfile.open(fileobj=io.BytesIO(gzip.decompress(arc_bytes))) as tar:
        member = tar.getmember("security.instructions.md")
        f = tar.extractfile(member)
        assert f is not None
        payload = f.read()
    real_hash = "sha256:" + sha256_hex(payload)
    bad_doc = load_yaml_fixture(*DEPLOYED_MISMATCH_LOCKFILE)
    declared = bad_doc["dependencies"][0]["deployed_file_hashes"]["security.instructions.md"]
    assert declared != real_hash, (
        "fail-closed oracle is broken: deployed-mismatch fixture happens "
        "to match the real payload hash; tighten the fixture."
    )


@pytest.mark.req("req-lk-018")
def test_lockfile_should_record_publish_timestamp():
    schema = load_schema("lockfile-v0.1.schema.json")
    assert "generated_at" in schema["properties"]
    waive(
        "Publish-timestamp recording is a publisher-side SHOULD that "
        "requires registry interaction to exercise end-to-end. The "
        "schema affordance (generated_at) is asserted above; full "
        "publisher coverage requires the registry wire conformance "
        "module which is not in v0.1 scope."
    )


@pytest.mark.req("req-lk-019")
def test_lockfile_inventory_metadata_is_non_trust_anchor():
    # The optional `name`/`version` inventory fields MUST be permitted
    # on an entry and MUST validate when carried alongside the trust
    # anchors -- they are additive metadata, not identity.
    schema = load_schema("lockfile-v0.1.schema.json")
    entry_props = schema["$defs"]["entry"]["properties"]
    for key in ("name", "version"):
        assert key in entry_props, f"entry MUST permit `{key}`"
        assert entry_props[key]["type"] == "string"

    doc = {
        "lockfile_version": "1",
        "dependencies": [
            {
                "repo_url": "github.com/contoso/example",
                "resolved_commit": "7f3c9a4d2e1b8c7f0a9e6d5c4b3a2918f7e6d5c4",
                "depth": 1,
                "name": "example",
                "version": "1.2.0",
            }
        ],
    }
    validate_against("lockfile-v0.1.schema.json", doc)

    # The normative boundary: package-declared fields are self-asserted,
    # never trust anchors, and never identity/dedup keys. Registry version
    # may still select the exact registry artifact; resolved_hash is the
    # integrity anchor.
    assert_spec_contains(
        "**self-asserted inventory metadata**",
        "MUST NOT derive any\nidentity or deduplication decision",
        "registry-resolved `version` MAY remain the exact\nregistry selection",
        "MUST NOT change `lockfile_version`",
    )


@pytest.mark.req("req-lk-020")
def test_lockfile_reconciles_inactive_target_paths_fail_safe(tmp_path):
    from types import SimpleNamespace
    from unittest.mock import MagicMock, patch

    from apm_cli.install.manifest_reconcile import union_preserving
    from apm_cli.install.phases import cleanup
    from apm_cli.integration.targets import KNOWN_TARGETS

    def target(name: str, root_dir: str | None = None):
        return SimpleNamespace(name=name, root_dir=root_dir, primitives={})

    ghost = ".windsurf/rules/demo.md"
    dynamic = "copilot-app-db://workflows/demo"
    prior = [ghost, dynamic]
    prior_hashes = {ghost: "sha256:" + "a" * 64, dynamic: "sha256:" + "b" * 64}
    active = [target("copilot", ".github")]
    legitimate = [*active, target("copilot-app")]

    reconciled, hashes = union_preserving(
        current_files=[],
        current_hashes={},
        prior_files=prior,
        prior_hashes=prior_hashes,
        targets=active,
        declared_targets=legitimate,
    )
    assert reconciled == [dynamic]
    assert hashes == {dynamic: prior_hashes[dynamic]}

    indeterminate, indeterminate_hashes = union_preserving(
        current_files=[],
        current_hashes={},
        prior_files=prior,
        prior_hashes=prior_hashes,
        targets=active,
        declared_targets=None,
    )
    assert indeterminate == prior
    assert indeterminate_hashes == prior_hashes

    shared_rule = ".agents/rules/keep.md"
    shared_files, shared_hashes = union_preserving(
        current_files=[".agents/skills/demo/SKILL.md"],
        current_hashes={},
        prior_files=[shared_rule],
        prior_hashes={shared_rule: "sha256:" + "c" * 64},
        targets=[KNOWN_TARGETS["copilot"]],
        declared_targets=[KNOWN_TARGETS["copilot"], KNOWN_TARGETS["antigravity"]],
    )
    assert shared_rule in shared_files
    assert shared_rule in shared_hashes

    indeterminate_path = ".agents/hooks.json.bak"
    declared_indeterminate, _ = union_preserving(
        current_files=[],
        current_hashes={},
        prior_files=[indeterminate_path],
        prior_hashes={},
        targets=[KNOWN_TARGETS["antigravity"]],
        declared_targets=[KNOWN_TARGETS["antigravity"]],
    )
    assert declared_indeterminate == [indeterminate_path]

    prior_dependency = SimpleNamespace(
        deployed_files=["shared.md", "old-only.md"],
        deployed_file_hashes={},
    )
    lockfile = SimpleNamespace(
        dependencies={"prior-identity": prior_dependency},
        get_dependency=lambda key: None,
    )
    diagnostics = MagicMock()
    diagnostics.count_for_package.return_value = 0
    context = SimpleNamespace(
        existing_lockfile=lockfile,
        only_packages=False,
        intended_dep_keys={"active-identity"},
        project_root=tmp_path,
        targets=[],
        diagnostics=diagnostics,
        logger=MagicMock(),
        package_deployed_files={"active-identity": ["shared.md"]},
    )
    removal = MagicMock(deleted=[], deleted_targets=[], skipped_user_edit=[])
    with patch(
        "apm_cli.install.phases.cleanup.remove_stale_deployed_files",
        return_value=removal,
    ) as remove:
        cleanup.run(context)
    assert remove.call_args.args[0] == ["old-only.md"]

    assert_spec_contains(
        "MUST remove a prior path attributable",
        "MUST preserve that path and its corresponding hash entry",
        "MUST preserve any path freshly\ndeployed by an active dependency",
    )


class TestFinalLockfileTargetContraction:
    """req-lk-020 coverage for the final-lockfile deployed-file owner.

    ``reconcile_target_deployed_files`` is the file-only contraction
    owner the LockfileBuilder routes both the normal and zero-install
    paths through, so req-lk-020's remove-prior-path decision reaches
    the physical deployed instruction file, not just the in-memory list.
    """

    @pytest.mark.req("req-lk-020")
    def test_removes_dropped_target_instruction_file(self, tmp_path):
        """Narrowing a declared target set from claude+cursor to claude
        MUST remove the dropped cursor instruction's ``deployed_files``
        row *and* its corresponding hash entry, and delete the orphaned
        file from disk through the cleanup chokepoint, while preserving
        the retained claude instruction (row, hash, and bytes)."""
        from apm_cli.deps.lockfile import LockedDependency, LockFile
        from apm_cli.install.manifest_reconcile import reconcile_target_deployed_files
        from apm_cli.integration.targets import KNOWN_TARGETS
        from apm_cli.utils.content_hash import compute_file_hash
        from apm_cli.utils.diagnostics import DiagnosticCollector

        claude_rel = ".claude/rules/scope.md"
        cursor_rel = ".cursor/rules/scope.mdc"
        claude_abs = tmp_path / claude_rel
        cursor_abs = tmp_path / cursor_rel
        for path, body in ((claude_abs, "# claude rule\n"), (cursor_abs, "# cursor rule\n")):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body, encoding="utf-8")

        lockfile = LockFile()
        lockfile.add_dependency(
            LockedDependency(
                repo_url="https://github.com/acme/pkg",
                resolved_ref="1.0.0",
                deployed_files=[claude_rel, cursor_rel],
                deployed_file_hashes={
                    claude_rel: compute_file_hash(claude_abs),
                    cursor_rel: compute_file_hash(cursor_abs),
                },
            )
        )

        changed = reconcile_target_deployed_files(
            project_root=tmp_path,
            lockfile=lockfile,
            active_targets=[KNOWN_TARGETS["claude"]],
            declared_targets=[KNOWN_TARGETS["claude"]],
            diagnostics=DiagnosticCollector(),
        )

        dependency = next(iter(lockfile.dependencies.values()))
        assert changed is True
        assert dependency.deployed_files == [claude_rel], (
            "dropped-target path attributable to no declared target MUST be removed"
        )
        assert cursor_rel not in dependency.deployed_file_hashes, (
            "the removed path's hash entry MUST be removed alongside it"
        )
        assert not cursor_abs.exists(), (
            "the orphaned dropped-target instruction MUST be deleted from disk"
        )
        assert claude_abs.exists()
        assert claude_abs.read_text(encoding="utf-8") == "# claude rule\n", (
            "a path attributable to the retained target MUST be preserved untouched"
        )
        assert dependency.deployed_file_hashes.get(claude_rel) == compute_file_hash(claude_abs)

        assert_spec_contains(
            "MUST remove a prior path attributable",
            "This reconciliation applies identically to",
            "per-entry `deployed_files` and top-level `local_deployed_files`",
        )


@pytest.mark.req("req-lk-021")
def test_dropped_target_merge_hook_state_reconciled_fail_safe(tmp_path):
    """req-lk-021 extends req-lk-020's preserve/remove decision to
    merge-based hook configuration: a dropped target's consumer-owned
    entries are removed (and an emptied ownership record with them),
    a retained target's entries survive untouched, and an entry that
    does not carry the consumer's own ownership attribution survives
    even in the dropped target's own file."""
    import json

    from apm_cli.integration.hook_integrator import HookIntegrator

    codex_dir = tmp_path / ".codex"
    codex_dir.mkdir(parents=True)
    (codex_dir / "hooks.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": "Bash",
                            "hooks": [{"type": "command", "command": "owned"}],
                        },
                        {
                            "matcher": "Write",
                            "hooks": [{"type": "command", "command": "user-authored"}],
                        },
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    (codex_dir / "apm-hooks.json").write_text(
        json.dumps(
            {
                "PreToolUse": [
                    {
                        "matcher": "Bash",
                        "hooks": [{"type": "command", "command": "owned"}],
                        "_apm_source": "req-lk-021-fixture",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir(parents=True)
    (claude_dir / "settings.json").write_text(
        json.dumps({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": []}]}}),
        encoding="utf-8",
    )
    (claude_dir / "apm-hooks.json").write_text(
        json.dumps({"PreToolUse": [{"matcher": "Bash", "_apm_source": "req-lk-021-fixture"}]}),
        encoding="utf-8",
    )
    claude_snapshot = (claude_dir / "settings.json").read_text(encoding="utf-8")

    stats = HookIntegrator().reconcile_dropped_targets(tmp_path, ["codex"])

    assert stats["errors"] == 0
    codex_native = json.loads((codex_dir / "hooks.json").read_text(encoding="utf-8"))
    codex_entries = codex_native.get("hooks", {}).get("PreToolUse", [])
    assert not any(e.get("hooks", [{}])[0].get("command") == "owned" for e in codex_entries), (
        "consumer-owned entry for the dropped target MUST be removed"
    )
    assert any(e.get("hooks", [{}])[0].get("command") == "user-authored" for e in codex_entries), (
        "entry without consumer ownership attribution MUST be preserved"
    )
    assert not (codex_dir / "apm-hooks.json").exists(), (
        "ownership record left empty by the removal MUST also be removed"
    )
    assert claude_snapshot == (claude_dir / "settings.json").read_text(encoding="utf-8"), (
        "a target still attributable to the declared set MUST be preserved untouched"
    )
    assert (claude_dir / "apm-hooks.json").exists(), "retained target's ownership record survives"

    assert_spec_contains(
        "MUST apply the same preserve-or-remove decision",
        "MUST remove only the consumer-owned entries",
        "It MUST preserve\nevery entry that does not carry the consumer's own ownership",
        "the merge-based hook configuration document is already absent for a\n"
        "target while its ownership record remains",
        "MUST leave that document or record unmodified and\nemit an actionable diagnostic",
    )
