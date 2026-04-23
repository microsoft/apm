# How the testing works (ELI5)

## The thing being tested

[src/apm/install.sh](src/apm/install.sh) — a shell script that runs inside a Docker container when someone adds the APM devcontainer feature to their project. Its job:

1. Check it's running as root.
2. Validate the `VERSION` option (must be `latest` or a semver like `1.2.3`).
3. Install `uv` (Astral's Python tool) if missing.
4. Install Python 3.10+ if missing — using `apt-get`, `apk`, or `dnf` depending on the distro.
5. Install `git` if missing.
6. Find a working `pip` (try `pip3`, then `pip`, then bootstrap via `ensurepip`).
7. Run `pip install apm-cli` (with a retry using `--break-system-packages` if the distro blocks it under PEP 668).
8. Verify `apm` landed on `PATH`.

The tests exist to prove all those branches behave correctly without having to manually spin up five different Linux images every time someone changes the script.

---

## Two layers of tests

There are two totally separate test layers. They answer different questions.

### Layer 1 — Unit tests (fast, no Docker)

**File:** [test/apm/unit/install.bats](test/apm/unit/install.bats)
**Tool:** [bats](https://github.com/bats-core/bats-core) — a bash testing framework. The `test/bats/` and `test/test_helper/` directories are git submodules that provide the framework and assertion helpers (`assert_success`, `assert_output`, etc.).

**The trick:** instead of _actually_ installing Python/uv/apm, the tests create fake versions of those commands (called "stubs") in a temporary directory, then set `PATH` to point only at that directory. When `install.sh` runs `command -v apt-get` or `pip3 install ...`, it finds the fakes and the fakes record what was called.

A stub looks like this:

```sh
#!/bin/sh
echo "$@" >> /tmp/bin/_apt-get_args   # record the arguments
exit 0                                 # pretend it worked
```

So a test for "installs python3 via apt-get when missing" does this:

1. Create a fake `apt-get` that records its arguments.
2. Delete the `python3` stub (simulating "not installed").
3. Run `install.sh` with the fake `PATH`.
4. Assert the script succeeded and that the recorded args mention `python3`, `python3-pip`, `git`.

This runs in milliseconds, no network, no Docker. It can exhaustively test error paths (pip fails, no package manager exists, `VERSION` is malformed) that would be painful to test for real.

**Key helpers in the file:**

- `setup()` (line 12) — runs before every test. Creates the stub directory and pre-stages a fake `python3`.
- `run_with_stubs()` (line 45) — runs `install.sh` with `PATH` locked to the stubs.
- `make_stub` / `make_pkg_mgr_stub` / `make_python3_stub` — factories that produce different kinds of fakes.
- `setup_happy_path()` (line 94) — stubs out _everything_ as working, so each test can then break _one_ thing and assert the failure.

**How to run it:**

```sh
cd devcontainer/test/apm/unit
../../bats/bin/bats install.bats
```

### Layer 2 — Integration tests (slow, real Docker)

**Tool:** the `@devcontainers/cli` command `devcontainer features test`. This is Microsoft's official test runner for devcontainer features.

**The flow:**

1. You run `devcontainer features test -f apm devcontainer/` (or similar).
2. The CLI reads [test/apm/scenarios.json](test/apm/scenarios.json). Each scenario has an `id`, a base Docker `image`, and optionally `options` or `features` to mix with.
3. For each scenario:
   - The CLI builds a real Docker image from the specified base.
   - It runs the real `install.sh` inside that image (with the scenario's options injected as environment variables — `VERSION` for the `version` option).
   - It copies a test script into the container and runs it.
   - The test script uses `check "<name>" <command>` (from the CLI-provided `dev-container-features-test-lib`) to assert things are working.
4. Each `check` pass/fail is reported; `reportResults` at the end returns the overall status.

### Which test script runs for which scenario?

The devcontainer CLI picks the test script by **matching the scenario `id` to a filename** in `test/apm/`:

| scenarios.json `id`   | Script that runs                                          | Base image                    | Purpose                                       |
| --------------------- | --------------------------------------------------------- | ----------------------------- | --------------------------------------------- |
| `default-ubuntu-24`   | [default-ubuntu-24.sh](test/apm/default-ubuntu-24.sh)     | ubuntu:24.04                  | PEP 668 path (distro blocks plain pip)        |
| `default-debian-12`   | [default-debian-12.sh](test/apm/default-debian-12.sh)     | debian:12                     | apt-get code path                             |
| `default-alpine-3`    | [default-alpine-3.sh](test/apm/default-alpine-3.sh)       | alpine:3.20                   | apk code path                                 |
| `default-fedora`      | [default-fedora.sh](test/apm/default-fedora.sh)           | fedora:41                     | dnf code path                                 |
| `pinned-version`      | [pinned-version.sh](test/apm/pinned-version.sh)           | ubuntu:22.04                  | `version: "0.8.11"` option end-to-end         |
| `with-python-feature` | [with-python-feature.sh](test/apm/with-python-feature.sh) | ubuntu:24.04 + python feature | Plays nicely with the official Python feature |

If a scenario doesn't have a matching file, the CLI falls back to [test.sh](test/apm/test.sh) (the "auto" test script). Right now, nothing falls back — every scenario has its own file.

### How the per-scenario scripts share code

Every scenario script opens the same way:

```bash
#!/bin/bash
set -e
source dev-container-features-test-lib                    # provided by the CLI
source "$(dirname "$0")/generic-checks.sh"                # shared checks
# ... scenario-specific checks ...
reportResults
```

[generic-checks.sh](test/apm/generic-checks.sh) holds the four checks that must pass on _every_ distro:

- `apm` is on `PATH`
- `apm --version` exits cleanly
- `apm --version` outputs a semver
- `apm --help` exits cleanly

The per-distro file then adds distro-specific assertions — e.g. `default-alpine-3.sh` also checks that `apk` is the package manager and that `python3` and `git` came from apk. That proves the _right code path_ in `install.sh` was actually exercised (not just that _some_ path happened to work).

---

## Walk-through: one scenario, end to end

Let's follow `default-ubuntu-24`:

1. `devcontainer features test` reads [scenarios.json](test/apm/scenarios.json) and finds the scenario.
2. It builds a container from `ubuntu:24.04`.
3. It runs the APM feature's `install.sh` inside the container with `VERSION=latest` (the default from [devcontainer-feature.json](src/apm/devcontainer-feature.json)).
4. `install.sh` executes:
   - Root check passes (Docker runs as root by default).
   - Installs uv via curl.
   - Finds `python3` is missing → runs `apt-get install python3 python3-pip git`.
   - Finds `pip3` now exists.
   - Runs `pip3 install apm-cli` → **fails** with PEP 668 error (Ubuntu 24.04 rejects it).
   - Detects the PEP 668 error in the output → retries with `--break-system-packages` → succeeds.
   - `apm --version` works.
5. The CLI copies [default-ubuntu-24.sh](test/apm/default-ubuntu-24.sh) into the container and runs it.
6. That script sources `generic-checks.sh` (4 checks) and then adds:
   - "Running on Ubuntu 24.04" (confirms the scenario ran on the right image)
   - "apm is installed in the system Python" (confirms the PEP 668 retry actually installed into system Python, not elsewhere)
7. `reportResults` exits 0 if every `check` passed.

---

## TL;DR mental model

- **Unit tests (bats):** "Does `install.sh`'s logic branch correctly?" Uses fake commands on a locked-down `PATH`. Fast, exhaustive, no Docker.
- **Integration tests (devcontainer CLI):** "Does `install.sh` actually work on real Linux distros?" Uses real Docker images. Slow but proves the thing _really_ installs.
- **generic-checks.sh:** the "apm works" smoke test every scenario inherits.
- **Per-scenario `.sh` files:** prove the distro-specific code path was exercised.
- **scenarios.json:** the matrix — which base image + options to test.
- **test.sh:** the fallback test, currently unused because every scenario has its own script.
