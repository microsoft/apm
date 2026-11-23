"""Audit consumers must not rederive primitive discovery or content applicability."""

from pathlib import Path

import pytest

from scripts.architecture_linter.runner import run_selected_rules

pytestmark = pytest.mark.component

_ROOT = Path(__file__).resolve().parents[2]
_RULE = "audit-primitive-discovery"
_SCANNER = "src/apm_cli/security/file_scanner.py"


def test_audit_discovery_uses_canonical_contract() -> None:
    result = run_selected_rules(_ROOT, {_RULE})
    assert result.failures == ()
    assert result.violations == ()


@pytest.mark.parametrize(
    "old,new",
    [
        (
            "for surface in primitive_surfaces(project_root, scoped, user_scope=user_scope):",
            "for surface in ():",
        ),
        ("return inspect_native_hooks(", "return bypass_native_hooks("),
        ("if not safe_surface_path(surface, path):", "if False:"),
    ],
)
def test_guard_rejects_discovery_applicability_or_containment_bypass(old: str, new: str) -> None:
    source = (_ROOT / _SCANNER).read_text(encoding="utf-8")
    mutated = source.replace(old, new, 1)
    assert mutated != source
    result = run_selected_rules(_ROOT, {_RULE}, source_overrides={_SCANNER: mutated})
    assert result.failures == ()
    assert any(v.rule_id == _RULE and v.path == _SCANNER for v in result.violations)


@pytest.mark.parametrize(
    "path,old,new",
    [
        (
            "src/apm_cli/integration/hook_bundle.py",
            "json.dumps(hook_module_type_metadata(module_type),",
            'json.dumps({"type": module_type},',
        ),
        (
            "src/apm_cli/integration/hook_bundle.py",
            "return node_module_type(package_type)",
            'return package_type if package_type in {"commonjs", "module"} else "commonjs"',
        ),
        (
            "src/apm_cli/integration/hook_bundle.py",
            'or content_hash_of_bytes(text.encode("utf-8")) != recorded_hash',
            "",
        ),
        (
            "src/apm_cli/integration/hook_bundle.py",
            "or not hook_bundle_deploys_json(target_name)",
            "",
        ),
        (
            _SCANNER,
            "if is_deployed_hook_module_metadata(\n"
            "            path, text, target_name=surface.target, recorded_hash=recorded_hash\n"
            "        ):",
            'if document in ({"type": "commonjs"}, {"type": "module"}):',
        ),
        (
            _SCANNER,
            "recorded_hashes=DeploymentLedgerCodec.deployed_file_hashes(lock) if lock else {},",
            "recorded_hashes={},",
        ),
        (
            "src/apm_cli/integration/hook_source_selection.py",
            "exclude_json_files=not hook_bundle_deploys_json(target_name),",
            'exclude_json_files=target_name == "copilot",',
        ),
    ],
    ids=[
        "inline-sidecar-writer",
        "inline-module-type-vocabulary",
        "recognizer-skips-recorded-hash",
        "recognizer-skips-target-eligibility",
        "scanner-hardcoded-predicate",
        "scanner-drops-recorded-hashes",
        "selection-hardcodes-json-exclusion",
    ],
)
def test_hook_bundle_module_metadata_cannot_bypass_its_owner(path: str, old: str, new: str) -> None:
    source = (_ROOT / path).read_text(encoding="utf-8")
    mutated = source.replace(old, new, 1)
    assert mutated != source
    result = run_selected_rules(_ROOT, {_RULE}, source_overrides={path: mutated})
    assert result.failures == ()
    assert any(v.rule_id == _RULE and v.path == path for v in result.violations)


@pytest.mark.parametrize(
    "path",
    [
        "src/apm_cli/security/primitive_discovery.py",
        "src/apm_cli/integration/skill_integrator.py",
    ],
)
def test_skills_destination_has_one_authority(path: str) -> None:
    source = (_ROOT / path).read_text(encoding="utf-8")
    mutated = source.replace("target.skills_deploy_path(", "target.bypass_skills_root(")
    assert mutated != source
    result = run_selected_rules(_ROOT, {_RULE}, source_overrides={path: mutated})
    assert result.failures == ()
    assert any(v.rule_id == _RULE and v.path == path for v in result.violations)


@pytest.mark.parametrize(
    "path",
    ["src/apm_cli/security/audit_report.py", "src/apm_cli/policy/models.py"],
)
@pytest.mark.parametrize("serializer", ["finding_to_json", "finding_to_sarif"])
def test_finding_serialization_cannot_bypass_shared_authority(path: str, serializer: str) -> None:
    source = (_ROOT / path).read_text(encoding="utf-8")
    # Preserve the owner's definition and imports while replacing consumer calls.
    mutated = source.replace(f"{serializer}(finding)", "bypass_serializer(finding)").replace(
        f"{serializer}(f)", "bypass_serializer(f)"
    )
    assert mutated != source
    result = run_selected_rules(_ROOT, {_RULE}, source_overrides={path: mutated})
    assert result.failures == ()
    assert any(
        v.rule_id == _RULE and v.path == path and serializer in v.message for v in result.violations
    )
