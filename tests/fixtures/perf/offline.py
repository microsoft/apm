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
import os
import sys
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
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


# A developer's global gitconfig: Git Credential Manager and gh helpers,
# non-interactive prompts, a URL-scoped useHttpPath and an SSH rewrite for a
# host the fixtures never use.
USER_GITCONFIG = """\
[user]
\tname = Dev User
\temail = dev@example.invalid
[init]
\tdefaultBranch = main
[pull]
\trebase = true
[credential]
\thelper = manager
\tinteractive = never
[credential "https://github.com"]
\thelper =
\thelper = !gh auth git-credential
[credential "https://dev.azure.com"]
\tuseHttpPath = true
[url "git@gitlab.example.invalid:"]
\tinsteadOf = https://gitlab.example.invalid/
"""

@contextlib.contextmanager
def serve_offline(
    fixtures: TreeFixture | Iterable[TreeFixture],
    directory: Path,
    *,
    rtt_ms: float = 0.0,
    base_env: Mapping[str, str] | None = None,
    policy: bool = False,
) -> Iterator[OfflineWorld]:
    """Serve every package of *fixtures* offline; yield the world for ``apm`` children.

    ``policy`` also serves ``POLICY_DOCUMENT`` as org ``POLICY_ORG``'s policy.
    """
    fixtures = (fixtures,) if isinstance(fixtures, TreeFixture) else tuple(fixtures)
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
    if any(fixture.recipe.user_gitconfig for fixture in fixtures):
        with open(env["GIT_CONFIG_GLOBAL"], "a", encoding="utf-8", newline="\n") as config:
            config.write(USER_GITCONFIG)
    if sys.platform == "win32":
        # Git for Windows' default schannel backend refuses the mirror's local CA
        # (no CRL or OCSP endpoint: "revocation status is unknown"). APM carries
        # only http.sslBackend/sslCAInfo/sslCAPath from this config into its
        # isolated git env, so pick OpenSSL, which trusts GIT_SSL_CAINFO as on Linux.
        with open(env["GIT_CONFIG_GLOBAL"], "a", encoding="utf-8", newline="\n") as config:
            config.write("[http]\n\tsslBackend = openssl\n")
    (directory / "services").mkdir(parents=True, exist_ok=True)
    services = WorldServices(repositories).start(directory / "services")
    try:
        services.proxy.rtt_ms = rtt_ms
        child = services.child_environment(env)
        if any(fixture.recipe.user_gitconfig for fixture in fixtures):
            # A developer has no REQUESTS_CA_BUNDLE: APM installs OS trust
            # (truststore) and adds the corporate (here: mirror) CA on top.
            child.pop("REQUESTS_CA_BUNDLE", None)
            child["APM_EXTRA_CA_BUNDLE"] = child["GIT_SSL_CAINFO"]
        world = OfflineWorld(child, services, isolated)
        yield world
    finally:
        services.close()
