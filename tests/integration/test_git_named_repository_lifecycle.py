"""Real-CLI lifecycle: repository names that end in git-suffix letters (#3715, 0.36.1 T4).

Shorthand parsing stripped the character set ``{., g, i, t}`` from the right of
each repository segment instead of the literal ``.git`` suffix, so names such
as ``git/git``, ``jonas/tig`` or ``owner/gitit`` were rejected, and installs by
URL wrote a manifest entry the next run could not parse. The marketplace
builder stripped ``./`` as a character set too, so a ``./.hidden`` local source
under ``pluginRoot: ./`` lost its leading dot. The approved 0.36.1 design only
widens what parses: names consisting solely of dots, ``.git`` alone and ``..``
stay rejected, and so do names still ending in ``.git`` once the transport
suffix is removed (they would be written back as a different repository).

Every package is served from an owned local repository under its real
identity; no host is contacted.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.utils.local_github_https_fixture import openssl_executable
from tests.utils.skill_selection_world import (
    AUDIT_CI,
    INSTALL,
    SkillSelectionWorld,
    audit_failures,
    skill_dirs,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.requires_e2e_mode,
    pytest.mark.skipif(openssl_executable() is None, reason="openssl CLI not available"),
]

_SKILLS_ROOT = Path(".agents") / "skills"

# case -> (cli argument, publish kwargs, manifest entry written and used to uninstall)
_ACCEPTED = {
    "github-git-git": ("git/git", {"owner": "git", "repo": "git"}, "git/git"),
    "github-jonas-tig": ("jonas/tig", {"owner": "jonas", "repo": "tig"}, "jonas/tig"),
    "github-owner-gitit": ("owner/gitit", {"owner": "owner", "repo": "gitit"}, "owner/gitit"),
    "github-dot-git-suffix": ("owner/git.git", {"owner": "owner", "repo": "git"}, "owner/git"),
    "github-url-form": (
        "https://github.com/git/git.git",
        {"owner": "git", "repo": "git"},
        "git/git",
    ),
    "gitlab-fqdn": (
        "gitlab.com/git/git",
        {"owner": "git", "repo": "git", "host": "gitlab.com"},
        "gitlab.com/git/git",
    ),
    "ado-shorthand": (
        "dev.azure.com/o/p/git",
        {
            "owner": "o/p",
            "repo": "git",
            "host": "dev.azure.com",
            "clone_url": "https://dev.azure.com/o/p/_git/git",
        },
        "dev.azure.com/o/p/git",
    ),
}


def _world(tmp_path: Path, apm_binary_path: Path) -> SkillSelectionWorld:
    return SkillSelectionWorld.create(tmp_path / "w", (str(apm_binary_path),))


def _manifest_apm(project: Path) -> list[object]:
    return list((load_yaml(project / "apm.yml").get("dependencies") or {}).get("apm") or [])


@pytest.mark.parametrize("case", sorted(_ACCEPTED))
def test_repository_named_with_git_letters_installs_reinstalls_and_uninstalls(
    tmp_path: Path, apm_binary_path: Path, case: str
) -> None:
    """Install by CLI argument, replay from the written manifest, then uninstall by name."""
    argument, publish, entry = _ACCEPTED[case]
    world = _world(tmp_path, apm_binary_path)
    world.publish("named", skills=("named-skill",), **publish)
    project = world.project("consumer")
    with world.serve() as env:
        world.run((*INSTALL, argument), cwd=project, env=env, scenario_id=f"{case}-add")
        assert skill_dirs(project / _SKILLS_ROOT) == ["named-skill"]
        assert _manifest_apm(project) == [entry]
        # The manifest entry APM wrote must parse on the next run (round trip).
        world.run(INSTALL, cwd=project, env=env, scenario_id=f"{case}-replay")
        audit = world.run(AUDIT_CI, cwd=project, env=env, expect=None, scenario_id=f"{case}-a")
        assert audit.returncode == 0, audit_failures(audit)
        world.run(("uninstall", entry), cwd=project, env=env, scenario_id=f"{case}-rm")
    assert _manifest_apm(project) == []
    assert skill_dirs(project / _SKILLS_ROOT) == []


def test_manifest_written_by_url_install_on_an_older_release_is_readable(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """A committed apm.yml holding ``git/git`` (what URL installs persisted) installs."""
    world = _world(tmp_path, apm_binary_path)
    world.publish("named", skills=("named-skill",), owner="git", repo="git")
    project = world.project("consumer", dependencies=["git/git"])
    with world.serve() as env:
        world.run(INSTALL, cwd=project, env=env, scenario_id="bricked-manifest")
        world.run(("deps", "list"), cwd=project, env=env, scenario_id="bricked-deps-list")
        world.run(("deps", "why", "git/git"), cwd=project, env=env, scenario_id="bricked-why")
    assert skill_dirs(project / _SKILLS_ROOT) == ["named-skill"]


@pytest.mark.parametrize(
    "argument",
    [
        "owner/.git",
        "owner/..",
        "owner/...",
        "https://github.com/owner/...",
        "https://github.com/owner/...git",
        "gitlab.com/owner/...",
        "owner/git.git.git",
    ],
)
def test_dot_only_or_bare_suffix_repository_names_stay_rejected_without_writes(
    tmp_path: Path, apm_binary_path: Path, argument: str
) -> None:
    """Rejected names leave the project untouched and exit nonzero."""
    world = _world(tmp_path, apm_binary_path)
    world.publish("placeholder", skills=("unused",))
    project = world.project("consumer")
    manifest_before = (project / "apm.yml").read_bytes()
    with world.serve() as env:
        result = world.run(
            (*INSTALL, argument), cwd=project, env=env, expect=None, scenario_id="rejected"
        )
    assert result.returncode != 0, result.stdout[-2000:]
    assert (project / "apm.yml").read_bytes() == manifest_before
    assert not (project / "apm.lock.yaml").exists()
    assert not (project / ".agents").exists()


@pytest.mark.parametrize("plugin_root", ["./", "."])
def test_marketplace_build_keeps_the_leading_dot_of_a_hidden_local_source(
    tmp_path: Path, apm_binary_path: Path, plugin_root: str
) -> None:
    """``pluginRoot: ./`` must not turn ``./.hidden/plug`` into ``./hidden/plug``."""
    world = _world(tmp_path, apm_binary_path)
    author = world.isolated.work_root / "author"
    plugin = author / ".hidden" / "plug"
    (plugin / "skills" / "hidden-skill").mkdir(parents=True)
    (plugin / "skills" / "hidden-skill" / "SKILL.md").write_text(
        "---\nname: hidden-skill\ndescription: hidden source\n---\n# hidden\n", encoding="utf-8"
    )
    dump_yaml(
        {"name": "plug", "version": "1.0.0", "description": "hidden plugin"},
        plugin / "apm.yml",
    )
    dump_yaml(
        {
            "name": "hidden-catalog",
            "version": "1.0.0",
            "description": "catalog with a hidden local source",
            "marketplace": {
                "owner": {"name": "Fixture"},
                "metadata": {"pluginRoot": plugin_root},
                "packages": [{"name": "plug", "source": "./.hidden/plug"}],
            },
        },
        author / "apm.yml",
    )
    env = world.isolated.subprocess_env()
    world.run(("pack", "--offline"), cwd=author, env=env, scenario_id="hidden-pack")
    catalogs = sorted(author.rglob("marketplace.json"))
    assert catalogs, sorted(p.as_posix() for p in author.rglob("*"))
    for catalog in catalogs:
        data = json.loads(catalog.read_text(encoding="utf-8"))
        sources = [p.get("source") for p in data.get("plugins", [])]
        assert sources == ["./.hidden/plug"], (catalog, sources)


def test_policy_deny_pattern_matches_a_git_named_repository(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """Names that used to fail parsing now reach policy evaluation and can be denied."""
    world = _world(tmp_path, apm_binary_path)
    world.publish("named", skills=("named-skill",), owner="git", repo="git")
    project = world.project("consumer")
    policy = world.isolated.work_root / "apm-policy.yml"
    dump_yaml(
        {
            "name": "deny-git-org",
            "version": "1.0.0",
            "enforcement": "block",
            "dependencies": {"deny": ["git/**"]},
        },
        policy,
    )
    with world.serve() as env:
        world.run((*INSTALL, "git/git"), cwd=project, env=env, scenario_id="policy-install")
        result = world.run(
            ("audit", "--ci", "--policy", str(policy), "--no-fail-fast", "--format", "json"),
            cwd=project,
            env=env,
            expect=None,
            scenario_id="policy-audit",
        )
    failures = audit_failures(result)
    assert result.returncode != 0, result.stdout[-3000:]
    assert any("git/git" in " ".join(details) for details in failures.values()), failures
