"""Mixed-host install keeps each package's explicit host (#2775).

PROMISE: P-OWN-host-reference-coordinates -- a host-qualified package keeps its
own host, repository boundary, subpath and ref through install, manifest and
lock reload, frozen reinstall, update and removal; ``GITHUB_HOST`` applies only
to shorthand without a host (source: models/dependency/reference.py;
docs/src/content/docs/getting-started/authentication.md "GitHub Enterprise
Server").
PROMISE: P-LAW-auth.credentials_isolated -- the GitHub Enterprise Server
credential reaches only that server and the github.com credential never
reaches it (source: tests/fixtures/lifecycle_bug_ledger/).
PROMISE: P-INV-I7 -- an unrecognised host fails before any state write, with
its configuration guidance (source: utils/github_host.py
unsupported_host_error).

The world serves one internal repository on the GHES alias only and two public
repositories on github.com only (every other host answers 404), so a package
fetched from the wrong host fails instead of passing by accident. The GHES
host is configured the supported way, with ``GITHUB_HOST``; APM writes its
entry as shorthand because ``GITHUB_HOST`` is the implementation-default host
it strips (req-mf-009, CONFORMANCE.md).
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, replace

import pytest
import yaml

from tests.lifecycle.scenarios._kit import Scenario, assert_changes, serve_repository
from tests.lifecycle.world.services import (
    API_HOST,
    CODELOAD_HOST,
    GITHUB_HOST,
    HOST_ALIASES,
    RAW_HOST,
)
from tests.utils.local_git_repository import (
    GitCommit,
    LocalGitRepository,
    LocalGitRepositoryFactory,
)

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.integration,
    pytest.mark.promise(
        "P-OWN-host-reference-coordinates",
        "P-OWN-host-credential-resolution",
        "P-LAW-auth.credentials_isolated",
        "P-INV-I1",
        "P-INV-I2",
        "P-INV-I7",
    ),
]

GHES = HOST_ALIASES["ghes"][0]
# A second, unrecognised enterprise server (served only here).
OTHER = HOST_ALIASES["generic"][0]
OTHER_SLUG = "beta/team-skills"
OTHER_SUBPATH = "skills/team-lint"
GITHUB_HOSTS = frozenset({GITHUB_HOST, API_HOST, RAW_HOST, CODELOAD_HOST})
INTERNAL = "acme/internal-skills"
ANDROID = "example/android-skills"
KOTLIN = "example/kotlin-skills"
# slug -> skill subpath inside the repository.
SUBPATHS = {
    INTERNAL: ".agents/skills/jdk-installer",
    ANDROID: "performance/analyzer",
    KOTLIN: "skills/java-to-kotlin",
}
TAG = "v1.0.0"
# Inert sentinels shaped like real tokens.
GHES_TOKEN = "ghp_" + "Ghes2775Sentinel".ljust(36, "0")
GITHUB_TOKEN = "ghp_" + "Dotcom2775Sentinel".ljust(36, "1")
MANIFEST = "home:.apm/apm.yml"
LOCK = "home:.apm/apm.lock.yaml"


def _skill_name(slug: str) -> str:
    return SUBPATHS[slug].rsplit("/", 1)[-1]


def _skills(*slugs: str) -> tuple[str, ...]:
    return tuple(f"home:.agents/skills/{_skill_name(slug)}*" for slug in slugs)


@dataclass
class MixedHosts:
    """The three served repositories and the commit each one's tag points at."""

    factory: LocalGitRepositoryFactory
    repositories: dict[str, LocalGitRepository]
    tagged: dict[str, GitCommit]

    def specs(self) -> dict[str, str]:
        """The install arguments: a tag on GHES, a branch and a full SHA on github.com."""
        return {
            _skill_name(INTERNAL): f"{GHES}/{INTERNAL}/{SUBPATHS[INTERNAL]}#{TAG}",
            _skill_name(ANDROID): f"github.com/{ANDROID}/{SUBPATHS[ANDROID]}#main",
            _skill_name(KOTLIN): (
                f"github.com/{KOTLIN}/{SUBPATHS[KOTLIN]}#{self.tagged[KOTLIN].sha}"
            ),
        }

    def advance(self, slug: str) -> GitCommit:
        """Publish a new commit on *slug*'s main branch."""
        repository = self.repositories[slug]
        skill = repository.worktree / SUBPATHS[slug] / "SKILL.md"
        skill.write_text(skill.read_text(encoding="ascii") + "\nRevised.\n", encoding="ascii")
        return self.factory.commit(repository, message=f"{slug} revised")


