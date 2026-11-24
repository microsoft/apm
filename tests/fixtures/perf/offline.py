"""Offline local-mirror mode of the perf fixtures, with an injectable RTT.

``serve_offline`` publishes generated packages as bare git repositories and
serves them under their GitHub-shaped ``owner/repo`` slugs through the
lifecycle world's ``WorldServices`` (a recording HTTPS proxy in front of
``git http-backend`` and a GitHub API shape). The child environment comes from
``IsolatedApmEnvironment``: a private HOME, APM_HOME, cache and git config, so
cold and warm cache runs are explicit and never touch the host.

The proxy injects ``rtt_ms`` per round trip (2 per new HTTPS connection, 1
per request) and counts connects, requests and round trips per command.

Unlike lifecycle tests, the environment drops the Python network-guard
``sitecustomize`` (it adds import-time cost to every run of a source install
and does not apply to the frozen binary). The proxy still refuses every host
it does not serve, so nothing leaves the machine through HTTPS.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import ssl
import subprocess
import sys
import threading
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import yaml

from tests.fixtures.perf.generator import (
    TreeFixture,
    commit_bare,
    publish_repositories,
    remove_tree,
)
from tests.lifecycle.world.services import WorldServices
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_scripted_https_proxy import Traffic

_NETWORK_GUARD_ENV = "PYTHONPATH"
# The org whose ``.github/apm-policy.yml`` governs a ``-policy`` fixture's
# project (its git remote selects the org, as it does for a real user).
POLICY_ORG = "perf-policy"
# A blocking org policy shaped like an enterprise one: deny and MCP rules that
# every dependency is matched against, none of which refuse the perf packages.
POLICY_DOCUMENT = {
    "name": "perf-enterprise",
    "version": "1.0",
    "enforcement": "block",
    "dependencies": {"deny": ["evil-org/**", "**/forbidden-*"]},
    "mcp": {"deny": ["evil-org/*"]},
}


@dataclass
class OfflineWorld:
    """A running local mirror plus the environment an ``apm`` child must use."""

    env: dict[str, str]
    services: WorldServices
    isolated: IsolatedApmEnvironment
    # The git TLS configuration the world modeled (Windows: "mode" plus the
    # system keys it carried).
    tls: dict[str, str] = field(default_factory=dict)

    @property
    def rtt_ms(self) -> float:
        return self.services.proxy.rtt_ms

    @rtt_ms.setter
    def rtt_ms(self, value: float) -> None:
        self.services.proxy.rtt_ms = value

    @property
    def cache_dir(self) -> Path:
        return self.isolated.cache_root

    @property
    def apm_home(self) -> Path:
        return self.isolated.config_root

    def traffic(self) -> Traffic:
        return self.services.proxy.traffic()

    def reset_traffic(self) -> None:
        self.services.proxy.reset_traffic()

    def egress_log(self) -> list[dict]:
        return self.services.egress_log()

    def clear_cache(self) -> None:
        """Empty APM_CACHE_DIR: the next command runs against a cold cache."""
        remove_tree(self.cache_dir)
        self.cache_dir.mkdir(parents=True)


def isolated_environment(
    root: Path, base_env: Mapping[str, str] | None = None
) -> tuple[IsolatedApmEnvironment, dict[str, str]]:
    """A private HOME / APM_HOME / cache environment for benchmark children."""
    base = dict(base_env or os.environ)
    isolated = IsolatedApmEnvironment.create(root, base_env=base)
    env = dict(isolated.process_environment)
    # A relocatable git (for example one bundled with an IDE) finds its helpers,
    # such as git-remote-https, only through the parent's GIT_EXEC_PATH.
    exec_path = base.get("GIT_EXEC_PATH")
    if exec_path and Path(exec_path).is_dir():
        env["GIT_EXEC_PATH"] = exec_path
    env.pop(_NETWORK_GUARD_ENV, None)
    for host_flag in ("CI", "GITHUB_ACTIONS"):
        env.pop(host_flag, None)
    env["APM_NON_INTERACTIVE"] = "1"
    env["COLUMNS"] = "200"
    return isolated, env


@contextlib.contextmanager
def serve_offline(
    fixtures: TreeFixture | Iterable[TreeFixture],
    directory: Path,
    *,
    rtt_ms: float = 0.0,
    base_env: Mapping[str, str] | None = None,
    policy: bool = False,
    tls_mode: str = "system",
) -> Iterator[OfflineWorld]:
    """Serve every package of *fixtures* offline; yield the world for ``apm`` children.

    ``policy`` also serves ``POLICY_DOCUMENT`` as org ``POLICY_ORG``'s policy.
    ``tls_mode`` (Windows only): ``"system"`` models the default Git for Windows
    TLS config (its system backend, schannel on default installs); ``"openssl"``
    forces OpenSSL with the mirror CA file.
    """
    if isinstance(fixtures, TreeFixture):
        fixtures = (fixtures,)
    repositories: dict[str, Path] = {}
    for fixture in fixtures:
        published = publish_repositories(fixture, directory / "repositories")
        overlap = sorted(set(published) & set(repositories))
        if overlap:
            raise ValueError(
                f"{fixture.recipe.name} reuses slugs another served fixture owns: "
                f"{overlap[:3]}...; serve one tree per world or give recipes distinct owners"
            )
        repositories.update(published)
    if policy:
        tree = directory / "policy-tree"
        remove_tree(tree)
        tree.mkdir(parents=True)
        (tree / "apm-policy.yml").write_text(yaml.safe_dump(POLICY_DOCUMENT), encoding="utf-8")
        bare = directory / "repositories" / f"{POLICY_ORG}__github.git"
        remove_tree(bare)
        commit_bare(bare, tree, tag=None)
        repositories[f"{POLICY_ORG}/.github"] = bare
    isolated, env = isolated_environment(directory / "env", base_env)
    (directory / "services").mkdir(parents=True, exist_ok=True)
    with contextlib.ExitStack() as stack:
        tls: dict[str, str] = {}
        crl_url = None
        if sys.platform == "win32" and tls_mode == "openssl":
            # Secondary mode: OpenSSL, which trusts GIT_SSL_CAINFO as on Linux.
            with open(env["GIT_CONFIG_GLOBAL"], "a", encoding="utf-8", newline="\n") as config:
                config.write("[http]\n\tsslBackend = openssl\n")
            tls = {"mode": "openssl"}
        elif sys.platform == "win32":
            # Default mode: the runner's own Git for Windows TLS config (schannel),
            # the mirror CA in the Windows store and a CRL the leaf names, so the
            # default revocation check passes with no config change.
            crl_url = stack.enter_context(_serve_crl(directory / "services" / "ca.crl"))
            tls = {"mode": "system", **carry_system_tls(env["GIT_CONFIG_GLOBAL"])}
        services = WorldServices(repositories).start(directory / "services", crl_url=crl_url)
        stack.callback(services.close)
        if crl_url:
            stack.enter_context(_trusted_by_windows(services.proxy.ca_path))
        services.proxy.rtt_ms = rtt_ms
        yield OfflineWorld(services.child_environment(env), services, isolated, tls)


_SYSTEM_TLS_KEYS = ("http.sslBackend", "http.sslCAInfo")


def carry_system_tls(global_config: str) -> dict[str, str]:
    """Copy the system git TLS keys into an isolated *global_config*; return them.

    Git for Windows keeps its TLS backend (and any CA bundle) in the SYSTEM
    config, which an isolated environment hides (``GIT_CONFIG_NOSYSTEM``).
    """
    carried: dict[str, str] = {}
    for key in _SYSTEM_TLS_KEYS:
        found = subprocess.run(
            ["git", "config", "--system", "--get", key],
            capture_output=True,
            text=True,
            check=False,
        )
        value = found.stdout.strip()
        if found.returncode == 0 and value:
            subprocess.run(
                ["git", "config", "--file", global_config, key, value],
                capture_output=True,
                check=True,
            )
            carried[key] = value
    return carried


@contextlib.contextmanager
def _serve_crl(path: Path) -> Iterator[str]:
    """Serve *path* (written later, at mint time) over loopback HTTP; yield its URL."""

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            body = path.read_bytes() if path.is_file() else b""
            self.send_response(200 if body else 404)
            self.send_header("Content-Type", "application/pkix-crl")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *_args: object) -> None:
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, name="perf-crl", daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/ca.crl"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@contextlib.contextmanager
def _trusted_by_windows(ca_pem: Path) -> Iterator[None]:
    """Add the mirror CA to the machine Root store (CI runners are admin)."""
    der = ssl.PEM_cert_to_DER_cert(ca_pem.read_text(encoding="ascii"))
    thumbprint = hashlib.sha1(der, usedforsecurity=False).hexdigest()
    added = subprocess.run(
        ["certutil", "-addstore", "-f", "Root", str(ca_pem)],
        capture_output=True,
        text=True,
        check=False,
    )
    if added.returncode != 0:
        raise RuntimeError(
            "the default Windows TLS mode puts the mirror CA in the machine Root store "
            f"(needs admin): {added.stdout[-300:]}{added.stderr[-300:]}; "
            "use an '-openssl' fixture to run without it"
        )
    try:
        yield
    finally:
        subprocess.run(
            ["certutil", "-delstore", "Root", thumbprint],
            capture_output=True,
            check=False,
        )
