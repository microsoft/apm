"""``apm enroll`` -- one-shot onboarding onto a marketplace on a new machine.

Wraps the steps a new joiner otherwise runs by hand: make sure a credential
for the marketplace host exists, register the marketplace, and smoke-test
that it is browsable.

Only the credential step is new logic. Registration and browsing delegate to
``apm marketplace add`` / ``apm marketplace browse`` via ``ctx.invoke``, so
this command inherits their source parsing, ref handling, manifest-path
auto-detection and error rendering rather than duplicating any of it.

Deliberately, this command does **not** pre-flight the marketplace fetch.
``marketplace.client._auto_detect_path`` already probes every candidate
manifest location, and ``_fetch_via_api`` already maps 404 -> "try the next
path" and raises on anything else. A second probe here would duplicate that
logic, could drift from it, and would have to answer "is this repo public?"
-- unanswerable cheaply, since unauthenticated GitHub API requests are capped
at 60/hour per IP and an exhausted quota returns a 403 indistinguishable from
a permissions failure. So the credential step asks only whether a token
*exists*, and lets registration be the authority on whether it works.

Known limitation, inherited not introduced: an *invalid* token against a
private GitHub repo is reported as "No marketplace.json found ... Checked:
<3 paths>" rather than as an auth failure. ``AuthResolver.try_with_fallback``
retries the failed authenticated request anonymously, and GitHub 404s a
private repo it will not admit exists, so the original 401 is swallowed.
``apm marketplace add`` alone behaves identically -- verified -- so this is
an APM-wide diagnostic gap, not something enrolment can fix by re-probing.
Worth a separate issue against the fetch path.

On resolution, note that APM finds tokens itself (see
``AuthResolver._resolve_token``): ``GITLAB_APM_PAT`` -> ``GITLAB_TOKEN`` ->
git credential helper for GitLab, and ``GITHUB_APM_PAT`` -> ``GITHUB_TOKEN``
-> ``gh auth token`` -> git credential helper for GitHub. Because the env
vars are consulted *before* any credential helper, exporting one is enough;
there is no need to wire an external CLI in as a git credential helper, and
no need to evict a shadowing platform keychain entry. When a shadowing entry
is detected we say so instead of deleting it -- silently mutating a global
credential store is not something a package manager should do on the user's
behalf.
"""

from __future__ import annotations

import os
import sys
from urllib.parse import quote, urlparse

import click

from ..core.command_logger import CommandLogger

# Scopes the token-creation page is prefilled with, per host class.
#   GitLab: granular scope names, passed as a ?scopes= query param.
#   GitHub: classic-PAT scope; `repo` is the narrowest scope that reads a
#           private repo's contents (there is no read-only variant).
_GITLAB_SCOPES = "read_repository,read_api"
_GITHUB_SCOPES = "repo"

_GITHUB_KINDS = ("github", "ghe_cloud", "ghes")

_EPILOG = (
    "\b\n"
    "Examples:\n"
    "  apm enroll acme/apm-marketplace --name acme\n"
    "  apm enroll gitlab.com/acme/team/apm-marketplace --name acme\n"
    "  apm enroll https://github.com/acme/apm-marketplace --name acme\n"
    "  apm enroll ./local-marketplace --name scratch\n"
    "\b\n"
    "Credentials:\n"
    "  Private marketplaces need a token. APM reads it from\n"
    "  GITLAB_APM_PAT / GITLAB_TOKEN (GitLab) or GITHUB_APM_PAT /\n"
    "  GITHUB_TOKEN / the gh CLI (GitHub), falling back to your git\n"
    "  credential helper."
)


def _token_env_var(host_kind: str) -> str | None:
    """Return the env var APM reads a token from for this host class."""
    if host_kind == "gitlab":
        return "GITLAB_APM_PAT"
    if host_kind in _GITHUB_KINDS:
        return "GITHUB_APM_PAT"
    return None


def _token_scopes(host_kind: str) -> str:
    """Return the scopes to prefill on this host's token-creation page."""
    return _GITHUB_SCOPES if host_kind in _GITHUB_KINDS else _GITLAB_SCOPES


def resolve_existing_token(host: str) -> tuple[str | None, str]:
    """Return ``(token, source)`` for *host* using APM's own resolution chain.

    ``source`` is the human-readable origin (env var name, ``gh-auth-token``,
    ``git-credential-fill``, or ``none``). Never returns the token in logs --
    callers must not print it.

    The resolver is deliberately throwaway. ``AuthResolver`` caches per
    instance, and ``run_enroll`` may set a token into ``os.environ`` *after*
    this returns a miss; a retained resolver would keep serving the cached
    ``token=None`` and the registration below would not see the new token. Do
    not hoist this to a shared instance -- see
    ``test_negative_resolution_is_not_cached_across_resolvers``.
    """
    from ..core.auth import AuthResolver

    ctx = AuthResolver().resolve(host)
    return getattr(ctx, "token", None), getattr(ctx, "source", "none")


