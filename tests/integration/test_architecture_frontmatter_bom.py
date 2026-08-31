"""Architecture guards for canonical frontmatter BOM decoding."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from scripts.architecture_linter.inventory import build_inventory
from scripts.architecture_linter.registry import load_registry
from scripts.architecture_linter.runner import RunReport, registered_rules, run_selected_rules

pytestmark = pytest.mark.component

_RULES_BY_ID = {rule.id: rule for rule in registered_rules()}


def _violated(report: RunReport, rule_id: str) -> bool:
    """Return whether a `run_selected_rules` report blames `rule_id`."""
    return any(violation.rule_id == rule_id for violation in report.violations) or any(
        failure.stage in (f"rule:{rule_id}", f"rule-result:{rule_id}")
        for failure in report.failures
    )


def test_frontmatter_bom_decoding_has_single_owner() -> None:
    """The shared frontmatter loader must own path and stream BOM handling."""
    root = Path(__file__).parents[2]
    owner = (root / "src/apm_cli/utils/yaml_io.py").read_text(encoding="utf-8")
    registry = load_registry(
        root / ".apm/architecture/owners",
        build_inventory(root).files,
    )
    registry_owner = next(
        owner for owner in registry.owners if owner.id == "frontmatter-bom-bounded-yaml"
    )
    rule = _RULES_BY_ID["contracts-tooling-frontmatter-yaml"]

    assert 'def load_frontmatter(fd: Any, encoding: str = "utf-8-sig")' in owner
    assert 'text.removeprefix("\\ufeff")' in owner
    assert (
        "Frontmatter BOM decoding and bounded YAML parsing stay owned by utils/yaml_io.py"
        in rule.description
    )
    assert registry_owner.selectors == ("src/apm_cli/utils/yaml_io.py",)

    duplicate_owners = []
    for source in (root / "src/apm_cli").rglob("*.py"):
        if source == root / "src/apm_cli/utils/yaml_io.py":
            continue
        if "utf-8-sig" in source.read_text(encoding="utf-8"):
            duplicate_owners.append(source.relative_to(root).as_posix())
    assert duplicate_owners == []


def test_frontmatter_bom_guard_rejects_caller_owned_encoding(tmp_path: Path) -> None:
    """The boundary lint rejects BOM decoding duplicated in a consumer."""
    root = Path(__file__).parents[2]
    sandbox = tmp_path / "repo"
    shutil.copytree(
        root,
        sandbox,
        ignore=shutil.ignore_patterns(
            ".git",
            ".venv",
            ".pytest_cache",
            "__pycache__",
            "build",
            "dist",
            "node_modules",
        ),
    )
    consumer = sandbox / "src/apm_cli/primitives/parser.py"
    consumer.write_text(
        consumer.read_text(encoding="utf-8").replace(
            'open(file_path, encoding="utf-8")',
            'open(file_path, encoding="utf-8-sig")',
            1,
        ),
        encoding="utf-8",
    )

    report = run_selected_rules(sandbox, ("contracts-tooling-frontmatter-yaml",))

    assert report.exit_code != 0
    assert _violated(report, "contracts-tooling-frontmatter-yaml")
