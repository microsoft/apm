"""``apm enroll`` -- one-shot onboarding onto a marketplace on a new machine.

Wraps the three steps a new joiner otherwise runs by hand: prove there is a
usable credential for the marketplace host, register the marketplace, and
smoke-test that it is browsable.

Only the credential step needs new logic. Registration and browsing delegate
to ``apm marketplace add`` / ``apm marketplace browse`` via ``ctx.invoke`` so
this command inherits their source parsing, ref handling and error rendering
rather than duplicating it.

On the credential step, note that APM resolves GitLab tokens itself --
``GITLAB_APM_PAT`` -> ``GITLAB_TOKEN`` -> git credential helper (see
``AuthResolver._resolve_token``). Because the env vars are consulted *before*
any credential helper, exporting one is enough; there is no need to wire an
external CLI in as a git credential helper, and no need to evict a shadowing
platform keychain entry. When a shadowing entry is detected we say so instead
of deleting it -- silently mutating a global credential store is not something
a package manager should do on the user's behalf.
"""

from __future__ import annotations

import os
import sys
from urllib.parse import quote, urlparse

import click

from ..core.command_logger import CommandLogger

# GitLab's PAT creation page accepts a prefilled name and scope list. Read
# access to the API is what the marketplace lookup below actually needs;
# read_repository additionally covers cloning plugin sources.
_TOKEN_SCOPES = "read_repository,read_api"  # noqa: S105 -- scope list, not a secret

_MARKETPLACE_MANIFEST = ".claude-plugin/marketplace.json"

_EPILOG = (
    "\b\n"
    "Examples:\n"
    "  apm enroll gitlab.com/acme/team/apm-marketplace --name acme\n"
    "  apm enroll https://gitlab.com/acme/apm-marketplace --name acme\n"
    "  apm enroll ./local-marketplace --name scratch\n"
    "\b\n"
    "Credentials:\n"
    "  Private GitLab marketplaces need a token with scopes\n"
    "  read_repository,read_api. APM reads it from GITLAB_APM_PAT or\n"
    "  GITLAB_TOKEN, falling back to your git credential helper."
)


def _token_env_var(host_kind: str) -> str | None:
    """Return the env var APM reads a token from for this host class."""
    if host_kind == "gitlab":
        return "GITLAB_APM_PAT"
    if host_kind in ("github", "ghe_cloud", "ghes"):
        return "GITHUB_APM_PAT"
    return None


def resolve_existing_token(host: str) -> tuple[str | None, str]:
    """Return ``(token, source)`` for *host* using APM's own resolution chain.

    ``source`` is the human-readable origin (env var name, ``gh-auth-token``,
    ``git-credential-fill``, or ``none``). Never returns the token in logs --
    callers must not print it.

    The resolver is deliberately throwaway. ``AuthResolver`` caches per
    instance, and ``run_enroll`` may set a token into ``os.environ`` *after*
    this returns a miss; a retained resolver would keep serving the cached
    ``token=None`` and break registration. Do not hoist this to a shared
    instance -- see ``test_negative_resolution_is_not_cached_across_resolvers``.
    """
    from ..core.auth import AuthResolver

    ctx = AuthResolver().resolve(host)
    return getattr(ctx, "token", None), getattr(ctx, "source", "none")


def verify_token(token: str | None, host: str, project_path: str) -> tuple[bool, int | None]:
    """Check *token* can actually read the marketplace manifest over REST.

    Returns ``(ok, status_code)``. A token that git accepts is not
    automatically one the GitLab REST API accepts: the API's PRIVATE-TOKEN
    convention is satisfied by a personal/project access token, while an
    OAuth session token valid for ``git clone`` gets a 401 here. Verifying
    against the real endpoint is the only reliable check.
    """
    if not token:
        return False, None

    import requests

    from ..core.auth import AuthResolver

    encoded_project = quote(project_path.strip("/"), safe="")
    encoded_file = quote(_MARKETPLACE_MANIFEST, safe="")
    url = (
        f"https://{host}/api/v4/projects/{encoded_project}"
        f"/repository/files/{encoded_file}/raw?ref=main"
    )
    try:
        response = requests.get(
            url,
            headers=AuthResolver.gitlab_rest_headers(token),
            timeout=15,
        )
    except requests.RequestException:
        return False, None
    return response.status_code == 200, response.status_code


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


def _open_token_page(logger: CommandLogger, host: str) -> None:
    """Open (or print) GitLab's token page with name and scopes prefilled."""
    import platform
    import webbrowser

    token_name = f"apm-{os.environ.get('USER') or 'user'}-{platform.node().split('.')[0]}"
    url = (
        f"https://{host}/-/user_settings/personal_access_tokens"
        f"?name={quote(token_name)}&scopes={quote(_TOKEN_SCOPES)}"
    )
    logger.progress(
        f"Create a token with scopes '{_TOKEN_SCOPES}', then paste it below.",
        symbol="info",
    )
    opened = False
    try:
        opened = webbrowser.open(url)
    except Exception:  # pragma: no cover - platform dependent
        opened = False
    if not opened:
        logger.tree_item(f"  {url}")


