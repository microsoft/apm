"""Keep first-party license declarations and distributed notices consistent."""

import ast
import hashlib
from pathlib import Path

import pytest
import toml
import yaml

pytestmark = pytest.mark.component

ROOT = Path(__file__).resolve().parents[2]
MANIFESTS = sorted(
    [
        ROOT / "apm.yml",
        *(ROOT / "packages").rglob("apm.yml"),
        *(ROOT / ".agents" / "skills").rglob("apm.yml"),
    ]
)


def test_python_distribution_declares_apache_license() -> None:
    """Python metadata must identify the same license as the root file."""
    project = toml.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project["license"] == {"file": "LICENSE"}
    license_classifiers = [
        value for value in project["classifiers"] if value.startswith("License ::")
    ]
    assert license_classifiers == ["License :: OSI Approved :: Apache Software License"]
    license_text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert "Version 2.0, January 2004" in license_text
    assert "3. Grant of Patent License." in license_text
    assert "END OF TERMS AND CONDITIONS" in license_text


def test_documentation_license_matches_official_legal_text() -> None:
    """Preserve the official legal text across LF and CRLF checkouts."""
    legal_text = (ROOT / "LICENSE-DOCS").read_text(encoding="utf-8")
    assert hashlib.sha256(legal_text.encode("utf-8")).hexdigest() == (
        "9ba9550ad48438d0836ddab3da480b3b69ffa0aac7b7878b5a0039e7ab429411"
    )


def test_python_distribution_includes_licenses_scope_and_notice() -> None:
    """Wheel and sdist configuration must carry the documentation scope too."""
    config = toml.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert set(config["tool"]["setuptools"]["license-files"]) == {
        "LICENSE",
        "LICENSE-DOCS",
        "LICENSING.md",
        "NOTICE",
    }


def test_documentation_scope_excludes_software_and_specification() -> None:
    """Markdown syntax must not relicense executable primitives or OpenAPM."""
    scope = (ROOT / "LICENSING.md").read_text(encoding="utf-8")
    assert "## General documentation: CC-BY-4.0" in scope
    assert "Root Markdown documents" in scope
    assert "docs/src/content/docs/" in scope
    assert "Code and configuration examples" in scope
    assert "Markdown used as an agent\n  primitive is software" in scope
    assert "docs/src/content/docs/specs/openapm-*.md" in scope
    assert "excluded from this\ndocumentation grant" in scope


@pytest.mark.parametrize(
    "filename",
    [
        "CONTRIBUTING.md",
        "NOTICE",
        "docs/src/content/docs/index.mdx",
        "docs/src/content/docs/enterprise/security.md",
    ],
)
def test_documentation_license_is_discoverable(filename: str) -> None:
    """Contributor and redistributor entry points link the scoped doc license."""
    content = (ROOT / filename).read_text(encoding="utf-8")
    assert "CC-BY-4.0" in content
    assert "LICENSE-DOCS" in content
    assert "LICENSING.md" in content


def test_docs_footer_exposes_both_license_scopes() -> None:
    """The site-wide footer must not imply that OpenAPM is CC-BY licensed."""
    config = (ROOT / "docs" / "astro.config.mjs").read_text(encoding="utf-8")
    footer = (ROOT / "docs" / "src" / "components" / "Footer.astro").read_text(encoding="utf-8")
    assert "Footer: './src/components/Footer.astro'" in config
    assert "<DefaultFooter />" in footer
    assert "General documentation:" in footer
    assert "CC-BY-4.0" in footer
    assert "Software, code examples and OpenAPM:" in footer
    assert "Apache-2.0" in footer


def test_filelock_notice_matches_reviewed_release() -> None:
    """Catch the stale Unlicense/public-domain override for filelock 3.29.0."""
    metadata = yaml.safe_load(
        (ROOT / "scripts" / "notice-metadata.yaml").read_text(encoding="utf-8")
    )
    component = next(item for item in metadata["components"] if item["name"] == "filelock")
    assert component["spdx"] == "MIT"
    assert component["copyright_snippet"] == (
        "Copyright (c) 2025 Bern\u00e1t G\u00e1bor and contributors"
    )
    assert "locked 3.29.0 release" in component["notes"]
    notice = (ROOT / "NOTICE").read_text(encoding="utf-8")
    section = notice.split("## Component. filelock\n", 1)[1].split("\n## Component.", 1)[0]
    assert "Unlicense" not in section
    assert component["copyright_snippet"] in section
    assert "MIT License" in section


def test_notice_discloses_direct_dependency_coverage_limit() -> None:
    """A drift-free NOTICE must not imply automatic coverage of nested deps."""
    notice = (ROOT / "NOTICE").read_text(encoding="utf-8")
    assert "direct Python runtime\ndependencies" in notice
    assert "not a complete inventory" in notice
    assert "setuptools is also a transitive runtime dependency through llm" in notice


@pytest.mark.parametrize("manifest", MANIFESTS, ids=lambda path: path.relative_to(ROOT).as_posix())
def test_first_party_manifest_declares_apache_license(manifest: Path) -> None:
    """Published first-party packages and installed skill copies agree."""
    data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    assert data["license"] == "Apache-2.0"


def test_notice_preserves_historical_mit_license() -> None:
    """Keep the original MIT copyright, permission and disclaimer verbatim."""
    notice = (ROOT / "NOTICE").read_text(encoding="utf-8")
    metadata = yaml.safe_load(
        (ROOT / "scripts" / "notice-metadata.yaml").read_text(encoding="utf-8")
    )
    preamble = metadata["_header"]["preamble"].rstrip("\n")
    assert preamble in notice
    historical = preamble.split("\nMIT License\n", 1)[1]
    original = "MIT License\n" + historical + "\n"
    assert hashlib.sha256(original.encode()).hexdigest() == (
        "7df20dcdf9197e9945c14858d41c60f11b52b93e5b69e2b63416b874d598d322"
    )
    assert "Copyright (c) Microsoft Corporation." in preamble
    assert "Apache License, Version 2.0" in preamble


def test_standalone_build_includes_license_and_notice() -> None:
    """Release binaries must carry the same legal files as Python distributions."""
    spec = ast.parse((ROOT / "build" / "apm.spec").read_text(encoding="utf-8"))
    datas = next(
        node.value
        for node in spec.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "datas" for target in node.targets)
    )
    assert isinstance(datas, ast.List)
    entries = {ast.dump(item) for item in datas.elts}
    for filename in ("LICENSE", "LICENSE-DOCS", "LICENSING.md", "NOTICE"):
        expected = ast.parse(f"(str(repo_root / {filename!r}), '.')", mode="eval").body
        assert ast.dump(expected) in entries, filename
