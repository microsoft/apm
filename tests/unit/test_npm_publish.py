import hashlib
import json
import os
import stat
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from scripts.npm_publish import (
    PLATFORMS,
    copy_common_files,
    generate_main_package,
    generate_platform_package,
    get_archive_name,
    get_repo_root,
    get_version,
    main,
    setup_jinja_env,
    verify_binary_hash,
)

# Realistic test version to prove dynamic injection is working
TEST_VERSION = "0.99.0"

@pytest.fixture
def repo_root():
    """Point directly to the actual project root to use the real templates."""
    return get_repo_root()

@pytest.fixture
def jinja_env(repo_root):
    """Initialize the Jinja environment using the project's actual templates."""
    return setup_jinja_env(repo_root)

# ====================================================================
# 1. Security & Hash Verification Tests
# ====================================================================

def test_verify_binary_hash_success(tmp_path):
    """Prove integrity passes if the hash matches the binary content."""
    archive_name = "apm-linux-x86_64"
    binary_dir = tmp_path / archive_name
    binary_dir.mkdir()
    
    binary_path = binary_dir / "apm"
    content = b"fake_executable_content"
    binary_path.write_bytes(content)

    expected_hash = hashlib.sha256(content).hexdigest()
    checksum_path = tmp_path / f"{archive_name}.sha256"
    checksum_path.write_text(f"{expected_hash}  apm\n")

    verify_binary_hash(binary_dir, archive_name, "linux")


def test_verify_binary_hash_rejects_tampered_binary(tmp_path):
    """Prove that a tampered binary raises a security ValueError."""
    archive_name = "apm-windows-x86_64"
    binary_dir = tmp_path / archive_name
    binary_dir.mkdir()
    
    binary_path = binary_dir / "apm.exe"
    binary_path.write_bytes(b"malicious_content")

    checksum_path = tmp_path / f"{archive_name}.sha256"
    checksum_path.write_text("abcd1234fakehash  apm.exe\n")

    with pytest.raises(ValueError, match="SECURITY ALERT: Hash mismatch"):
        verify_binary_hash(binary_dir, archive_name, "win32")


def test_verify_binary_hash_missing_files(tmp_path):
    """Prove that missing binary or checksum files block the process."""
    archive_name = "apm-darwin-arm64"
    binary_dir = tmp_path / archive_name
    binary_dir.mkdir()

    binary_path = binary_dir / "apm"
    binary_path.write_bytes(b"content")
    
    with pytest.raises(FileNotFoundError, match="Checksum file not found"):
         verify_binary_hash(binary_dir, archive_name, "darwin")

# ====================================================================
# 2. Utility Functions Tests
# ====================================================================

@pytest.mark.parametrize(
    "npm_platform, npm_arch, expected",
    [
        ("linux", "x64", "apm-linux-x86_64"),
        ("linux", "arm64", "apm-linux-arm64"),
        ("darwin", "x64", "apm-darwin-x86_64"),
        ("darwin", "arm64", "apm-darwin-arm64"),
        ("win32", "x64", "apm-windows-x86_64"),
    ]
)
def test_get_archive_name_mapping(npm_platform, npm_arch, expected):
    """Validate npm platform/arch mapping to APM release archive names."""
    assert get_archive_name(npm_platform, npm_arch) == expected


def test_get_version_extracts_from_pyproject_toml(tmp_path):
    """Validate get_version reads pyproject.toml and extracts the version."""
    manifest = tmp_path / "pyproject.toml"
    manifest.write_text(
        '[project]\n'
        'name = "apm-cli"\n'
        'version = "0.28.5"\n'
    )
    assert get_version(tmp_path) == "0.28.5"


def test_copy_common_files(tmp_path):
    """Validate documentation and license files are copied safely."""
    repo_root = tmp_path / "repo"
    dest_dir = tmp_path / "dest"
    repo_root.mkdir()
    dest_dir.mkdir()

    # Test missing files gracefully skipped
    copy_common_files(repo_root, dest_dir)
    assert not (dest_dir / "README.md").exists()

    # Test copy success
    (repo_root / "README.md").write_text("readme")
    copy_common_files(repo_root, dest_dir)
    assert (dest_dir / "README.md").read_text() == "readme"

# ====================================================================
# 3. Package Generators Tests (Dynamic File Creation)
# ====================================================================

