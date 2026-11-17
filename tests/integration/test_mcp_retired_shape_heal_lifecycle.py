"""APM-owned MCP entries written in a retired shape heal on upgrade (0.36.1 harness track).

Native configs and lockfiles here were produced by the tagged v0.35.1 (no
``content_hash`` fingerprint) and v0.36.0 (fingerprinted) releases; see
``tests/fixtures/upgrade_0361/t3/README.md``. Per approved decision A1 the next
``install``, ``update`` or ``install --frozen`` rewrites only APM-owned entries
into the current per-harness shape, exits 0, leaves ``audit --ci`` green and
converges (a second install writes nothing). A foreign same-name entry is
preserved and reported (nonzero). Policy ``mcp.transport.allow`` keeps judging
the declared transport only (decision H1): a heal must not newly block an
install, must not slip past a declared-transport deny, and must not change the
policy verdict. Policy is served by a loopback GitLab HTTPS fixture.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml

from tests.integration._harness_output_kit import HarnessScene, describe, warning_segments
from tests.utils.local_github_https_fixture import openssl_executable
from tests.utils.local_gitlab_policy_https_fixture import local_gitlab_policy_fixture

pytestmark = [pytest.mark.integration, pytest.mark.e2e, pytest.mark.requires_apm_binary]

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "upgrade_0361" / "t3"
_PLACEHOLDER = "__REGISTRY_URL__"
_REGISTRY_NAME = "io.github.apm-fixture/heal-registry"
_REGISTRY_DOCUMENT = {
    "name": _REGISTRY_NAME,
    "description": "upgrade heal fixture",
    "version": "1.0.0",
    "remotes": [{"type": "sse", "url": "https://registry-sse.example.invalid/sse"}],
}
_PROJECT_TARGETS = ("vscode", "copilot", "claude", "gemini", "antigravity", "hermes")
_USER_TARGETS = ("copilot", "claude", "gemini", "antigravity", "hermes")
_SSE = "https://sse.example.invalid/sse"
_REGISTRY_SSE = "https://registry-sse.example.invalid/sse"
_HTTP = "https://http.example.invalid/mcp"
_STREAMABLE = "https://streamable.example.invalid/mcp"
_ORIGIN = "https://gitlab.com/acme/service.git"
_POLICY_PROJECT = "acme/apm-policy"


@pytest.fixture
def scene(tmp_path: Path, apm_binary_path: Path) -> Iterator[HarnessScene]:
    result = HarnessScene.create(tmp_path / "scene", apm_binary_path, signal_dirs=())
    try:
        yield result
    finally:
        result.close()


def _seed(scene: HarnessScene, label: str, *, user: bool, tree: str | None = None) -> str:
    """Copy one tagged-release fixture tree into the scene and bind the registry."""
    registry = scene.start_registry(_REGISTRY_DOCUMENT)
    source = _FIXTURES / label / (tree or ("user-mcp" if user else "project-mcp"))
    roots = {"project": scene.project, "home": scene.home}
    for path in sorted(source.rglob("*")):
        if not path.is_file():
            continue
        head, *rest = path.relative_to(source).parts
        target = roots[head].joinpath(*rest)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            path.read_text(encoding="utf-8").replace(_PLACEHOLDER, registry), encoding="utf-8"
        )
    return registry


def _targets(user: bool) -> tuple[str, ...]:
    return _USER_TARGETS if user else _PROJECT_TARGETS


def _entry(scene: HarnessScene, harness: str, name: str, *, user: bool) -> dict:
    key = _REGISTRY_NAME if (harness == "vscode" and name == "heal-registry") else name
    return scene.servers(harness, user=user).get(key) or {}


def _assert_healed(scene: HarnessScene, *, user: bool, claims: bool = True) -> None:
    targets = _targets(user)
    for harness in ("copilot", "claude", "vscode"):
        if harness not in targets:
            continue
        for name, url in (("heal-sse", _SSE), ("heal-registry", _REGISTRY_SSE)):
            entry = _entry(scene, harness, name, user=user)
            assert (entry.get("type"), entry.get("url")) == ("sse", url), (harness, name, entry)
        for name in ("heal-http", "heal-streamable"):
            assert _entry(scene, harness, name, user=user).get("type") == "http", (harness, name)
    gemini_registry = _entry(scene, "gemini", "heal-registry", user=user)
    assert gemini_registry.get("url") == _REGISTRY_SSE and "httpUrl" not in gemini_registry
    assert _entry(scene, "gemini", "heal-http", user=user).get("httpUrl") == _HTTP
    for name, url in (("heal-http", _HTTP), ("heal-streamable", _STREAMABLE)):
        entry = _entry(scene, "antigravity", name, user=user)
        assert entry.get("serverUrl") == url and "httpUrl" not in entry, (name, entry)
    antigravity = scene.servers("antigravity", user=user)
    for name in ("heal-sse", _REGISTRY_NAME):
        if claims:
            assert "antigravity" not in scene.claimed_runtimes(name, user=user), name
    # Stale APM-owned SSE entries are removed once SSE is unsupported there.
    assert not {"heal-sse", "heal-registry"} & antigravity.keys(), antigravity
    # The Hermes row (transport: sse, cwd) lands with #3749, which restores
    # its assertions here.


def _snapshot(scene: HarnessScene, *, user: bool) -> dict[str, bytes | None]:
    native = scene.native_bytes(_targets(user), user=user)
    lock = scene.manifest_root(user) / "apm.lock.yaml"
    return {**native, "lock": lock.read_bytes() if lock.exists() else None}


def _lock_bytes(scene: HarnessScene, *, user: bool) -> bytes | None:
    path = scene.manifest_root(user) / "apm.lock.yaml"
    return path.read_bytes() if path.exists() else None


class _AuditAfterGlobalUpdate(Exception):
    """Known gap #3781: ``audit --ci`` after ``update -g`` misses apm_modules."""