def _detect_shadowing_helper(host: str) -> bool:
    """Return True when a macOS osxkeychain helper may shadow a good token.

    The system osxkeychain helper is unscoped, so a stale entry for *host*
    answers ``git credential fill`` before any scoped helper and keeps
    winning. We only *report* this -- see the module docstring.
    """
    if sys.platform != "darwin":
        return False
    import shutil
    import subprocess

    helper = shutil.which("git-credential-osxkeychain") or next(
        (
            path
            for path in (
                "/usr/libexec/git-core/git-credential-osxkeychain",
                "/Library/Developer/CommandLineTools/usr/libexec/git-core/"
                "git-credential-osxkeychain",
            )
            if os.path.exists(path)
        ),
        None,
    )
    if not helper:
        return False
    try:
        result = subprocess.run(
            [helper, "get"],
            input=f"protocol=https\nhost={host}\n\n",
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.SubprocessError, OSError):
        return False
    return "password=" in (result.stdout or "")


def _is_interactive() -> bool:
    """Return True when it is safe to prompt (honours APM_NON_INTERACTIVE/CI)."""
    if os.environ.get("APM_NON_INTERACTIVE") or os.environ.get("CI"):
        return False
    return sys.stdin.isatty() and sys.stdout.isatty()


def _token_page_url(host: str, host_kind: str, token_name: str) -> str:
    """Return the host's token-creation page, name and scopes prefilled.

    GitHub and GitLab use different paths and different query-param names.
    Both accept a prefilled description so the token is identifiable later.
    The host is carried through rather than hardcoded, so GitHub Enterprise
    Server points at the enterprise instance.
    """
    if host_kind in _GITHUB_KINDS:
        return (
            f"https://{host}/settings/tokens/new"
            f"?description={quote(token_name)}&scopes={quote(_GITHUB_SCOPES)}"
        )
    return (
        f"https://{host}/-/user_settings/personal_access_tokens"
        f"?name={quote(token_name)}&scopes={quote(_GITLAB_SCOPES)}"
    )


def _open_token_page(logger: CommandLogger, host: str, host_kind: str) -> None:
    """Open (or print) the host's token page with name and scopes prefilled."""
    import platform
    import webbrowser

    token_name = f"apm-{os.environ.get('USER') or 'user'}-{platform.node().split('.')[0]}"
    url = _token_page_url(host, host_kind, token_name)
    logger.progress(
        f"Create a token with scopes '{_token_scopes(host_kind)}', then paste it below.",
        symbol="info",
    )
    opened = False
    try:
        opened = webbrowser.open(url)
    except Exception:  # pragma: no cover - platform dependent
        opened = False
    if not opened:
        logger.tree_item(f"  {url}")


def _host_and_kind(source: str, host_flag: str | None) -> tuple[str, str, str]:
    """Return ``(url, host, host_kind)`` for *source*.

    Reuses ``marketplace add``'s parser so shorthand, SSH and full-URL forms
    behave identically across both commands. Local sources yield an empty
    host, which skips the credential step.
    """
    from ..core.auth import AuthResolver
    from .marketplace import _parse_marketplace_source

    url, kind, embedded_host = _parse_marketplace_source(source, host_flag)
    if kind == "local":
        return url, "", "local"
    host = embedded_host or urlparse(url).netloc
    return url, host, AuthResolver.classify_host(host).kind if host else "local"


