"""``apm enroll`` -- one-shot onboarding onto a marketplace on a new machine.

Wraps the three steps a new joiner otherwise runs by hand: prove there is a
usable credential for the marketplace host, register the marketplace, and
smoke-test that it is browsable.

Only the credential step needs new logic. Registration and browsing delegate
to ``apm marketplace add`` / ``apm marketplace browse`` via ``ctx.invoke`` so
this command inherits their source parsing, ref handling and error rendering
rather than duplicating it.

On the credential step, note that APM resolves tokens itself (see
``AuthResolver._resolve_token``): ``GITLAB_APM_PAT`` -> ``GITLAB_TOKEN`` ->
git credential helper for GitLab, and ``GITHUB_APM_PAT`` -> ``GITHUB_TOKEN``
-> ``gh auth token`` -> git credential helper for GitHub. Because the env
vars are consulted *before* any credential helper, exporting one is enough;
there is no need to wire an external CLI in as a git credential helper, and
no need to evict a shadowing platform keychain entry. When a shadowing entry
is detected we say so instead of deleting it -- silently mutating a global
credential store is not something a package manager should do on the user's
behalf.

The credential probe covers GitLab and GitHub (including GitHub Enterprise
Server, whose API base ``HostInfo`` resolves). ADO and generic git hosts have
no equivalent manifest endpoint here, so they skip the check and rely on
``marketplace add``'s own resolution. Public marketplaces need no token at
all, so a failed check falls back to an anonymous probe before demanding one.
"""

from __future__ import annotations

import os
import sys
from urllib.parse import quote, urlparse

import click

from ..core.command_logger import CommandLogger

# Scopes the token-creation page is prefilled with, per host class. Read
# access to the API is what the marketplace lookup needs; the repository
# scope additionally covers cloning plugin sources.
#   GitLab: granular scope names, passed as a ?scopes= query param.
#   GitHub: classic-PAT scope; `repo` is the narrowest scope that reads a
#           private repo's contents (there is no read-only variant).
_GITLAB_SCOPES = "read_repository,read_api"
_GITHUB_SCOPES = "repo"

_GITHUB_KINDS = ("github", "ghe_cloud", "ghes")

