"""Static regression guards for canonical hook-only package admission."""

import ast
from pathlib import Path

import pytest

from scripts.architecture_linter.runner import run_selected_rules

pytestmark = pytest.mark.component
ROOT = Path(__file__).resolve().parents[2]
RULE = "marketplace-integrations-package-format-precedence"


@pytest.mark.parametrize(
    ("path", "old", "new"),
    [
        (
            "src/apm_cli/deps/apm_resolver.py",
            "            PackageType.HOOK_PACKAGE,\n",
            "",
        ),
        (
            "src/apm_cli/deps/apm_resolver.py",
            "package_type, _ = detect_package_type(install_path)",
            "package_type = PackageType.HOOK_PACKAGE if list(install_path.glob('*.json')) else None",
        ),
        (
            "src/apm_cli/models/validation.py",
            "hook_files = HookIntegrator().find_hook_files(package_path)",
            "hook_files = list(package_path.rglob('*.json'))",
        ),
        (
            "src/apm_cli/models/validation.py",
            "parse_hook_source(json.load(handle))",
            "json.load(handle)",
        ),
        (
            "src/apm_cli/models/validation.py",
            "    _validate_hook_descriptors(package_path, result)\n",
            "",
        ),
        (
            "src/apm_cli/drift.py",
            "package_type, _ = detect_package_type(package_path)",
            "package_type = None",
        ),
        (
            "src/apm_cli/drift.py",
            "if update_keys is not None and dep_ref.get_unique_key() not in update_keys:",
            "if False:",
        ),
        (
            "src/apm_cli/deps/apm_resolver.py",
            "update_keys=self._update_keys,",
            "update_keys=None,",
        ),
        (
            "src/apm_cli/deps/apm_resolver.py",
            "self._update_keys.add(sub_dep.get_unique_key())",
            "pass",
        ),
        (
            "src/apm_cli/deps/apm_resolver.py",
            "and dep_ref.get_unique_key() not in self._scoped_rechecked",
            "and False",
        ),
        (
            "src/apm_cli/install/phases/integrate.py",
            "receipt[0] == install_path.resolve()",
            "True",
        ),
        (
            "src/apm_cli/install/phases/integrate.py",
            "if verify_package_hash(install_path, receipt[1]):",
            "if True:",
        ),
        (
            "src/apm_cli/install/phases/integrate.py",
            "and (update_refs or ctx.refresh)",
            "and update_refs",
        ),
        (
            "src/apm_cli/install/phases/integrate.py",
            "and (update_refs or ctx.refresh)",
            "and True",
        ),
        (
            "src/apm_cli/install/phases/resolve.py",
            "content_hash = compute_package_hash(candidate_path)",
            "content_hash = 'unverified'",
        ),
        (
            "src/apm_cli/install/phases/resolve.py",
            "selected_dependency_keys(ctx.only_packages, ctx.all_apm_deps)",
            "set(ctx.only_packages)",
        ),
        (
            "src/apm_cli/install/package_selection.py",
            "_selected_dependency_reference(spec, dependencies).get_unique_key()",
            "spec",
        ),
    ],
)
def test_hook_admission_cannot_bypass_canonical_owners(path: str, old: str, new: str) -> None:
    source = (ROOT / path).read_text(encoding="utf-8")
    assert source.count(old) == 1
    mutated = source.replace(old, new, 1)
    ast.parse(mutated, filename=path)
    report = run_selected_rules(ROOT, (RULE,), source_overrides={path: mutated})
    assert report.failures == ()
    assert any(violation.rule_id == RULE for violation in report.violations)
