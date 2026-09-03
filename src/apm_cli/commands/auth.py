"""``apm auth`` -- get a working credential for a git host.

Does one job: make sure you have a token APM can use for a host, walking you
through creating one if you do not. It deliberately does **not** register a
marketplace or install anything -- ``apm marketplace add`` and ``apm install``
already do that, and they report their own errors better than a second
pre-flight check here could.

Why it prints an ``export`` line rather than saving the token: a child process
cannot mutate its parent shell's environment, and ``AuthResolver`` reads
credentials only from environment variables and git credential helpers --
never from ``~/.apm/config.json``. So writing the token into APM's own config
would be inert; APM would never read it back. Printing the line you need is
the honest option, and ``eval "$(apm auth <host> --export)"`` makes it a
one-liner.

Token resolution (see ``AuthResolver._resolve_token``) is APM's, not ours:
``GITLAB_APM_PAT`` -> ``GITLAB_TOKEN`` -> git credential helper for GitLab,
and ``GITHUB_APM_PAT`` -> ``GITHUB_TOKEN`` -> ``GH_TOKEN`` -> ``gh auth
token`` -> git credential helper for GitHub (``TOKEN_PRECEDENCE["modules"]``
in ``token_manager``). Because the env vars are consulted first, exporting one
is enough -- there is no need to wire an external CLI in as a git credential
helper, and no need to evict a shadowing platform keychain entry.

That ordering is also why the keychain-shadowing notice is gated: a helper can
only shadow when a helper was actually consulted, so we mention it when the
token came from ``git-credential-fill`` or when nothing resolved -- never when
an env var answered. And when we do mention it we say so instead of deleting
it: silently mutating a global credential store is not something a package
manager should do on the user's behalf, doubly so when the entry it would
erase is the one plain ``git push`` depends on.

A note on what "working" means here. ``--check`` validates a token against
the host's REST API, because a token git accepts is not automatically one the
API accepts -- a GitLab OAuth session token is valid for ``git clone`` but
gets a 401 from the REST API, which is what marketplace lookups use. That
check is opt-in: it costs a network round trip and, on GitHub, unauthenticated
requests are capped at 60/hour per IP.

``--check`` answers with three states, not two, because "the API said no" and
"the API never answered" call for opposite advice. Only an outright rejection
sends you off to mint a token; an unreachable API, a 5xx, or a GitHub App
installation token that cannot be validated at all leaves the credential in
place and exits 0. See ``check_token``.
"""

from __future__ import annotations

import os
import sys
from urllib.parse import quote

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
    "  apm auth github.com              Check, and prompt if missing\n"
    "  apm auth gitlab.com --check      Also validate against the REST API\n"
    '  eval "$(apm auth gitlab.com --export)"   Set it in the current shell\n'
    "\b\n"
    "APM reads tokens from GITHUB_APM_PAT / GITHUB_TOKEN / GH_TOKEN / the gh\n"
    "CLI, or GITLAB_APM_PAT / GITLAB_TOKEN, falling back to your git\n"
    "credential helper. Environment variables win, so exporting one is enough."
)


def token_env_var(host_kind: str) -> str | None:
    """Return the env var APM reads a token from for this host class."""
    if host_kind == "gitlab":
        return "GITLAB_APM_PAT"
    if host_kind in _GITHUB_KINDS:
        return "GITHUB_APM_PAT"
    return None


def token_scopes(host_kind: str) -> str:
    """Return the scopes to prefill on this host's token-creation page."""
    return _GITHUB_SCOPES if host_kind in _GITHUB_KINDS else _GITLAB_SCOPES


def resolve_existing_token(host: str) -> tuple[str | None, str]:
    """Return ``(token, source)`` for *host* using APM's own resolution chain.

    ``source`` is the human-readable origin (env var name, ``gh-auth-token``,
    ``git-credential-fill``, or ``none``). Never returns the token in logs --
    callers must not print it except through the explicit ``--export`` path.

    The resolver is deliberately throwaway. ``AuthResolver`` caches per
    instance, so a retained resolver would keep serving a stale
    ``token=None`` after the caller sets a token into ``os.environ``.
    """
    from ..core.auth import AuthResolver

    ctx = AuthResolver().resolve(host)
    return getattr(ctx, "token", None), getattr(ctx, "source", "none")


