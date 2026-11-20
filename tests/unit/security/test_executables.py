"""Unit tests for ``apm_cli.security.executables``.

Covers:
- ``ExecutableDeclaration`` data model properties
- ``is_package_approved`` and ``is_any_type_approved`` checking logic
- ``build_approval_key`` construction
- ``parse_allow_executables`` validation
- ``prompt_executable_approval`` interactive / CI / trust-all / no-executables paths
- ``_is_fully_approved`` helper

Filesystem cases (scanning, manifest writes, bundle hashing) live in
test_exec_vocab_unification.py.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from apm_cli.security.executables import (
    EXEC_TYPE_BIN,
    EXEC_TYPE_HOOKS,
    EXEC_TYPE_LSP,
    EXEC_TYPE_MCP,
    REGISTRABLE_EXEC_STATUSES,
    TRUST_ABSENT,
    TRUST_DENIED,
    TRUST_DEPLOYED,
    TRUST_GATED,
    ExecutableDeclaration,
    _is_fully_approved,
    build_approval_key,
    filter_lsp_by_allow_executables,
    is_any_type_approved,
    is_package_approved,
    more_severe_exec_status,
    parse_allow_executables,
    prompt_executable_approval,
)

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# ExecutableDeclaration
# ---------------------------------------------------------------------------


class TestExecutableDeclaration:
    """Tests for the ExecutableDeclaration data model."""

    def test_has_executables_false_when_all_zero(self) -> None:
        decl = ExecutableDeclaration(package_key="a#1.0", package_name="a")
        assert not decl.has_executables

    def test_has_executables_true_with_hooks(self) -> None:
        decl = ExecutableDeclaration(package_key="a#1.0", package_name="a", hook_count=2)
        assert decl.has_executables

    def test_has_executables_true_with_mcp_only(self) -> None:
        """MCP-only packages are now flagged (MCP enforcement is active)."""
        decl = ExecutableDeclaration(package_key="a#1.0", package_name="a", mcp_count=1)
        assert decl.has_executables

    def test_has_executables_true_with_bin(self) -> None:
        decl = ExecutableDeclaration(package_key="a#1.0", package_name="a", bin_count=3)
        assert decl.has_executables

    def test_has_executables_true_with_lsp_only(self) -> None:
        decl = ExecutableDeclaration(package_key="a#1.0", package_name="a", lsp_count=1)
        assert decl.has_executables
        assert decl.exec_types == [EXEC_TYPE_LSP]
        assert decl.summary_line() == "1 LSP server(s)"

    def test_exec_types_empty(self) -> None:
        decl = ExecutableDeclaration(package_key="a#1.0", package_name="a")
        assert decl.exec_types == []

    def test_exec_types_all(self) -> None:
        """exec_types includes all enforced types (hooks, mcp, bin, canvas)."""
        decl = ExecutableDeclaration(
            package_key="a#1.0",
            package_name="a",
            hook_count=1,
            mcp_count=1,
            bin_count=1,
        )
        assert EXEC_TYPE_HOOKS in decl.exec_types
        assert EXEC_TYPE_BIN in decl.exec_types
        assert EXEC_TYPE_MCP in decl.exec_types

    def test_exec_types_partial(self) -> None:
        decl = ExecutableDeclaration(
            package_key="a#1.0", package_name="a", hook_count=1, bin_count=2
        )
        assert decl.exec_types == [EXEC_TYPE_HOOKS, EXEC_TYPE_BIN]

    def test_summary_line(self) -> None:
        """summary_line shows all enforced types (hooks, mcp, bin, canvas)."""
        decl = ExecutableDeclaration(
            package_key="a#1.0",
            package_name="a",
            hook_count=2,
            mcp_count=1,
            bin_count=3,
        )
        summary = decl.summary_line()
        assert "2 hook(s)" in summary
        assert "1 MCP server(s)" in summary
        assert "3 bin executable(s)" in summary

    def test_summary_line_hooks_only(self) -> None:
        decl = ExecutableDeclaration(package_key="a#1.0", package_name="a", hook_count=1)
        assert decl.summary_line() == "1 hook(s)"

    def test_is_frozen(self) -> None:
        decl = ExecutableDeclaration(package_key="a#1.0", package_name="a")
        try:
            decl.hook_count = 5  # type: ignore[misc]
            raise AssertionError("Expected FrozenInstanceError")
        except AttributeError:
            pass


# ---------------------------------------------------------------------------
# is_package_approved
# ---------------------------------------------------------------------------


class TestIsPackageApproved:
    """Tests for is_package_approved."""

    def test_none_allow_executables_returns_false(self) -> None:
        assert not is_package_approved(None, "a#1.0", EXEC_TYPE_HOOKS)

    def test_empty_allow_executables_returns_false(self) -> None:
        assert not is_package_approved({}, "a#1.0", EXEC_TYPE_HOOKS)

    def test_missing_key_returns_false(self) -> None:
        allow = {"b#1.0": {"hooks": True}}
        assert not is_package_approved(allow, "a#1.0", EXEC_TYPE_HOOKS)

    def test_wrong_exec_type_returns_false(self) -> None:
        allow = {"a#1.0": {"hooks": True}}
        assert not is_package_approved(allow, "a#1.0", EXEC_TYPE_BIN)

    def test_approved_returns_true(self) -> None:
        allow = {"a#1.0": {"hooks": True, "bin": True}}
        assert is_package_approved(allow, "a#1.0", EXEC_TYPE_HOOKS)
        assert is_package_approved(allow, "a#1.0", EXEC_TYPE_BIN)

    def test_false_value_returns_false(self) -> None:
        allow = {"a#1.0": {"hooks": False}}
        assert not is_package_approved(allow, "a#1.0", EXEC_TYPE_HOOKS)

    def test_non_dict_entry_returns_false(self) -> None:
        allow = {"a#1.0": True}  # type: ignore[dict-item]
        assert not is_package_approved(allow, "a#1.0", EXEC_TYPE_HOOKS)


# ---------------------------------------------------------------------------
# is_any_type_approved
# ---------------------------------------------------------------------------


class TestIsAnyTypeApproved:
    """Tests for is_any_type_approved."""

    def test_none_returns_false(self) -> None:
        assert not is_any_type_approved(None, "a#1.0")

    def test_empty_returns_false(self) -> None:
        assert not is_any_type_approved({}, "a#1.0")

    def test_any_true_returns_true(self) -> None:
        allow = {"a#1.0": {"mcp": True}}
        assert is_any_type_approved(allow, "a#1.0")

    def test_all_false_returns_false(self) -> None:
        allow = {"a#1.0": {"hooks": False, "mcp": False, "bin": False}}
        assert not is_any_type_approved(allow, "a#1.0")


# ---------------------------------------------------------------------------
# build_approval_key
# ---------------------------------------------------------------------------


class TestBuildApprovalKey:
    """Tests for build_approval_key."""

    def test_with_version(self) -> None:
        assert build_approval_key("owner/repo", "v1.0") == "owner/repo#v1.0"

    def test_empty_version(self) -> None:
        assert build_approval_key("owner/repo", "") == "owner/repo"

    def test_marketplace_format(self) -> None:
        assert build_approval_key("ci-hooks@acme", "1.2.0") == "ci-hooks@acme#1.2.0"


# ---------------------------------------------------------------------------
# scan_package_executables
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# parse_allow_executables
# ---------------------------------------------------------------------------


class TestParseAllowExecutables:
    """Tests for parse_allow_executables validation."""

    def test_absent_returns_none(self) -> None:
        assert parse_allow_executables({}) is None

    def test_valid_block(self) -> None:
        data = {
            "allowExecutables": {
                "ci-hooks@acme#1.2.0": {"hooks": True, "bin": False},
                "mcp-tools@corp#2.0": {"mcp": True},
            }
        }
        result = parse_allow_executables(data)
        assert result is not None
        assert result["ci-hooks@acme#1.2.0"]["hooks"] is True
        assert result["ci-hooks@acme#1.2.0"]["bin"] is False
        assert result["mcp-tools@corp#2.0"]["mcp"] is True

    def test_empty_block(self) -> None:
        data = {"allowExecutables": {}}
        result = parse_allow_executables(data)
        assert result == {}

    def test_non_dict_top_level_raises(self) -> None:
        data = {"allowExecutables": "invalid"}
        try:
            parse_allow_executables(data)
            raise AssertionError("Expected ValueError")
        except ValueError as e:
            assert "must be a mapping" in str(e)

    def test_non_dict_entry_raises(self) -> None:
        data = {"allowExecutables": {"pkg#1.0": True}}
        try:
            parse_allow_executables(data)
            raise AssertionError("Expected ValueError")
        except ValueError as e:
            assert "must be a mapping" in str(e)

    def test_non_bool_value_raises(self) -> None:
        data = {"allowExecutables": {"pkg#1.0": {"hooks": "yes"}}}
        try:
            parse_allow_executables(data)
            raise AssertionError("Expected ValueError")
        except ValueError as e:
            assert "must be a boolean" in str(e)

    def test_unknown_exec_type_raises(self) -> None:
        data = {"allowExecutables": {"pkg#1.0": {"hokks": True}}}
        try:
            parse_allow_executables(data)
            raise AssertionError("Expected ValueError")
        except ValueError as e:
            assert "unknown exec type" in str(e)
            assert "hokks" in str(e)


# ---------------------------------------------------------------------------
# write_allow_executables
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _is_fully_approved
# ---------------------------------------------------------------------------


class TestIsFullyApproved:
    """Tests for _is_fully_approved helper."""

    def test_all_types_approved(self) -> None:
        allow = {"a#1.0": {"hooks": True, "bin": True}}
        decl = ExecutableDeclaration(
            package_key="a#1.0", package_name="a", hook_count=1, bin_count=1
        )
        assert _is_fully_approved(allow, decl)

    def test_partial_approval(self) -> None:
        allow = {"a#1.0": {"hooks": True}}
        decl = ExecutableDeclaration(
            package_key="a#1.0", package_name="a", hook_count=1, bin_count=1
        )
        assert not _is_fully_approved(allow, decl)

    def test_no_entry(self) -> None:
        decl = ExecutableDeclaration(package_key="a#1.0", package_name="a", hook_count=1)
        assert not _is_fully_approved({}, decl)


# ---------------------------------------------------------------------------
# prompt_executable_approval
# ---------------------------------------------------------------------------


class TestPromptExecutableApproval:
    """Tests for prompt_executable_approval flow."""

    def _make_decl(
        self,
        key: str = "pkg#1.0",
        name: str = "pkg",
        hooks: int = 1,
        bins: int = 0,
    ) -> ExecutableDeclaration:
        return ExecutableDeclaration(
            package_key=key,
            package_name=name,
            hook_count=hooks,
            bin_count=bins,
        )

    def test_trust_all_approves_everything(self) -> None:
        decl = self._make_decl()
        result = prompt_executable_approval([decl], trust_all=True)
        assert "pkg#1.0" in result
        assert result["pkg#1.0"]["hooks"] is True

    def test_no_executables_denies_everything(self) -> None:
        decl = self._make_decl()
        result = prompt_executable_approval([decl], no_executables=True)
        assert "pkg#1.0" not in result

    def test_already_approved_skipped(self) -> None:
        decl = self._make_decl()
        existing = {"pkg#1.0": {"hooks": True}}
        result = prompt_executable_approval([decl], allow_executables=existing, trust_all=True)
        # Should preserve existing
        assert result["pkg#1.0"]["hooks"] is True

    def test_no_pending_returns_existing(self) -> None:
        decl = self._make_decl()
        existing = {"pkg#1.0": {"hooks": True}}
        result = prompt_executable_approval([decl], allow_executables=existing)
        assert result == existing

    def test_non_interactive_exits(self) -> None:
        decl = self._make_decl()
        with patch("apm_cli.security.executables._is_interactive", return_value=False):
            try:
                prompt_executable_approval([decl])
                raise AssertionError("Expected SystemExit")
            except SystemExit as e:
                assert e.code == 1

    def test_empty_declarations_returns_existing(self) -> None:
        existing = {"old#1.0": {"hooks": True}}
        result = prompt_executable_approval([], allow_executables=existing)
        assert result == existing

    def test_no_executable_declarations_returns_existing(self) -> None:
        # Declaration with no actual executables
        decl = ExecutableDeclaration(package_key="a#1.0", package_name="a")
        result = prompt_executable_approval([decl], trust_all=True)
        assert "a#1.0" not in result

    def test_trust_all_with_multiple_types(self) -> None:
        decl = self._make_decl(hooks=2, bins=3)
        result = prompt_executable_approval([decl], trust_all=True)
        assert result["pkg#1.0"]["hooks"] is True
        assert result["pkg#1.0"]["bin"] is True

    @patch("apm_cli.security.executables._is_interactive", return_value=True)
    @patch("click.confirm", return_value=True)
    def test_interactive_approve(self, mock_confirm, mock_interactive) -> None:
        decl = self._make_decl()
        result = prompt_executable_approval([decl])
        assert "pkg#1.0" in result
        assert result["pkg#1.0"]["hooks"] is True

    @patch("apm_cli.security.executables._is_interactive", return_value=True)
    @patch("click.confirm", return_value=False)
    def test_interactive_deny(self, mock_confirm, mock_interactive) -> None:
        decl = self._make_decl()
        result = prompt_executable_approval([decl])
        assert "pkg#1.0" not in result

    @patch("apm_cli.security.executables._is_interactive", return_value=False)
    def test_non_interactive_message_uses_unified_vocab(self, _mock, capsys) -> None:
        # M4 (#1873): the live install-gate message must drop the retired
        # ``allowExecutables`` noun, lead with the bulk one-liner, and offer
        # the introspection hatch.
        decl = self._make_decl()
        with pytest.raises(SystemExit):
            prompt_executable_approval([decl])
        out = capsys.readouterr().out
        assert "allowExecutables" not in out
        # No org set configured: the bulk command would find nothing (#3994).
        assert "apm approve --recommended" not in out
        assert "apm policy explain" in out
        with pytest.raises(SystemExit):
            prompt_executable_approval([decl], recommend=("owner/pkg",))
        assert "apm approve --recommended" in capsys.readouterr().out


class TestAliasDeprecationWarning:
    """S1 (#1873): the allowExecutables alias warns once, not per-package."""

    def test_warns_once(self, capsys) -> None:
        import apm_cli.security.executables as ex

        ex._ALIAS_DEPRECATION_WARNED = False
        ex.warn_allow_executables_alias_once()
        ex.warn_allow_executables_alias_once()
        out = capsys.readouterr().out
        assert out.count("allowExecutables") == 1
        assert "deprecated" in out
        assert "executables.allow" in out

    def test_uses_logger_when_given(self) -> None:
        import apm_cli.security.executables as ex

        ex._ALIAS_DEPRECATION_WARNED = False
        calls = []

        class _Logger:
            def warning(self, msg, symbol=None):
                calls.append(msg)

        ex.warn_allow_executables_alias_once(_Logger())
        assert len(calls) == 1
        assert "deprecated" in calls[0]


# ---------------------------------------------------------------------------
# filter_lsp_by_allow_executables (fail-closed under v1 deny-wins)
# ---------------------------------------------------------------------------


class _FakeMcpDep:
    """Minimal stand-in for an MCP dependency exposing ``.name``."""

    def __init__(
        self,
        name: str | None,
        *,
        resolved_by: str | None = None,
        approval_keys: tuple[str, ...] = (),
    ) -> None:
        self.name = name
        self.resolved_by = resolved_by
        self.approval_keys = approval_keys


class _RecordingLogger:
    """Captures verbose_detail / warning calls for assertions."""

    def __init__(self) -> None:
        self.verbose: list[str] = []
        self.warnings: list[str] = []

    def verbose_detail(self, message: str, *args, **kwargs) -> None:
        self.verbose.append(message)

    def warning(self, message: str, *args, **kwargs) -> None:
        self.warnings.append(message)


class TestFilterLspFailClosed:
    def test_root_lsp_passes_as_project_authored_content(self) -> None:
        deps = [_FakeMcpDep("pyright")]
        logger = _RecordingLogger()

        result = filter_lsp_by_allow_executables(
            deps,
            {},
            logger,
        )

        assert result == deps
        assert logger.warnings == []

    def test_unapproved_declaring_package_is_filtered(self) -> None:
        deps = [
            _FakeMcpDep(
                "pyright",
                resolved_by="evil/package",
                approval_keys=("evil/package",),
            )
        ]
        logger = _RecordingLogger()

        result = filter_lsp_by_allow_executables(
            deps,
            {},
            logger,
        )

        assert result == []
        assert logger.warnings == [
            "Filtered 1 LSP server from 'evil/package': declaring package is "
            "not trusted yet. Run 'apm policy explain evil/package'; approve it "
            "only if policy permits."
        ]
        assert "LSP server 'pyright' from 'evil/package'" in logger.verbose[0]
        assert "if policy permits" in logger.verbose[0]

    def test_approval_is_bound_to_declarer_not_server_name(self) -> None:
        approved = _FakeMcpDep(
            "shared-name",
            resolved_by="trusted/package",
            approval_keys=("trusted/package",),
        )
        impersonator = _FakeMcpDep(
            "shared-name",
            resolved_by="evil/package",
            approval_keys=("evil/package",),
        )
        logger = _RecordingLogger()

        result = filter_lsp_by_allow_executables(
            [approved, impersonator],
            {"trusted/package": {"lsp": True}},
            logger,
        )

        assert result == [approved]

    def test_unlocked_declarer_gets_lock_recovery_guidance(self) -> None:
        dependency = _FakeMcpDep("pyright", resolved_by="ghe.example/owner/repo")
        logger = _RecordingLogger()

        result = filter_lsp_by_allow_executables([dependency], {}, logger)

        assert result == []
        assert "regenerate apm.lock.yaml" in logger.verbose[0]

    def test_multiple_unlocked_declarers_get_plural_recovery_guidance(self) -> None:
        dependencies = [
            _FakeMcpDep("pyright", resolved_by="ghe.example/owner/one"),
            _FakeMcpDep("ruff", resolved_by="ghe.example/owner/two"),
        ]
        logger = _RecordingLogger()

        result = filter_lsp_by_allow_executables(dependencies, {}, logger)

        assert result == []
        assert "approve each package" in logger.warnings[0]
        assert "LSP server 'pyright'" in logger.verbose[0]
        assert "LSP server 'ruff'" in logger.verbose[1]

    def test_mixed_declarers_get_distinct_recovery_guidance(self) -> None:
        dependencies = [
            _FakeMcpDep(
                "pyright",
                resolved_by="owner/locked",
                approval_keys=("owner/locked",),
            ),
            _FakeMcpDep("ruff", resolved_by="owner/unlocked"),
        ]
        logger = _RecordingLogger()

        result = filter_lsp_by_allow_executables(dependencies, {}, logger)

        assert result == []
        warning = logger.warnings[0]
        assert "policy explain owner/locked" in warning
        assert "regenerate apm.lock.yaml" in warning


@pytest.mark.parametrize(
    ("current", "candidate", "expected"),
    [
        (TRUST_DENIED, TRUST_DEPLOYED, TRUST_DENIED),
        (TRUST_DEPLOYED, TRUST_GATED, TRUST_GATED),
        (TRUST_GATED, TRUST_DENIED, TRUST_DENIED),
        (None, TRUST_ABSENT, None),
        (TRUST_ABSENT, None, TRUST_ABSENT),
        (TRUST_DEPLOYED, TRUST_DEPLOYED, TRUST_DEPLOYED),
        # Fail closed: an unrecognised status ranks ABOVE every known one, so a
        # surprise value can never silently downgrade a denial.
        (TRUST_DENIED, "some-future-status", "some-future-status"),
        ("some-future-status", TRUST_DEPLOYED, "some-future-status"),
    ],
)
def test_more_severe_exec_status_never_downgrades(current, candidate, expected) -> None:
    """The deny-wins ladder is total and monotonic in both argument orders."""
    assert more_severe_exec_status(current, candidate) == expected


def test_registrable_exec_statuses_excludes_every_untrusted_value() -> None:
    """Only cleared statuses may reach native Copilot registration."""
    assert TRUST_DENIED not in REGISTRABLE_EXEC_STATUSES
    assert TRUST_GATED not in REGISTRABLE_EXEC_STATUSES
    assert "some-future-status" not in REGISTRABLE_EXEC_STATUSES
    assert frozenset({None, TRUST_ABSENT, TRUST_DEPLOYED}) == REGISTRABLE_EXEC_STATUSES


class TestDenyIdentitySpellings:
    """Deny rules match every spelling of one package identity."""

    @staticmethod
    def _ctx(*, org_deny=(), project_allow=None, project_deny=None, user_deny=None):
        from types import SimpleNamespace

        from apm_cli.security.executables import ExecTrustContext, build_exec_trust_context

        policy = SimpleNamespace(
            executables=SimpleNamespace(
                deny_all=False, deny=list(org_deny), require=(), recommend=(), enforce=()
            ),
            bin_deploy=None,
        )
        ctx = build_exec_trust_context(
            policy=policy,
            project_data={
                "executables": {"allow": project_allow or {}, "deny": project_deny or {}}
            },
            migrate_user_legacy=False,
        )
        if user_deny:
            from dataclasses import replace

            ctx = replace(ctx, user_deny=user_deny)
        assert isinstance(ctx, ExecTrustContext)
        return ctx

    @pytest.mark.parametrize(
        "deny",
        [
            "contoso/pkg-hook",
            "Contoso/Pkg-Hook",
            "github.com/contoso/pkg-hook",
            "https://github.com/Contoso/Pkg-Hook.git",
            "contoso/*",
            "Contoso/*",
        ],
    )
    @pytest.mark.parametrize(
        "key", ["github.com/contoso/pkg-hook#v1.0.0", "contoso/pkg-hook", "Contoso/Pkg-Hook"]
    )
    def test_org_deny_matches_any_spelling(self, deny: str, key: str) -> None:
        from apm_cli.security.executables import resolve_exec_decision

        ctx = self._ctx(org_deny=[deny], project_allow={key.split("#")[0]: {"hooks": True}})
        decision = resolve_exec_decision(ctx, key, "hooks")
        assert decision.allowed is False
        assert decision.trust_state == "denied"

    def test_org_deny_covers_sub_packages(self) -> None:
        from apm_cli.security.executables import resolve_exec_decision

        key = "github.com/contoso/pkg-hook/skills/review"
        ctx = self._ctx(org_deny=["contoso/pkg-hook"], project_allow={key: {"hooks": True}})
        assert resolve_exec_decision(ctx, key, "hooks").allowed is False

    def test_denied_prefix_does_not_cover_sibling_names(self) -> None:
        from apm_cli.security.executables import resolve_exec_decision

        key = "contoso/pkg-hooks"
        ctx = self._ctx(org_deny=["contoso/pkg-hook"], project_allow={key: {"hooks": True}})
        assert resolve_exec_decision(ctx, key, "hooks").allowed is True

    @pytest.mark.parametrize("layer", ["project", "user"])
    def test_project_and_user_deny_match_any_spelling(self, layer: str) -> None:
        from apm_cli.security.executables import resolve_exec_decision

        key = "github.com/contoso/pkg-hook#v1.0.0"
        deny = {"contoso/pkg-hook#1.0.0": {"hooks": True}}
        kwargs = {"project_deny": deny} if layer == "project" else {"user_deny": deny}
        ctx = self._ctx(project_allow={"github.com/contoso/pkg-hook": {"hooks": True}}, **kwargs)
        assert resolve_exec_decision(ctx, key, "hooks").allowed is False

    def test_deny_ignores_the_port_and_grants_do_not(self) -> None:
        """#3927: denies cover every port of a package; a grant names one server."""
        from apm_cli.security.executables import canonical_exec_identity, resolve_exec_decision

        assert canonical_exec_identity("https://github.com:8443/Evil/Pkg.git") == "evil/pkg"
        ported = "github.com:8443/acme/tool"
        ctx = self._ctx(project_allow={"acme/tool": {"hooks": True}})
        assert resolve_exec_decision(ctx, "acme/tool", "hooks").allowed is True
        assert resolve_exec_decision(ctx, ported, "hooks").allowed is False

        allow = {ported: {"hooks": True}}
        for ctx in (
            self._ctx(org_deny=["acme/tool"], project_allow=allow),
            self._ctx(org_deny=["github.com:7999/acme/*"], project_allow=allow),
            self._ctx(project_allow=allow, project_deny={"acme/tool": {"hooks": True}}),
        ):
            assert resolve_exec_decision(ctx, ported, "hooks").allowed is False

    def test_content_bound_keys_stay_exact(self) -> None:
        from apm_cli.security.executables import canonical_exec_identity

        key = "bundle@sha256:abc"
        assert canonical_exec_identity(key) == key


class TestCanonicalExecIdentityHostCase:
    """Identity case folding follows the host classification."""

    @pytest.mark.parametrize(
        ("key", "expected"),
        [
            ("contoso.ghe.com/Evil/Pkg", "contoso.ghe.com/evil/pkg"),
            ("https://dev.azure.com/Org/Proj/_git/Repo", "dev.azure.com/org/proj/repo"),
            ("dev.azure.com/org/proj/_git/repo#main", "dev.azure.com/org/proj/repo"),
            ("gitlab.com/Evil/Pkg", "gitlab.com/Evil/Pkg"),
        ],
    )
    def test_case_folding_by_host(self, key: str, expected: str) -> None:
        from apm_cli.security.executables import canonical_exec_identity

        assert canonical_exec_identity(key) == expected

    def test_ghes_host_from_environment_is_case_folded(self, monkeypatch) -> None:
        from apm_cli.security.executables import canonical_exec_identity

        monkeypatch.setenv("GITHUB_HOST", "ghe.corp.example.com")
        assert canonical_exec_identity("ghe.corp.example.com/Evil/Pkg") == (
            "ghe.corp.example.com/evil/pkg"
        )

    def test_ado_deny_matches_case_variant(self) -> None:
        from types import SimpleNamespace

        from apm_cli.security.executables import build_exec_trust_context, resolve_exec_decision

        key = "dev.azure.com/Org/Proj/Repo"
        policy = SimpleNamespace(
            executables=SimpleNamespace(
                deny_all=False,
                deny=["dev.azure.com/org/proj/repo"],
                require=(),
                recommend=(),
                enforce=(),
            ),
            bin_deploy=None,
        )
        ctx = build_exec_trust_context(
            policy=policy,
            project_data={"executables": {"allow": {key: {"hooks": True}}}},
            migrate_user_legacy=False,
        )
        assert resolve_exec_decision(ctx, key, "hooks").allowed is False


def test_locked_replacement_keys_name_the_selected_source() -> None:
    """MCP/LSP views key a replaced row like the install gate, never upstream (#3710)."""
    from apm_cli.deps.lockfile import LockFile
    from apm_cli.security.executables import locked_dependency_approval_keys

    lock = LockFile.from_yaml(
        """lockfile_version: '3'
replacements:
  - for:
      git: https://github.com/acme/hooky.git
    with:
      path: ./local-hooky
dependencies:
  - repo_url: acme/hooky
    version: 1.0.0
    replacement:
      source:
        path: ./local-hooky
      requests:
        - parent: null
          ref: null
          skills: null
          targets: null
          dev: false
          alias: null
"""
    )
    (locked,) = lock.get_package_dependencies()

    assert locked_dependency_approval_keys(locked) == ("./local-hooky",)


def test_locked_fork_replacement_keys_name_the_fork() -> None:
    """A fork-replaced row is keyed by the fork, with and without its ref."""
    from apm_cli.deps.lockfile import LockFile
    from apm_cli.security.executables import locked_dependency_approval_keys

    lock = LockFile.from_yaml(
        """lockfile_version: '3'
replacements:
  - for:
      git: https://github.com/acme/hooky.git
    with:
      git: https://github.com/myfork/hooky.git
      ref: v1.2.0
dependencies:
  - repo_url: acme/hooky
    version: 1.0.0
    replacement:
      source:
        git: https://github.com/myfork/hooky.git
        ref: v1.2.0
      requests:
        - parent: null
          ref: null
          skills: null
          targets: null
          dev: false
          alias: null
"""
    )
    (locked,) = lock.get_package_dependencies()

    assert locked_dependency_approval_keys(locked) == ("myfork/hooky#v1.2.0", "myfork/hooky")
