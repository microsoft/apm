"""Hermetic content twins for consumer journeys J04-J09 (#4110).

Shapes mirror the upstream repositories the docs name, with short original
bodies:

- ``vercel-labs/agent-skills``: a skill bundle with no ``apm.yml`` and no
  semver tags (J04);
- ``anthropics/claude-code``: the ``claude-code-plugins`` marketplace, whose
  entries are ``git-subdir`` sources into ``anthropics/claude-plugins-official``
  (J05); one extra entry declares a component path that does not exist;
- ``acme/release-kit``: a package with annotated tags ``v1.0.0`` and
  ``v1.1.0`` on fixed commit dates, so its commit SHAs are stable (J07);
- ``acme/live-kit``: a package tracked on ``#main`` (J07);
- ``io.github.github/github-mcp-server`` in the world MCP registry (J06).

``publish_consumer_a_twins`` mirrors ``twins.publish_twins``.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import quote, urlsplit

from tests.lifecycle.journeys.twins import RELEASE_TAG, Twin, _author, _md
from tests.lifecycle.world.services import REGISTRY_HOST, ScriptedResponse
from tests.utils.local_git_repository import LocalGitRepositoryFactory

# -- J04: vercel-labs/agent-skills ------------------------------------------

_VERCEL_SKILLS = (
    "deploy-to-vercel",
    "preview",
    "react-best-practices",
    "web-design-guidelines",
    "composition-patterns",
)
AGENT_SKILLS = Twin(
    "vercel-labs/agent-skills",
    "main",
    {
        "README.md": "# Agent Skills (lifecycle twin)\n",
        **{
            f"skills/{name}/SKILL.md": _md(
                f"name: {name}\ndescription: Lifecycle twin of the {name} skill.\n",
                f"# {name}\n\nFollow these steps.",
            )
            for name in _VERCEL_SKILLS
        },
    },
)

# -- J05: anthropics/claude-code marketplace -> claude-plugins-official ------

MARKETPLACE_TAG = "v2.1.0"
PLUGINS_REPO = "anthropics/claude-plugins-official"
BAD_PATH_PLUGIN = "broken-paths"
_PLUGINS = (
    "agent-sdk-dev",
    "code-review",
    "commit-commands",
    "explanatory-output-style",
    "feature-dev",
    "frontend-design",
    "hookify",
    "learning-output-style",
    "plugin-dev",
    "pr-review-toolkit",
    "ralph-loop",
    "security-guidance",
)
_COMMIT_COMMANDS = ("commit", "commit-push-pr", "clean_gone")


def _plugin_entry(name: str) -> dict[str, object]:
    return {
        "name": name,
        "description": f"Lifecycle twin of the {name} plugin",
        "source": {
            "source": "git-subdir",
            "url": f"https://github.com/{PLUGINS_REPO}.git",
            "path": f"plugins/{name}",
            "ref": "main",
        },
        "category": "productivity",
    }


def _plugin_json(name: str, **extra: object) -> str:
    document = {"name": name, "description": f"Lifecycle twin of {name}", "version": "1.0.0"}
    document.update(extra)
    return json.dumps(document, indent=2) + "\n"


_MARKETPLACE = {
    "name": "claude-code-plugins",
    "version": "1.0.0",
    "description": "Lifecycle twin of the Claude Code plugins marketplace",
    "owner": {"name": "Anthropic"},
    "plugins": [_plugin_entry(name) for name in (*_PLUGINS, BAD_PATH_PLUGIN)],
}
CLAUDE_CODE = Twin(
    "anthropics/claude-code",
    MARKETPLACE_TAG,
    {
        "README.md": "# claude-code (lifecycle twin)\n",
        ".claude-plugin/marketplace.json": json.dumps(_MARKETPLACE, indent=2) + "\n",
    },
)


def _official_files() -> dict[str, str]:
    files = {"README.md": "# claude-plugins-official (lifecycle twin)\n"}
    for name in _PLUGINS:
        files[f"plugins/{name}/.claude-plugin/plugin.json"] = _plugin_json(name)
        files[f"plugins/{name}/README.md"] = f"# {name}\n"
        if name != "commit-commands":
            files[f"plugins/{name}/commands/{name}.md"] = _md(
                f"description: Lifecycle twin command of {name}\n", f"# {name}\n\nRun it."
            )
    for command in _COMMIT_COMMANDS:
        files[f"plugins/commit-commands/commands/{command}.md"] = _md(
            f"allowed-tools: Bash(git status:*)\ndescription: Lifecycle twin {command}\n",
            f"## {command}\n\nDo the {command} step.",
        )
    files[f"plugins/{BAD_PATH_PLUGIN}/.claude-plugin/plugin.json"] = _plugin_json(
        BAD_PATH_PLUGIN, commands="./missing-commands/"
    )
    return files


CLAUDE_PLUGINS_OFFICIAL = Twin(PLUGINS_REPO, "main", _official_files())

# -- J07: versioned and mutable packages ------------------------------------


def _kit(name: str, version: str, skills: tuple[str, ...]) -> dict[str, str]:
    files = {
        "apm.yml": f"name: {name}\nversion: {version}\ndescription: Lifecycle twin {name}.\n",
        "README.md": f"# {name} {version}\n",
    }
    for skill in skills:
        files[f".apm/skills/{skill}/SKILL.md"] = _md(
            f"name: {skill}\ndescription: {name} {skill} skill.\n", f"# {skill}\n\n{version}"
        )
    return files


# v1.1.0 changes ``review``, adds ``triage`` and drops ``legacy``.
RELEASE_KIT_VERSIONS = (
    ("v1.0.0", _kit("release-kit", "1.0.0", ("review", "legacy"))),
    ("v1.1.0", _kit("release-kit", "1.1.0", ("review", "triage"))),
)
RELEASE_KIT = Twin("acme/release-kit", "v1.1.0", dict(RELEASE_KIT_VERSIONS[-1][1]))
# live-kit also ships an instruction, so ``apm compile`` has input (J09).
LIVE_KIT = Twin(
    "acme/live-kit",
    "main",
    {
        **_kit("live-kit", "0.1.0", ("live",)),
        ".apm/instructions/live.instructions.md": _md(
            'description: Live kit house rules\napplyTo: "**"\n',
            "# Live kit\n\nPrefer small, reviewed changes.",
        ),
    },
)

# Release metadata mirror for ``apm self-update --check`` (APM_RELEASE_METADATA_URL).
RELEASE_MIRROR = Twin(
    "acme/apm-release-mirror",
    "main",
    {"latest.json": json.dumps({"tag_name": RELEASE_TAG}) + "\n"},
)
RELEASE_METADATA_URL = "https://raw.githubusercontent.com/acme/apm-release-mirror/main/latest.json"

TWINS_CONSUMER_A: Mapping[str, Twin] = {
    twin.slug: twin
    for twin in (
        AGENT_SKILLS,
        CLAUDE_CODE,
        CLAUDE_PLUGINS_OFFICIAL,
        RELEASE_KIT,
        LIVE_KIT,
        RELEASE_MIRROR,
    )
}

# Fixed identity and dates make every twin commit SHA stable across runs.
_FIXED_GIT = {
    "GIT_AUTHOR_NAME": "APM Lifecycle",
    "GIT_AUTHOR_EMAIL": "lifecycle@example.invalid",
    "GIT_COMMITTER_NAME": "APM Lifecycle",
    "GIT_COMMITTER_EMAIL": "lifecycle@example.invalid",
}
_DATES = ("2026-01-01T00:00:00Z", "2026-02-01T00:00:00Z")

# -- J06: registry server ----------------------------------------------------

GITHUB_MCP_SERVER = "io.github.github/github-mcp-server"
GITHUB_MCP_DOCUMENT = {
    "name": GITHUB_MCP_SERVER,
    "description": "Lifecycle twin of the GitHub MCP server",
    "version": "1.0.0",
    "remotes": [{"transport_type": "streamable-http", "url": "https://api.githubcopilot.com/mcp/"}],
}


def _registry_with(server: Mapping[str, object], fallback):
    name = str(server["name"])
    detail = {
        f"/v0.1/servers/{quote(name, safe='')}/versions/latest",
        f"/v0.1/servers/{name}/versions/latest",
    }

    def handler(request):
        path = urlsplit(request.target).path
        if path in detail:
            body = json.dumps({"server": server}, sort_keys=True).encode("utf-8")
            return ScriptedResponse(200, {"Content-Type": "application/json"}, body)
        response = fallback(request)
        if path == "/v0.1/servers" and response.status == 200:
            listing = json.loads(response.body)
            summary = {"name": name, "description": server["description"]}
            if all(s["server"]["name"] != name for s in listing["servers"]):
                listing["servers"].append({"server": summary})
            body = json.dumps(listing, sort_keys=True).encode("utf-8")
            return ScriptedResponse(200, {"Content-Type": "application/json"}, body)
        return response

    handler.consumer_a = True
    return handler


def _publish_registry(services) -> None:
    # Core gap: WorldServices has no public registry publish(); wrap the handler once.
    handlers = services.proxy._server.handlers
    current = handlers[REGISTRY_HOST]
    if not getattr(current, "consumer_a", False):
        handlers[REGISTRY_HOST] = _registry_with(GITHUB_MCP_DOCUMENT, current)


def publish_consumer_a_twins(services, root: Path, env: Mapping[str, str]) -> dict[str, Path]:
    """Publish every consumer-a twin under *root* and register it with *services*."""
    git_env = {**env, **_FIXED_GIT}
    factory = LocalGitRepositoryFactory(root / "repositories", env=git_env)
    published: dict[str, Path] = {}
    for slug, twin in TWINS_CONSUMER_A.items():
        repository = factory.create(slug.replace("/", "__"))
        if twin is RELEASE_KIT:
            for (tag, files), date in zip(RELEASE_KIT_VERSIONS, _DATES, strict=True):
                shutil.rmtree(repository.worktree / ".apm", ignore_errors=True)
                _author(Twin(slug, tag, files), repository.worktree)
                factory._env.update(GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date)
                commit = factory.commit(repository, message=f"{slug} {tag}")
                factory.tag(repository, tag, commit, annotated=True)
        else:
            _author(twin, repository.worktree)
            factory._env.update(GIT_AUTHOR_DATE=_DATES[0], GIT_COMMITTER_DATE=_DATES[0])
            commit = factory.commit(repository, message=f"{slug} {twin.ref}")
            if twin.ref != "main":
                factory.tag(repository, twin.ref, commit)
        published[slug] = repository.origin
    # Core gap: WorldServices has no public publish(); see the T-8a report.
    services._repositories.update(published)
    _publish_registry(services)
    return published
