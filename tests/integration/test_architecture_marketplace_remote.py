"""Executable mutations protect the marketplace-to-transport owner seam."""

from pathlib import Path

import pytest

from scripts.architecture_linter.runner import run_selected_rules

pytestmark = pytest.mark.component
ROOT = Path(__file__).resolve().parents[2]
RULE = "transport-platform-marketplace-package-remote"
RESOLVER = "src/apm_cli/marketplace/resolver.py"
VERSION = "src/apm_cli/marketplace/version_resolver.py"


def test_marketplace_remote_owner_is_compliant() -> None:
    """The registered rule accepts the real consumer."""
    report = run_selected_rules(ROOT, (RULE,))
    assert not report.failures
    assert not report.violations


@pytest.mark.parametrize(
    ("path", "old", "new"),
    [
        (RESOLVER, "return dep_ref", "return DependencyReference.parse(source.url)"),
        (
            RESOLVER,
            "dependency = DependencyReference.parse_from_dict(entry)",
            "dependency = DependencyReference.parse('github.com/wrong/package')",
        ),
        (
            RESOLVER,
            'return _gitlab_in_marketplace_dependency_reference(source, in_repo_path or "", ref)',
            "return DependencyReference.parse('github.com/wrong/catalog')",
        ),
        (RESOLVER, "initial_transport_scheme(lookup)", "'https'"),
        (RESOLVER, 'version_auth["port"] = lookup.port', 'version_auth["port"] = source.port'),
        (
            VERSION,
            'resolver_kwargs["transport_scheme"] = transport_scheme',
            'resolver_kwargs["transport_scheme"] = "https"',
        ),
        (VERSION, 'resolver_kwargs["ssh_user"] = ssh_user', 'resolver_kwargs["ssh_user"] = "git"'),
        (RESOLVER, "dep_ref = lookup", "dep_ref = None"),
        (
            RESOLVER,
            "return DependencyReference.parse(canonical)",
            "return DependencyReference.parse(source.url)",
        ),
        (RESOLVER, 'elif source_type in {"github", "git-subdir", "gitlab"}:', "elif False:"),
    ],
    ids=[
        "catalog-instead-of-package",
        "invalid-locator-fallback",
        "catalog-local",
        "scheme-owner",
        "foreign-port",
        "scheme-forwarding",
        "ssh-user-forwarding",
        "lookup-return-handoff",
        "bare-source-authority",
        "explicit-source-validation",
    ],
)
def test_marketplace_remote_owner_rejects_mutation(path: str, old: str, new: str) -> None:
    """A bypass cannot be hidden by retaining the correct expression in a comment."""
    source = (ROOT / path).read_text(encoding="utf-8")
    assert old in source
    mutated = source.replace(old, new, 1) + f"\n# Original owner edge: {old}\n"
    report = run_selected_rules(ROOT, (RULE,), source_overrides={path: mutated})
    assert not report.failures
    assert {finding.rule_id for finding in report.violations} == {RULE}
