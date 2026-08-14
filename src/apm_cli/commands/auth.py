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
and ``GITHUB_APM_PAT`` -> ``GITHUB_TOKEN`` -> ``gh auth token`` -> git
credential helper for GitHub. Because the env vars are consulted first,
exporting one is enough -- there is no need to wire an external CLI in as a
git credential helper, and no need to evict a shadowing platform keychain
entry. When a shadowing entry is detected we say so instead of deleting it:
silently mutating a global credential store is not something a package
manager should do on the user's behalf.

A note on what "working" means here. ``--check`` validates a token against
the host's REST API, because a token git accepts is not automatically one the
API accepts -- a GitLab OAuth session token is valid for ``git clone`` but
gets a 401 from the REST API, which is what marketplace lookups use. That
check is opt-in: it costs a network round trip and, on GitHub, unauthenticated
requests are capped at 60/hour per IP.
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
    "APM reads tokens from GITHUB_APM_PAT / GITHUB_TOKEN / the gh CLI, or\n"
    "GITLAB_APM_PAT / GITLAB_TOKEN, falling back to your git credential\n"
    "helper. Environment variables win, so exporting one is enough."
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


def check_token(token: str, host: str, host_kind: str) -> tuple[bool, int | None]:
    """Validate *token* against the host's REST API. Returns ``(ok, status)``.

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
        return False, None

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
        return False, None
    return status == 200, status


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
        logger.error(
            f"No token flow for '{host}' (host class '{host_kind}'). APM resolves "
            f"this host through git credentials; see 'apm doctor'."
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
            ok, status = check_token(token, host, host_kind)
            if ok:
                logger.success(f"{host}: credential from {source} works.", symbol="check")
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

    if detect_shadowing_helper(host):
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
        ok, status = check_token(pasted, host, host_kind)
        if not ok:
            detail = f" (HTTP {status})" if status else " (no response)"
            logger.error(
                f"That token was rejected by the {host} API{detail}. "
                f"Check it has scopes '{scopes}'."
            )
            return 1
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
        # stdout must carry ONLY the export line, so eval "$(...)" is safe.
        # CommandLogger writes to stdout, so capture that for the whole run,
        # replay it on stderr, and put just the export line on real stdout.
        import contextlib
        import io

        sink: list[str] = []
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            exit_code = run_auth(host, check, export_, verbose, sink)
        sys.stderr.write(buf.getvalue())
        sys.stderr.flush()
        for line in sink:
            click.echo(line)
    else:
        exit_code = run_auth(host, check, export_, verbose)
    if exit_code != 0:
        sys.exit(exit_code)
