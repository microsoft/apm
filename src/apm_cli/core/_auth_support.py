"""Boundary-free support methods for :class:`apm_cli.core.auth.AuthResolver`.

This module exists purely to keep ``core/auth.py`` under the 800-line ceiling
(issue #1078, Strangler Stage 2) WITHOUT changing behaviour or the public
import/monkeypatch surface. Everything here is composed into ``AuthResolver``
via :class:`_AuthSupportMixin`, so callers and tests continue to patch
``apm_cli.core.auth.AuthResolver`` exactly as before.

AUTH-BOUNDARY INVARIANT (see ``scripts/lint-auth-signals.sh``): nothing in this
module names the Azure bearer-provider lookup symbol or issues a ``git
ls-remote`` against ADO. The PAT->AAD bearer protocol stays wholly inside
``core/auth.py``. The only seam back into the boundary is
``self._ado_bearer_provider()`` (defined on ``AuthResolver``), through which
:meth:`_AuthSupportMixin.build_error_context` obtains the provider without
naming the boundary symbol here.

To avoid an import cycle, this module never imports ``apm_cli.core.auth`` at
module scope -- ``HostInfo`` is imported lazily inside :meth:`classify_host`.
"""

from __future__ import annotations

import logging
import os
import sys

from apm_cli.utils.github_host import (
    is_azure_devops_hostname,
    is_gitlab_hostname,
    is_valid_fqdn,
)

logger = logging.getLogger(__name__)


_PORT_CREDENTIAL_DOCS_URL = (
    "https://microsoft.github.io/apm/getting-started/authentication/"
    "#custom-port-hosts-and-per-port-credentials"
)


def _org_to_env_suffix(org: str) -> str:
    """Convert an org name to an env-var suffix (upper-case, hyphens → underscores)."""
    return org.upper().replace("-", "_")