@pytest.fixture
def mixed_hosts(lifecycle_world) -> MixedHosts:
    world = lifecycle_world
    factory = LocalGitRepositoryFactory(world.root / "mixed-repos", env=world.env)
    repositories: dict[str, LocalGitRepository] = {}
    tagged: dict[str, GitCommit] = {}
    for slug, subpath in SUBPATHS.items():
        tree = world.root / "mixed-src" / slug.replace("/", "-")
        skill = tree / subpath
        skill.mkdir(parents=True)
        name = _skill_name(slug)
        (skill / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: Mixed-host skill {name}.\n---\n\nUse {name}.\n",
            encoding="ascii",
        )
        repository = factory.create(slug.replace("/", "-"), source_tree=tree)
        commit = factory.commit(repository, message=slug)
        factory.tag(repository, TAG, commit)
        serve_repository(world, slug, repository.origin)
        repositories[slug] = repository
        tagged[slug] = commit
    # Each repository exists on one host only, as for a real user.
    world.services.script(GHES, "/example/", 404)
    world.services.script(GHES, "/api/v3/repos/example/", 404)
    world.services.script(GITHUB_HOST, "/acme/", 404)
    world.services.script(API_HOST, "/repos/acme/", 404)
    world.services.script(RAW_HOST, "/acme/", 404)
    mixed = MixedHosts(factory, repositories, tagged)
    # main moves past the tag (GHES) and SHA (Kotlin) pins, so a default-branch
    # fetch would lock a different commit than the pin.
    for slug in (INTERNAL, KOTLIN):
        assert mixed.advance(slug).sha != tagged[slug].sha
    return mixed


def _with_env(world, **values: str | None):
    env = dict(world.env)
    for name, value in values.items():
        if value is None:
            env.pop(name, None)
        else:
            env[name] = value
    return replace(world, env=env)


def _scenario(world, mixed: MixedHosts) -> Scenario:
    specs = mixed.specs()
    return Scenario(world, specs, kinds=dict.fromkeys(specs, ("skill",)), scope="user")


def _lock_rows(scenario: Scenario) -> dict[str, tuple[str | None, str | None, str | None]]:
    """``host/repo_url`` -> (virtual_path, resolved_ref, resolved_commit) of every lock row."""
    lock = scenario.lock()
    assert lock is not None, "no apm.lock.yaml"
    return {
        f"{row.host}/{row.repo_url}": (row.virtual_path, row.resolved_ref, row.resolved_commit)
        for row in lock.get_package_dependencies()
    }


def _manifest_entries(scenario: Scenario) -> list[object]:
    data = yaml.safe_load((scenario.manifest_root / "apm.yml").read_text(encoding="utf-8"))
    return data["dependencies"]["apm"]


def _assert_host_isolation(world) -> None:
    """Every request went to its repository's own host, carrying only that host's token."""
    misrouted = [
        entry
        for entry in world.services.egress_log()
        if ("/acme/" in entry["path"] and entry["host"] != GHES)
        or ("/example/" in entry["path"] and entry["host"] not in GITHUB_HOSTS)
    ]
    assert not misrouted, f"requests reached the wrong host: {misrouted}"
    assert _carriers(world, GHES_TOKEN) <= {GHES}, "the GHES credential left its host"
    assert GHES not in _carriers(world, GITHUB_TOKEN), "the github.com credential reached GHES"


def _carriers(world, secret: str) -> set[str]:
    """Hosts of every request that carried *secret* in a header."""
    return {
        request.host
        for request in world.services.proxy.observations
        if any(secret in value for value in request.header_values())
    }


