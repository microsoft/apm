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
    for filename in ("LICENSE", "NOTICE"):
        expected = ast.parse(f"(str(repo_root / {filename!r}), '.')", mode="eval").body
        assert ast.dump(expected) in entries, filename
