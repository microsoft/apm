"""Real shell startup coverage for the Unix standalone installer."""

from __future__ import annotations

import fcntl
import hashlib
import os
import platform
import pty
import select
import shlex
import shutil
import subprocess
import sys
import termios
from pathlib import Path

import pytest

INSTALLER = Path(__file__).resolve().parents[2] / "install.sh"
_HISTORICAL_PROBE = (
    "apm_resolve_install_paths /usr/local/bin/apm /opt/homebrew/bin/apm /usr/local/lib/apm/apm"
)
_HARNESS_HISTORICAL_ENV = "APM_FIXTURE_HISTORICAL_APM"
_BASE_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"

pytestmark = [
    pytest.mark.component,
    pytest.mark.integration,
    pytest.mark.lifecycle_smoke,
    pytest.mark.skipif(sys.platform == "win32", reason="Unix shell startup contract"),
    pytest.mark.xdist_group("home_env"),
]


def _require_shell(path: str) -> str:
    """Return an available shell path or skip with an explicit reason."""
    resolved = shutil.which(path) if "/" not in path else path
    if resolved is None or not Path(resolved).exists():
        pytest.skip(f"required shell is not available: {path}")
    return resolved


def _native_platform_dir() -> str:
    """Return the release directory name install.sh will select on this host."""
    system = platform.system()
    machine = platform.machine()
    if system == "Darwin":
        os_name = "darwin"
    elif system == "Linux":
        os_name = "linux"
    else:
        pytest.skip(f"unsupported Unix installer platform: {system}")
    if machine in {"arm64", "aarch64"}:
        arch = "arm64"
    elif machine == "x86_64":
        arch = "x86_64"
    else:
        pytest.skip(f"unsupported Unix installer architecture: {machine}")
    return f"apm-{os_name}-{arch}"


def _stage_release(tmp_path: Path) -> tuple[Path, Path]:
    """Create a hermetic release archive and fake curl downloader."""
    platform_dir = _native_platform_dir()
    release_root = tmp_path / "release"
    extracted = release_root / platform_dir
    extracted.mkdir(parents=True)
    binary = extracted / "apm"
    binary.write_text("#!/bin/sh\nprintf 'apm integration fixture\\n'\n", encoding="ascii")
    binary.chmod(0o755)
    (extracted / "VERSION").write_text("fixture\n", encoding="ascii")
    archive = tmp_path / "apm-fixture.tar.gz"
    subprocess.run(
        ["tar", "-czf", str(archive), "-C", str(release_root), platform_dir],
        check=True,
        capture_output=True,
        text=True,
    )
    tools = tmp_path / "tools"
    tools.mkdir()
    curl = tools / "curl"
    curl.write_text(
        "#!/bin/sh\n"
        "out=\n"
        "while [ $# -gt 0 ]; do\n"
        '  case "$1" in -o) shift; out=$1 ;; esac\n'
        "  shift || true\n"
        "done\n"
        '[ -n "$out" ] || exit 2\n'
        'cp "$APM_FIXTURE_ARCHIVE" "$out"\n',
        encoding="ascii",
    )
    curl.chmod(0o755)
    return archive, tools


def _first_existing(*candidates: str) -> str | None:
    """Return the first existing executable path from explicit candidates."""
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    return None


def _which_distinct(command: str, *, not_path: str) -> str | None:
    """Return a PATH-resolved executable when it differs from another path."""
    resolved = shutil.which(command)
    if resolved is None:
        return None
    try:
        if Path(resolved).resolve() == Path(not_path).resolve():
            return None
    except OSError:
        return None
    return resolved


