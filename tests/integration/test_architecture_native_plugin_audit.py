"""Static witnesses for canonical, read-only native plugin audit replay."""

from pathlib import Path

import pytest

from scripts.architecture_linter.runner import run_selected_rules

pytestmark = pytest.mark.component

ROOT = Path(__file__).resolve().parents[2]
REPLAY = "src/apm_cli/install/drift.py"
ADMISSION = "src/apm_cli/install/native_plugin_admission.py"
RULE = "marketplace-integrations-package-projection"
BOUNDARY = "marketplace-integrations-projection-boundary"


def test_native_replay_owner_rules_pass() -> None:
    """Live replay must route through canonical projection and target admission."""
    report = run_selected_rules(ROOT, (RULE, BOUNDARY))
    assert report.violations == ()
    assert report.failures == ()


@pytest.mark.parametrize(
    ("path", "before", "after"),
    [
        (
            REPLAY,
            "native_detection = route_agent_plugin_package(install_path)",
            "native_detection = None",
        ),
        (REPLAY, "agent_plugin_detection=native_detection", "agent_plugin_detection=None"),
        (REPLAY, "read_only=True", "read_only=False"),
        (REPLAY, "with native_registration_scope(targets):", "with native_registration_scope([]):"),
        (REPLAY, 'if result.get("native_plugin"):', "if False:"),
        (
            ADMISSION,
            "_copy_tree_dereferencing_validated(source, replay_path, source)",
            "_copy_install_tree(source, replay_path)",
        ),
        (ADMISSION, "verify_package_hash(candidate, expected_hash)", "True"),
    ],
)
def test_native_replay_owner_bypass_is_rejected(path: str, before: str, after: str) -> None:
    """Each canonical replay route has a mutation-sensitive static witness."""
    source = (ROOT / path).read_text(encoding="utf-8")
    assert before in source
    report = run_selected_rules(
        ROOT, (RULE,), source_overrides={path: source.replace(before, after, 1)}
    )
    assert {item.rule_id for item in report.violations} == {RULE}


@pytest.mark.parametrize("import_line", ["import shutil", "from shutil import which as locate"])
def test_native_replay_cannot_discover_copilot(import_line: str) -> None:
    """Allow named filesystem copying, never binary discovery or aliasing."""
    source = (ROOT / REPLAY).read_text(encoding="utf-8")
    report = run_selected_rules(
        ROOT, (BOUNDARY,), source_overrides={REPLAY: source + "\n" + import_line + "\n"}
    )
    assert {item.rule_id for item in report.violations} == {BOUNDARY}
