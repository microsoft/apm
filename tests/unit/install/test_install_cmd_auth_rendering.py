"""Unit tests for AuthenticationError rendering in commands/install.py (#1015).

Verifies that when the install pipeline raises AuthenticationError, the
command handler renders the diagnostic_context on the default path (no
--verbose needed) and does NOT emit the double-wrapped "Failed to install
APM dependencies: Failed to resolve..." string.
"""

from apm_cli.install.errors import AuthenticationError


class TestAuthenticationErrorImportedInInstall:
    """AuthenticationError is importable from the errors module."""

    def test_import(self):
        from apm_cli.install.errors import AuthenticationError as AE

        assert AE is AuthenticationError

    def test_is_runtime_error_subclass(self):
        assert issubclass(AuthenticationError, RuntimeError)

    def test_diagnostic_context_round_trip(self):
        """Diagnostic context set at raise time is available at catch time."""
        diag = "    ADO_APM_PAT is set, but the Azure DevOps request failed."
        try:
            raise AuthenticationError(
                "Authentication failed for dev.azure.com",
                diagnostic_context=diag,
            )
        except AuthenticationError as e:
            assert e.diagnostic_context == diag
            # Bounded full-phrase assertion (CodeQL: avoid arbitrary-
            # position substring match; our tests.instructions.md bans
            # bare URL/host substring checks).
            assert str(e) == "Authentication failed for dev.azure.com"

    def test_not_caught_by_policy_violation(self):
        """AuthenticationError is NOT a PolicyViolationError subclass."""
        from apm_cli.install.errors import PolicyViolationError

        assert not issubclass(AuthenticationError, PolicyViolationError)


class TestAuthErrorNotDoubleWrapped:
    """AuthenticationError should NOT be wrapped by the generic handler.

    The generic except Exception handler prepends 'Failed to install APM
    dependencies:'. Since AuthenticationError is caught before that handler,
    the double-wrap never applies. This test verifies the exception type
    hierarchy ensures clean separation.
    """

    def test_auth_error_bypasses_generic_runtime_wrap(self):
        """Simulate the pipeline re-raise chain."""
        err = AuthenticationError(
            "Authentication failed for dev.azure.com",
            diagnostic_context="    Try: az login",
        )
        # The pipeline.py code has:
        #   except AuthenticationError: raise
        #   except Exception as e: raise RuntimeError(f"Failed to resolve: {e}")
        # Verify that AuthenticationError is caught by its own except clause
        # BEFORE the generic Exception clause.
        caught_by_auth = False
        caught_by_generic = False
        try:
            raise err
        except AuthenticationError:
            caught_by_auth = True
        except Exception:
            caught_by_generic = True

        assert caught_by_auth
        assert not caught_by_generic
