"""Runnable next steps keep the original scope and quote the package (review folds).

Each printed command must reproduce the user's request: ``-g`` stays on a
user-scope install, and a package argument with shell metacharacters is
quoted (#4174, #4199).
"""

from __future__ import annotations

import pytest

from apm_cli.core.auth_outcome import RepositoryNotFoundError
from apm_cli.core.scope import InstallScope
from apm_cli.install.repository_access import inaccessible_package_reason, not_found_next_step
from apm_cli.models.dependency.reference import DependencyReference

pytestmark = [
    pytest.mark.unit,
    pytest.mark.promise("P-OWN-install-outcome"),
]


@pytest.mark.parametrize(
    ("user_scope", "package", "retry"),
    [
        (False, "acme/pkg", "apm install --verbose acme/pkg"),
        (True, "acme/pkg", "apm install -g --verbose acme/pkg"),
        (False, "acme/pkg#rel;echo", "apm install --verbose 'acme/pkg#rel;echo'"),
    ],
)
def test_inaccessible_retry_keeps_scope_and_quotes(
    user_scope: bool, package: str, retry: str
) -> None:
    dep_ref = DependencyReference.parse("acme/pkg")
    reason = inaccessible_package_reason(dep_ref, package, None, False, user_scope=user_scope)
    assert reason.endswith(f"Run '{retry}' to see each probe and credential.")


@pytest.mark.parametrize(
    ("scope", "command"),
    [
        (InstallScope.PROJECT, "apm uninstall acme/pkg"),
        (InstallScope.USER, "apm uninstall -g acme/pkg"),
    ],
)
def test_missing_dependency_uninstall_step_keeps_scope(scope, command: str) -> None:
    dep_ref = DependencyReference.parse("acme/pkg")
    step = not_found_next_step(
        RepositoryNotFoundError("missing"), dep_ref, is_direct=True, scope=scope
    )
    assert step == f" Run '{command}' to remove it from apm.yml."