def _fish_quote(value: str) -> str:
    """Quote an argument for the Fish test probe."""
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _real_shell_cases() -> list[tuple[str, str, tuple[str, ...], str]]:
    """Build portable shell startup cases for whichever supported shells exist."""
    cases: list[tuple[str, str, tuple[str, ...], str]] = []
    system_bash = _first_existing(os.environ.get("APM_SHELL_RUNTIME_SYSTEM_BASH", ""), "/bin/bash")
    if system_bash is not None:
        cases.extend(
            [
                ("bash-fresh-login-system", system_bash, ("-lc",), ".bash_profile"),
                ("bash-fresh-interactive-system", system_bash, ("-ic",), ".bashrc"),
            ]
        )
    modern_bash = _first_existing(
        os.environ.get("APM_SHELL_RUNTIME_BASH5", ""),
        "/opt/homebrew/bin/bash",
        "/usr/local/bin/bash",
        _which_distinct("bash", not_path=system_bash or ""),
    )
    if modern_bash is not None:
        cases.extend(
            [
                ("bash-fresh-login-modern", modern_bash, ("-lc",), ".bash_profile"),
                ("bash-fresh-interactive-modern", modern_bash, ("-ic",), ".bashrc"),
            ]
        )
    zsh = _first_existing(
        os.environ.get("APM_SHELL_RUNTIME_ZSH", ""), "/bin/zsh", shutil.which("zsh") or ""
    )
    if zsh is not None:
        cases.append(("zsh-default", zsh, ("-ic",), ".zshrc"))
    fish = _first_existing(
        os.environ.get("APM_SHELL_RUNTIME_FISH", ""),
        shutil.which("fish") or "",
        "/opt/homebrew/bin/fish",
        "/usr/local/bin/fish",
    )
    if fish is not None:
        cases.append(("fish-xdg", fish, ("-ic",), ".config/fish/conf.d/apm.fish"))
    if not cases:
        cases.append(pytest.param("no-shells", "missing", ("-c",), "", marks=pytest.mark.skip))
    return cases


def _harnessed_installer(tmp_path: Path) -> Path:
    """Copy install.sh with historical host probes redirected into the fixture."""
    installer = tmp_path / "install.sh"
    installer.write_text(
        INSTALLER.read_text(encoding="ascii").replace(
            _HISTORICAL_PROBE,
            f'apm_resolve_install_paths "${_HARNESS_HISTORICAL_ENV}"',
        ),
        encoding="ascii",
    )
    return installer


def _harnessed_installer_text(tmp_path: Path) -> str:
    """Return the production installer text with only host probes redirected."""
    return _harnessed_installer(tmp_path).read_text(encoding="ascii")