def _unreachable_detail(status: int | None) -> str:
    """Explain an ``indeterminate`` verdict without blaming the credential."""
    if status is None:
        return " (the API could not be reached)"
    if status == 403:
        return " (HTTP 403 -- an app/installation token has no user context)"
    return f" (HTTP {status} from the API)"


def check_token(token: str, host: str, host_kind: str) -> tuple[str, int | None]:
    """Validate *token* against the host's REST API.

    Returns ``(verdict, status)`` where *verdict* is one of:

    ``"ok"``
        The API accepted the credential.
    ``"rejected"``
        The API refused it and a new token is genuinely needed.
    ``"indeterminate"``
        We could not find out. The credential may well be fine, so the caller
        must not tell the user to mint a replacement.

    The distinction is the whole point. A two-state answer reports "your
    credential was rejected" when the real story is a captive-portal wifi, a
    502, or a rate limit -- sending the user off to create a PAT that fixes
    nothing, and failing CI on a transient blip.

    ``indeterminate`` also covers GitHub App installation tokens (``ghs_``),
    including Actions' own ``GITHUB_TOKEN``: they have no user context, so
    ``GET /user`` answers 403 even though the same token reads repository
    contents -- which is what marketplace lookups actually do. We cannot
    validate such a token here, and refusing to guess keeps CI green.

    Hits the identity endpoint rather than any repository, so the answer is
    "is this credential valid for the API" and does not depend on access to a
    particular project. Delegates header construction for GitLab to
    ``AuthResolver.gitlab_rest_headers`` so the PRIVATE-TOKEN convention stays
    in one place.
    """
    import requests

    from ..core.auth import AuthResolver

    host_info = AuthResolver.classify_host(host)
    api_base = (host_info.api_base or "").rstrip("/")
    if not api_base:
        # No endpoint to ask -- that is ignorance, not a rejection.
        return "indeterminate", None

    if host_kind in _GITHUB_KINDS:
        url = f"{api_base}/user"
        headers = {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "apm-cli",
        }
    else:
        url = f"{api_base}/user"
        headers = {"User-Agent": "apm-cli"}
        headers.update(AuthResolver.gitlab_rest_headers(token))

    try:
        status = requests.get(url, headers=headers, timeout=15).status_code
    except requests.RequestException:
        return "indeterminate", None

    if status == 200:
        return "ok", status
    if status == 403 and AuthResolver.detect_token_type(token) == "github-app":
        # An installation token has no user context; 403 here says nothing
        # about whether it can read repositories.
        return "indeterminate", status
    if status in (401, 403):
        return "rejected", status
    # 5xx, rate limits, proxies: the host did not tell us about the token.
    return "indeterminate", status


def detect_shadowing_helper(host: str) -> bool:
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


def is_interactive() -> bool:
    """Return True when it is safe to prompt (honours APM_NON_INTERACTIVE/CI)."""
    if os.environ.get("APM_NON_INTERACTIVE") or os.environ.get("CI"):
        return False
    return sys.stdin.isatty()


def token_page_url(host: str, host_kind: str, token_name: str) -> str:
    """Return the host's token-creation page, name and scopes prefilled.

    GitHub and GitLab use different paths and query-param names. The host is
    carried through rather than hardcoded, so GitHub Enterprise Server points
    at the enterprise instance.
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


def open_token_page(logger: CommandLogger, host: str, host_kind: str) -> None:
    """Open (or print) the host's token page with name and scopes prefilled."""
    import platform
    import webbrowser

    token_name = f"apm-{os.environ.get('USER') or 'user'}-{platform.node().split('.')[0]}"
    url = token_page_url(host, host_kind, token_name)
    logger.progress(
        f"Create a token with scopes '{token_scopes(host_kind)}', then paste it below.",
        symbol="info",
    )
    opened = False
    try:
        opened = webbrowser.open(url)
    except Exception:  # pragma: no cover - platform dependent
        opened = False
    if not opened:
        logger.tree_item(f"  {url}")