def _host_and_project(source: str, host_flag: str | None) -> tuple[str, str, str]:
    """Return ``(url, host, project_path)`` for *source*.

    Reuses ``marketplace add``'s parser so shorthand, SSH and full-URL forms
    behave identically across both commands.
    """
    from .marketplace import _parse_marketplace_source

    url, kind, embedded_host = _parse_marketplace_source(source, host_flag)
    if kind == "local":
        return url, "", ""
    parsed = urlparse(url)
    host = embedded_host or parsed.netloc
    project_path = parsed.path.strip("/").removesuffix(".git")
    return url, host, project_path


def run_enroll(
    ctx: click.Context,
    source: str,
    name: str | None,
    ref: str | None,
    host_flag: str | None,
    skip_verify: bool,
    verbose: bool,
) -> int:
    """Execute the enrollment flow. Returns a process exit code."""
    logger = CommandLogger("enroll", verbose=verbose)

    try:
        url, host, project_path = _host_and_project(source, host_flag)
    except Exception as exc:
        logger.error(f"Invalid source '{source}': {exc}")
        return 1

    from ..core.auth import AuthResolver

    host_kind = AuthResolver.classify_host(host).kind if host else "local"
    is_gitlab = host_kind == "gitlab"

    # --- Step 1: credentials ------------------------------------------
    # Only GitLab gets the verify-and-prompt treatment: the REST probe below
    # is GitLab-specific. Other hosts fall through to whatever credentials
    # 'marketplace add' already resolves, which is the status quo.
    if is_gitlab and not skip_verify and project_path:
        logger.start(f"Checking credentials for {host}...", symbol="search")
        token, token_source = resolve_existing_token(host)
        ok, status = verify_token(token, host, project_path)

        if ok:
            logger.success(
                f"Existing credential works (source: {token_source})",
                symbol="check",
            )
        else:
            if token and status == 401:
                logger.warning(
                    f"The credential from {token_source} was rejected by the "
                    f"{host} API (401). An OAuth session token works for git "
                    f"but not for the REST API."
                )
            elif token and status == 404:
                logger.warning(
                    f"The credential from {token_source} cannot see "
                    f"'{project_path}' (404). It may lack access, or the path "
                    f"may be wrong."
                )
            elif token:
                detail = f" (HTTP {status})" if status else ""
                logger.warning(f"The credential from {token_source} did not work{detail}.")
            else:
                logger.progress("No GitLab credential found.", symbol="info")

            if _detect_shadowing_helper(host):
                logger.warning(
                    f"A macOS keychain entry for {host} may be shadowing newer "
                    f"credentials. If enrollment keeps failing, clear it with:\n"
                    f"    printf 'protocol=https\\nhost={host}\\n\\n' | "
                    f"git credential-osxkeychain erase"
                )

            env_var = _token_env_var(host_kind) or "GITLAB_APM_PAT"

            if not _is_interactive():
                logger.error(
                    f"No usable {host} credential and not running interactively.\n"
                    f"    Set {env_var} to a token with scopes "
                    f"'{_TOKEN_SCOPES}' and re-run."
                )
                return 1

            _open_token_page(logger, host)
            pasted = click.prompt("    Paste the token (input hidden)", hide_input=True, default="")
            pasted = (pasted or "").strip()
            if not pasted:
                logger.error("No token entered.")
                return 1

            ok, status = verify_token(pasted, host, project_path)
            if not ok:
                detail = f" (HTTP {status})" if status else ""
                logger.error(
                    f"That token did not work against the {host} API{detail}. "
                    f"Check it has scopes '{_TOKEN_SCOPES}' and try again."
                )
                return 1

            # Make it usable for the rest of THIS process, so the
            # marketplace add below succeeds without a second prompt.
            os.environ[env_var] = pasted
            logger.success("Token verified against the GitLab API", symbol="check")
            logger.warning(
                f"This token is set for the current command only. To persist "
                f"it, add to your shell profile:\n    export {env_var}='<token>'"
            )

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
        "Enroll this machine on a marketplace: verify credentials, register "
        "the marketplace, and confirm it is browsable. Safe to re-run."
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
    "--skip-verify",
    is_flag=True,
    help="Skip the credential pre-check and go straight to registration",
)
@click.option("--verbose", "-v", is_flag=True, help="Show detailed output")
@click.pass_context
def enroll(ctx, source, name, ref, host_flag, skip_verify, verbose):
    """Onboard onto a marketplace in one command.

    SOURCE accepts the same forms as ``apm marketplace add``: OWNER/REPO or
    HOST/OWNER/REPO shorthand, a full HTTPS or SSH git URL, a local path, or
    a ``file://`` URI.
    """
    exit_code = run_enroll(ctx, source, name, ref, host_flag, skip_verify, verbose)
    if exit_code != 0:
        sys.exit(exit_code)