def _run_pty(command: str, *, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Run a shell command with a controlling pseudo-terminal."""
    master_fd, slave_fd = pty.openpty()

    def _child() -> None:
        os.setsid()
        fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)

    proc = subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        shell=True,
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        text=False,
        preexec_fn=_child,
    )
    os.close(slave_fd)
    chunks: list[bytes] = []
    while True:
        ready, _, _ = select.select([master_fd], [], [], 0.2)
        if ready:
            try:
                data = os.read(master_fd, 4096)
            except OSError:
                data = b""
            if data:
                chunks.append(data)
        if proc.poll() is not None:
            break
    os.close(master_fd)
    return subprocess.CompletedProcess(
        args=command,
        returncode=proc.wait(timeout=5),
        stdout=b"".join(chunks).decode("utf-8", errors="replace"),
        stderr="",
    )


def _base_install_env(
    tmp_path: Path,
    *,
    home: Path,
    tools: Path,
    archive: Path,
    shell: str,
) -> dict[str, str]:
    """Build a hermetic installer environment without production test overrides."""
    env = {
        **os.environ,
        "APM_FIXTURE_ARCHIVE": str(archive),
        "APM_RELEASE_BASE_URL": "https://example.invalid/apm",
        "CI": "",
        "GITHUB_ACTIONS": "",
        "HOME": str(home),
        "LC_ALL": "C",
        "PATH": f"{tools}:{_BASE_PATH}",
        "SHELL": shell,
        _HARNESS_HISTORICAL_ENV: str(tmp_path / "missing-historical/apm"),
        "VERSION": "v0.0.0",
    }
    return env


def _run_piped_installer_with_pty(
    *,
    tmp_path: Path,
    shell: str,
    installer: Path,
    prefix: Path,
    env: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    """Run actual installer bytes through `sh -s --` with a controlling TTY."""
    installer_cmd = (
        f"env PATH={shlex.quote(env['PATH'])} cat {shlex.quote(str(installer))} | "
        f"env PATH={shlex.quote(env['PATH'])} sh -s -- --prefix {shlex.quote(str(prefix))}"
    )
    return _run_pty(
        f"{shlex.quote(shell)} -lc {shlex.quote(installer_cmd)}",
        cwd=tmp_path,
        env=env,
    )


def _real_home_snapshot() -> dict[Path, tuple[bool, int | None, str | None]]:
    """Snapshot existing real HOME startup files without creating sentinels."""
    real_home = Path.home()
    candidates = [
        real_home / ".bash_profile",
        real_home / ".bash_login",
        real_home / ".profile",
        real_home / ".bashrc",
        real_home / ".zshrc",
        real_home / ".zshenv",
        real_home / ".config/fish/conf.d/apm.fish",
        real_home / ".apm/shell/env",
        real_home / ".apm/shell/fish.fish",
    ]
    snapshot: dict[Path, tuple[bool, int | None, str | None]] = {}
    for candidate in candidates:
        try:
            stat = candidate.lstat()
        except FileNotFoundError:
            snapshot[candidate] = (False, None, None)
            continue
        digest = None
        if candidate.is_file() and not candidate.is_symlink():
            digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
        snapshot[candidate] = (True, stat.st_mode & 0o7777, digest)
    return snapshot


def _assert_real_home_unchanged(
    before: dict[Path, tuple[bool, int | None, str | None]],
) -> None:
    """Assert a prior real HOME snapshot still matches."""
    assert _real_home_snapshot() == before


@pytest.mark.parametrize(
    ("case_id", "shell_path", "startup_args", "profile_rel"),
    _real_shell_cases(),
)
def test_native_installer_enrolls_real_shell_startup(
    tmp_path: Path,
    case_id: str,
    shell_path: str,
    startup_args: tuple[str, ...],
    profile_rel: str,
) -> None:
    """Real bash, zsh, and fish startup files resolve apm after native install."""
    shell = _require_shell(shell_path)
    archive, tools = _stage_release(tmp_path)
    installer = _harnessed_installer(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    prefix = tmp_path / "prefix with spaces and 'quotes'"
    env = _base_install_env(tmp_path, home=home, tools=tools, archive=archive, shell=shell)
    before_real_home = _real_home_snapshot()
    install = _run_piped_installer_with_pty(
        tmp_path=tmp_path,
        shell=shell,
        installer=installer,
        prefix=prefix,
        env=env,
    )
    assert install.returncode == 0, install.stdout
    _assert_real_home_unchanged(before_real_home)
    assert case_id.split("-", 1)[0] in {"bash", "zsh", "fish"}
    assert (home / profile_rel).is_file()
    probe = subprocess.run(
        [shell, *startup_args, "command -v apm >/dev/null && apm --version"],
        cwd=tmp_path,
        env={**env, "PATH": _BASE_PATH},
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert probe.returncode == 0, probe.stderr or probe.stdout
    assert probe.stdout == "apm integration fixture\n"
    if case_id == "fish-xdg":
        fish_probe = subprocess.run(
            [
                shell,
                *startup_args,
                f"contains -- {_fish_quote(str(prefix / 'bin'))} $PATH",
            ],
            cwd=tmp_path,
            env={**env, "PATH": _BASE_PATH},
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        assert fish_probe.returncode == 0, fish_probe.stderr or fish_probe.stdout


def test_headless_piped_sh_does_not_write_profiles(tmp_path: Path) -> None:
    """headless: real no-PTY byte pipe installs but leaves profiles untouched."""
    shell = _require_shell("/bin/bash")
    archive, tools = _stage_release(tmp_path)
    installer_text = _harnessed_installer_text(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    prefix = tmp_path / "headless-prefix"
    env = _base_install_env(tmp_path, home=home, tools=tools, archive=archive, shell=shell)
    result = subprocess.run(
        ["sh", "-s", "--", "--prefix", str(prefix)],
        input=installer_text,
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    assert (prefix / "bin/apm").exists()
    assert "no interactive terminal detected" in result.stdout
    assert "For this shell, run the following" in result.stdout
    assert not (home / ".bash_profile").exists()
    assert not (home / ".bashrc").exists()
    assert not (home / ".apm/shell").exists()


def test_fish_hook_quotes_mixed_literal_paths(tmp_path: Path) -> None:
    """literal-paths: real Fish executes hooks with quotes and backslashes."""
    fish = _require_shell(
        _first_existing(os.environ.get("APM_SHELL_RUNTIME_FISH", ""), shutil.which("fish") or "")
        or "fish"
    )
    archive, tools = _stage_release(tmp_path)
    installer = _harnessed_installer(tmp_path)
    for suffix in ("it's\\quoted", "backslash\\then'quote", "dollar$paren$(echo nope)"):
        home = tmp_path / ("home-" + str(len(list(tmp_path.glob("home-*")))))
        home.mkdir()
        env = _base_install_env(tmp_path, home=home, tools=tools, archive=archive, shell=fish)
        prefix = tmp_path / suffix
        install = _run_piped_installer_with_pty(
            tmp_path=tmp_path,
            shell=fish,
            installer=installer,
            prefix=prefix,
            env=env,
        )
        assert install.returncode == 0, install.stdout
        hook = home / ".apm/shell/fish.fish"
        probe = subprocess.run(
            [
                fish,
                "-ic",
                f"source {_fish_quote(str(hook))}; contains -- {_fish_quote(str(prefix / 'bin'))} $PATH; and apm --version",
            ],
            cwd=tmp_path,
            env={**env, "PATH": _BASE_PATH},
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        assert probe.returncode == 0, probe.stderr or probe.stdout
        assert probe.stdout == "apm integration fixture\n"


def test_fish_absolute_guidance_quotes_unrepresentable_path(tmp_path: Path) -> None:
    """unrepresentable-path: fish can execute printed absolute launcher guidance."""
    fish = _require_shell(
        _first_existing(os.environ.get("APM_SHELL_RUNTIME_FISH", ""), shutil.which("fish") or "")
        or "fish"
    )
    archive, tools = _stage_release(tmp_path)
    installer = _harnessed_installer(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    prefix = tmp_path / "colon:it's\\quoted"
    env = _base_install_env(tmp_path, home=home, tools=tools, archive=archive, shell=fish)
    install = _run_piped_installer_with_pty(
        tmp_path=tmp_path,
        shell=fish,
        installer=installer,
        prefix=prefix,
        env=env,
    )
    assert install.returncode == 0, install.stdout
    output = install.stdout.replace("\r\n", "\n")
    command = (
        output.split("Run APM using its absolute path:\n", 1)[1]
        .split("\nFor PATH discovery,", 1)[0]
        .strip()
    )
    probe = subprocess.run(
        [fish, "-ic", command],
        cwd=tmp_path,
        env={**env, "PATH": _BASE_PATH},
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert probe.returncode == 0, probe.stderr or probe.stdout
    assert probe.stdout == "apm integration fixture\n"
    assert not (home / ".config/fish/conf.d/apm.fish").exists()


@pytest.mark.parametrize("profile_rel", [".profile", ".bash_login"])
def test_bash_existing_profile_precedence_real_login_shell(
    tmp_path: Path,
    profile_rel: str,
) -> None:
    """bash-preserve-precedence: real login shell uses existing readable profile."""
    shell = _require_shell("/bin/bash")
    archive, tools = _stage_release(tmp_path)
    installer = _harnessed_installer(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    (home / profile_rel).write_text("export APM_SENTINEL=kept\n", encoding="ascii")
    prefix = tmp_path / "prefix"
    env = _base_install_env(tmp_path, home=home, tools=tools, archive=archive, shell=shell)
    install = _run_piped_installer_with_pty(
        tmp_path=tmp_path,
        shell=shell,
        installer=installer,
        prefix=prefix,
        env=env,
    )
    assert install.returncode == 0, install.stdout
    assert not (home / ".bash_profile").exists()
    probe = subprocess.run(
        [shell, "-lc", 'test "$APM_SENTINEL" = kept && apm --version'],
        cwd=tmp_path,
        env={**env, "PATH": _BASE_PATH},
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert probe.returncode == 0, probe.stderr or probe.stdout
    assert probe.stdout == "apm integration fixture\n"


def test_bash_standalone_return_profile_stays_manual_in_real_startup(tmp_path: Path) -> None:
    """profile-content-corners: top-level return stays unreachable and manual."""
    shell = _require_shell("/bin/bash")
    archive, tools = _stage_release(tmp_path)
    installer = _harnessed_installer(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    sentinel = tmp_path / "profile-sentinel"
    profile = home / ".bash_profile"
    profile.write_text(
        f"printf before > {shlex.quote(str(sentinel))}\n"
        f"return 0\nprintf after >> {shlex.quote(str(sentinel))}\n",
        encoding="ascii",
    )
    before_profile = profile.read_bytes()
    prefix = tmp_path / "prefix"
    env = _base_install_env(tmp_path, home=home, tools=tools, archive=archive, shell=shell)
    install = _run_piped_installer_with_pty(
        tmp_path=tmp_path,
        shell=shell,
        installer=installer,
        prefix=prefix,
        env=env,
    )
    assert install.returncode == 0, install.stdout
    assert "cannot safely update" in install.stdout
    assert profile.read_bytes() == before_profile
    probe = subprocess.run(
        [shell, "-lc", f'test "$(command -v apm)" = {shlex.quote(str(prefix / "bin/apm"))}'],
        cwd=tmp_path,
        env={**env, "PATH": _BASE_PATH},
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert probe.returncode != 0
    assert sentinel.read_text(encoding="ascii") == "before"


def test_zsh_zdotdir_real_startup_finds_apm(tmp_path: Path) -> None:
    """zsh-zdotdir: real zsh startup honors exported ZDOTDIR."""
    zsh = _require_shell("/bin/zsh")
    archive, tools = _stage_release(tmp_path)
    installer = _harnessed_installer(tmp_path)
    home = tmp_path / "home"
    zdotdir = tmp_path / "zdot dir"
    home.mkdir()
    zdotdir.mkdir()
    prefix = tmp_path / "prefix"
    env = {
        **_base_install_env(tmp_path, home=home, tools=tools, archive=archive, shell=zsh),
        "ZDOTDIR": str(zdotdir),
    }
    install = _run_piped_installer_with_pty(
        tmp_path=tmp_path,
        shell=zsh,
        installer=installer,
        prefix=prefix,
        env=env,
    )
    assert install.returncode == 0, install.stdout
    assert not (home / ".zshrc").exists()
    assert (zdotdir / ".zshrc").exists()
    probe = subprocess.run(
        [zsh, "-ic", "apm --version"],
        cwd=tmp_path,
        env={**env, "PATH": _BASE_PATH},
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert probe.returncode == 0, probe.stderr or probe.stdout
    assert probe.stdout == "apm integration fixture\n"


def test_bash_common_interactive_guard_real_interactive_shell(tmp_path: Path) -> None:
    """common-bash-guard: real interactive bash still reaches managed hook."""
    shell = _require_shell("/bin/bash")
    archive, tools = _stage_release(tmp_path)
    installer = _harnessed_installer(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    (home / ".bashrc").write_text("case $- in *i*) ;; *) return;; esac\n", encoding="ascii")
    prefix = tmp_path / "prefix"
    env = _base_install_env(tmp_path, home=home, tools=tools, archive=archive, shell=shell)
    install = _run_piped_installer_with_pty(
        tmp_path=tmp_path,
        shell=shell,
        installer=installer,
        prefix=prefix,
        env=env,
    )
    assert install.returncode == 0, install.stdout
    probe = subprocess.run(
        [shell, "-ic", "apm --version"],
        cwd=tmp_path,
        env={**env, "PATH": _BASE_PATH},
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert probe.returncode == 0, probe.stderr or probe.stdout
    assert probe.stdout == "apm integration fixture\n"


def test_posix_sh_install_stays_manual_without_profile_mutation(tmp_path: Path) -> None:
    """unknown-shell: POSIX sh can install, but does not create shell profiles."""
    shell = _require_shell("sh")
    archive, tools = _stage_release(tmp_path)
    installer = _harnessed_installer(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    env = _base_install_env(tmp_path, home=home, tools=tools, archive=archive, shell=shell)
    installer_cmd = (
        f"cat {shlex.quote(str(installer))} | "
        f"sh -s -- --prefix {shlex.quote(str(tmp_path / 'prefix'))}"
    )
    install = _run_pty(installer_cmd, cwd=tmp_path, env={**env, "SHELL": shell})
    assert install.returncode == 0, install.stdout
    assert "No shell profiles were changed." in install.stdout
    assert not (home / ".bash_profile").exists()
    assert not (home / ".bashrc").exists()
    assert not (home / ".zshrc").exists()
    assert not (home / ".config/fish/conf.d/apm.fish").exists()