def ensure_credential(
    logger: CommandLogger,
    host: str,
    host_kind: str,
) -> int:
    """Make sure a credential for *host* is available, prompting if needed.

    Returns a process exit code: ``0`` to continue with registration.

    Only asks whether a token *exists*. Whether it actually works is decided
    by the fetch during registration, which reports auth failures precisely
    (see the module docstring). A missing token is therefore not fatal here:
    a public marketplace needs none, so we warn and let registration decide.
    """
    env_var = _token_env_var(host_kind)
    if not env_var:
        # ADO / generic git: APM resolves these differently and there is no
        # token page to point at. Leave it to marketplace add.
        return 0

    token, token_source = resolve_existing_token(host)
    if token:
        logger.success(f"Using {host} credential from {token_source}", symbol="check")
        return 0

    logger.progress(f"No {host} credential found.", symbol="info")

    if _detect_shadowing_helper(host):
        logger.warning(
            f"A macOS keychain entry for {host} may be shadowing newer "
            f"credentials. If enrollment keeps failing, clear it with:\n"
            f"    printf 'protocol=https\\nhost={host}\\n\\n' | "
            f"git credential-osxkeychain erase"
        )

    scopes = _token_scopes(host_kind)

    if not _is_interactive():
        # Not fatal: a public marketplace needs no credential, and this
        # cannot tell public from private without a probe that GitHub
        # rate-limits. Registration below gives the real answer.
        logger.warning(
            f"Continuing without a credential: this succeeds for a public "
            f"marketplace and fails below for a private one. Set {env_var} "
            f"to a token with scopes '{scopes}' to authenticate."
        )
        return 0

    _open_token_page(logger, host, host_kind)
    pasted = click.prompt("    Paste the token (input hidden)", hide_input=True, default="")
    pasted = (pasted or "").strip()
    if not pasted:
        logger.error("No token entered.")
        return 1

    # Set for the rest of THIS process so the registration below resolves it
    # through the normal chain -- no second prompt, no special plumbing.
    os.environ[env_var] = pasted
    logger.warning(
        f"This token is set for the current command only. To persist it, add "
        f"to your shell profile:\n    export {env_var}='<token>'"
    )
    return 0


def run_enroll(
    ctx: click.Context,
    source: str,
    name: str | None,
    ref: str | None,
    host_flag: str | None,
    no_token: bool,
    verbose: bool,
) -> int:
    """Execute the enrollment flow. Returns a process exit code."""
    logger = CommandLogger("enroll", verbose=verbose)

    try:
        url, host, host_kind = _host_and_kind(source, host_flag)
    except Exception as exc:
        logger.error(f"Invalid source '{source}': {exc}")
        return 1

    # --- Step 1: credentials ------------------------------------------
    if host and not no_token:
        code = ensure_credential(logger, host, host_kind)
        if code != 0:
            return code

    # --- Step 2: register ---------------------------------------------
    from .marketplace import add as marketplace_add
    from .marketplace import browse as marketplace_browse

    alias = name
    logger.start(f"Registering marketplace from {url}...", symbol="running")
    try:
        ctx.invoke(
            marketplace_add,
            source=source,
            name=alias,
            ref=ref,
            branch=None,
            host=host_flag,
            verbose=verbose,
        )
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
        if code != 0:
            logger.error("Marketplace registration failed -- see the error above.")
            return code

    # 'marketplace add' derives the alias from the repo name when --name is
    # omitted; recover whatever it actually registered so browse targets the
    # right entry rather than a guess. Last-element is correct because
    # add_marketplace() filters any same-name entry then appends (registry.py).
    if not alias:
        try:
            from ..marketplace.registry import get_registered_marketplaces

            registered = get_registered_marketplaces()
            if registered:
                alias = registered[-1].name
        except Exception:
            alias = None
    if not alias:
        logger.warning("Could not determine the marketplace alias; skipping the browse check.")
        return 0

    # --- Step 3: smoke test -------------------------------------------
    try:
        ctx.invoke(marketplace_browse, name=alias, verbose=verbose)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
        if code != 0:
            logger.error(
                f"Marketplace '{alias}' was registered but is not browsable. "
                f"Run 'apm marketplace browse {alias} --verbose' for detail."
            )
            return code

    logger.success("Enrolled. Install a plugin with:", symbol="sparkles")
    logger.tree_item(f"  apm install <plugin-name>@{alias} --target claude")
    return 0


@click.command(
    help=(
        "Enroll this machine on a marketplace: ensure a credential exists, "
        "register the marketplace, and confirm it is browsable. Safe to re-run."
    ),
    epilog=_EPILOG,
)
@click.argument("source", metavar="SOURCE", required=True)
@click.option("--name", "-n", default=None, help="Marketplace alias (defaults to the repo name)")
@click.option("--ref", "-r", default=None, help="Git ref (branch, tag, or commit). Default: main")
@click.option(
    "--host",
    "host_flag",
    default=None,
    help="Git host FQDN for OWNER/REPO shorthand (default: github.com)",
)
@click.option(
    "--no-token",
    is_flag=True,
    help="Skip the credential step and go straight to registration",
)
@click.option("--verbose", "-v", is_flag=True, help="Show detailed output")
@click.pass_context
def enroll(ctx, source, name, ref, host_flag, no_token, verbose):
    """Onboard onto a marketplace in one command.

    SOURCE accepts the same forms as ``apm marketplace add``: OWNER/REPO or
    HOST/OWNER/REPO shorthand, a full HTTPS or SSH git URL, a local path, or
    a ``file://`` URI.
    """
    exit_code = run_enroll(ctx, source, name, ref, host_flag, no_token, verbose)
    if exit_code != 0:
        sys.exit(exit_code)
