"""Real CLI provenance across source checkouts and installed wheel locations."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import sysconfig
import venv
from pathlib import Path

import pytest

from apm_cli.utils.git_env import get_git_executable, git_subprocess_env
from apm_cli.version import get_version

pytestmark = [pytest.mark.integration, pytest.mark.e2e]

REPO_ROOT = Path(__file__).resolve().parents[2]


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        [get_git_executable(), "-C", str(root), *args],
        env=git_subprocess_env(),
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    ).stdout.strip()


def _init_repo(root: Path) -> str:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "--quiet")
    _git(root, "config", "user.name", "Version test")
    _git(root, "config", "user.email", "version@example.invalid")
    _git(root, "config", "commit.gpgsign", "false")
    _git(root, "add", ".")
    _git(root, "commit", "--quiet", "--allow-empty", "-m", root.name)
    return _git(root, "rev-parse", "--short", "HEAD")


def _copy_source(root: Path) -> Path:
    package = root / "src" / "apm_cli"
    shutil.copytree(
        REPO_ROOT / "src" / "apm_cli", package, ignore=shutil.ignore_patterns("__pycache__")
    )
    shutil.copyfile(REPO_ROOT / "pyproject.toml", root / "pyproject.toml")
    return package.parent


def _version_output(
    import_root: Path,
    cwd: Path,
    *,
    overrides: dict[str, str] | None = None,
    installed: bool = False,
) -> str:
    env = git_subprocess_env()
    env.update(
        PYTHONPATH=str(import_root),
        HOME=str(cwd),
        USERPROFILE=str(cwd),
        NO_COLOR="1",
        COLUMNS="200",
    )
    env.pop("FORCE_COLOR", None)
    env.pop("APM_EXPERIMENTAL_VERBOSE_VERSION", None)
    env.update(overrides or {})
    code = (
        "import pathlib, apm_cli, sys; "
        "assert pathlib.Path(apm_cli.__file__).resolve().parent == "
        "pathlib.Path(sys.argv.pop(1)).resolve() / 'apm_cli'; "
    )
    if installed:
        probe = subprocess.run(
            [sys.executable, "-c", code, str(import_root)],
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert probe.returncode == 0, probe.stderr
        candidates = [
            import_root / "bin" / "apm",
            import_root / "Scripts" / "apm.exe",
            import_root / "bin" / "apm.exe",
        ]
        launcher = next((path for path in candidates if path.is_file()), None)
        assert launcher is not None, "wheel installer did not create the apm console script"
        command = [str(launcher), "--version"]
    else:
        code += "from apm_cli.entrypoint import main; main()"
        command = [sys.executable, "-c", code, str(import_root), "--version"]
    result = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    return result.stdout.strip()


@pytest.fixture(scope="module")
def wheel(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build the current tree once; never skip a failed build."""
    out = tmp_path_factory.mktemp("version-wheel")
    uv = shutil.which("uv")
    assert uv is not None, "uv is required to build and install the CLI wheel"
    result = subprocess.run(
        [
            uv,
            "build",
            "--offline",
            "--no-build-isolation",
            "--python",
            sys.executable,
            "--wheel",
            "--out-dir",
            str(out),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    return next(out.glob("*.whl"))


@pytest.mark.parametrize(
    "ancestor", ["unrelated", "apm", "none", "missing-git", "target-unrelated", "target-apm"]
)
def test_installed_wheel_does_not_borrow_ancestor_sha(
    tmp_path: Path, wheel: Path, ancestor: str
) -> None:
    root = tmp_path / ancestor
    root.mkdir()
    if ancestor in {"apm", "target-apm"}:
        _copy_source(root)
    if ancestor not in {"none", "missing-git"}:
        _init_repo(root)
    site = (
        root / "src"
        if ancestor.startswith("target-")
        else root / "lib" / "python3" / "site-packages"
    )
    uv = shutil.which("uv")
    assert uv is not None
    result = subprocess.run(
        [
            uv,
            "pip",
            "install",
            "--python",
            sys.executable,
            "--no-deps",
            "--target",
            str(site),
            str(wheel),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    cwd = tmp_path / "caller"
    _init_repo(cwd)
    overrides = None
    if ancestor == "missing-git":
        empty_path = tmp_path / "empty-path"
        empty_path.mkdir()
        overrides = {"PATH": str(empty_path)}
    assert _version_output(site, cwd, overrides=overrides, installed=True) == (
        f"Agent Package Manager (APM) CLI version {get_version()}"
    )


@pytest.mark.parametrize("layout", ["source", "worktree"])
def test_source_checkout_uses_its_own_revision(tmp_path: Path, layout: str) -> None:
    root = tmp_path / "fork"
    import_root = _copy_source(root)
    expected = _init_repo(root)
    if layout == "worktree":
        worktree = tmp_path / "linked"
        _git(root, "worktree", "add", "--detach", str(worktree), "HEAD")
        import_root = worktree / "src"
    cwd = tmp_path / "caller"
    _init_repo(cwd)
    # A source/editable import must ignore both the caller and its Git overrides.
    overrides = {"GIT_DIR": str(cwd / ".git"), "GIT_WORK_TREE": str(cwd)}
    assert _version_output(import_root, cwd, overrides=overrides) == (
        f"Agent Package Manager (APM) CLI version {get_version()} ({expected})"
    )


@pytest.mark.parametrize("layout", ["nested", "direct"])
def test_unversioned_source_under_unrelated_checkout(tmp_path: Path, layout: str) -> None:
    root = tmp_path / "unrelated"
    _init_repo(root)
    import_root = _copy_source(root / "untracked-apm" if layout == "nested" else root)
    assert _version_output(import_root, root) == (
        f"Agent Package Manager (APM) CLI version {get_version()}"
    )


def test_source_without_git(tmp_path: Path) -> None:
    import_root = _copy_source(tmp_path / "source")
    _init_repo(import_root.parent)
    empty_path = tmp_path / "empty-path"
    empty_path.mkdir()
    overrides = {"PATH": str(empty_path)}
    if os.name == "nt":
        overrides["PATHEXT"] = ".EXE"
    assert _version_output(import_root, tmp_path, overrides=overrides) == (
        f"Agent Package Manager (APM) CLI version {get_version()}"
    )


def test_editable_install_retains_checkout_revision(tmp_path: Path) -> None:
    root = tmp_path / "editable-source"
    _copy_source(root)
    shutil.copyfile(REPO_ROOT / "README.md", root / "README.md")
    expected = _init_repo(root)
    environment = tmp_path / "venv"
    venv.EnvBuilder(with_pip=False, symlinks=os.name != "nt").create(environment)
    scripts = environment / ("Scripts" if os.name == "nt" else "bin")
    python = scripts / ("python.exe" if os.name == "nt" else "python")
    site = environment / (
        "Lib/site-packages"
        if os.name == "nt"
        else f"lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages"
    )
    # Reuse installed dependencies without processing the parent APM editable .pth.
    (site / "dependencies.pth").write_text(sysconfig.get_path("purelib") + "\n", encoding="utf-8")
    uv = shutil.which("uv")
    assert uv is not None
    result = subprocess.run(
        [
            uv,
            "pip",
            "install",
            "--python",
            str(python),
            "--offline",
            "--no-deps",
            "--no-build-isolation",
            "--editable",
            str(root),
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    env = git_subprocess_env()
    env.pop("PYTHONPATH", None)
    env.update(NO_COLOR="1", HOME=str(tmp_path), USERPROFILE=str(tmp_path))
    result = subprocess.run(
        [str(scripts / ("apm.exe" if os.name == "nt" else "apm")), "--version"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == (
        f"Agent Package Manager (APM) CLI version {get_version()} ({expected})"
    )