def _audit(scene: HarnessScene, *, user: bool, extra: tuple[str, ...] = ("--no-policy",)):
    return scene.runner.run(
        ("audit", "--ci", *extra),
        cwd=scene.manifest_root(user),
        env=scene.env,
        scenario_id="audit",
    )


def _command(kind: str, *, user: bool) -> tuple[str, ...]:
    scope = ("--global",) if user else ()
    targets = ("--target", ",".join(_targets(user)))
    if kind == "update":
        return ("update", "--yes", *scope, *targets)
    frozen = ("--frozen",) if kind == "install-frozen" else ()
    return ("install", "--no-policy", *frozen, *scope, *targets)


@pytest.mark.parametrize("kind", ["install", "update", "install-frozen"])
@pytest.mark.parametrize("label", ["v0.35.1", "v0.36.0"])
@pytest.mark.parametrize("user", [False, True], ids=["project", "user"])
def test_upgrade_rewrites_apm_owned_entries_into_the_current_shape(
    scene: HarnessScene, label: str, kind: str, user: bool, request: pytest.FixtureRequest
) -> None:
    if user and kind == "update":
        request.applymarker(
            pytest.mark.xfail(strict=True, reason="#3781", raises=_AuditAfterGlobalUpdate)
        )
    _seed(scene, label, user=user)
    frozen = kind == "install-frozen"
    lock_before = _lock_bytes(scene, user=user)

    result = scene.run(*_command(kind, user=user))

    assert result.returncode == 0, describe(result)
    # Audit straight after the upgrade command (a later install re-materializes
    # apm_modules, see #3781); --frozen keeps the claim until the next install.
    audit = None if frozen else _audit(scene, user=user)
    _assert_healed(scene, user=user, claims=not frozen)
    warnings = warning_segments(result)
    assert any("antigravity" in seg.lower() and "heal-sse" in seg for seg in warnings), warnings
    if frozen:
        # --frozen never writes apm.lock.yaml (req-lk-006); the claim drops on the next install.
        assert _lock_bytes(scene, user=user) == lock_before, "--frozen rewrote apm.lock.yaml"
        scene.ok(*_command("install", user=user))
        _assert_healed(scene, user=user)
        audit = _audit(scene, user=user)
    before = _snapshot(scene, user=user)
    again = scene.run(*_command("install", user=user))
    assert again.returncode == 0, describe(again)
    assert _snapshot(scene, user=user) == before, "second install rewrote healed state"
    if user and kind == "update" and audit.returncode != 0:
        raise _AuditAfterGlobalUpdate(describe(audit))
    assert audit.returncode == 0, describe(audit)


