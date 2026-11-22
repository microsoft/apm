"""Transport parity: a cold install lands the same bytes via archive or git.

PROMISE: P-OWN-package-content-hash -- the lock's ``content_hash`` of a git
package is the same whichever transport fetched it, so a lock written after an
archive fetch replays through git and vice versa (source:
src/apm_cli/deps/archive_transport.py, src/apm_cli/utils/content_hash.py).
PROMISE: P-INV-I7 -- an archive that does not hash to the commit's git tree is
never deployed or cached: the install falls back to git and succeeds with the
locked bytes (source: src/apm_cli/deps/archive_checkout.py).

Cell: OP=install SRC=git(github.com, anonymous) LOCK=present DEPLOYED=present,
cache cold, transport in {archive, git (APM_ARCHIVE_TRANSPORT=0), tampered
archive}. Each step runs through ``Scenario`` (I1-I9 per step) and pins the
exact changed set over project, HOME and the cache root.
"""

from __future__ import annotations

import io
import shutil
import tarfile

import pytest

from tests.lifecycle.scenarios._kit import Scenario, assert_changes
from tests.lifecycle.world import use_policy
from tests.lifecycle.world.services import CODELOAD_HOST

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.integration,
    pytest.mark.lifecycle_smoke,
    pytest.mark.promise("P-OWN-package-content-hash", "P-INV-I1", "P-INV-I7"),
]

SPEC = "lifecycle/p-skill#v1.0.0"
DEPLOYED_SKILL = ".agents/skills/p-skill/SKILL.md"
COLD_CHANGES = ("project:apm_modules*", "cache:git*")
SECRET = "l7-archive-pat-sentinel"
NETRC_SECRET = "l7-archive-netrc-sentinel"
TAMPERED = b"---\nname: p-skill\ndescription: Tampered archive.\n---\n\nNot the commit.\n"


def _go_cold(scenario: Scenario) -> None:
    shutil.rmtree(scenario.world.project / "apm_modules")
    shutil.rmtree(scenario.world.cache_root / "git")
    scenario.source_moved()


def _lock_row(scenario: Scenario) -> tuple[str | None, str | None, dict]:
    dep = scenario.lock().get_package_dependencies()[0]
    return dep.resolved_commit, dep.content_hash, dict(dep.deployed_file_hashes)


def _package_requests(scenario: Scenario, start: int) -> tuple[list[dict], list[dict]]:
    """(archive requests, git pack requests) for p-skill from egress entry *start* on."""
    log = scenario.world.services.egress_log()[start:]
    archives = [e for e in log if e["host"] == CODELOAD_HOST and "/p-skill/" in e["path"]]
    packs = [e for e in log if e["method"] == "POST" and "p-skill" in e["path"]]
    return archives, packs


def _assert_archive_path_sent_no_credential(scenario: Scenario) -> None:
    """Codeload and git/trees requests carry no Authorization and no secret."""
    proxy = scenario.world.services.proxy
    archive_path = [
        *proxy.requests_to(CODELOAD_HOST),
        *(r for r in proxy.requests_to("api.github.com") if "/git/trees/" in r.path),
    ]
    assert {r.host for r in archive_path} == {CODELOAD_HOST, "api.github.com"}, archive_path
    for request in archive_path:
        names = {name.lower() for name, _value in request.headers}
        assert "authorization" not in names, request.target
        values = " ".join(request.header_values())
        assert SECRET not in values and NETRC_SECRET not in values, request.target


