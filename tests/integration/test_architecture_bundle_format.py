"""Per-subcheck mutations for the registered bundle-format authority rule."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

import pytest

from scripts.architecture_linter.runner import run_selected_rules

pytestmark = [
    pytest.mark.component,
    pytest.mark.xdist_group(name="architecture_bundle_format_mutations"),
]

ROOT = Path(__file__).resolve().parents[2]
RULE_ID = "marketplace-integrations-bundle-format-authority"


@dataclass(frozen=True)
class BundleMutation:
    name: str
    path: str
    old: str | None = None
    new: str | None = None
    append: str = ""


MUTATIONS: tuple[BundleMutation, ...] = (
    BundleMutation(
        "B1-single-format-owner",
        "src/apm_cli/bundle/packer.py",
        append="\n\ndef resolve_bundle_format(value):\n    return value\n",
    ),
    BundleMutation(
        "B2-preferred-plugin-pin",
        "src/apm_cli/bundle/formats.py",
        "PREFERRED_PLUGIN_FORMAT = BundleFormat.CLAUDE_PLUGIN",
        "PREFERRED_PLUGIN_FORMAT = BundleFormat.AGENT_PLUGIN",
    ),
    BundleMutation(
        "B3-plugin-token-pin",
        "src/apm_cli/bundle/formats.py",
        '"plugin": BundleFormat.CLAUDE_PLUGIN',
        '"plugin": BundleFormat.AGENT_PLUGIN',
    ),
    BundleMutation(
        "B4-selector-seam",
        "src/apm_cli/bundle/formats.py",
        "if len(selections) > 1:",
        "if len(selections) > 2:",
    ),
    BundleMutation(
        "B5-plugin-option-ban",
        "src/apm_cli/commands/plugin/init.py",
        '    "--format",',
        '    "--plugin",',
    ),
    BundleMutation(
        "B8-streaming-archive",
        "src/apm_cli/bundle/reproducible_archive.py",
        "shutil.copyfileobj(source, member)",
        "member.write(source.read())",
    ),
    BundleMutation(
        "B9-init-scaffolding",
        "src/apm_cli/commands/init.py",
        "plugin = load_agent_plugin(staged_root)",
        "plugin = None",
    ),
    BundleMutation(
        "B17-schema-admission",
        "src/apm_cli/marketplace/resolver.py",
        append=(
            "\n\ndef _rogue_schema_admission(schema):\n    return detect_agent_plugin(schema)\n"
        ),
    ),
    BundleMutation(
        "B18-boundary-order",
        "src/apm_cli/commands/install.py",
        (
            "                enforce_agent_plugin_deployment_boundary(bundle_info=_bundle_info)\n"
            "                from ..install.local_bundle_handler import (\n"
            "                    effective_bundle_allow_map as _effective_bundle_allow_map,\n"
            "                )\n"
            "                from ..install.local_bundle_handler import install_local_bundle as _install_lb"
        ),
        (
            "                from ..install.local_bundle_handler import (\n"
            "                    effective_bundle_allow_map as _effective_bundle_allow_map,\n"
            "                )\n"
            "                from ..install.local_bundle_handler import install_local_bundle as _install_lb\n"
            "                enforce_agent_plugin_deployment_boundary(bundle_info=_bundle_info)"
        ),
    ),
)


def _mutate(case: BundleMutation) -> str:
    source = (ROOT / case.path).read_text(encoding="utf-8")
    if case.old is not None:
        assert case.new is not None
        assert source.count(case.old) == 1
        source = source.replace(case.old, case.new, 1)
    source += case.append
    ast.parse(source, filename=case.path)
    return source


@pytest.mark.parametrize("case", MUTATIONS, ids=[case.name for case in MUTATIONS])
def test_registered_bundle_rule_rejects_each_compound_subcheck(
    case: BundleMutation,
) -> None:
    report = run_selected_rules(
        ROOT,
        (RULE_ID,),
        source_overrides={case.path: _mutate(case)},
    )

    assert report.failures == ()
    assert report.exit_code == 2
    assert any(violation.rule_id == RULE_ID for violation in report.violations)
