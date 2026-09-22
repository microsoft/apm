"""Architecture coverage for embedded git URL subpath validation."""

from pathlib import Path

import pytest

from scripts.architecture_linter.runner import registered_rules, run_selected_rules

pytestmark = pytest.mark.component

ROOT = Path(__file__).parents[2]
RULE_ID = "contracts-tooling-dependency-identity"
REFERENCE = "src/apm_cli/models/dependency/reference.py"


def test_embedded_git_url_subpath_has_one_provider_aware_owner() -> None:
    """The URL guard owns primitive-tail classification through host providers."""
    reference = (ROOT / REFERENCE).read_text(encoding="utf-8")
    owner_registry = (ROOT / ".apm/architecture/owners/contracts-tooling.json").read_text(
        encoding="utf-8"
    )
    rule = next(rule for rule in registered_rules() if rule.id == RULE_ID)

    assert reference.count("def _check_no_embedded_subpath(") == 1
    assert "classify_host_provider(host, host_type=host_type)" in reference
    assert 'provider.kind == "gitlab"' in reference
    assert "embedded git URL subpath validation" in owner_registry
    assert rule.guard_ids == (RULE_ID,)


def test_embedded_git_url_subpath_guard_rejects_provider_bypass() -> None:
    """The registered static guard rejects a universal GitLab bypass mutation."""
    source = (ROOT / REFERENCE).read_text(encoding="utf-8")
    mutated = source.replace('provider.kind == "gitlab"', "True", 1)
    assert mutated != source

    report = run_selected_rules(
        ROOT,
        (RULE_ID,),
        source_overrides={REFERENCE: mutated},
    )

    assert any(
        violation.rule_id == RULE_ID and "Embedded git URL subpath validation" in violation.message
        for violation in report.violations
    )


def test_alias_consumers_share_validation_and_materialization() -> None:
    """All ingress and install boundaries route through the existing owners."""
    report = run_selected_rules(ROOT, (RULE_ID,))
    assert not report.violations


@pytest.mark.parametrize(
    ("path", "before", "after"),
    [
        ("src/apm_cli/deps/lockfile.py", "alias=self.alias", "alias=None"),
        ("src/apm_cli/deps/lockfile.py", 'result["alias"] = self.alias', "pass"),
        (
            "src/apm_cli/deps/lockfile.py",
            "self.alias = parse_alias_override(self.alias)",
            "pass",
        ),
        (REFERENCE, "alias = parse_alias_override(alias)", "alias = alias"),
        (
            "src/apm_cli/models/dependency/registry_entry.py",
            'alias = parse_alias_override(entry.get("alias"))',
            'alias = entry.get("alias")',
        ),
        (
            "src/apm_cli/models/dependency/object_fields.py",
            'validate_path_segments(alias, context="dependency alias")',
            "pass",
        ),
        (
            "src/apm_cli/models/dependency/materialization.py",
            "if resolved == ensure_path_within(apm_modules_dir, apm_modules_dir):",
            "if False:",
        ),
        (
            "src/apm_cli/install/phases/download.py",
            "_pd_path = _pd_ref.get_install_path(apm_modules_dir)",
            "_pd_path = apm_modules_dir / _pd_ref.alias",
        ),
        (
            "src/apm_cli/install/phases/integrate.py",
            "install_path = dep_ref.get_install_path(apm_modules_dir)",
            "install_path = apm_modules_dir / dep_ref.alias",
        ),
        (
            "src/apm_cli/install/phases/resolve.py",
            "cache_validation_callback=partial(",
            "bypassed_cache_validation_callback=partial(",
        ),
        (
            "src/apm_cli/deps/apm_resolver.py",
            "if parent_dep.alias:",
            "if False:",
        ),
        (
            "src/apm_cli/deps/apm_resolver.py",
            "self._cache_validation_callback(install_path, dep_ref.get_unique_key())",
            "pass",
        ),
        (
            "src/apm_cli/deps/apm_resolver.py",
            "had_existing_install = install_path.exists()",
            "had_existing_install = install_path.exists()\n"
            "        self._cache_validation_callback(install_path, dep_ref.get_unique_key())",
        ),
    ],
)
def test_alias_owner_guard_rejects_bypass(path: str, before: str, after: str) -> None:
    """Restoring a split alias decision must trip the registered static guard."""
    source = (ROOT / path).read_text(encoding="utf-8")
    assert before in source
    report = run_selected_rules(
        ROOT,
        (RULE_ID,),
        source_overrides={path: source.replace(before, after, 1)},
    )
    assert any(
        violation.rule_id == RULE_ID and "Dependency aliases" in violation.message
        for violation in report.violations
    )
