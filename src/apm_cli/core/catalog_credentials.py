"""Credential resolution for hosted HTTPS marketplace catalogs.

Single owner of one decision: which credential, if any, may be sent to a
hosted ``marketplace.json`` the user registered under a name. Reached only
through :meth:`apm_cli.core.auth.AuthResolver.resolve_marketplace_catalog_credential`.

* Variables follow the registry convention (``APM_REGISTRY_*``):
  ``APM_MARKETPLACE_TOKEN_<NAME>`` (HTTP Bearer) or
  ``APM_MARKETPLACE_USER_<NAME>`` plus ``APM_MARKETPLACE_PASS_<NAME>``
  (HTTP Basic). Bearer wins when both are set.
* ``<NAME>`` uses :func:`credential_env_suffix`, shared with the registry
  variables. Two registrations whose names normalize to the same suffix make
  the variables ambiguous, so no credential is resolved for either.
* There is no fallback: no Git credential helper, no host token, no package
  registry credential and no ``.netrc``. A catalog without variables is
  fetched anonymously.
* The credential is bound to the registered catalog URL. :attr:`scope_url`
  is the boundary (scheme, host, effective port, the catalog's directory and
  its query) that ``utils/http_credentials.origin_bounded_request`` checks on
  every redirect hop.
* Secret values never appear in messages, ``repr`` or exceptions.
"""

from __future__ import annotations

import base64
import urllib.parse
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

TOKEN_PREFIX = "APM_MARKETPLACE_TOKEN_"  # noqa: S105 -- variable name prefix
USER_PREFIX = "APM_MARKETPLACE_USER_"
PASS_PREFIX = "APM_MARKETPLACE_PASS_"  # noqa: S105 -- variable name prefix

SCHEME_BEARER = "bearer"
SCHEME_BASIC = "basic"


def credential_env_suffix(name: str) -> str:
    """Return the environment-variable suffix for a registry or marketplace name.

    ``corp-main`` and ``corp.main`` both become ``CORP_MAIN``.
    """
    return name.upper().replace("-", "_").replace(".", "_")


class CatalogCredentialError(ValueError):
    """The catalog credential configuration cannot be used safely.

    Messages name variables and marketplaces only, never a value.
    """


@dataclass(frozen=True)
class CatalogCredentialVariables:
    """Names of the variables that may hold one marketplace's credential."""

    token: str
    user: str
    password: str

    @classmethod
    def for_name(cls, name: str) -> CatalogCredentialVariables:
        suffix = credential_env_suffix(name)
        return cls(
            token=f"{TOKEN_PREFIX}{suffix}",
            user=f"{USER_PREFIX}{suffix}",
            password=f"{PASS_PREFIX}{suffix}",
        )

    @property
    def names(self) -> tuple[str, str, str]:
        return (self.token, self.user, self.password)

    def setup_hint(self) -> str:
        """Return the one-line remediation naming every variable."""
        return f"Set {self.token} (Bearer token), or {self.user} and {self.password} (Basic auth)."


def catalog_scope_url(binding_url: str) -> str:
    """Return the credential boundary for a registered catalog URL.

    The boundary keeps the catalog's scheme, host, port and query, and the
    path of the directory that holds the catalog document.
    """
    parts = urllib.parse.urlsplit(binding_url)
    directory = parts.path.rsplit("/", 1)[0] + "/" if "/" in parts.path else "/"
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, directory, parts.query, ""))


@dataclass(frozen=True)
class CatalogCredential:
    """The credential decision for one catalog fetch.

    ``scheme`` is ``None`` for an anonymous fetch. ``source_variable`` names
    the variable that supplied the credential. The header value is kept out
    of ``repr`` and comparisons.
    """

    marketplace: str
    binding_url: str
    variables: CatalogCredentialVariables
    bound: bool
    scheme: str | None = None
    source_variable: str | None = None
    _authorization: str | None = field(default=None, repr=False, compare=False)

    @property
    def presented(self) -> bool:
        """Return whether a credential will be sent."""
        return self._authorization is not None

    @property
    def scope_url(self) -> str:
        """Return the boundary credentials stay within, checked on every hop."""
        return catalog_scope_url(self.binding_url)

    def headers(self) -> dict[str, str]:
        """Return the credential header (empty when anonymous)."""
        if self._authorization is None:
            return {}
        return {"Authorization": self._authorization}


