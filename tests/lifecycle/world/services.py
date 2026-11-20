"""Local services of the lifecycle world: one recording HTTPS proxy (#4094).

Every HTTPS request the CLI or its git children make goes through one
``ScriptedHttpsProxy`` per pytest worker (``tests/utils/local_scripted_https_proxy.py``),
so ``egress_log`` sees all of it. The proxy terminates TLS for these origins:

- git smart HTTP for ``github.com`` and the host aliases (GHE Cloud, GHES,
  Azure DevOps, GitLab URL shapes), answered by ``git http-backend`` from
  local bare repositories;
- ``api.github.com`` (and the GHE Cloud / GHES API shapes, including
  ``https://<host>/api/v3`` on both): repository
  metadata, commits and the Contents API, read from the same repositories --
  policy discovery and marketplace catalogs use it;
- ``raw.githubusercontent.com``;
- a local MCP registry (``/v0.1/servers``);
- a private APM package registry (``registry.py``).

Policy variants live in one org per variant: the project's ``origin`` remote
selects the org, as it does for a real user (``use_policy`` in
``tests/lifecycle/world/__init__.py``). Any other host is refused by the proxy
and still recorded.
"""

from __future__ import annotations

import base64
import contextlib
import json
import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit

from tests.lifecycle.world import registry as package_registry
from tests.utils.local_scripted_https_proxy import (
    Handler,
    ScriptedHttpsProxy,
    ScriptedRequest,
    ScriptedResponse,
    git_smart_http,
    local_scripted_https_proxy,
)

ORG = "lifecycle"
POLICY_REPO = ".github"
POLICY_FILE = "apm-policy.yml"
MARKETPLACE_REPO = "marketplace"
MARKETPLACE_NAME = "lifecycle-market"
REGISTRY_SERVER = "io.github.lifecycle/p-registry"
REGISTRY_REMOTE = "https://mcp.lifecycle.test/p-registry"

GITHUB_HOST = "github.com"
API_HOST = "api.github.com"
RAW_HOST = "raw.githubusercontent.com"
REGISTRY_HOST = "mcp-registry.lifecycle.test"
# shape -> (git host, repository URL prefix under that host)
HOST_ALIASES: Mapping[str, tuple[str, str]] = {
    "ghe": ("lifecycle.ghe.com", ORG),
    "ghes": ("ghes.lifecycle.test", ORG),
    "ado": ("dev.azure.com", f"{ORG}/{ORG}/_git"),
    "gitlab": ("gitlab.com", f"{ORG}/group"),
}
ADO_HOST = HOST_ALIASES["ado"][0]
# Azure DevOps credentials of the world: a PAT (ADO_APM_PAT) and the bearer
# the az stub hands out after ``login_azure`` (shaped like a JWT, as APM checks).
ADO_PAT = "lifecycle-ado-pat-7d41e2"
ADO_BEARER = "eyJ" + "lifecycle-ado-bearer-" + "0" * 96
_ADO_ITEMS = "/_apis/git/repositories/"
# Org policy repositories APM reads on Azure DevOps (policy/discovery.py), both
# served from the variant's ``.github`` repository.
_ADO_POLICY_REPOS = {("apm", "apm-policy"), ("_apm", "_apm")}
# API base per GitHub-family host: GHE Cloud uses an api. subdomain, GHES /api/v3.
_API_HOSTS = {API_HOST: "", f"api.{HOST_ALIASES['ghe'][0]}": ""}
_GHES_API_PREFIX = "/api/v3"
# Hosts that also answer ``https://<host>/api/v3``: GHES, and GHE Cloud, whose
# contents and commits APM reads there (deps/download_strategies.py,
# policy/discovery.py).
_V3_API_HOSTS = frozenset({HOST_ALIASES["ghes"][0], HOST_ALIASES["ghe"][0]})
_GIT_SERVICES = ("/info/refs", "/git-upload-pack", "/HEAD")

# Policy documents per variant (schema: src/apm_cli/policy/schema.py).
POLICY_VARIANTS = (
    "allow",
    "deny-pkg",
    "exec-deny-all",
    "mcp-deny",
    "unreachable",
    "hash-mismatch",
    "chain-cycle",
    "deny-lsp",
    "deny-native",
    "exec-gate-on",
)
DENIED_PACKAGE = "p-exec"
# exec-gate-on: any org executables signal turns the gate on
# (security/executables.py org_signal); recommending a package the world never
# serves denies nothing.
RECOMMENDED_ONLY = f"{ORG}/p-recommended"
_CHAIN_PEER = "chain-cycle-peer"


