"""Mutation proof for the source-package selection and non-activation boundary."""

from pathlib import Path

import pytest

from scripts.architecture_linter.runner import run_selected_rules

pytestmark = pytest.mark.component
ROOT = Path(__file__).resolve().parents[2]
RULE = "marketplace-integrations-source-resources"


@pytest.mark.parametrize(
    ("path", "old", "new"),
    [
        ("bundle/packer.py", "require_resource_pack_mode(package)", "pass"),
        ("bundle/plugin_exporter.py", "require_resource_pack_mode(package)", "pass"),
        ("bundle/agent_plugin_exporter.py", "require_resource_pack_mode(package)", "pass"),
        ("bundle/unpacker.py", "reject_source_deployment(source_dir)", "pass"),
        ("models/format_detection.py", "reject_source_deployment(package_path)", "pass"),
        ("bundle/source_package.py", "verify_bundle_integrity(root, metadata)", "[]"),
        (
            "models/package_resources.py",
            "from pathlib import Path",
            "from pathlib import Path\nimport subprocess",
        ),
    ],
)
def test_source_owner_guard_rejects_boundary_mutations(path: str, old: str, new: str) -> None:
    relative = f"src/apm_cli/{path}"
    source = (ROOT / relative).read_text(encoding="utf-8")
    assert old in source
    result = run_selected_rules(
        ROOT, (RULE,), source_overrides={relative: source.replace(old, new)}
    )
    assert result.failures == ()
    assert result.exit_code == 2
    assert any(item.rule_id == RULE for item in result.violations)


def test_source_owner_guard_accepts_current_tree() -> None:
    result = run_selected_rules(ROOT, (RULE,))
    assert result.violations == ()
    assert result.failures == ()