def test_mixed_host_global_lifecycle_keeps_each_package_host(lifecycle_world, mixed_hosts):
    world = _with_env(
        lifecycle_world,
        GITHUB_HOST=GHES,
        GITHUB_APM_PAT_ACME=GHES_TOKEN,
        GITHUB_APM_PAT=GITHUB_TOKEN,
    )
    scenario = _scenario(world, mixed_hosts)
    specs = mixed_hosts.specs()
    kotlin_sha = mixed_hosts.tagged[KOTLIN].sha

    install = scenario.run(f"install {' '.join(specs)}")
    assert_changes(
        install.changed,
        (MANIFEST, LOCK, "home:.apm/apm_modules*", *_skills(*SUBPATHS)),
        label="mixed-host install",
    )
    scenario.assert_deployed()
    assert _manifest_entries(scenario) == [
        f"{INTERNAL}/{SUBPATHS[INTERNAL]}#{TAG}",
        f"github.com/{ANDROID}/{SUBPATHS[ANDROID]}#main",
        f"github.com/{KOTLIN}/{SUBPATHS[KOTLIN]}#{kotlin_sha}",
    ]
    locked = {
        f"{GHES}/{INTERNAL}": (SUBPATHS[INTERNAL], TAG, mixed_hosts.tagged[INTERNAL].sha),
        f"github.com/{ANDROID}": (SUBPATHS[ANDROID], "main", mixed_hosts.tagged[ANDROID].sha),
        f"github.com/{KOTLIN}": (SUBPATHS[KOTLIN], kotlin_sha, kotlin_sha),
    }
    assert _lock_rows(scenario) == locked
    _assert_host_isolation(world)
    assert _carriers(world, GHES_TOKEN) == {GHES}, "the GHES credential left its host"
    world.services.proxy.clear()

    # Manifest and lock reload: a converged install changes nothing.
    reload = scenario.run("install")
    assert_changes(reload.changed, (), label="reload")
    assert _lock_rows(scenario) == locked

    # Frozen reinstall on a clean machine: every package from its own host
    # at the locked commit.
    shutil.rmtree(scenario.manifest_root / "apm_modules")
    shutil.rmtree(world.cache_root)
    for slug in SUBPATHS:
        shutil.rmtree(scenario.deploy_root / ".agents" / "skills" / _skill_name(slug))
    scenario.source_moved()
    frozen = scenario.run("install --frozen")
    assert_changes(
        frozen.changed, ("home:.apm/apm_modules*", *_skills(*SUBPATHS)), label="frozen reinstall"
    )
    scenario.assert_deployed()
    assert _lock_rows(scenario) == locked
    assert {entry["host"] for entry in world.services.egress_log()} >= {GHES, GITHUB_HOST}
    _assert_host_isolation(world)
    world.services.proxy.clear()

    # Update moves the branch-pinned package only.
    revised = mixed_hosts.advance(ANDROID)
    scenario.source_moved()
    update = scenario.run("update --yes")
    assert_changes(
        update.changed, (LOCK, "home:.apm/apm_modules*", *_skills(ANDROID)), label="update"
    )
    locked[f"github.com/{ANDROID}"] = (SUBPATHS[ANDROID], "main", revised.sha)
    assert _lock_rows(scenario) == locked
    _assert_host_isolation(world)
    world.services.proxy.clear()

    # Removal drops one github.com package and keeps the others on their hosts.
    # The full spec, so the CLI and the scenario model select the same package.
    remove = scenario.run(f"uninstall {specs[_skill_name(ANDROID)]}")
    assert_changes(
        remove.changed,
        (MANIFEST, LOCK, "home:.apm/apm_modules*", *_skills(ANDROID)),
        label="remove",
    )
    del locked[f"github.com/{ANDROID}"]
    assert _lock_rows(scenario) == locked
    assert _manifest_entries(scenario) == [
        f"{INTERNAL}/{SUBPATHS[INTERNAL]}#{TAG}",
        f"github.com/{KOTLIN}/{SUBPATHS[KOTLIN]}#{kotlin_sha}",
    ]
    assert world.services.egress_log() == [], "uninstall reached the network"
    scenario.finish()


def test_strict_host_credentials_keep_the_ghes_token_on_ghes(lifecycle_world, mixed_hosts):
    world = _with_env(
        lifecycle_world,
        GITHUB_HOST=GHES,
        GITHUB_APM_PAT=GHES_TOKEN,
        APM_STRICT_HOST_CREDENTIALS="1",
        # Resolve github.com credentials before the anonymous attempt, so the
        # carrier check sees strict mode withhold the GHES token there.
        APM_GITHUB_AUTH_FIRST="1",
    )
    scenario = _scenario(world, mixed_hosts)
    step = scenario.run(f"install {' '.join(mixed_hosts.specs())}")
    assert_changes(
        step.changed,
        (MANIFEST, LOCK, "home:.apm/apm_modules*", *_skills(*SUBPATHS)),
        label="strict mixed-host install",
    )
    scenario.assert_deployed()
    _assert_host_isolation(world)
    assert _carriers(world, GHES_TOKEN) == {GHES}, "the GHES credential left its host"
    scenario.finish()