def _tampered_archive(scenario: Scenario, commit: str) -> bytes:
    """The genuine tree of *commit* with SKILL.md replaced, named as that commit."""
    shard = next((scenario.world.cache_root / "git" / "checkouts_v1").glob(f"*/{commit}/full"))
    buffer = io.BytesIO()
    with tarfile.open(
        fileobj=buffer, mode="w:gz", format=tarfile.PAX_FORMAT, pax_headers={"comment": commit}
    ) as tar:
        for path in sorted(shard.rglob("*")):
            relative = path.relative_to(shard).as_posix()
            if not path.is_file() or relative.split("/")[0] == ".git":
                continue
            data = TAMPERED if relative.endswith("SKILL.md") else path.read_bytes()
            info = tarfile.TarInfo(f"lifecycle-p-skill-{commit[:7]}/{relative}")
            info.size = len(data)
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def test_archive_git_and_tampered_archive_land_identical_lock_rows(lifecycle_world) -> None:
    use_policy(lifecycle_world, "allow")
    scenario = Scenario(
        lifecycle_world,
        {"p-skill": SPEC},
        policy="allow",
        extra_roots={"cache": lifecycle_world.cache_root},
    )

    # Archive transport (default): one codeload request, no git pack, and no
    # credential on it even with a PAT and a netrc entry for both hosts.
    (lifecycle_world.home / ".netrc").write_text(
        "".join(
            f"machine {host} login apm password {NETRC_SECRET}\n"
            for host in (CODELOAD_HOST, "api.github.com")
        )
    )
    lifecycle_world.env["GITHUB_APM_PAT"] = SECRET
    try:
        scenario.run("install p-skill")
    finally:
        lifecycle_world.env.pop("GITHUB_APM_PAT")
        (lifecycle_world.home / ".netrc").unlink()
    _assert_archive_path_sent_no_credential(scenario)
    archives, packs = _package_requests(scenario, 0)
    assert [e["status"] for e in archives] == [200], archives
    assert packs == []
    commit, content_hash, deployed = _lock_row(scenario)
    genuine = (lifecycle_world.project / DEPLOYED_SKILL).read_bytes()

    # Warm restore from the archive-landed shard: no content request at all.
    shutil.rmtree(lifecycle_world.project / "apm_modules")
    scenario.source_moved()
    start = len(lifecycle_world.services.egress_log())
    step = scenario.run("install")
    assert_changes(step.changed, ("project:apm_modules*",), label="warm restore")
    archives, packs = _package_requests(scenario, start)
    trees = [
        e
        for e in lifecycle_world.services.egress_log()[start:]
        if "/p-skill/git/trees/" in e["path"]
    ]
    assert (archives, packs, trees) == ([], [], []), "a warm cache reaches the network"
    assert _lock_row(scenario) == (commit, content_hash, deployed)

    # Git transport, cold cache: the same lock row, byte for byte.
    _go_cold(scenario)
    start = len(lifecycle_world.services.egress_log())
    lifecycle_world.env["APM_ARCHIVE_TRANSPORT"] = "0"
    try:
        step = scenario.run("install")
    finally:
        lifecycle_world.env.pop("APM_ARCHIVE_TRANSPORT")
    assert_changes(step.changed, COLD_CHANGES, label="git transport")
    archives, packs = _package_requests(scenario, start)
    assert archives == []
    assert packs, "APM_ARCHIVE_TRANSPORT=0 fetches through git"
    assert _lock_row(scenario) == (commit, content_hash, deployed)

    # Tampered archive: rejected before anything lands; git serves the commit.
    tampered = _tampered_archive(scenario, commit)
    _go_cold(scenario)
    start = len(lifecycle_world.services.egress_log())
    lifecycle_world.services.script(
        CODELOAD_HOST,
        "/lifecycle/p-skill/tar.gz/",
        200,
        {"Content-Type": "application/x-gzip"},
        tampered,
    )
    step = scenario.run("install")
    assert_changes(step.changed, COLD_CHANGES, label="tampered archive")
    archives, packs = _package_requests(scenario, start)
    assert [e["status"] for e in archives] == [200], archives
    assert packs, "a rejected archive falls back to git"
    assert _lock_row(scenario) == (commit, content_hash, deployed)
    assert (lifecycle_world.project / DEPLOYED_SKILL).read_bytes() == genuine
    cached = list((lifecycle_world.cache_root / "git").rglob("SKILL.md"))
    assert cached and all(path.read_bytes() != TAMPERED for path in cached)
    scenario.finish()