@pytest.mark.parametrize("npm_platform, npm_arch", PLATFORMS)
def test_generate_platform_package_dynamic(
    npm_platform, npm_arch, jinja_env, tmp_path, monkeypatch
):
    """Validate the platform package generator produces valid structural outputs."""
    monkeypatch.setattr("scripts.npm_publish.verify_binary_hash", lambda *args, **kwargs: None)
    monkeypatch.setattr("shutil.copytree", lambda *args, **kwargs: None)

    npm_dist_root = tmp_path / "npm"
    
    archive_name = get_archive_name(npm_platform, npm_arch)
    
    archive_dir = tmp_path / "dist" / archive_name
    archive_dir.mkdir(parents=True, exist_ok=True)

    generate_platform_package(
        env=jinja_env,
        repo_root=tmp_path,  # <-- Pass tmp_path to maintain isolation
        npm_dist_root=npm_dist_root,
        npm_platform=npm_platform,
        npm_arch=npm_arch,
        version=TEST_VERSION,
    )

    pkg_json_path = npm_dist_root / f"apm-cli-{npm_platform}-{npm_arch}" / "package.json"
    assert pkg_json_path.exists()

    data = json.loads(pkg_json_path.read_text(encoding="utf-8"))
    assert data["name"] == f"@microsoft/apm-cli-{npm_platform}-{npm_arch}"
    assert data["version"] == TEST_VERSION
    assert data["os"] == [npm_platform]
    assert data["cpu"] == [npm_arch]


def test_generate_main_package_dynamic(repo_root, jinja_env, tmp_path):
    """Validate the wrapper package generator correctly produces the node launcher and package.json."""
    npm_dist_root = tmp_path / "npm"

    generate_main_package(
        env=jinja_env,
        repo_root=repo_root,
        npm_dist_root=npm_dist_root,
        version=TEST_VERSION,
    )

    main_pkg_json_path = npm_dist_root / "apm-cli" / "package.json"
    assert main_pkg_json_path.exists()
    
    data = json.loads(main_pkg_json_path.read_text(encoding="utf-8"))
    assert data["name"] == "@microsoft/apm-cli"
    assert data["version"] == TEST_VERSION
    
    optional_deps = data.get("optionalDependencies", {})
    assert len(optional_deps) == len(PLATFORMS), "Mismatch in optionalDependencies count."
    
    for p_os, p_arch in PLATFORMS:
        dep_name = f"@microsoft/apm-cli-{p_os}-{p_arch}"
        assert dep_name in optional_deps, f"Missing dynamically injected dependency: {dep_name}"
        assert optional_deps[dep_name] == TEST_VERSION, f"Wrong version for {dep_name}"

    launcher_path = npm_dist_root / "apm-cli" / "bin" / "apm"
    
    # Verify execution permissions (+x)
    mode = os.stat(launcher_path).st_mode
    assert bool(mode & stat.S_IXUSR), "Launcher lacks user execution permissions."
    
    content = launcher_path.read_text(encoding="utf-8")
    assert content.startswith("#!/usr/bin/env node")
    
    for p_os, p_arch in PLATFORMS:
        binary_suffix = ".exe" if p_os == "win32" else ""
        expected_target = f'"{p_arch}": "@microsoft/apm-cli-{p_os}-{p_arch}/apm{binary_suffix}"'
        
        assert expected_target in content, f"Missing JS mapping for target {p_os}-{p_arch}"

# ====================================================================
# 4. Main Execution Flow Tests
# ====================================================================

def test_main_execution_success(monkeypatch, tmp_path):
    """Validate the orchestrator runs all platforms without exception."""
    monkeypatch.setattr("scripts.npm_publish.get_repo_root", lambda: tmp_path)
    monkeypatch.setattr("scripts.npm_publish.get_version", lambda r: "1.0.0")
    monkeypatch.setattr("scripts.npm_publish.setup_jinja_env", lambda r: MagicMock())
    
    mock_platform_gen = MagicMock()
    mock_main_gen = MagicMock()
    monkeypatch.setattr("scripts.npm_publish.generate_platform_package", mock_platform_gen)
    monkeypatch.setattr("scripts.npm_publish.generate_main_package", mock_main_gen)

    main()

    assert mock_platform_gen.call_count == len(PLATFORMS)
    assert mock_main_gen.call_count == 1


def test_main_execution_handles_errors(monkeypatch, tmp_path):
    """Validate main catches exceptions and exits with code 1."""
    monkeypatch.setattr("scripts.npm_publish.get_repo_root", lambda: tmp_path)
    monkeypatch.setattr("scripts.npm_publish.get_version", lambda r: "1.0.0")
    monkeypatch.setattr("scripts.npm_publish.setup_jinja_env", lambda r: MagicMock())

    def raise_error(*args, **kwargs):
        raise ValueError("Simulated pipeline failure")

    monkeypatch.setattr("scripts.npm_publish.generate_platform_package", raise_error)

    with pytest.raises(SystemExit) as exc_info:
        main()
        
    assert exc_info.value.code == 1, "Expected sys.exit(1) on failure."