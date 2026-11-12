"""Unit tests for is_ado_auth_failure_signal predicate.

The predicate is the single source of truth for the ADO auth-failure
signal set used to gate PAT->bearer fallback. Tests assert phrase
signals and anchored HTTP 401/403 match case-insensitively, and that
None/empty/non-matching inputs return False. Drift in this set caused
#1212.
"""

import subprocess

import pytest

from apm_cli.utils.github_host import is_ado_auth_failure_signal


class TestAdoAuthFailureSignal:
    @pytest.mark.parametrize(
        "stderr_text",
        [
            "fatal: Authentication failed for 'https://dev.azure.com/org/proj'",
            "fatal: unable to access 'https://...': The requested URL returned error: 401",
            "fatal: unable to access 'https://...': The requested URL returned error: 403",
            "fatal: could not read Username for 'https://dev.azure.com'",
            "fatal: HTTP 401 Unauthorized",
        ],
    )
    def test_canonical_signals_match(self, stderr_text: str) -> None:
        assert is_ado_auth_failure_signal(stderr_text) is True

    @pytest.mark.parametrize(
        "stderr_text",
        [
            "FATAL: AUTHENTICATION FAILED for ...",
            "Could Not Read Username for ...",
            "UNAUTHORIZED",
            "Error 401",
            "Error 403",
        ],
    )
    def test_case_insensitive(self, stderr_text: str) -> None:
        assert is_ado_auth_failure_signal(stderr_text) is True

    @pytest.mark.parametrize("text", [None, "", "   "])
    def test_empty_or_none_is_false(self, text) -> None:
        assert is_ado_auth_failure_signal(text) is False

    @pytest.mark.parametrize(
        "stderr_text",
        [
            "fatal: repository not found",
            "fatal: unable to resolve host",
            "Connection timed out",
            "remote: Repository not found.",
            "fatal: unable to access: SSL certificate problem",
        ],
    )
    def test_non_auth_errors_dont_match(self, stderr_text: str) -> None:
        assert is_ado_auth_failure_signal(stderr_text) is False

    def test_substring_match_accepted(self) -> None:
        # The full git stderr is multi-line; predicate must match
        # substring within the blob, not require exact equality.
        blob = (
            "Cloning into 'repo'...\n"
            "remote: TF400813:\n"
            "fatal: Authentication failed for 'https://dev.azure.com/org/proj'\n"
        )
        assert is_ado_auth_failure_signal(blob) is True

    @pytest.mark.parametrize(
        "stderr_text",
        [
            "remote: TF401019: The Git repository does not exist",
            "TF401027: You need the Git 'GenericContribute' permission",
            "VS403403: The item does not exist",
        ],
    )
    def test_ado_work_item_codes_are_not_http_auth(self, stderr_text: str) -> None:
        assert is_ado_auth_failure_signal(stderr_text) is False

    def test_flattens_called_process_error_stderr_on_cause(self) -> None:
        inner = subprocess.CalledProcessError(
            128,
            ["git", "clone"],
            stderr=b"fatal: unable to access: The requested URL returned error: 401",
        )
        wrapped = RuntimeError("git clone failed")
        wrapped.__cause__ = inner
        assert is_ado_auth_failure_signal(wrapped) is True
        assert is_ado_auth_failure_signal(str(wrapped)) is False