def test_foreign_entry_in_a_retired_shape_is_never_taken_over(scene: HarnessScene) -> None:
    """A user-authored same-name entry equal to an old APM render stays foreign."""
    _seed(scene, "v0.36.0", user=False)
    foreign_url = "https://foreign.example.invalid/sse"
    manifest_path = scene.project / "apm.yml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    manifest["dependencies"]["mcp"].append(
        {"name": "heal-foreign", "registry": False, "transport": "sse", "url": foreign_url}
    )
    manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    for relative, key, entry in (
        (".github/mcp.json", "mcpServers", {"type": "http", "url": foreign_url, "tools": ["*"]}),
        (".mcp.json", "mcpServers", {"type": "http", "url": foreign_url}),
    ):
        path = scene.project / relative
        document = json.loads(path.read_text(encoding="utf-8"))
        document[key]["heal-foreign"] = entry
        path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    foreign = {
        "copilot": _entry(scene, "copilot", "heal-foreign", user=False),
        "claude": _entry(scene, "claude", "heal-foreign", user=False),
    }

    result = scene.run(*_command("install", user=False))

    assert result.returncode != 0, describe(result)
    assert "heal-foreign" in result.stdout + result.stderr
    for harness, entry in foreign.items():
        assert _entry(scene, harness, "heal-foreign", user=False) == entry, harness
        assert harness not in scene.claimed_runtimes("heal-foreign", user=False)
    assert _entry(scene, "copilot", "heal-sse", user=False).get("type") == "sse"


# --------------------------------------------------------------------------
# Cross-track: MCP heal x policy mcp.transport.allow (decision H1)
# --------------------------------------------------------------------------


def _policy(allow: list[str]) -> str:
    return yaml.safe_dump(
        {
            "name": "acme",
            "version": "1.0.0",
            "enforcement": "block",
            "mcp": {"self_defined": "allow", "transport": {"allow": allow}},
        },
        sort_keys=False,
    )


def _governed(scene: HarnessScene, *, drop: tuple[str, ...] = ()) -> None:
    _seed(scene, "v0.36.0", user=False)
    git_env = scene.isolated.subprocess_env()
    for args in (("init", "--quiet"), ("remote", "add", "origin", _ORIGIN)):
        subprocess.run(["git", *args], cwd=scene.project, env=git_env, check=True)
    if drop:
        path = scene.project / "apm.yml"
        manifest = yaml.safe_load(path.read_text(encoding="utf-8"))
        manifest["dependencies"]["mcp"] = [
            item for item in manifest["dependencies"]["mcp"] if item.get("name") not in drop
        ]
        path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")


def _governed_install(scene, fixture, *extra: str):
    env = fixture.child_environment(scene.env)
    args = ("install", *extra, "--target", ",".join(_PROJECT_TARGETS))
    return scene.runner.run(args, cwd=scene.project, env=env, scenario_id="governed-install")


def _transport_verdict(scene, fixture) -> bool:
    env = fixture.child_environment(scene.env)
    shutil.rmtree(scene.isolated.cache_root / "policy_v1", ignore_errors=True)
    result = scene.runner.run(
        ("audit", "--ci", "--no-fail-fast", "--no-drift", "-f", "json"),
        cwd=scene.project,
        env=env,
        scenario_id="governed-audit",
    )
    report = json.loads(result.stdout[result.stdout.index("{") :])
    checks = {check["name"]: check for check in report["checks"]}
    return bool(checks["mcp-transport"]["passed"])


_needs_openssl = pytest.mark.skipif(openssl_executable() is None, reason="openssl CLI missing")


@_needs_openssl
@pytest.mark.parametrize("frozen", [False, True], ids=["install", "install-frozen"])
def test_heal_under_a_permissive_transport_policy_is_not_blocked(
    scene: HarnessScene, frozen: bool
) -> None:
    _governed(scene)
    with local_gitlab_policy_fixture(scene.isolated.root / "policy-proxy") as fixture:
        fixture.publish({_POLICY_PROJECT: _policy(["stdio", "http", "sse", "streamable-http"])})
        verdict_before = _transport_verdict(scene, fixture)

        lock_before = _lock_bytes(scene, user=False)

        result = _governed_install(scene, fixture, *(("--frozen",) if frozen else ()))

        assert result.returncode == 0, describe(result)
        _assert_healed(scene, user=False, claims=not frozen)
        if frozen:
            assert _lock_bytes(scene, user=False) == lock_before, "--frozen rewrote the lockfile"
            follow_up = _governed_install(scene, fixture)
            assert follow_up.returncode == 0, describe(follow_up)
            _assert_healed(scene, user=False)
        assert _transport_verdict(scene, fixture) == verdict_before is True


@_needs_openssl
def test_heal_does_not_slip_past_a_declared_transport_deny(scene: HarnessScene) -> None:
    _governed(scene)
    before = _snapshot(scene, user=False)
    with local_gitlab_policy_fixture(scene.isolated.root / "policy-proxy") as fixture:
        fixture.publish({_POLICY_PROJECT: _policy(["stdio", "http", "streamable-http"])})

        result = _governed_install(scene, fixture)

    assert result.returncode != 0, describe(result)
    assert "heal-sse" in result.stdout + result.stderr
    assert _snapshot(scene, user=False) == before, "blocked install still rewrote native files"


