"""Canonical install conflict message."""

import pytest

from apm_cli.deps.lockfile import LockfileConflictError
from apm_cli.install.errors import (
    DirectDependencyError,  # noqa: F401
    PolicyViolationError,  # noqa: F401
    install_failure_message,
)

pytestmark = [pytest.mark.component, pytest.mark.promise("P-OWN-install-outcome")]


def test_install_conflict_message_remains_canonical(tmp_path):
    error = LockfileConflictError(tmp_path / "apm.lock.yaml")
    assert install_failure_message(error) == str(error)