@pytest.mark.parametrize("remedy", ["github-host", "object-form"])
def test_unconfigured_ghes_host_is_refused_with_working_remedies(
    lifecycle_world, mixed_hosts, remedy
):
    world = _with_env(lifecycle_world, GITHUB_HOST=None)
    scenario = _scenario(world, mixed_hosts)
    specs = mixed_hosts.specs()
    internal = _skill_name(INTERNAL)
    public = [spec for name, spec in specs.items() if name != internal]

    # Without GITHUB_HOST the GHES host is not GitHub-class: its package is
    # refused before any write and never re-homed to github.com; the others install.
    step = scenario.run(
        f"install {' '.join(specs.values())}",
        requested=scenario.requested(("install", *public)),
    )
    assert_changes(
        step.changed,
        (MANIFEST, LOCK, "home:.apm/apm_modules*", *_skills(ANDROID, KOTLIN)),
        label="unconfigured host",
    )
    scenario.assert_deployed()
    assert f"{GHES}/{INTERNAL}" not in _lock_rows(scenario)
    assert [entry for entry in _manifest_entries(scenario) if "acme" in str(entry)] == []
    assert f"[x] {specs[internal]} -- '{GHES}' was treated as a single repository path" in (
        step.output
    ), step.output
    assert (
        f"If this is GitHub Enterprise Server, set GITHUB_HOST={GHES}, write github.com/ "
        "before apm.yml entries for packages on GitHub, and re-run."
    ) in step.output, step.output
    assert "use an explicit 'git:' + 'path:' entry in apm.yml" in step.output, step.output
    _assert_host_isolation(world)
    world.services.proxy.clear()

    if remedy == "github-host":
        # Printed remedy 1: configure the GHES host, qualify the GitHub entries
        # (written as shorthand while github.com was the default host) and
        # re-run the same command.
        scenario.world = _with_env(world, GITHUB_HOST=GHES)
        manifest = scenario.manifest_root / "apm.yml"
        data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
        assert data["dependencies"]["apm"] == [spec.removeprefix("github.com/") for spec in public]
        data["dependencies"]["apm"] = public
        manifest.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
        scenario.source_moved()
        remedied = scenario.run(f"install {' '.join(specs.values())}")
        assert_changes(
            remedied.changed,
            (MANIFEST, LOCK, "home:.apm/apm_modules*", *_skills(INTERNAL)),
            label="GITHUB_HOST remedy",
        )
        scenario.assert_deployed()
        assert _lock_rows(scenario)[f"{GHES}/{INTERNAL}"] == (
            SUBPATHS[INTERNAL],
            TAG,
            mixed_hosts.tagged[INTERNAL].sha,
        )
        _assert_host_isolation(world)
        scenario.finish()
        return

    # Printed remedy 2: an explicit repository + path object, still without GITHUB_HOST.
    manifest = scenario.manifest_root / "apm.yml"
    data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    data["dependencies"]["apm"].append(
        {"git": f"https://{GHES}/{INTERNAL}.git", "path": SUBPATHS[INTERNAL], "ref": TAG}
    )
    manifest.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    scenario.adopt(internal, lock="stale")
    remedied = scenario.run("install")
    assert_changes(
        remedied.changed,
        (LOCK, "home:.apm/apm_modules*", *_skills(INTERNAL)),
        label="object-form remedy",
    )
    scenario.assert_deployed()
    assert _lock_rows(scenario)[f"{GHES}/{INTERNAL}"] == (
        SUBPATHS[INTERNAL],
        TAG,
        mixed_hosts.tagged[INTERNAL].sha,
    )
    _assert_host_isolation(world)
    scenario.finish()


def test_frozen_reload_without_the_configured_host_fails_closed(lifecycle_world, mixed_hosts):
    world = _with_env(lifecycle_world, GITHUB_HOST=GHES)
    scenario = _scenario(world, mixed_hosts)
    scenario.run(f"install {' '.join(mixed_hosts.specs())}")
    world.services.proxy.clear()

    # The shorthand GHES entry needs GITHUB_HOST; without it the lock no
    # longer matches and --frozen refuses before any fetch or write.
    scenario.world = _with_env(world, GITHUB_HOST=None)
    scenario.adopt(lock="stale")
    refused = scenario.run("install --frozen", exit=1)
    assert_changes(refused.changed, (), label="frozen without GITHUB_HOST")
    assert (
        f"{INTERNAL}/{SUBPATHS[INTERNAL]} is declared in apm.yml but missing from apm.lock.yaml"
        in refused.output
    ), refused.output
    assert world.services.egress_log() == [], "a refused frozen install reached the network"
    scenario.assert_deployed()
    assert f"{GHES}/{INTERNAL}" in _lock_rows(scenario)

    # Its printed tip, run in the same environment, fails and keeps every byte.
    tip = scenario.run("install", exit=1)
    assert_changes(tip.changed, (), label="tip without GITHUB_HOST")
    scenario.assert_deployed()
    scenario.finish()