@_needs_openssl
def test_registry_sse_heal_is_not_newly_blocked_by_effective_transport(
    scene: HarnessScene,
) -> None:
    """Only declared transports are judged in 0.36.1, so the healed SSE remote passes."""
    _governed(scene, drop=("heal-sse",))
    with local_gitlab_policy_fixture(scene.isolated.root / "policy-proxy") as fixture:
        fixture.publish({_POLICY_PROJECT: _policy(["stdio", "http", "streamable-http"])})
        verdict_before = _transport_verdict(scene, fixture)

        result = _governed_install(scene, fixture)

        assert result.returncode == 0, describe(result)
        copilot = _entry(scene, "copilot", "heal-registry", user=False)
        assert copilot.get("type") == "sse", copilot
        assert _transport_verdict(scene, fixture) == verdict_before is True


@_needs_openssl
@pytest.mark.parametrize("mode", ["warn", "block"])
def test_policy_fetch_failure_keeps_its_disposition_during_heal(
    scene: HarnessScene, mode: str
) -> None:
    _governed(scene)
    if mode == "block":
        path = scene.project / "apm.yml"
        manifest = yaml.safe_load(path.read_text(encoding="utf-8"))
        manifest["policy"] = {"fetch_failure_default": "block"}
        path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    before = _snapshot(scene, user=False)
    with local_gitlab_policy_fixture(scene.isolated.root / "policy-proxy") as fixture:
        fixture.publish({_POLICY_PROJECT: 500})

        result = _governed_install(scene, fixture)

    if mode == "block":
        assert result.returncode != 0, describe(result)
        assert _snapshot(scene, user=False) == before, "fetch-failure block still wrote"
    else:
        assert result.returncode == 0, describe(result)
        _assert_healed(scene, user=False)


@pytest.mark.parametrize("user", [False, True], ids=["project", "user"])
def test_healed_entries_are_cleaned_when_their_declaration_is_removed(
    scene: HarnessScene, user: bool
) -> None:
    _seed(scene, "v0.36.0", user=user)
    scene.ok(*_command("install", user=user))
    path = scene.manifest_root(user) / "apm.yml"
    manifest = yaml.safe_load(path.read_text(encoding="utf-8"))
    dropped = {"heal-sse", _REGISTRY_NAME}
    manifest["dependencies"]["mcp"] = [
        item for item in manifest["dependencies"]["mcp"] if item.get("name") not in dropped
    ]
    path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")

    result = scene.run(*_command("install", user=user))

    assert result.returncode == 0, describe(result)
    for harness in _targets(user):
        servers = scene.servers(harness, user=user)
        assert not {"heal-sse", "heal-registry", _REGISTRY_NAME} & servers.keys(), harness
        assert "heal-http" in servers, harness
    audit = _audit(scene, user=user)
    assert audit.returncode == 0, describe(audit)


@pytest.mark.parametrize("kind", ["install", "update", "install-frozen"])
@pytest.mark.parametrize("label", ["v0.35.1", "v0.36.0"])
def test_upgrade_removes_a_stale_codex_sse_entry(
    scene: HarnessScene, label: str, kind: str
) -> None:
    """v0.36.0 wrote registry SSE remotes into Codex; Codex cannot use SSE."""
    _seed(scene, label, user=False, tree="project-codex")
    lock_before = _lock_bytes(scene, user=False)
    command = {
        "install": ("install", "--no-policy", "--target", "codex"),
        "update": ("update", "--yes", "--target", "codex"),
        "install-frozen": ("install", "--no-policy", "--frozen", "--target", "codex"),
    }[kind]

    result = scene.run(*command)

    assert result.returncode == 0, describe(result)
    servers = scene.servers("codex", user=False)
    assert "heal-registry" not in servers, servers
    assert servers.get("heal-http", {}).get("url") == _HTTP, servers
    warnings = warning_segments(result)
    assert any("codex" in seg.lower() and "heal-registry" in seg for seg in warnings), warnings
    if kind == "install-frozen":
        # --frozen never writes apm.lock.yaml (req-lk-006); the claim drops on the next install.
        assert _lock_bytes(scene, user=False) == lock_before, "--frozen rewrote apm.lock.yaml"
        scene.ok("install", "--no-policy", "--target", "codex")
        assert "heal-registry" not in scene.servers("codex", user=False)
    assert "codex" not in scene.claimed_runtimes(_REGISTRY_NAME, user=False)
    audit = _audit(scene, user=False)
    assert audit.returncode == 0, describe(audit)
    before = scene.native_bytes(("codex",), user=False)
    scene.ok("install", "--no-policy", "--target", "codex")
    assert scene.native_bytes(("codex",), user=False) == before