def run_auth(
    host: str,
    check: bool,
    export: bool,
    verbose: bool,
    sink: list[str] | None = None,
) -> int:
    """Execute the auth flow. Returns a process exit code.

    When *export* is set, the ``export`` line is appended to *sink* rather
    than printed, so the caller can put it on the real stdout while narration
    goes to stderr -- keeping ``eval "$(apm auth <host> --export)"`` safe.
    """
    from ..core.auth import AuthResolver

    _EXPORT_LINE = sink if sink is not None else []

    logger = CommandLogger("auth", verbose=verbose)
    host = (host or "").strip().lower()
    if not host or "/" in host:
        logger.error(f"Expected a host name like 'github.com', got '{host}'.")
        return 1

    host_kind = AuthResolver.classify_host(host).kind
    env_var = token_env_var(host_kind)
    if not env_var:
        # 'generic' is also where a self-managed GHES/GitLab host lands when its
        # env hint is unset -- APM classifies those by GITHUB_HOST / GITLAB_HOST,
        # never by guessing from the hostname. Naming the variable is the
        # actionable part; "host class 'generic'" on its own is not.
        logger.error(
            f"No token flow for '{host}' (host class '{host_kind}').\n"
            f"    If this is a self-managed GitHub Enterprise Server, set "
            f"GITHUB_HOST={host} and re-run.\n"
            f"    If it is a self-managed GitLab, set GITLAB_HOST={host} "
            f"(or add it to APM_GITLAB_HOSTS) and re-run.\n"
            f"    Otherwise APM resolves this host through git credentials; "
            f"see 'apm doctor'."
        )
        return 1

    def emit_export(value: str) -> None:
        """Record the export line for the caller to write to real stdout.

        Single-quoted for the shell, with any embedded single quote escaped
        the POSIX way ('\\'') so a pathological token cannot break out of the
        quoting and execute under ``eval``.
        """
        safe = value.replace("'", "'\\''")
        _EXPORT_LINE.append(f"export {env_var}='{safe}'")

    token, source = resolve_existing_token(host)

    if token:
        if check:
            verdict, status = check_token(token, host, host_kind)
            if verdict == "ok":
                logger.success(f"{host}: credential from {source} works.", symbol="check")
                if export:
                    emit_export(token)
                return 0
            if verdict == "indeterminate":
                # We could not validate it -- that is not evidence it is bad.
                # Keep the credential and say what we do not know, rather than
                # sending the user to mint a replacement that fixes nothing.
                logger.warning(
                    f"{host}: could not validate the credential from {source}"
                    f"{_unreachable_detail(status)}. Keeping it."
                )
                if export:
                    emit_export(token)
                return 0
            detail = f" (HTTP {status})" if status else " (no response)"
            if status == 401 and host_kind == "gitlab":
                logger.warning(
                    f"{host}: the credential from {source} was rejected{detail}. "
                    f"An OAuth session token (e.g. from 'glab auth login') works "
                    f"for git but not for the REST API -- you need a personal "
                    f"access token."
                )
            else:
                logger.warning(f"{host}: the credential from {source} was rejected{detail}.")
            # Fall through to the prompt so the user can replace it.
        else:
            logger.success(f"{host}: using credential from {source}.", symbol="check")
            logger.progress("Add --check to validate it against the API.", symbol="info")
            if export:
                emit_export(token)
            return 0
    else:
        logger.progress(f"{host}: no credential found.", symbol="info")

    # Only meaningful when a credential helper actually had a say. Env vars are
    # consulted *before* any helper in ``_resolve_token``, so when the token
    # came from one, the keychain entry never got a chance to shadow anything --
    # and telling the user to erase it would destroy the credential plain
    # ``git clone``/``git push`` relies on, to fix a problem it was not causing.
    helper_could_be_shadowing = token is None or source == "git-credential-fill"
    if helper_could_be_shadowing and detect_shadowing_helper(host):
        logger.warning(
            f"A macOS keychain entry for {host} may be shadowing newer "
            f"credentials. If authentication keeps failing, clear it with:\n"
            f"    printf 'protocol=https\\nhost={host}\\n\\n' | "
            f"git credential-osxkeychain erase"
        )

    scopes = token_scopes(host_kind)

    if not is_interactive():
        logger.error(
            f"No usable {host} credential and not running interactively.\n"
            f"    Set {env_var} to a token with scopes '{scopes}'."
        )
        return 1

    open_token_page(logger, host, host_kind)
    pasted = click.prompt("    Paste the token (input hidden)", hide_input=True, default="")
    pasted = (pasted or "").strip()
    if not pasted:
        logger.error("No token entered.")
        return 1

    if check:
        verdict, status = check_token(pasted, host, host_kind)
        if verdict == "rejected":
            detail = f" (HTTP {status})" if status else " (no response)"
            logger.error(
                f"That token was rejected by the {host} API{detail}. "
                f"Check it has scopes '{scopes}'."
            )
            return 1
        if verdict == "indeterminate":
            logger.warning(
                f"Could not validate the token against the {host} API"
                f"{_unreachable_detail(status)}. Continuing."
            )
        else:
            logger.success("Token validated against the API.", symbol="check")

    if export:
        emit_export(pasted)
    else:
        logger.success("Token accepted. Add it to your environment:", symbol="check")
        logger.tree_item(f"  export {env_var}='<the token you just pasted>'")
        logger.progress(
            'Or re-run as: eval "$(apm auth ' + host + ' --export)"',
            symbol="info",
        )
    return 0