def test_configured_server_keeps_shorthand_and_object_form_adds_another(
    lifecycle_world, mixed_hosts
):
    """GITHUB_HOST already names a server: the hint keeps it; object form adds the second one."""
    world = _with_env(lifecycle_world, GITHUB_HOST=GHES)
    tree = world.root / "mixed-src" / "beta-team-skills"
    (tree / OTHER_SUBPATH).mkdir(parents=True)
    (tree / OTHER_SUBPATH / "SKILL.md").write_text(
        "---\nname: team-lint\ndescription: Second-server skill.\n---\n\nUse team-lint.\n",
        encoding="ascii",
    )
    repository = mixed_hosts.factory.create("beta-team-skills", source_tree=tree)
    other_commit = mixed_hosts.factory.commit(repository, message=OTHER_SLUG)
    mixed_hosts.factory.tag(repository, TAG, other_commit)
    serve_repository(world, OTHER_SLUG, repository.origin)
    for host, prefix in ((GHES, "/beta/"), (GHES, "/api/v3/repos/beta/"), (GITHUB_HOST, "/beta/")):
        world.services.script(host, prefix, 404)
    world.services.script(API_HOST, "/repos/beta/", 404)
    world.services.script(RAW_HOST, "/beta/", 404)

    internal = _skill_name(INTERNAL)
    shorthand = f"{INTERNAL}/{SUBPATHS[INTERNAL]}#{TAG}"
    specs = {internal: shorthand, "team-lint": f"{OTHER}/{OTHER_SLUG}/{OTHER_SUBPATH}#{TAG}"}
    scenario = Scenario(world, specs, kinds=dict.fromkeys(specs, ("skill",)), scope="user")
    scenario.run(f"install {shorthand}")
    ghes_row = (SUBPATHS[INTERNAL], TAG, mixed_hosts.tagged[INTERNAL].sha)
    assert _lock_rows(scenario) == {f"{GHES}/{INTERNAL}": ghes_row}
    world.services.proxy.clear()

    scenario.source_moved()
    refused = scenario.run(f"install {specs['team-lint']}", requested=scenario.model, exit=1)
    assert_changes(refused.changed, (), label="second server refused")
    assert (
        f"GITHUB_HOST already names {GHES}; keep it and, if this is another GitHub "
        "Enterprise Server, add this package as an explicit 'git:' + 'path:' entry in apm.yml."
    ) in refused.output, refused.output
    assert "set GITHUB_HOST=" not in refused.output, refused.output
    assert _manifest_entries(scenario) == [shorthand]
    world.services.proxy.clear()

    # The printed remedy: keep GITHUB_HOST and add an explicit object entry.
    manifest = scenario.manifest_root / "apm.yml"
    data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    data["dependencies"]["apm"].append(
        {"git": f"https://{OTHER}/{OTHER_SLUG}.git", "path": OTHER_SUBPATH, "ref": TAG}
    )
    manifest.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    scenario.adopt(internal, "team-lint", lock="stale")
    remedied = scenario.run("install")
    assert_changes(
        remedied.changed,
        (LOCK, "home:.apm/apm_modules*", "home:.agents/skills/team-lint*"),
        label="object-form second server",
    )
    scenario.assert_deployed()
    assert _lock_rows(scenario) == {
        f"{GHES}/{INTERNAL}": ghes_row,
        f"{OTHER}/{OTHER_SLUG}": (OTHER_SUBPATH, TAG, other_commit.sha),
    }
    assert _manifest_entries(scenario)[0] == shorthand, "the shorthand entry was rewritten"
    misrouted = [
        entry
        for entry in world.services.egress_log()
        if ("/beta/" in entry["path"] and entry["host"] != OTHER)
        or ("/acme/" in entry["path"] and entry["host"] != GHES)
    ]
    assert not misrouted, f"requests reached the wrong host: {misrouted}"
    assert any(entry["host"] == OTHER for entry in world.services.egress_log())
    scenario.finish()
