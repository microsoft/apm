"""Architecture guards for canonical frontmatter detection and BOM decoding."""

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
    report = run_selected_rules(root, ("contracts-tooling-frontmatter-yaml",))

    assert report.violations == ()
    assert report.failures == ()
    assert 'def load_frontmatter(fd: Any, encoding: str = "utf-8-sig")' in owner
    assert 'text.removeprefix("\\ufeff")' in owner
    assert (
        "Frontmatter delimiter detection, BOM decoding, and bounded YAML parsing stay owned by utils/yaml_io.py"
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


@pytest.mark.parametrize(
    "mutation",
    [
        "local-detector",
        "aliased-parser-bypass",
        "identity-reread",
        "identity-adoption-reread",
        "decoded-security-bypass",
        "decoded-force-bypass",
    ],
)
def test_frontmatter_authority_guard_rejects_split_owners(
    tmp_path: Path,
    mutation: str,
) -> None:
    """The registered guard rejects local delimiter grammar and parser bypasses."""
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
    if mutation == "local-detector":
        owner = sandbox / "src/apm_cli/utils/yaml_io.py"
        owner.write_text(
            owner.read_text(encoding="utf-8").replace(
                "_BOUNDED_FRONTMATTER_HANDLER.detect(text)",
                'text.startswith("---")',
                1,
            ),
            encoding="utf-8",
        )
    elif mutation == "aliased-parser-bypass":
        bypass = sandbox / "src/apm_cli/frontmatter_bypass.py"
        bypass.write_text(
            "from frontmatter import loads as parse\n\n"
            "def read(text: str):\n"
            "    return parse(text)\n",
            encoding="utf-8",
        )
    elif mutation == "identity-reread":
        integrator = sandbox / "src/apm_cli/integration/instruction_integrator.py"
        integrator.write_text(
            integrator.read_text(encoding="utf-8").replace(
                "                    prepared=prepared_instructions[source_file],\n",
                "",
                1,
            ),
            encoding="utf-8",
        )
    elif mutation == "identity-adoption-reread":
        integrator = sandbox / "src/apm_cli/integration/instruction_integrator.py"
        integrator.write_text(
            integrator.read_text(encoding="utf-8").replace(
                "                expected_content=new_content,\n",
                "",
                1,
            ),
            encoding="utf-8",
        )
    elif mutation == "decoded-security-bypass":
        integrator = sandbox / "src/apm_cli/integration/instruction_integrator.py"
        integrator.write_text(
            integrator.read_text(encoding="utf-8").replace(
                "        if verdict.should_block:\n",
                "        if False:\n",
                1,
            ),
            encoding="utf-8",
        )
    else:
        integrator = sandbox / "src/apm_cli/integration/instruction_integrator.py"
        integrator.write_text(
            integrator.read_text(encoding="utf-8").replace(
                "            force=force,\n",
                "            force=False,\n",
                1,
            ),
            encoding="utf-8",
        )

    report = run_selected_rules(sandbox, ("contracts-tooling-frontmatter-yaml",))

    assert report.exit_code != 0
    assert _violated(report, "contracts-tooling-frontmatter-yaml")
