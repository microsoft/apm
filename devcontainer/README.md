# APM Dev Container Feature -- Overview

A comprehensive reference for the APM (Agent Package Manager) Dev Container Feature: what it does, how it's structured, how to use it, how it's tested, and where it's supported.

---

## 1. Feature Overview

The APM Dev Container Feature packages the `apm-cli` tool as a reusable, declarative unit that can be added to any project's `devcontainer.json`. It eliminates the need for manual `postCreateCommand` installs and makes APM discoverable through the standard [Dev Container Features ecosystem](https://containers.dev/features).

**What it installs**

- [uv](https://github.com/astral-sh/uv) -- Astral's fast Python tool (installed to `/usr/local/bin`)
- Python 3.10+ -- only if not already present
- `git` -- required by `apm-cli` (uses GitPython at startup)
- `apm-cli` -- installed via `pip` (with automatic PEP 668 fallback)

**What motivated it**

- APM was previously only installable via ad-hoc `postCreateCommand` lines -- not reusable, not discoverable, hard to standardise.
- See GitHub issue [#717](https://github.com/microsoft/apm/issues/717) for the original feature request.

**Options**

| Option    | Type   | Default  | Description                                                          |
| --------- | ------ | -------- | -------------------------------------------------------------------- |
| `version` | string | `latest` | Version of `apm-cli` to install. `latest`, or a semver like `1.2.3`. |

The feature declares `installsAfter: ghcr.io/devcontainers/features/python` so the official Python feature (when present) runs first and provides Python.

---

## 2. `devcontainer` Directory Structure

```
devcontainer/
+-- src/
|   \-- apm-cli/
|       +-- devcontainer-feature.json   # Feature manifest (id, options, metadata)
|       \-- install.sh                  # Install script executed inside the container
\-- test/
    +-- apm-cli/
    |   +-- scenarios.json              # Integration test matrix (base image x options)
    |   +-- generic-checks.sh           # Shared post-install checks (apm on PATH, --version, --help)
    |   +-- default-ubuntu-24.sh        # Ubuntu 24.04 scenario (PEP 668 path)
    |   +-- default-debian-12.sh        # Debian 12 scenario (apt-get path)
    |   +-- default-alpine-3.sh         # Alpine 3.20 scenario (apk path)
    |   +-- default-fedora.sh           # Fedora 41 scenario (dnf path)
    |   +-- pinned-version.sh           # Confirms `version: "0.8.11"` option is honoured
    |   +-- with-python-feature.sh      # Confirms compatibility with the Python feature
    |   +-- test.sh                     # Fallback "auto" test (currently unused)
    |   \-- unit/
    |       \-- install.bats            # Bats unit tests for install.sh (37 tests)
    +-- bats/                           # git submodule -- bats-core runner
    \-- test_helper/                    # git submodules -- bats-support, bats-assert
```

---

## 3. How It Works

### Manifest

[src/apm-cli/devcontainer-feature.json](src/apm-cli/devcontainer-feature.json) declares the feature id (`apm-cli`), its options, and `installsAfter`. The devcontainer CLI reads this to understand how to build an image that consumes the feature.

### Install flow

When a devcontainer is built, the CLI injects each option as an uppercased environment variable (e.g. `VERSION`) and runs [src/apm-cli/install.sh](src/apm-cli/install.sh) as root. The script:

1. **Validates `VERSION`** -- accepts `latest` or a strict semver `X.Y.Z`; otherwise exits `1`.
2. **Verifies it is running as root** -- fails with a clear message otherwise.
3. **Installs `uv`** (idempotent) -- installs `curl` first via the detected package manager if needed, downloads `https://astral.sh/uv/install.sh`, and runs it with `UV_INSTALL_DIR=/usr/local/bin`. The installer temp file is cleaned up via `trap` on exit.
4. **Ensures Python 3.10+ is present** -- if `python3` is missing, installs `python3`, `python3-pip`, and `git` using `apt-get`, `apk`, or `dnf`. Then asserts `python3 --version` is >= 3.10.
5. **Ensures `git` is present** -- installs via the detected package manager if absent.
6. **Locates a working `pip`** -- prefers `pip3`, falls back to `pip`, then bootstraps via `python3 -m ensurepip --upgrade`.
7. **Installs `apm-cli`** -- `pip install apm-cli` (or `apm-cli==<version>` when pinned). On Ubuntu 24.04+ `pip` rejects the install under PEP 668 ("externally-managed-environment"); the script detects that specific error and retries with `--break-system-packages`.
8. **Adds `bash` on Alpine** -- required because devcontainer test scripts use `#!/bin/bash`.
9. **Verifies `apm` is on `PATH`** -- prints the installed version and path. If it isn't on `PATH`, prints a warning (not a failure).

### Compatibility with the official Python feature

`installsAfter` guarantees that if a user also declares `ghcr.io/devcontainers/features/python`, Python is already present when `install.sh` runs -- the script then detects `python3` and skips the distro package-manager branch.

---

## 4. How to use `devcontainer` in your project

### Quick start -- add the published feature

Add the published feature to any `.devcontainer/devcontainer.json`:

```json
{
  "image": "mcr.microsoft.com/devcontainers/base:ubuntu-24.04",
  "features": {
    "ghcr.io/microsoft/apm/apm-cli:1": {}
  }
}
```

Rebuild the container in VS Code (Dev Containers: Rebuild Container), GitHub Codespaces, or JetBrains Gateway. The `apm` binary is on `PATH`; verify with `apm --version`.

### Pin a specific apm-cli release

```json
{
  "features": {
    "ghcr.io/microsoft/apm/apm-cli:1": {
      "version": "0.10.0"
    }
  }
}
```

### Combine with the official Python feature

The APM feature declares `installsAfter` for the upstream Python feature, so ordering is automatic:

```json
{
  "image": "ubuntu:24.04",
  "features": {
    "ghcr.io/devcontainers/features/python:1": {},
    "ghcr.io/microsoft/apm/apm-cli:1": {}
  }
}
```

### Tag selection

| Tag                                       | Resolves to              | Use when                              |
| ----------------------------------------- | ------------------------ | ------------------------------------- |
| `ghcr.io/microsoft/apm/apm-cli:1`         | latest 1.x.y             | recommended default                   |
| `ghcr.io/microsoft/apm/apm-cli:1.0`       | latest 1.0.x             | locked to a minor line                |
| `ghcr.io/microsoft/apm/apm-cli:1.0.0`     | exact patch              | maximum reproducibility               |
| `ghcr.io/microsoft/apm/apm-cli:latest`    | newest published         | not recommended (crosses majors)      |

The feature manifest version is independent of the `apm-cli` PyPI release. To pin the CLI, use the `version` option above.

### Local development -- test an unpublished build

Recent versions of the Dev Containers CLI (bundled with `ms-vscode-remote.remote-containers` >= 0.454.0) enforce that a local Feature path must resolve **inside** the `.devcontainer/` folder. An upward `../devcontainer/src/apm-cli` path -- and symlinks pointing outside `.devcontainer/` -- are rejected with:

```
Local file path parse error. Resolved path must be a child of the .devcontainer/ folder.
```

To test the feature against this repo's own dev container, run the helper script from the repo root before opening the container -- it copies the feature into `.devcontainer/apm-cli-feature` and writes a matching `devcontainer.json`:

```sh
./devcontainer/scripts/sync-local-devcontainer.sh
```

The script is idempotent: re-run it whenever [src/apm-cli/install.sh](src/apm-cli/install.sh) or [src/apm-cli/devcontainer-feature.json](src/apm-cli/devcontainer-feature.json) changes.

This constraint only affects local consumption and is primarily meant for local testing. Published OCI references and tarball references are unaffected.

### Requirements for the base image

- Linux (Debian/Ubuntu, Alpine, or Fedora family -- see [#7](#7-supported-environments-os-and-shells)).
- Root on install (the feature runs as root; most base images already do).
- A reachable network (needs to fetch `uv` and `apm-cli`).
- Either a pre-installed Python 3.10+ or one of `apt-get` / `apk` / `dnf` available so the feature can install it.

---

## 5. Unit tests

**Where:** [test/apm-cli/unit/install.bats](test/apm-cli/unit/install.bats)
**Tool:** [bats-core](https://github.com/bats-core/bats-core), plus `bats-support` and `bats-assert` (all vendored as git submodules under `test/bats/` and `test/test_helper/`).
**Count:** 37 tests.

### Approach

The tests create a **temporary stub directory** (`STUB_BIN`) and populate it with fake versions of every command `install.sh` touches -- `apt-get`, `apk`, `dnf`, `curl`, `pip3`, `python3`, `git`, and so on. Each stub records its arguments and returns a configurable exit code. `PATH` is then locked to `STUB_BIN:/bin` via `run_with_stubs()`, so the script sees only the fakes.

This makes it possible to exhaustively cover every branch -- success paths, each package-manager variant, the PEP 668 retry, the ensurepip bootstrap, every invalid `VERSION` shape, missing-root, missing-curl, temp-file cleanup -- in milliseconds, with no Docker and no network.

### What's covered (representative)

- Root check and error message.
- `VERSION` validation: `latest`, valid semver, empty string, two-part, four-part, prerelease, build metadata, default.
- Python install branches across `apt-get` / `apk` / `dnf`; failure when no package manager is found.
- Python version boundary: continues at exactly 3.10; fails at 3.9.
- `git` install branch and its no-package-manager failure.
- `pip` location: prefers `pip3`, falls back to `pip`, bootstraps via `ensurepip`, fails cleanly if bootstrapping fails.
- `apm-cli` install: pins on semver; retries with `--break-system-packages` on PEP 668; fails on non-PEP-668 errors; fails if the retry itself fails.
- `uv` install: installs via curl when missing; curl install branch per package manager; fails if curl or installer script fails; cleans temp file on success and failure; skips install if `uv` already present.
- POSIX compliance: `install.sh` does not use the non-POSIX `local` keyword.
- Warn-not-fail when `apm` ends up off `PATH` after a successful install.

### How to run

Git submodules manage the test dependencies (`bats-core`, `bats-support`, `bats-assert`). After cloning, run:

```sh
git clone <repo-url>
cd <repo>
git submodule update --init --recursive
```

Then:

```sh
cd devcontainer/test/apm-cli/unit
../../bats/bin/bats install.bats
```

---

## 6. Integration tests

**Tool:** `devcontainer features test` from [`@devcontainers/cli`](https://github.com/devcontainers/cli) -- the official Microsoft test runner for Dev Container Features.
**Matrix:** [test/apm-cli/scenarios.json](test/apm-cli/scenarios.json).

### How scenarios are wired

For each entry in `scenarios.json` the CLI:

1. Builds a Docker image from the scenario's base `image`.
2. Runs the real `install.sh` inside the container with the scenario's options injected as environment variables.
3. Copies the `<scenario-id>.sh` file into the container and runs it -- the scenario id must match a filename under `test/apm-cli/`.
4. The test script sources `dev-container-features-test-lib` (provided by the CLI) and `generic-checks.sh`, then issues per-distro assertions, and calls `reportResults`.

### Scenario matrix

| Scenario id           | Base image                      | Purpose / code path exercised                    |
| --------------------- | ------------------------------- | ------------------------------------------------ |
| `default-ubuntu-24`   | `ubuntu:24.04`                  | PEP 668 retry with `--break-system-packages`     |
| `default-debian-12`   | `debian:12`                     | `apt-get` path (no PEP 668 enforcement)          |
| `default-alpine-3`    | `alpine:3.20`                   | `apk` path; confirms `bash` is installed         |
| `default-fedora`      | `fedora:41`                     | `dnf` path                                       |
| `pinned-version`      | `ubuntu:22.04`                  | `version: "0.8.11"` option end-to-end            |
| `with-python-feature` | `ubuntu:24.04` + Python feature | `installsAfter` ordering with the Python feature |

### Shared checks

[test/apm-cli/generic-checks.sh](test/apm-cli/generic-checks.sh) runs on every scenario and verifies:

- `apm` is on `PATH`
- `apm --version` exits `0`
- `apm --version` outputs a semver
- `apm --help` exits `0`

Per-scenario scripts add distro-specific assertions -- e.g. `default-alpine-3.sh` confirms `apk` is the package manager and that `python3` / `git` came from apk (proving the right branch was actually exercised).

### How to run

From the repo root, with Docker running and `@devcontainers/cli` installed (`npm install -g @devcontainers/cli`):

```sh
# All scenarios
devcontainer features test \
  --features apm-cli \
  --skip-autogenerated \
  --project-folder devcontainer

# One scenario
devcontainer features test \
  --features apm-cli \
  --filter default-ubuntu-24 \
  --skip-autogenerated \
  --project-folder devcontainer
```

`--skip-autogenerated` skips the CLI's default baseline test on `ubuntu:focal`, which is not supported (Python too old). Add `--log-level trace` for verbose build output.

---

## 7. Supported Environments, OS, and Shells

### Runtime environment

- Any platform where [Dev Containers](https://containers.dev/) run: VS Code + Dev Containers extension, GitHub Codespaces, JetBrains Gateway with Dev Containers, and the `devcontainer` CLI directly.
- Requires Docker (or a compatible engine) on the host.

### Operating systems (verified via integration tests)

| OS family | Version       | Package manager | PEP 668 enforced |
| --------- | ------------- | --------------- | ---------------- |
| Ubuntu    | 24.04 (noble) | `apt-get`       | Yes              |
| Ubuntu    | 22.04 (jammy) | `apt-get`       | No               |
| Debian    | 12 (bookworm) | `apt-get`       | No               |
| Alpine    | 3.20          | `apk`           | No               |
| Fedora    | 41            | `dnf`           | No               |

**Not supported:** `ubuntu:focal` (20.04) and earlier -- Python 3.10+ is not available from the default repos, and the feature's Python version check fails fast with a clear error. macOS and Windows as host OSes are fine (Dev Containers runs Linux inside Docker on both); they are not valid feature-install targets.

### Shells

- `install.sh` is `#!/bin/sh` and written to be strictly POSIX (verified by a dedicated unit test that greps for the non-POSIX `local` keyword). It runs under `dash` on Debian/Ubuntu, `ash` on Alpine, and `bash` on Fedora.
- Integration test scripts (`default-*.sh`, `pinned-version.sh`, etc.) are `#!/bin/bash`. On Alpine the install script adds `bash` because the base image ships only `ash`.
- The installed `apm` CLI itself has no shell-specific requirements; users interact with it from whatever interactive shell the container provides (commonly `bash` or `zsh`).