def anonymous_catalog_credential(
    marketplace: str, binding_url: str, *, bound: bool
) -> CatalogCredential:
    """Return the decision for a catalog that sends no credential."""
    return CatalogCredential(
        marketplace=marketplace,
        binding_url=binding_url,
        variables=CatalogCredentialVariables.for_name(marketplace),
        bound=bound,
    )


def _has_control_characters(value: str) -> bool:
    return any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value)


def _bearer_value(variable: str, token: str) -> str:
    if any(not 0x21 <= ord(ch) <= 0x7E for ch in token):
        raise CatalogCredentialError(
            f"{variable} is malformed: a Bearer token must be printable ASCII "
            "without spaces. Check the value for stray quotes, spaces or line breaks."
        )
    return f"Bearer {token}"


def _basic_value(variables: CatalogCredentialVariables, user: str, password: str) -> str:
    if ":" in user or _has_control_characters(user):
        raise CatalogCredentialError(
            f"{variables.user} is malformed: a Basic auth user name cannot "
            "contain ':' or control characters."
        )
    if _has_control_characters(password):
        raise CatalogCredentialError(
            f"{variables.password} is malformed: a Basic auth password cannot "
            "contain control characters such as line breaks."
        )
    encoded = base64.b64encode(f"{user}:{password}".encode()).decode("ascii")
    return f"Basic {encoded}"


def require_unambiguous_name(name: str, peer_names: Iterable[str]) -> None:
    """Reject *name* when another bound name maps to the same variables."""
    suffix = credential_env_suffix(name)
    for peer in peer_names:
        if peer.lower() != name.lower() and credential_env_suffix(peer) == suffix:
            raise CatalogCredentialError(
                f"Marketplace names '{name}' and '{peer}' both map to the "
                f"APM_MARKETPLACE_*_{suffix} variables, so APM cannot tell which "
                "catalog they belong to. Remove one with 'apm marketplace remove' "
                "and register it again under a distinct --name."
            )


def resolve_catalog_credential(
    *,
    marketplace: str,
    binding_url: str,
    peer_names: Iterable[str],
    environ: Mapping[str, str],
) -> CatalogCredential:
    """Resolve the credential for a catalog the user bound to *marketplace*.

    *peer_names* are the names of the other credential-bound catalogs; a name
    that normalizes like *marketplace* makes its variables ambiguous.

    Raises:
        CatalogCredentialError: Ambiguous names, an incomplete Basic pair or a
            malformed value. The message never contains a value.
    """
    variables = CatalogCredentialVariables.for_name(marketplace)
    token = environ.get(variables.token) or ""
    user = environ.get(variables.user)
    password = environ.get(variables.password)
    if not token and user is None and password is None:
        return anonymous_catalog_credential(marketplace, binding_url, bound=True)
    require_unambiguous_name(marketplace, peer_names)
    if token:
        authorization = _bearer_value(variables.token, token)
        scheme, source = SCHEME_BEARER, variables.token
    else:
        if user is None or password is None:
            missing = variables.password if user is not None else variables.user
            present = variables.user if user is not None else variables.password
            raise CatalogCredentialError(
                f"{present} is set but {missing} is not; Basic auth needs both."
            )
        authorization = _basic_value(variables, user, password)
        scheme, source = SCHEME_BASIC, f"{variables.user}/{variables.password}"
    return CatalogCredential(
        marketplace=marketplace,
        binding_url=binding_url,
        variables=variables,
        bound=True,
        scheme=scheme,
        source_variable=source,
        _authorization=authorization,
    )