def _run_auth_for_export(host: str, check: bool, verbose: bool) -> int:
    """Run the auth flow with stdout reserved for the ``export`` line alone.

    ``eval "$(apm auth <host> --export)"`` executes everything on stdout, so a
    single stray byte there is executed as shell. Keeping that promise takes
    two layers, because there are two ways to write to stdout:

    1. **Python level** -- narration is redirected to ``sys.stderr`` for the
       duration of the run. Streaming (rather than buffering and replaying)
       matters: the flow blocks on ``click.prompt``, so a buffer would hold
       "paste it below" and the token-page URL until after the paste they were
       meant to precede, and would lose them entirely if the user hits Ctrl-C.

    2. **File-descriptor level** -- ``redirect_stdout`` only rebinds
       ``sys.stdout``; fd 1 still points at the terminal, and subprocesses
       inherit it. ``webbrowser.open`` (``xdg-open``: "no method available"),
       ``gh auth token`` and ``git credential fill`` all speak on that fd, so
       fd 1 is pointed at stderr for the whole run and restored in ``finally``.

    The export line is written once both layers have unwound, so it is the
    only thing the caller's ``eval`` ever sees.
    """
    import contextlib

    sink: list[str] = []
    sys.stdout.flush()
    real_stdout_fd = os.dup(1)
    try:
        # Point fd 1 at stderr so inherited-fd chatter cannot reach the eval.
        os.dup2(2, 1)
        with contextlib.redirect_stdout(sys.stderr):
            try:
                return run_auth(host, check, True, verbose, sink)
            finally:
                sys.stderr.flush()
    finally:
        os.dup2(real_stdout_fd, 1)
        os.close(real_stdout_fd)
        # Both layers have unwound here -- ``redirect_stdout`` restored
        # ``sys.stdout`` on exit, and fd 1 is back -- so this is the only
        # thing the caller's ``eval`` sees.
        for line in sink:
            click.echo(line)


@click.command(
    help=(
        "Get a working credential for a git host. Reports what APM resolves, "
        "and walks you through creating a token if there is none."
    ),
    epilog=_EPILOG,
)
@click.argument("host", metavar="HOST", required=True)
@click.option(
    "--check",
    is_flag=True,
    help="Validate the token against the host's REST API (one network call)",
)
@click.option(
    "--export",
    "export_",
    is_flag=True,
    help='Print "export VAR=token" on stdout for eval; narration goes to stderr',
)
@click.option("--verbose", "-v", is_flag=True, help="Show detailed output")
def auth(host, check, export_, verbose):
    """Set up credentials for HOST (e.g. ``github.com``, ``gitlab.com``).

    Does not register marketplaces or install packages -- use
    ``apm marketplace add`` and ``apm install`` for that.
    """
    if export_:
        exit_code = _run_auth_for_export(host, check, verbose)
    else:
        exit_code = run_auth(host, check, export_, verbose)
    if exit_code != 0:
        sys.exit(exit_code)