def policy_org(variant: str) -> str:
    if variant not in (*POLICY_VARIANTS, _CHAIN_PEER):
        raise ValueError(f"unknown policy variant {variant!r}; known: {POLICY_VARIANTS}")
    return f"policy-{variant}"


def policy_documents() -> dict[str, dict[str, object]]:
    """The served ``apm-policy.yml`` per variant (``unreachable`` serves none)."""
    base = {"version": "1.0", "enforcement": "block"}
    return {
        "allow": {"name": "allow", **base},
        "deny-pkg": {
            "name": "deny-pkg",
            **base,
            "dependencies": {"deny": [f"**/{DENIED_PACKAGE}", f"{ORG}/{DENIED_PACKAGE}"]},
        },
        "exec-deny-all": {"name": "exec-deny-all", **base, "executables": {"deny_all": True}},
        "mcp-deny": {"name": "mcp-deny", **base, "mcp": {"deny": ["*"]}},
        # The project pins a different hash (``use_policy``); the served bytes are benign.
        "hash-mismatch": {"name": "hash-mismatch", **base},
        "deny-lsp": {
            "name": "deny-lsp",
            **base,
            "dependencies": {"deny": ["**/p-lsp", f"{ORG}/p-lsp"]},
        },
        "deny-native": {
            "name": "deny-native",
            **base,
            "dependencies": {"deny": ["**/p-native-claude", "**/p-native-codex"]},
        },
        "exec-gate-on": {
            "name": "exec-gate-on",
            **base,
            "executables": {"recommend": [RECOMMENDED_ONLY]},
        },
        "chain-cycle": {
            "name": "chain-cycle",
            **base,
            "extends": f"{policy_org(_CHAIN_PEER)}/{POLICY_REPO}",
        },
        _CHAIN_PEER: {
            "name": "chain-cycle-peer",
            **base,
            "extends": f"{policy_org('chain-cycle')}/{POLICY_REPO}",
        },
    }


def _json(status: int, payload: object) -> ScriptedResponse:
    body = json.dumps(payload, sort_keys=True).encode("utf-8")
    return ScriptedResponse(status, {"Content-Type": "application/json"}, body)


def _not_found() -> ScriptedResponse:
    return _json(404, {"message": "Not Found"})


