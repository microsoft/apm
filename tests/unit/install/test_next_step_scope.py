"""Runnable next steps keep the original scope and quote the package (review folds).

Each printed command must reproduce the user's request: ``-g`` stays on a
user-scope install, and a package argument with shell metacharacters is
quoted (#4174, #4199, #4206).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from apm_cli.core.scope import InstallScope
from apm_cli.install.outcome import require_requested_components, uninstall_next_step
from apm_cli.install.validation import inaccessible_package_reason
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


def test_skill_miss_next_step_keeps_user_scope() -> None:
    diagnostics = MagicMock()
    assert not require_requested_components(
        diagnostics,
        option="--skill",
        component="skill",
        requested=("nope",),
        available=("alpha",),
        package="o/r",
        install_spec="o/r",
        user_scope=True,
    )
    (message,), _kwargs = diagnostics.error.call_args
    assert "Run 'apm install -g o/r --skill alpha'" in message, message


@pytest.mark.parametrize(
    ("scope", "command"),
    [
        (InstallScope.PROJECT, "apm uninstall acme/pkg"),
        (InstallScope.USER, "apm uninstall -g acme/pkg"),
    ],
)
def test_missing_dependency_uninstall_step_keeps_scope(scope, command: str) -> None:
    dep_ref = DependencyReference.parse("acme/pkg")
    assert uninstall_next_step(dep_ref, scope) == f" Run '{command}' to remove it from apm.yml."
