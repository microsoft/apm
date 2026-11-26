"""Real-CLI marketplace ref pins compare commits, not ref strings (#5526).

PROMISE: P-OWN-marketplace-ref-pin-commit -- a ref that contradicts the
recorded pin is refused; a ref that selects the same commit is no
contradiction, and every refusal names a fix that works (source:
src/apm_cli/marketplace/version_pins.py enforce_ref_pin; #5526).
CELL: OP=install SRC=marketplace(git-subdir, ref+sha) SCOPE=project
LOCK=none DEPLOYED=none, plugin transitive through a newly added package,
legacy alias-keyed tag pin written by an older APM, annotated release tag.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from apm_cli.marketplace.models import MarketplaceSource
from apm_cli.marketplace.source_identity import catalog_pin_identity
from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.integration.test_marketplace_ref_pin_lifecycle import (
    _assert_output_oracles,
    _evidence,
    _snapshot,
)
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_git_repository import LocalGitRepositoryFactory
from tests.utils.local_package import LocalPackageFactory

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.requires_e2e_mode,
    pytest.mark.promise("P-OWN-marketplace-ref-pin-commit", "P-INV-I7"),
]

_HOST = "gitlab.example.invalid"
_ALIAS = "atlas"
_PLUGIN = "okf"
_VERSION = "0.2.1"
_TAG = "v0.2.1"
_CATALOG_REMOTE = f"https://{_HOST}/team/atlas-marketplace"
_PLUGIN_REMOTE = f"https://{_HOST}/team/okf"
_PACKAGE = "danielmeppiel/precedent"
_ATLAS_PACKAGE = "sergio-sisternes-epam/atlas"
_BASE_PACKAGE = "acme/base"
_CATALOG_PATH = ".claude-plugin/marketplace.json"
_FLAGS = ("--target", "codex", "--parallel-downloads", "0")
_LEGACY_PIN = {f"{_ALIAS}/{_PLUGIN}/{_VERSION}": _TAG}
_PIN_KEY = (
    catalog_pin_identity(MarketplaceSource(name=_ALIAS, url=_CATALOG_REMOTE, path=_CATALOG_PATH))
    + f"#{_PLUGIN}/{_VERSION}"
)


def _skill(body: str) -> str:
    return f"---\nname: {_PLUGIN}\ndescription: d\n---\n{body}\n"


class _World:
    """A catalog listing okf 0.2.1 by tag and SHA, okf with an annotated tag, and precedent."""

    def __init__(self, tmp_path: Path, apm_binary_path: Path, *, catalog_sha: str) -> None:
        isolated = IsolatedApmEnvironment.create(
            tmp_path.resolve() / "isolated", base_env=dict(os.environ)
        )
        repos = LocalGitRepositoryFactory(isolated.repository_root, env=isolated.subprocess_env())

        okf = repos.create("okf")
        plugins = LocalPackageFactory(okf.worktree / "plugins")
        plugin = plugins.create(_PLUGIN, targets=("codex",))
        skill = plugins.add_skill(plugin, _PLUGIN, _skill("Released"))
        release = repos.commit(okf, message="release 0.2.1")
        # An annotated tag: ls-remote lists the tag object, then its peeled commit.
        repos.tag(okf, _TAG, release, annotated=True)
        self.released = release.sha
        skill.write_text(_skill("Swapped"), encoding="utf-8")
        self.swapped = repos.commit(okf, message="other content").sha
        sha = {"released": self.released, "swapped": self.swapped}[catalog_sha]

        catalog = repos.create("atlas-marketplace")
        (catalog.worktree / ".claude-plugin").mkdir()
        (catalog.worktree / _CATALOG_PATH).write_text(
            json.dumps(
                {
                    "name": "atlas",
                    "owner": {"name": "APM Test"},
                    "plugins": [
                        {
                            "name": _PLUGIN,
                            "version": _VERSION,
                            "source": {
                                "source": "git-subdir",
                                "url": _PLUGIN_REMOTE,
                                "path": f"plugins/{_PLUGIN}",
                                "ref": _TAG,
                                "sha": sha,
                            },
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        repos.commit(catalog, message="publish catalog")

        # precedent -> atlas (git) -> okf@atlas, as in the report.
        atlas = self._package(repos, "atlas", {"name": _PLUGIN, "marketplace": _ALIAS})
        repos.tag(atlas, "v0.13.1", repos.commit(atlas, message="atlas"))
        precedent = self._package(repos, "precedent", {"git": _ATLAS_PACKAGE, "ref": "v0.13.1"})
        repos.commit(precedent, message="precedent")
        # The reporter's project already had dependencies, so an apm.lock.yaml.
        base = self._package(repos, "base", None)
        repos.commit(base, message="base")

        self.env = repos.url_rewrite_subprocess_env_many(
            (
                (okf, _PLUGIN_REMOTE),
                (catalog, _CATALOG_REMOTE),
                (atlas, f"https://github.com/{_ATLAS_PACKAGE}.git"),
                (precedent, f"https://github.com/{_PACKAGE}.git"),
                (base, f"https://github.com/{_BASE_PACKAGE}.git"),
            )
        )
        self.apm_home = isolated.config_root
        self.pins = isolated.config_root / "cache" / "marketplace" / "version-pins.json"
        self.project = (
            LocalPackageFactory(isolated.work_root).create("consumer", targets=("codex",)).root
        )
        self._runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=300)
        self.apm("marketplace", "add", _CATALOG_REMOTE, "--name", _ALIAS, "--ref", "main")
        self.apm("marketplace", "update", _ALIAS)
        self.apm("install", _BASE_PACKAGE, *_FLAGS)
        assert (self.project / "apm.lock.yaml").is_file()
        # The pin an older APM wrote: alias-keyed, holding the tag (#5526).
        self.pins.parent.mkdir(parents=True, exist_ok=True)
        self.pins.write_text(json.dumps(_LEGACY_PIN), encoding="utf-8")

    @staticmethod
    def _package(repos: LocalGitRepositoryFactory, name: str, dependency: dict[str, str] | None):
        repository = repos.create(name)
        manifest: dict[str, object] = {"name": name, "version": "1.0.0"}
        if dependency is not None:
            manifest["dependencies"] = {"apm": [dependency]}
        dump_yaml(manifest, repository.worktree / "apm.yml")
        skill = repository.worktree / ".apm" / "skills" / name / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text(f"---\nname: {name}\ndescription: d\n---\nBody\n", encoding="utf-8")
        return repository

    def apm(self, *args: str, expected: int = 0) -> CommandResult:
        result = self._runner.run(args, cwd=self.project, env=self.env)
        _assert_output_oracles(result)
        assert result.returncode == expected, _evidence(result)
        return result

    def pinned(self) -> dict[str, str]:
        return json.loads(self.pins.read_text(encoding="utf-8"))

    def deployed_skill(self) -> Path:
        return self.project / ".agents" / "skills" / _PLUGIN / "SKILL.md"

    def locked_commit(self) -> str:
        lock = load_yaml(self.project / "apm.lock.yaml")
        (row,) = [dep for dep in lock["dependencies"] if dep["repo_url"].endswith("team/okf")]
        return row["resolved_commit"]

    def snapshot(self) -> tuple[dict[str, bytes], dict[str, bytes]]:
        return _snapshot(self.project), _snapshot(self.apm_home)


def _text(result: CommandResult) -> str:
    return " ".join((result.stdout + result.stderr).split())


def test_a_tag_pin_and_the_sha_it_peels_to_install_and_migrate_the_pin(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """The user's shape: legacy tag pin, catalog ref+sha, annotated tag, transitive plugin."""
    world = _World(tmp_path, apm_binary_path, catalog_sha="released")

    installed = world.apm("install", _PACKAGE, *_FLAGS)

    text = _text(installed)
    assert "ref swap" not in text, _evidence(installed)
    assert text.count("same commit") == 1, _evidence(installed)
    assert (
        f"[i] Plugin {_PLUGIN}@{_ALIAS}: recorded ref '{_TAG}' is the same commit as "
        f"'{world.released}'; accepted, and the install records it as the pin." in text
    ), _evidence(installed)
    assert world.pinned() == {_PIN_KEY: world.released}
    assert world.locked_commit() == world.released
    assert "Released" in world.deployed_skill().read_text(encoding="utf-8")
    assert _PACKAGE in (world.project / "apm.yml").read_text(encoding="utf-8")

    # I2: the converged install changes no bytes and says nothing about the pin.
    before = world.snapshot()
    again = world.apm("install", *_FLAGS)
    assert "same commit" not in _text(again), _evidence(again)
    assert world.snapshot() == before, "I2: a converged install changed durable state"