class WorldServices:
    """The ``Services`` of a lifecycle world; one instance per pytest worker."""

    git_base_url = f"https://{GITHUB_HOST}/{ORG}"
    marketplace_url = f"https://{GITHUB_HOST}/{ORG}/{MARKETPLACE_REPO}"
    mcp_registry_url = f"https://{REGISTRY_HOST}"

    package_registry_url = package_registry.URL

    def __init__(self, repositories: Mapping[str, Path]) -> None:
        self._repositories = dict(repositories)
        self._proxy: ScriptedHttpsProxy | None = None
        self.package_registry = package_registry.PackageRegistry()
        self.ado_auth_required = False
        self._unserved: dict[str, Path] = {}

    @property
    def proxy(self) -> ScriptedHttpsProxy:
        if self._proxy is None:
            raise RuntimeError("world services are not running")
        return self._proxy

    @property
    def proxy_url(self) -> str:
        return self.proxy.proxy_url

    @property
    def ca_path(self) -> Path:
        return self.proxy.ca_path

    def policy_url(self, variant: str) -> str:
        """The org policy repository of *variant* (also the project remote's org)."""
        return f"https://{GITHUB_HOST}/{policy_org(variant)}/{POLICY_REPO}"

    def host_alias(self, shape: str) -> str:
        """URL prefix of a world repository under *shape*: append ``/<repo>``."""
        if shape == "github":
            return self.git_base_url
        if shape not in HOST_ALIASES:
            raise ValueError(f"unknown host shape {shape!r}; known: {sorted(HOST_ALIASES)}")
        host, prefix = HOST_ALIASES[shape]
        return f"https://{host}/{prefix}"

    def egress_log(self) -> list[dict]:
        """Every request seen since the last ``reset``, refused CONNECTs included."""
        return [
            {
                "host": request.host,
                "method": request.method,
                "path": request.path if request.method != "CONNECT" else request.target,
                "query": request.query if request.method != "CONNECT" else "",
                "status": request.status if request.method != "CONNECT" else 502,
            }
            for request in self.proxy.observations
        ]

    def reset(self) -> None:
        """Clear the egress log and every per-test mode (services are shared per worker)."""
        self.proxy.clear()
        self.ado_auth_required = False
        for slug in list(self._unserved):
            self.restore(slug)

    def require_ado_auth(self, required: bool = True) -> None:
        """Make dev.azure.com refuse anonymous git and REST requests with 401."""
        self.ado_auth_required = required

    def serve(self, slug: str, origin: Path) -> None:
        """Serve bare repository *origin* as ``owner/repo`` *slug* on every git host.

        For repositories a test authors itself (``file://`` dependency specs are
        refused by the CLI). Served until the worker's services close.
        """
        owner, _, repo = slug.partition("/")
        if not owner or not repo or "/" in repo:
            raise ValueError(f"slug must be owner/repo, got {slug!r}")
        self._repositories[slug] = origin

    def unserve(self, slug: str) -> None:
        """Stop serving repository *slug*: every host answers 404 for it until ``restore``.

        Simulates a deleted or renamed upstream. ``reset`` restores every
        unserved repository, so the next world sees them again.
        """
        if slug not in self._repositories:
            raise KeyError(f"{slug!r} is not served; served: {sorted(self._repositories)}")
        self._unserved[slug] = self._repositories.pop(slug)

    def restore(self, slug: str) -> None:
        """Serve an ``unserve``d repository again."""
        if slug not in self._unserved:
            raise KeyError(f"{slug!r} is not unserved")
        self._repositories[slug] = self._unserved.pop(slug)

    def start(self, directory: Path) -> WorldServices:
        """Start the proxy; ``directory`` receives its private CA material."""
        stack = contextlib.ExitStack()
        self._proxy = stack.enter_context(local_scripted_https_proxy(directory, self.handlers()))
        self._stack = stack
        return self

    def close(self) -> None:
        if self._proxy is not None:
            self._stack.close()
            self._proxy = None

    def child_environment(self, environment: Mapping[str, str]) -> dict[str, str]:
        """Route HTTPS through the proxy, trust only its CA, and allow git over https."""
        child = self.proxy.child_environment(environment)
        child["GIT_ALLOW_PROTOCOL"] = "file:https"
        child["APM_GIT_PROTOCOL"] = "https"
        child["MCP_REGISTRY_URL"] = self.mcp_registry_url
        return child

    # -- handlers ------------------------------------------------------------

    def handlers(self) -> dict[str, Handler]:
        handlers: dict[str, Handler] = {GITHUB_HOST: self._git, RAW_HOST: self._raw}
        handlers[REGISTRY_HOST] = self._registry
        handlers[package_registry.HOST] = self.package_registry.handle
        for host in _API_HOSTS:
            handlers[host] = self._api
        for host, _prefix in HOST_ALIASES.values():
            handlers[host] = self._git
        return handlers

    def _slug_repository(self, owner: str, repo: str) -> Path | None:
        return self._repositories.get(f"{owner}/{repo.removesuffix('.git')}")

    def _ado_refusal(self, request: ScriptedRequest) -> ScriptedResponse | None:
        if request.host != ADO_HOST or not self.ado_auth_required:
            return None
        header = request.header("Authorization") or ""
        scheme, _, value = header.partition(" ")
        if scheme == "Bearer" and value == ADO_BEARER:
            return None
        if scheme == "Basic":
            with contextlib.suppress(ValueError):
                if base64.b64decode(value).decode("ascii").partition(":")[2] == ADO_PAT:
                    return None
        return ScriptedResponse(
            401, {"WWW-Authenticate": f'Basic realm="https://{ADO_HOST}/"'}, b"Unauthorized"
        )

    def _git(self, request: ScriptedRequest) -> ScriptedResponse:
        path = unquote(request.path)
        refusal = self._ado_refusal(request)
        if refusal is not None:
            return refusal
        if request.host == ADO_HOST and _ADO_ITEMS in path:
            return self._ado_items(request, path)
        if request.host in _V3_API_HOSTS and path.startswith(_GHES_API_PREFIX + "/"):
            return self._api(request, path[len(_GHES_API_PREFIX) :])
        service = next((s for s in _GIT_SERVICES if path.endswith(s)), None)
        if service is None:
            return _not_found()
        segments = [s for s in path[: -len(service)].split("/") if s and s != "_git"]
        if len(segments) < 2:
            return _not_found()
        repository = self._slug_repository(segments[0], segments[-1])
        if repository is None:
            return _not_found()
        return git_smart_http(repository, request, service)

    def _ado_items(self, request: ScriptedRequest, path: str) -> ScriptedResponse:
        """Azure DevOps Items API: ``/{org}/{project}/_apis/git/repositories/{repo}/items``."""
        head, _, tail = path.partition(_ADO_ITEMS)
        org, _, project = head.strip("/").partition("/")
        repo, _, endpoint = tail.partition("/")
        query = parse_qs(request.query)
        file_path = (query.get("path") or [""])[0].lstrip("/")
        if endpoint != "items" or not file_path:
            return _not_found()
        if org == policy_org("unreachable"):
            return _json(503, {"message": "Service Unavailable"})
        slug_repo = POLICY_REPO if (project, repo) in _ADO_POLICY_REPOS else repo
        repository = self._slug_repository(org, slug_repo)
        ref = (query.get("versionDescriptor.version") or ["HEAD"])[0]
        content = None if repository is None else self._blob(repository, ref, file_path)
        if content is None:
            return _json(404, {"message": f"TF401174: The item '{file_path}' could not be found."})
        return ScriptedResponse(200, {"Content-Type": "application/octet-stream"}, content)

    def _blob(self, repository: Path, ref: str, file_path: str) -> bytes | None:
        git = shutil.which("git") or "git"
        result = subprocess.run(
            (git, "--git-dir", str(repository), "cat-file", "blob", f"{ref}:{file_path}"),
            capture_output=True,
            check=False,
            timeout=30,
        )
        return result.stdout if result.returncode == 0 else None

    def _rev_parse(self, repository: Path, ref: str) -> str | None:
        git = shutil.which("git") or "git"
        result = subprocess.run(
            (
                git,
                "--git-dir",
                str(repository),
                "rev-parse",
                "--verify",
                "--quiet",
                f"{ref}^{{commit}}",
            ),
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        return result.stdout.strip() if result.returncode == 0 else None

    def _api(self, request: ScriptedRequest, path: str | None = None) -> ScriptedResponse:
        parts = unquote(path or request.path).lstrip("/").split("/", 4)
        if len(parts) < 3 or parts[0] != "repos":
            return _not_found()
        owner, repo = parts[1], parts[2]
        if owner == policy_org("unreachable"):
            return _json(503, {"message": "Service Unavailable"})
        repository = self._slug_repository(owner, repo)
        if repository is None:
            return _not_found()
        if len(parts) == 3:
            return _json(200, {"full_name": f"{owner}/{repo}", "default_branch": "main"})
        if len(parts) == 5 and parts[3] == "commits":
            return self._api_commit(request, repository, parts[4])
        if len(parts) == 5 and parts[3] == "contents":
            return self._api_contents(request, repository, parts[4])
        return _not_found()

    def _api_commit(self, request: ScriptedRequest, repository: Path, ref: str) -> ScriptedResponse:
        sha = self._rev_parse(repository, ref)
        if sha is None:
            return _not_found()
        if "sha" in (request.header("Accept") or ""):
            return ScriptedResponse(200, {"Content-Type": "text/plain"}, sha.encode("ascii"))
        return _json(200, {"sha": sha})

    def _api_contents(
        self, request: ScriptedRequest, repository: Path, file_path: str
    ) -> ScriptedResponse:
        ref = (parse_qs(request.query).get("ref") or ["HEAD"])[0]
        content = self._blob(repository, ref, file_path)
        if content is None:
            return _not_found()
        if "raw" in (request.header("Accept") or ""):
            return ScriptedResponse(200, {"Content-Type": "application/octet-stream"}, content)
        return _json(
            200,
            {
                "type": "file",
                "name": file_path.rsplit("/", 1)[-1],
                "path": file_path,
                "encoding": "base64",
                "content": base64.b64encode(content).decode("ascii"),
            },
        )

    def _raw(self, request: ScriptedRequest) -> ScriptedResponse:
        parts = unquote(request.path).lstrip("/").split("/")
        if len(parts) < 4:
            return _not_found()
        repository = self._slug_repository(parts[0], parts[1])
        if repository is None:
            return _not_found()
        for split in range(3, len(parts)):
            content = self._blob(repository, "/".join(parts[2:split]), "/".join(parts[split:]))
            if content is not None:
                return ScriptedResponse(200, {"Content-Type": "text/plain"}, content)
        return _not_found()

    def _registry(self, request: ScriptedRequest) -> ScriptedResponse:
        document = {
            "name": REGISTRY_SERVER,
            "description": "Lifecycle world registry server",
            "version": "1.0.0",
            "remotes": [{"transport_type": "http", "url": REGISTRY_REMOTE}],
        }
        path = urlsplit(request.target).path
        if path == "/v0.1/servers":
            summary = {"name": REGISTRY_SERVER, "description": document["description"]}
            return _json(200, {"servers": [{"server": summary}], "metadata": {}})
        detail = f"/v0.1/servers/{quote(REGISTRY_SERVER, safe='')}/versions/latest"
        if path in (detail, f"/v0.1/servers/{REGISTRY_SERVER}/versions/latest"):
            return _json(200, {"server": document})
        return _not_found()