_EPILOG = (
    "\b\n"
    "Examples:\n"
    "  apm enroll gitlab.com/acme/team/apm-marketplace --name acme\n"
    "  apm enroll acme/apm-marketplace --name acme\n"
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
    ``token=None`` and break registration. Do not hoist this to a shared
    instance -- see ``test_negative_resolution_is_not_cached_across_resolvers``.
    """
    from ..core.auth import AuthResolver

    ctx = AuthResolver().resolve(host)
    return getattr(ctx, "token", None), getattr(ctx, "source", "none")


def _probe_manifest(
    token: str | None,
    host: str,
    project_path: str,
    ref: str,
) -> tuple[bool, int | None]:
    """Try to read the marketplace manifest over REST with *token* (or none).

    Returns ``(ok, status)``. ``status`` is the most informative code seen
    across candidate paths -- a 401/403 anywhere outranks a 404, because
    "you cannot authenticate" is the actionable message and 404 is what a
    host returns for a private repo it will not admit exists.

    Walks the same candidate paths as the real fetch: a marketplace may keep
    its manifest at ``marketplace.json``, ``.github/plugin/…`` or
    ``.claude-plugin/…``. Probing only one would report a false negative for
    the other two.

    URL and header construction are delegated to ``marketplace.client`` --
    the same builders the real fetch uses -- so this probe cannot drift from
    what registration will actually do, and inherits GitHub Enterprise
    Server support (the builders derive the API base from ``HostInfo``).
    """
    import requests

    from ..core.auth import AuthResolver
    from ..marketplace.client import (
        _MARKETPLACE_PATHS,
        _github_contents_url,
        _github_headers,
        _gitlab_file_raw_url,
        _gitlab_headers,
    )
    from ..marketplace.models import MarketplaceSource

    cleaned = project_path.strip("/")
    owner, _, repo = cleaned.rpartition("/")
    if not owner or not repo:
        return False, None

    host_info = AuthResolver.classify_host(host)
    is_github = host_info.kind in _GITHUB_KINDS
    headers = _github_headers(token) if is_github else _gitlab_headers(token)

    best: int | None = None
    for candidate in _MARKETPLACE_PATHS:
        source = MarketplaceSource(
            name="_verify",
            url=f"https://{host}/{cleaned}",
            ref=ref,
            path=candidate,
            owner=owner,
            repo=repo,
            host=host,
        )
        url = (
            _github_contents_url(source, candidate, host_info)
            if is_github
            else _gitlab_file_raw_url(source, candidate, host_info)
        )
        try:
            status = requests.get(url, headers=headers, timeout=15).status_code
        except requests.RequestException:
            continue
        if status == 200:
            return True, 200
        # An auth failure is more actionable than a missing-file 404.
        if best is None or (status in (401, 403) and best not in (401, 403)):
            best = status
    return False, best


def verify_token(
    token: str | None,
    host: str,
    project_path: str,
    *,
    ref: str = "main",
) -> tuple[bool, int | None]:
    """Check *token* can actually read the marketplace manifest over REST.

    Returns ``(ok, status_code)``. A token that git accepts is not
    automatically one the host's REST API accepts: GitLab's PRIVATE-TOKEN
    convention is satisfied by a personal/project access token, while an
    OAuth session token valid for ``git clone`` gets a 401 there. Verifying
    against the real endpoint is the only reliable check.
    """
    if not token:
        return False, None
    return _probe_manifest(token, host, project_path, ref)


def _may_allow_anonymous(host_kind: str) -> bool:
    """Return whether *host_kind* can serve a public repo without credentials.

    Deliberately not an anonymous HTTP probe. Unauthenticated GitHub API
    requests are capped at 60/hour per IP, and an exhausted quota returns
    403 -- indistinguishable from a permissions 403. Gating on such a probe
    makes enrollment fail intermittently for reasons that have nothing to do
    with the user's credentials.

    So this is a static capability answer, and the *caller* degrades to a
    warning rather than a hard stop: ``marketplace add`` is the authority on
    whether the fetch actually works, and it already handles anonymous public
    access. A public repo therefore proceeds and succeeds; a private one
    proceeds and fails with the fetch's own error.
    """
    return host_kind in _GITHUB_KINDS or host_kind == "gitlab"


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

    GitHub and GitLab use different paths and different query-param names
    (``scopes`` as a comma list vs. repeated ``scopes[]``). Both accept a
    prefilled description so the token is identifiable later.
    """
    if host_kind in _GITHUB_KINDS:
        # GitHub's classic-PAT page takes `description` and repeated `scopes`.
        return (
            f"https://{host}/settings/tokens/new"
            f"?description={quote(token_name)}&scopes={quote(_GITHUB_SCOPES)}"
        )
    return (
        f"https://{host}/-/user_settings/personal_access_tokens"
        f"?name={quote(token_name)}&scopes={quote(_GITLAB_SCOPES)}"
    )


def _open_token_page(logger: CommandLogger, host: str, host_kind: str = "gitlab") -> None:
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
    # GitLab and GitHub (incl. GHES) both have a REST manifest endpoint the
    # probe below can use. ADO / generic git hosts do not, so they fall
    # through to whatever credentials 'marketplace add' already resolves.
    can_verify = host_kind == "gitlab" or host_kind in _GITHUB_KINDS

    # --- Step 1: credentials ------------------------------------------
    if can_verify and not skip_verify and project_path:
        logger.start(f"Checking credentials for {host}...", symbol="search")
        token, token_source = resolve_existing_token(host)
        ok, status = verify_token(token, host, project_path, ref=ref or "main")

        if ok:
            logger.success(
                f"Existing credential works (source: {token_source})",
                symbol="check",
            )
        else:
            if token and status == 401:
                hint = (
                    "The token may be expired or revoked."
                    if host_kind in _GITHUB_KINDS
                    else "An OAuth session token works for git but not for the REST API."
                )
                logger.warning(
                    f"The credential from {token_source} was rejected by the "
                    f"{host} API (401). {hint}"
                )
            elif token and status in (403, 404):
                # GitHub returns 404 (not 403) for a private repo the token
                # cannot see, to avoid leaking existence. Same remedy either way.
                logger.warning(
                    f"The credential from {token_source} cannot see "
                    f"'{project_path}' (HTTP {status}). It may lack access, or "
                    f"the path may be wrong."
                )
            elif token:
                detail = f" (HTTP {status})" if status else ""
                logger.warning(f"The credential from {token_source} did not work{detail}.")
            else:
                logger.progress(f"No {host} credential found.", symbol="info")

            if _detect_shadowing_helper(host):
                logger.warning(
                    f"A macOS keychain entry for {host} may be shadowing newer "
                    f"credentials. If enrollment keeps failing, clear it with:\n"
                    f"    printf 'protocol=https\\nhost={host}\\n\\n' | "
                    f"git credential-osxkeychain erase"
                )

            env_var = _token_env_var(host_kind) or "GITLAB_APM_PAT"
            scopes = _token_scopes(host_kind)

            if not _is_interactive():
                # A public marketplace needs no credential, and this check
                # cannot tell public from private without an anonymous probe
                # that GitHub rate-limits to 60/hour per IP. Rather than fail
                # a working public enrollment on an unrelated 403, warn and
                # let 'marketplace add' -- the actual authority -- decide.
                if _may_allow_anonymous(host_kind):
                    remedy = (
                        f"Replace {env_var} with a token that has scopes '{scopes}'."
                        if token
                        else f"Set {env_var} to a token with scopes '{scopes}'."
                    )
                    logger.warning(
                        f"Continuing without a working credential: this succeeds "
                        f"for a public marketplace and fails below for a private "
                        f"one. {remedy}"
                    )
                else:
                    logger.error(
                        f"No usable {host} credential and not running interactively.\n"
                        f"    Set {env_var} to a token with scopes "
                        f"'{scopes}' and re-run."
                    )
                    return 1
            else:
                _open_token_page(logger, host, host_kind)
                pasted = click.prompt(
                    "    Paste the token (input hidden)", hide_input=True, default=""
                )
                pasted = (pasted or "").strip()
                if not pasted:
                    logger.error("No token entered.")
                    return 1

                ok, status = verify_token(pasted, host, project_path, ref=ref or "main")
                if not ok:
                    detail = f" (HTTP {status})" if status else ""
                    logger.error(
                        f"That token did not work against the {host} API{detail}. "
                        f"Check it has scopes '{scopes}' and try again."
                    )
                    return 1

                # Make it usable for the rest of THIS process, so the
                # marketplace add below succeeds without a second prompt.
                os.environ[env_var] = pasted
                logger.success(f"Token verified against the {host} API", symbol="check")
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