def test_a_different_commit_is_refused_and_the_printed_install_accepts_it(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """Negative control: another commit still refuses; the named fix then works."""
    world = _World(tmp_path, apm_binary_path, catalog_sha="swapped")
    before = world.snapshot()

    refused = world.apm("install", _PACKAGE, *_FLAGS, expected=1)

    text = _text(refused)
    assert f"ref changed: was '{_TAG}', now '{world.swapped}'" in text, _evidence(refused)
    assert "refusing to install" in text, _evidence(refused)
    hint = "'apm install <package> --accept-ref-change' to accept it while adding the package"
    assert hint in text, _evidence(refused)
    assert world.snapshot() == before, "I7: a refused install changed durable state"

    # Execute the printed fix with <package> filled in.
    accepted = world.apm("install", _PACKAGE, "--accept-ref-change", *_FLAGS)

    assert (
        f"ref changed: was '{_TAG}', now '{world.swapped}'. Accepted by --accept-ref-change."
        in _text(accepted)
    ), _evidence(accepted)
    assert world.pinned() == {_PIN_KEY: world.swapped}
    assert world.locked_commit() == world.swapped
    assert "Swapped" in world.deployed_skill().read_text(encoding="utf-8")

    # The acceptance was for that attempt only: a plain install is converged and silent.
    converged = world.snapshot()
    world.apm("install", *_FLAGS)
    assert world.snapshot() == converged, "I2: a converged install changed durable state"
