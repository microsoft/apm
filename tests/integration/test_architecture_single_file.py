"""Surgical route and duplicate-owner mutations for single-file packages."""

from pathlib import Path

import pytest

from scripts.architecture_linter.runner import run_selected_rules

pytestmark = pytest.mark.component

ROOT = Path(__file__).resolve().parents[2]
RULE = "install-deployment-single-file-materialization"


@pytest.mark.parametrize(
    ("path", "before", "after"),
    [
        (
            "src/apm_cli/integration/mcp_config_view.py",
            "source = read_local_package_source(dep_ref, source_base)",
            "source = bypass_admission(dep_ref, source_base)",
        ),
        (
            "src/apm_cli/install/phases/local_content.py",
            "admitted = read_local_package_source(dependency, source.parent)",
            "admitted = bypass_admission(dependency, source.parent)",
        ),
        (
            "src/apm_cli/install/service.py",
            "observed_hash = compute_local_package_hash(selected_source)",
            "observed_hash = private_frozen_hash(selected_source)",
        ),
        (
            "src/apm_cli/install/drift.py",
            "source = read_local_package_source(dependency, source_base)",
            "source = bypass_admission(dependency, source_base)",
        ),
        (
            "src/apm_cli/deps/github_downloader.py",
            "package = materialize_virtual_file_package(",
            "package = duplicate_materializer(",
        ),
        (
            "src/apm_cli/install/validation.py",
            "read_local_package_source(dep_ref, Path.cwd())",
            "admit_by_extension_only(dep_ref, Path.cwd())",
        ),
        pytest.param(
            "src/apm_cli/install/phases/local_content.py",
            "load_frontmatter(BytesIO(content))",
            "unbounded_parse(BytesIO(content))",
            id="frontmatter-loader",
        ),
        (
            "src/apm_cli/deps/virtual_file.py",
            "DependencyReference.VIRTUAL_FILE_SUBDIRECTORIES.items()",
            '{"*.md": "agents"}.items()',
        ),
    ],
)
def test_route_mutation_is_rejected(path: str, before: str, after: str) -> None:
    source = (ROOT / path).read_text(encoding="utf-8")
    assert before in source
    report = run_selected_rules(
        ROOT, (RULE,), source_overrides={path: source.replace(before, after)}
    )
    assert report.failures == ()
    assert any(violation.rule_id == RULE for violation in report.violations)


def test_duplicate_materializer_is_rejected() -> None:
    path = "src/apm_cli/install/validation.py"
    source = (ROOT / path).read_text(encoding="utf-8")
    report = run_selected_rules(
        ROOT,
        (RULE,),
        source_overrides={
            path: source + "\n\ndef materialize_virtual_file_package():\n    return None\n"
        },
    )
    assert report.failures == ()
    assert any(violation.rule_id == RULE for violation in report.violations)


def test_current_single_file_routes_have_one_owner() -> None:
    report = run_selected_rules(ROOT, (RULE,))
    assert report.failures == ()
    assert report.violations == ()
