"""Guard the commit a remote package's same-repository path child is fetched at (#5525).

``deps/apm_resolver.py`` expands a remote package's relative ``path:`` into a
virtual dependency on the same repository. That child is fetched at the commit
``deps/same_repo_pin.py`` selects from its parent, never at the parent's bare
selector, and an inherited commit stays marked as no user pin through nested
children. Part of the ``contracts-tooling-dependency-identity`` guard.
"""

from __future__ import annotations

from scripts.architecture_linter.checks.contracts_test_shared import _present, _summary
from scripts.architecture_linter.models import Violation

RESOLVER = "src/apm_cli/deps/apm_resolver.py"


def same_repo_pin_findings(resolver: object, rule_id: str) -> list[Violation]:
    """Return the finding when the resolver bypasses the same-repository pin owner."""
    if (
        _present(resolver, "reference = self._same_repo_child_reference(parent_dep)")
        and _present(resolver, "return same_repo_child_reference(")
        and _present(resolver, "pinned_by_parent=parent_dep.pinned_by_parent or")
        and not _present(resolver, "reference=parent_dep.reference")
    ):
        return []
    message = "A same-repository path child must be fetched at the commit same_repo_pin selects"
    return [_summary(rule_id, RESOLVER, message)]
