"""Version identity, preservation, and fresh-evidence boundary regressions."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path
from urllib.parse import urlparse

import pytest
import yaml

from tests.spec_conformance import _manifest, gen_statement, orphan_check
from tests.spec_conformance._helpers import spec_text

pytestmark = pytest.mark.component

PRESERVATION_BASE = "f8df1b751efc30b32dc01b125616b51f777b4c81"
PRESERVED_DIGESTS = {
    "docs/src/content/docs/specs/openapm-v0.1.md": (
        "e1178aaa3f23eb1de76578dab63660d319f3695d593742a3ca3b226236524698"
    ),
    "docs/public/specs/manifests/openapm-v0.1.requirements.yml": (
        "034b9ede42be09ec53d4b8b6843003121a92bc7c3015341e8eb828fe9c9f5f2c"
    ),
    "docs/public/specs/schemas/lockfile-v0.1.schema.json": (
        "6c0dca9e7994035b55da17340b1f9f6c6673501c63ebd947400cf472d5723560"
    ),
    "docs/public/specs/schemas/manifest-v0.1.schema.json": (
        "7bdefbe443d3315d71add021c777d776c9cfd4942acb19750a799f46fa0d1344"
    ),
    "docs/public/specs/schemas/policy-v0.1.schema.json": (
        "577d7ef2aa1f84d280074f1713258afdd3f8bfb96c4b3b7e5b8ac58e0bb724bb"
    ),
    "docs/public/specs/schemas/requirements-v0.1.schema.json": (
        "5e294eef59498538efbe3b5f9ba54423f5d21dd07dc174d9588e6f15cb8b5d6f"
    ),
}


@pytest.mark.parametrize("relative,digest", PRESERVED_DIGESTS.items())
def test_previous_minor_bytes_are_preserved(relative: str, digest: str) -> None:
    """Pin the actual main baseline without rewriting its immutable contracts."""
    content = (_manifest.REPO_ROOT / relative).read_bytes()
    assert hashlib.sha256(content).hexdigest() == digest, PRESERVATION_BASE


def test_selected_identity_citation_and_exact_content_route_agree() -> None:
    selection = _manifest.selected_assessment()
    assert selection.version == _manifest.load_manifest_raw()["spec_version"]
    assert urlparse(selection.citation).path == f"/apm/spec/{selection.version}"
    assert selection.version in selection.coverage_path.name
    config = (_manifest.REPO_ROOT / "docs/astro.config.mjs").read_text()
    redirects = dict(re.findall(r"'(/spec[^']*)': '([^']+)'", config))
    assert urlparse(redirects[f"/spec/{selection.version}"]).path == (
        f"/apm/specs/openapm-{selection.version.replace('.', '')}/"
    )
    assert redirects["/spec/latest"] == redirects["/spec/v0.1"]
    assert redirects["/spec"] == redirects["/spec/v0.1"]


def test_every_exact_citation_resolves_to_its_own_revision() -> None:
    """Overwriting the minor-line file for a later patch must not strand an exact pin."""
    config = (_manifest.REPO_ROOT / "docs/astro.config.mjs").read_text()
    exact_routes = re.findall(r"'(/spec/v[0-9]+\.[0-9]+\.[0-9]+)': '([^']+)'", config)
    artifacts = {}
    for path in _manifest.SPEC_DIR.glob("openapm-*.md"):
        frontmatter = yaml.safe_load(path.read_text().split("---\n", 2)[1])
        if "slug" in frontmatter:
            artifacts[f"/apm/{frontmatter['slug']}/"] = frontmatter["title"]
    assert exact_routes
    for source, destination in exact_routes:
        version = urlparse(source).path.rsplit("/", 1)[-1]
        assert artifacts[urlparse(destination).path] == f"OpenAPM {version}"


@pytest.mark.parametrize(
    "old,new",
    [
        ("title: OpenAPM v0.2.0", "title: OpenAPM v0.1"),
        ("slug: specs/openapm-v020", "slug: specs/openapm-v02"),
    ],
)
def test_artifact_identity_mismatch_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, old: str, new: str
) -> None:
    artifact = tmp_path / "selected.md"
    artifact.write_text(_manifest.SPEC_PATH.read_text().replace(old, new), encoding="utf-8")
    monkeypatch.setattr(_manifest, "SPEC_PATH", artifact)
    with pytest.raises(ValueError, match=r"identity|exact-revision"):
        _manifest.selected_assessment()


@pytest.mark.parametrize("version", ["v0.1.39", "v0.2"])
def test_manifest_must_select_an_exact_revision_of_the_active_minor(
    monkeypatch: pytest.MonkeyPatch, version: str
) -> None:
    raw = {**_manifest.load_manifest_raw(), "spec_version": version}
    monkeypatch.setattr(_manifest, "load_manifest_raw", lambda: raw)
    with pytest.raises(ValueError, match=r"exact specification|minor artifact"):
        _manifest.selected_assessment()


@pytest.mark.parametrize("field", ["spec_version", "spec_sha256", "manifest_sha256"])
def test_mismatched_or_stale_coverage_is_rejected(tmp_path: Path, field: str) -> None:
    document = _manifest.coverage_document({})
    document[field] = "stale"
    path = tmp_path / "coverage.json"
    path.write_text(json.dumps(document))
    with pytest.raises(ValueError, match="fingerprints"):
        _manifest.load_coverage(path)


def test_unversioned_old_coverage_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "old-coverage.json"
    path.write_text(json.dumps({"req-mf-016": [{"test_nodeid": "old", "status": "active"}]}))
    with pytest.raises(ValueError, match="identity"):
        _manifest.load_coverage(path)


@pytest.mark.parametrize("exit_code,write_output", [(1, True), (2, False), (0, False)])
def test_failed_or_incomplete_collection_cannot_reuse_coverage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    exit_code: int,
    write_output: bool,
) -> None:
    prior = tmp_path / "old.json"
    prior.write_text(json.dumps(_manifest.coverage_document({})))
    monkeypatch.setenv(_manifest.COVERAGE_ENV, str(prior))

    def collect(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        env = kwargs["env"]
        assert isinstance(env, dict)
        output = Path(env[_manifest.COVERAGE_ENV])
        assert output != prior
        if write_output:
            output.write_text(json.dumps(_manifest.coverage_document({})))
        return subprocess.CompletedProcess(command, exit_code, "collection details", "failed")

    monkeypatch.setattr(_manifest.subprocess, "run", collect)
    with pytest.raises(RuntimeError, match=r"collection failed|no binding inventory"):
        _manifest.collect_coverage()
    assert prior.is_file()


def test_every_collection_is_fresh_and_ignores_ambient_test_selectors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PYTEST_ADDOPTS", "-k no_tests --lf")
    outputs: list[Path] = []

    def collect(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        env = kwargs["env"]
        assert isinstance(env, dict)
        assert "PYTEST_ADDOPTS" not in env
        assert command[3:5] == ["tests/spec_conformance", "--collect-only"]
        assert "addopts=" in command
        output = Path(env[_manifest.COVERAGE_ENV])
        assert not output.exists()
        outputs.append(output)
        output.write_text(json.dumps(_manifest.coverage_document({})))
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(_manifest.subprocess, "run", collect)
    assert _manifest.collect_coverage() == {}
    assert _manifest.collect_coverage() == {}
    assert len(set(outputs)) == 2
    assert all(not path.exists() for path in outputs)


def test_generator_refuses_collection_failure_without_overwriting_outputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    json_path, md_path = tmp_path / "report.json", tmp_path / "report.md"
    json_path.write_bytes(b"original-json")
    md_path.write_bytes(b"original-md")
    monkeypatch.setattr(gen_statement, "CONFORMANCE_JSON", json_path)
    monkeypatch.setattr(gen_statement, "CONFORMANCE_MD", md_path)

    def fail() -> _manifest.Coverage:
        raise RuntimeError("collection failed")

    monkeypatch.setattr(gen_statement, "collect_coverage", fail)
    assert gen_statement.main() == 2
    assert json_path.read_bytes() == b"original-json"
    assert md_path.read_bytes() == b"original-md"


def test_orphan_check_refuses_failed_collection(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail() -> _manifest.Coverage:
        raise RuntimeError("collection failed despite a stale map")

    monkeypatch.setattr(orphan_check, "collect_coverage", fail)
    assert orphan_check.main() == 2


def test_generator_requires_complete_current_bindings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gen_statement, "collect_coverage", lambda: {})
    with pytest.raises(ValueError, match="four-way bind"):
        gen_statement.build_json()


def test_reserved_features_are_not_activated_by_the_new_minor() -> None:
    text = spec_text().split("## Appendix D.", 1)[0]
    for phrase in (
        "**producer** MUST NOT\ndeclare a top-level `workspaces:` key",
        "MUST refuse the install",
        "Readers MUST accept bare 64-character",
        "one of `sha256`, `sha384`, or `sha512`",
        "The frozen-install operation is opt-in in\nthis revision",
        "broader wire contract remains reserved",
        "manifest remains informative in this revision",
        "cryptographic publisher provenance and does not activate",
    ):
        assert phrase in text
    assert not re.search(r"v0\.2 (?:will|introduces|normative)", text)
    assert "reserved-for-v02" not in text


def test_report_identity_and_links_follow_the_selected_assessment() -> None:
    document = gen_statement.build_json()
    selection = _manifest.selected_assessment()
    assert document["spec_version"] == selection.version
    assert document["inventory_kind"] == "static-test-bindings"
    assert document["assessment_status"] == "DRAFT"
    assert document["human_ratification"] == "UNSATISFIED"
    assert document["activation"] == "UNSATISFIED"
    assert urlparse(document["spec_citation"]) == urlparse(selection.citation)
    markdown = gen_statement.build_md(document)
    assert "not executed test results or a runtime pass certificate" in markdown
    assert "Human ratification and activation are UNSATISFIED" in markdown
    requirement_links = re.findall(r"\[req-[^\]]+\]\(([^)]+)\)", markdown)
    assert len(requirement_links) == len(document["requirements"])
    assert {urlparse(link).path for link in requirement_links} == {document["spec_path"]}


def test_corrective_revision_adds_only_the_approved_audit_requirement() -> None:
    old = (_manifest.SPEC_DIR / "openapm-v0.1.md").read_text()
    current = spec_text()
    pattern = r'<a id="(req-[a-z]{2,3}-[0-9]{3})"></a>'
    old_ids, new_ids = set(re.findall(pattern, old)), re.findall(pattern, current)
    assert len(old_ids) == 122
    assert "req-pl-018" in old_ids
    assert len(new_ids) == 123
    assert len(new_ids) == len(set(new_ids))
    assert set(new_ids) == old_ids | {"req-lk-023"}
    assert len(new_ids) == len(_manifest.load_requirements())
    assert re.search(
        rf"\*\*{len(new_ids)} normative statements \(118 MUST, 5 SHOULD\)\*\*", current
    )
    assert f"**Total normative statements: {len(new_ids)}**" in current


def test_corrective_revision_preserves_current_main_amendment_process() -> None:
    """The new assessment cannot restore a stale version of Section 9."""
    old = (_manifest.SPEC_DIR / "openapm-v0.1.md").read_text()
    heading = "## 9. Versioning and amendment process"
    following = "## 10. Security considerations"
    previous_process = old.split(heading, 1)[1].split(following, 1)[0]
    current_process = spec_text().split(heading, 1)[1].split(following, 1)[0]
    assert current_process == previous_process


def test_binding_inventory_discloses_unresolved_bare_audit_conformance_limit() -> None:
    """A new assessed version cannot turn a known implementation gap into a pass."""
    document = gen_statement.build_json()
    assert document["assessment_limitations"] == gen_statement.ASSESSMENT_LIMITATIONS
    markdown = gen_statement.build_md(document)
    assert "does not claim full Consumer conformance in bare audit mode" in markdown
    assert "controlled pre-existing standalone-skill" in markdown