class _AuthSupportMixin:
    """Boundary-free helpers mixed into :class:`AuthResolver`.

    These methods carry no Azure bearer-provider or ADO ls-remote references,
    so they live outside ``auth.py`` to keep that file under the size ceiling.
    They are invoked through ``self`` on an ``AuthResolver`` instance and rely
    on attributes/methods that ``AuthResolver`` itself defines (``self.resolve``,
    ``self._token_manager``, ``self._lock``, ``self._ado_bearer_provider`` …).
    """

    # -- host classification ------------------------------------------------

    @staticmethod
    def classify_host(
        host: str,
        port: int | None = None,
        host_type: str | None = None,
    ) -> object:
        """Return a ``HostInfo`` describing *host*.

        ``port`` is carried through onto the returned ``HostInfo`` so that
        downstream code (cache keys, credential-helper input, error text)
        can discriminate between the same hostname on different ports.
        Host-kind classification itself is transport-agnostic -- the port
        never influences whether a host is GitHub/GHES/ADO/generic.
        ``host_type`` is an explicit manifest hint for hosts whose names do
        not reveal the backing service.
        """
        # Lazy import keeps this module free of an auth.py module-scope cycle.
        from apm_cli.core.auth import HostInfo

        h = host.lower()
        host_type_value = (host_type or "").strip().lower()

        if h == "github.com":
            return HostInfo(
                host=host,
                kind="github",
                has_public_repos=True,
                api_base="https://api.github.com",
                port=port,
            )

        if h.endswith(".ghe.com"):
            return HostInfo(
                host=host,
                kind="ghe_cloud",
                has_public_repos=False,
                api_base=f"https://{host}/api/v3",
                port=port,
            )

        if is_azure_devops_hostname(host):
            return HostInfo(
                host=host,
                kind="ado",
                has_public_repos=True,
                api_base="https://dev.azure.com",
                port=port,
            )

        if host_type_value == "gitlab":
            api_base = "https://gitlab.com/api/v4" if h == "gitlab.com" else f"https://{h}/api/v4"
            return HostInfo(
                host=host,
                kind="gitlab",
                has_public_repos=True,
                api_base=api_base,
                port=port,
            )
        if host_type_value:
            raise ValueError(
                f"Unsupported dependency host type: {host_type_value}. Supported values: gitlab"
            )

        # GHES: GITHUB_HOST is set to a non-github.com, non-ghe.com FQDN
        ghes_host = os.environ.get("GITHUB_HOST", "").lower()
        if (
            ghes_host
            and ghes_host == h
            and ghes_host not in {"github.com", "gitlab.com"}
            and not ghes_host.endswith(".ghe.com")
        ):
            if is_valid_fqdn(ghes_host):
                return HostInfo(
                    host=host,
                    kind="ghes",
                    has_public_repos=True,
                    api_base=f"https://{host}/api/v3",
                    port=port,
                )

        # GitLab (SaaS + env-configured self-managed) -- after GHES per spec (no silent GHES -> GitLab)
        if is_gitlab_hostname(host):
            api_base = "https://gitlab.com/api/v4" if h == "gitlab.com" else f"https://{h}/api/v4"
            return HostInfo(
                host=host,
                kind="gitlab",
                has_public_repos=True,
                api_base=api_base,
                port=port,
            )

        # Generic FQDN (Bitbucket, self-hosted non-GitLab, etc.)
        return HostInfo(
            host=host,
            kind="generic",
            has_public_repos=True,
            api_base=f"https://{host}/api/v3",
            port=port,
        )

    # -- token type detection -----------------------------------------------

    @staticmethod
    def detect_token_type(token: str) -> str:
        """Classify a token string by its prefix.

        Note: EMU (Enterprise Managed Users) tokens use standard PAT
        prefixes (``ghp_`` or ``github_pat_``).  There is no prefix that
        identifies a token as EMU-scoped — that's a property of the
        account, not the token format.

        Prefix reference (docs.github.com):
        - ``github_pat_`` → fine-grained PAT
        - ``ghp_``        → classic PAT
        - ``ghu_``        → OAuth user-to-server (e.g. ``gh auth login``)
        - ``gho_``        → OAuth app token
        - ``ghs_``        → GitHub App installation (server-to-server)
        - ``ghr_``        → GitHub App refresh token
        """
        if token.startswith("github_pat_"):
            return "fine-grained"
        if token.startswith("ghp_"):
            return "classic"
        if token.startswith("ghu_"):
            return "oauth"
        if token.startswith("gho_"):
            return "oauth"
        if token.startswith("ghs_"):
            return "github-app"
        if token.startswith("ghr_"):
            return "github-app"
        return "unknown"

    @staticmethod
    def gitlab_rest_headers(
        token: str | None,
        *,
        oauth_bearer: bool = False,
    ) -> dict[str, str]:
        """Build HTTP headers for GitLab REST API v4 calls.

        Personal access tokens use ``PRIVATE-TOKEN``. OAuth2 access tokens
        typically use ``Authorization: Bearer <token>``; set *oauth_bearer*
        to use that style.

        Does not log or print *token*. Callers must not log the returned dict.
        """
        if not token:
            return {}
        if oauth_bearer:
            return {"Authorization": f"Bearer {token}"}
        return {"PRIVATE-TOKEN": token}

    # -- error context ------------------------------------------------------

    def build_error_context(
        self,
        host: str,
        operation: str,
        org: str | None = None,
        *,
        port: int | None = None,
        dep_url: str | None = None,
        bearer_also_failed: bool = False,
    ) -> str:
        """Build an actionable error message for auth failures.

        ``bearer_also_failed=True`` prepends a single line to the Case 4
        block (PAT set, az available, both attempts failed) clarifying
        that ADO_APM_PAT was tried first and rejected before the bearer
        attempt -- so the user understands why both halves of the
        protocol failed without having to read the full diagnostic
        context. Callers MUST only set this when the bearer attempt
        actually ran (see :class:`BearerFallbackOutcome.bearer_attempted`).
        """
        auth_ctx = self.resolve(host, org, port=port)
        host_info = auth_ctx.host_info
        display = host_info.display_name

        # --- ADO-specific error cases ---
        if host_info.kind == "ado":
            # Provider access is routed through the auth-boundary accessor on
            # AuthResolver so the bearer-provider symbol stays inside
            # core/auth.py (see scripts/lint-auth-signals.sh, Rule A).
            provider = self._ado_bearer_provider()
            az_available = provider.is_available()
            pat_set = bool(os.environ.get("ADO_APM_PAT"))

            org_part = org or ""
            if not org_part:
                source_url = dep_url or ""
                if source_url:
                    parts = source_url.replace("https://", "").split("/")
                    if len(parts) >= 2 and (
                        parts[0] in ("dev.azure.com",) or parts[0].endswith(".visualstudio.com")
                    ):
                        org_part = parts[1] if len(parts) > 1 else ""

            token_url = (
                f"https://dev.azure.com/{org_part}/_usersSettings/tokens"
                if org_part
                else "https://dev.azure.com/<org>/_usersSettings/tokens"
            )

            if pat_set:
                if az_available:
                    # Case 4: PAT and bearer were both available; both attempts
                    # failed. We may not have observed an explicit 401 (could be
                    # a 404, a network error, etc.) so the wording stays
                    # tentative -- see #856 review C6.
                    prefix = (
                        "    ADO_APM_PAT was rejected; az cli bearer was also rejected.\n\n"
                        if bearer_also_failed
                        else ""
                    )
                    return (
                        f"\n{prefix}"
                        f"    ADO_APM_PAT is set, and Azure CLI credentials may also be available,\n"
                        f"    but the Azure DevOps request still failed.\n\n"
                        f"    If this is an authentication failure, the PAT may be expired, revoked,\n"
                        f"    or scoped to a different org, and Azure CLI credentials may need to\n"
                        f"    be refreshed.\n\n"
                        f"    To fix:\n"
                        f"      1. Unset the PAT to test Azure CLI auth only:  unset ADO_APM_PAT\n"
                        f"      2. Re-authenticate Azure CLI if needed:        az login\n"
                        f"      3. Retry:                                       apm install\n\n"
                        f"    Docs: https://microsoft.github.io/apm/getting-started/authentication/#azure-devops"
                    )
                # PAT set but rejected, no az -> bare PAT failure
                return (
                    f"\n    ADO_APM_PAT is set, but the Azure DevOps request failed.\n"
                    f"    If this is an authentication failure, the token may be expired,\n"
                    f"    revoked, or scoped to a different org.\n\n"
                    f"    Generate a new PAT at {token_url}\n"
                    f"    with Code (Read) scope.\n\n"
                    f"    Docs: https://microsoft.github.io/apm/getting-started/authentication/#azure-devops"
                )

            # No PAT set
            if not az_available:
                # Case 1: no az, no PAT
                return (
                    f"\n    Azure DevOps requires authentication. You have two options:\n\n"
                    f"    1. Install Azure CLI and sign in (recommended for Entra ID users):\n"
                    f"         brew install azure-cli            # macOS\n"
                    f"         winget install Microsoft.AzureCLI # Windows\n"
                    f"         apt-get install azure-cli         # Debian/Ubuntu\n"
                    f"         dnf install azure-cli             # Fedora/RHEL\n"
                    f"         (full guide: https://aka.ms/InstallAzureCli)\n"
                    f"         az login\n"
                    f"         apm install                   # retry -- no env var needed\n\n"
                    f"    2. Use a Personal Access Token:\n"
                    f"         export ADO_APM_PAT=your_token\n"
                    f"         (Create one at {token_url} with Code (Read) scope.)\n\n"
                    f"    Docs: https://microsoft.github.io/apm/getting-started/authentication/#azure-devops"
                )

            # az is available; check if logged in by trying to get tenant
            tenant = provider.get_current_tenant_id()
            if tenant is None:
                # Case 3: az present, not logged in
                return (
                    "\n    Azure DevOps requires authentication. You have two options:\n\n"
                    "    1. Sign in with Azure CLI (recommended for Entra ID users):\n"
                    "         az login\n"
                    "         apm install                   # retry -- no env var needed\n\n"
                    "    2. Use a Personal Access Token:\n"
                    "         export ADO_APM_PAT=your_token\n\n"
                    "    Docs: https://microsoft.github.io/apm/getting-started/authentication/#azure-devops"
                )

            # Case 2: az returned token (tenant known) but ADO rejected it.
            # Note: bearer_also_failed=True is structurally unreachable here --
            # callers only set it when source == "ADO_APM_PAT" (i.e. pat_set
            # is True), and Case 2 lives in the `not pat_set` branch. We do
            # not render a "PAT was also rejected" prefix in this case
            # because no PAT was tried.
            return (
                f"\n    Your az cli session (tenant: {tenant}) returned a bearer token,\n"
                f"    but Azure DevOps rejected it (HTTP 401).\n\n"
                f"    Check that you are signed into the correct tenant:\n"
                f"      az account show\n"
                f"      az login --tenant <correct-tenant-id>\n\n"
                f"    Docs: https://microsoft.github.io/apm/getting-started/authentication/#azure-devops"
            )

        # --- Non-ADO error paths ---
        lines: list[str] = [f"Authentication failed for {operation} on {display}."]

        if auth_ctx.token:
            lines.append(
                f"Token was provided (source: {auth_ctx.source}, type: {auth_ctx.token_type})."
            )
            if host_info.kind == "ghe_cloud":
                lines.append(
                    "GHE Cloud Data Residency hosts (*.ghe.com) require "
                    "enterprise-scoped tokens. Ensure your PAT is authorized "
                    "for this enterprise."
                )
            elif host_info.kind == "gitlab":
                lines.append(
                    "Ensure your GitLab personal or project access token meets the "
                    "API read requirements for your instance policy."
                )
            elif host.lower() == "github.com":
                lines.append(
                    "If your organization uses SAML SSO or is an EMU org, "
                    "ensure your PAT is authorized at "
                    "https://github.com/settings/tokens"
                )
            elif host_info.kind == "generic":
                lines.append("Verify credentials for this host in your git credential helper.")
            else:
                lines.append(
                    "If your organization uses SAML SSO, you may need to "
                    "authorize your token at https://github.com/settings/tokens"
                )
        else:
            lines.append("No token available.")
            if host_info.kind == "gitlab":
                lines.append(
                    "Set GITLAB_APM_PAT or GITLAB_TOKEN, or configure git credential fill "
                    f"for {display}."
                )
            elif host_info.kind == "generic":
                lines.append(
                    "APM does not apply GitHub PAT environment variables to generic git "
                    f"hosts; configure git credential fill for {display} or use a "
                    "public repository if available."
                )
            else:
                lines.append("Set GITHUB_APM_PAT or GITHUB_TOKEN, or run 'gh auth login'.")

        if org and host_info.kind not in ("ado", "gitlab", "generic"):
            lines.append(
                f"If packages span multiple organizations, set per-org tokens: "
                f"GITHUB_APM_PAT_{_org_to_env_suffix(org)}"
            )

        # When a custom port is in play, helpers that key by hostname alone
        # (some `gh` integrations, older keychain backends) can silently
        # return the wrong credential. Point the user at the concrete fix.
        if host_info.port is not None:
            lines.append(
                f"[i] Host '{display}' -- this helper may key by host only.\n"
                f"    Verify with: printf 'protocol=https\\nhost={display}\\n\\n'"
                f" | git credential fill\n"
                f"    Docs: {_PORT_CREDENTIAL_DOCS_URL}"
            )

        lines.append("Run with --verbose for detailed auth diagnostics.")
        return "\n".join(lines)

    # -- internals ----------------------------------------------------------

    @staticmethod
    def _purpose_for_host(host_info) -> str:
        if host_info.kind == "ado":
            return "ado_modules"
        if host_info.kind == "gitlab":
            return "gitlab_modules"
        if host_info.kind == "generic":
            return "generic_modules"
        return "modules"

    def _identify_env_source(self, purpose: str) -> str:
        """Return the name of the first env var that matched for *purpose*."""
        for var in self._token_manager.TOKEN_PRECEDENCE.get(purpose, []):
            if os.environ.get(var):
                return var
        return "env"

    @staticmethod
    def _build_git_env(
        token: str | None = None,
        *,
        scheme: str = "basic",
        host_kind: str = "github",
    ) -> dict:
        """Pre-built env dict for subprocess git calls.

        For ADO bearer tokens (scheme='bearer'), injects an Authorization header
        via GIT_CONFIG_COUNT/KEY/VALUE env vars (see github_host.build_ado_bearer_git_env).
        For all other cases, behavior is unchanged.
        """
        env = os.environ.copy()
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GIT_ASKPASS"] = "echo"
        if scheme == "bearer" and token and host_kind == "ado":
            # B2 #852: skip GIT_TOKEN for bearer scheme -- the JWT is injected via
            # GIT_CONFIG_VALUE_0 only; GIT_TOKEN here would leak it into every
            # child-process env (visible in /proc/<pid>/environ, ps eww).
            #
            # #1214 follow-up: a stale GIT_TOKEN already in the parent env
            # (set by a prior shell, CI step, or another tool) would survive
            # the os.environ.copy() above and defeat the isolation guarantee.
            # Drop it explicitly so the bearer env is clean by construction.
            env.pop("GIT_TOKEN", None)
            from apm_cli.utils.github_host import build_ado_bearer_git_env

            env.update(build_ado_bearer_git_env(token))
        elif token:
            env["GIT_TOKEN"] = token
        return env

    def emit_stale_pat_diagnostic(self, host_display: str) -> None:
        """Emit a [!] warning when PAT was rejected but bearer succeeded.

        F3 #852: when an InstallLogger is wired via :meth:`set_logger`, the
        warning is collected by its DiagnosticCollector so it appears in the
        install summary. Without a logger (e.g. unit tests) we fall back to
        the inline ``_rich_warning`` emission for backwards compatibility.

        #1212 follow-up: dedup per host_display so the user sees ONE warning
        per ADO host even when preflight, list_remote_refs, and the clone
        path each trigger the bearer-fallback path against the same host.

        Naming: previously ``_emit_stale_pat_diagnostic`` (private). Public
        now (#856 follow-up C9) so external modules (validation.py,
        github_downloader.py) do not reach into the underscore API.

        #1214 follow-up: guard the check-then-add under self._lock so two
        threads (parallel install) racing on the same ADO host cannot both
        pass the membership check before either calls add(); without the
        lock the dedup set defeats its own purpose.
        """
        with self._lock:
            if host_display in self._stale_pat_warned_hosts:
                return
            self._stale_pat_warned_hosts.add(host_display)
        msg = f"ADO_APM_PAT was rejected for {host_display}; fell back to az cli bearer."
        detail = "Consider unsetting the stale variable."
        diagnostics = self._diagnostics_or_none()
        if diagnostics is not None:
            diagnostics.warn(msg, detail=detail)
            return
        try:
            from apm_cli.utils.console import _rich_warning

            _rich_warning(msg, symbol="warning")
            _rich_warning(f"    {detail}", symbol="warning")
        except ImportError as exc:
            logger.debug("Console module unavailable for stale-PAT warning; skipping: %s", exc)

    # Backwards-compat alias for any in-tree caller still importing the
    # private name. Safe to remove once all callers move to the public name.
    _emit_stale_pat_diagnostic = emit_stale_pat_diagnostic

    def _diagnostics_or_none(self):
        """Return the wired logger's DiagnosticCollector, or None."""
        if self._logger is None:
            return None
        try:
            return self._logger.diagnostics
        except AttributeError:
            return None

    def notify_auth_source(self, host_display: str, ctx) -> None:
        """Emit the verbose auth-source line for ``host_display`` exactly once.

        F2 #852: routes through CommandLogger when wired (so the line obeys
        the same verbose channel as every other diagnostic), and falls back
        to a direct stderr write when no logger is set so the existing
        bearer e2e tests keep working.
        """
        host_key = (host_display or "").lower()
        if not host_key or host_key in self._verbose_auth_logged_hosts:
            return
        self._verbose_auth_logged_hosts.add(host_key)
        if ctx is None or getattr(ctx, "source", "none") == "none":
            return
        if getattr(ctx, "auth_scheme", None) == "bearer":
            line = f"  [i] {host_key} -- using bearer from az cli (source: {ctx.source})"
        else:
            line = f"  [i] {host_key} -- token from {ctx.source}"
        if self._logger is not None and getattr(self._logger, "verbose", False):
            try:
                from apm_cli.utils.console import _rich_echo

                _rich_echo(line, color="dim")
                return
            except ImportError as exc:
                logger.debug(
                    "Console module unavailable for auth-source logging; skipping: %s", exc
                )
        # No logger wired -- the install path always wires one in the
        # bearer branch, so this fallback only fires in unit-test contexts
        # that opt-in via APM_VERBOSE=1.
        sys.stderr.write(line + "\n")


__all__ = ["_PORT_CREDENTIAL_DOCS_URL", "_AuthSupportMixin", "_org_to_env_suffix"]
