"""Unit contract for the canonical ``core.symlinks`` pin and its diagnostic (#3457)."""

from __future__ import annotations

import subprocess

import pytest
from git.exc import GitCommandError

from apm_cli.utils.git_env import (
    GitSymlinkCapabilityError,
    git_symlink_pin_args,
    raise_for_symlink_capability,
)

pytestmark = [pytest.mark.unit, pytest.mark.windows_compat]

_GIT_SYMLINK_STDERR = (
    "error: unable to create symlink AGENTS.md: Permission denied\n"
    "fatal: unable to checkout working tree\n"
)


def test_pin_args_force_real_symlinks() -> None:
    assert git_symlink_pin_args() == ("-c", "core.symlinks=true")


def test_capability_failure_names_package_and_windows_requirement() -> None:
    error = subprocess.CalledProcessError(128, ["git", "checkout"], stderr=_GIT_SYMLINK_STDERR)

    with pytest.raises(GitSymlinkCapabilityError) as raised:
        raise_for_symlink_capability(error, "microsoft/aspire-skills")

    message = str(raised.value)
    assert raised.value.package == "microsoft/aspire-skills"
    assert "microsoft/aspire-skills" in message
    assert "symbolic links" in message
    assert "Developer Mode" in message
    assert "Create symbolic links" in message
    assert raised.value.__cause__ is error
    assert message.isascii()


def test_capability_failure_is_found_through_wrapped_causes() -> None:
    git_error = subprocess.CalledProcessError(1, ["git"], stderr=_GIT_SYMLINK_STDERR.encode())
    try:
        try:
            raise git_error
        except subprocess.CalledProcessError as exc:
            raise RuntimeError("Failed to create checkout") from exc
    except RuntimeError as wrapped:
        with pytest.raises(GitSymlinkCapabilityError):
            raise_for_symlink_capability(wrapped, "owner/repo")


def test_gitpython_capability_failure_is_recognized() -> None:
    error = GitCommandError(["git", "clone"], 128, stderr=_GIT_SYMLINK_STDERR)

    with pytest.raises(GitSymlinkCapabilityError):
        raise_for_symlink_capability(error, "owner/repo")


@pytest.mark.parametrize(
    "error",
    [
        subprocess.CalledProcessError(128, ["git"], stderr="fatal: repository not found"),
        subprocess.CalledProcessError(1, ["git"], stderr=None),
        subprocess.TimeoutExpired(["git", "checkout"], 60),
        RuntimeError("unrelated failure"),
    ],
)
def test_other_failures_are_left_to_the_caller(error: BaseException) -> None:
    raise_for_symlink_capability(error, "owner/repo")


def test_existing_capability_error_is_reraised_unchanged() -> None:
    original = GitSymlinkCapabilityError("owner/repo")
    try:
        try:
            raise original
        except GitSymlinkCapabilityError as exc:
            raise RuntimeError("Failed to clone repository") from exc
    except RuntimeError as wrapped:
        with pytest.raises(GitSymlinkCapabilityError) as raised:
            raise_for_symlink_capability(wrapped, "other/repo")

    assert raised.value is original


def test_successful_checkout_reporting_link_failure_raises() -> None:
    """``git checkout`` prints the failure but exits 0 on Windows without privilege."""
    result = subprocess.CompletedProcess(["git", "checkout"], 0, "", _GIT_SYMLINK_STDERR)

    with pytest.raises(GitSymlinkCapabilityError) as raised:
        raise_for_symlink_capability(result, "owner/repo")

    assert raised.value.__cause__ is None


def test_clean_successful_checkout_returns() -> None:
    raise_for_symlink_capability(
        subprocess.CompletedProcess(["git", "checkout"], 0, "", "HEAD is now at abc\n"),
        "owner/repo",
    )
