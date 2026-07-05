---
title: "SSL / TLS issues"
description: "Diagnose and fix TLS verification failures during apm install and apm audit."
sidebar:
  order: 4
---

`apm install` and `apm audit` reach out to GitHub, GHES, GitLab, Azure DevOps, and package archives over HTTPS. When the system can't verify the server certificate, the operation fails. This page maps the failure modes to fixes.

Related: [environment variables](../reference/environment-variables/), [install failures](./install-failures/), [security model](../enterprise/security/), [authentication](../getting-started/authentication/).

## Symptoms

Typical errors APM surfaces or passes through from the underlying HTTP/git stack:

```text
[!] TLS verification failed -- APM uses the system trust store by default.
    If you're behind a corporate proxy or firewall, make sure your
    organisation's CA is installed in the OS trust store, or set
    REQUESTS_CA_BUNDLE to a readable PEM bundle and retry.
```

```text
SSLError: HTTPSConnectionPool(host='api.github.com', port=443):
  Max retries exceeded ... [SSL: CERTIFICATE_VERIFY_FAILED]
  certificate verify failed: unable to get local issuer certificate
```

```text
fatal: unable to access 'https://github.example.com/...':
  SSL certificate problem: self-signed certificate in certificate chain
```

```text
fatal: unable to access '...': server certificate verification failed.
  CAfile: none CRLfile: none
```

All of these mean the same thing: the TLS chain presented by the server can't be validated against the trust store APM is using.

## First diagnostic

Decide which of the three categories you are in before changing anything:

[*] **Corporate TLS-intercepting proxy** (Zscaler, Netskope, Palo Alto, Cisco Umbrella, Blue Coat). The server cert is re-signed by an internal CA. Affects every HTTPS host. Fix: trust the corporate CA.

[*] **Self-hosted server with internal CA** (GHES, GitLab self-managed, internal artifact host). Only that one host fails; public hosts like `api.github.com` work fine. Fix: trust the internal CA, often per-host.

[*] **Genuine certificate problem** (expired, wrong hostname, broken chain). Reproduce with `curl -v https://<host>` from the same shell. If `curl` also fails, the problem is upstream of APM.

Re-run the failing command with `--verbose` to see the underlying exception and the host that triggered it:

```bash
apm install --verbose
```

## Default behaviour: the OS trust store

APM verifies HTTPS against the **operating-system trust store** by default (via [`truststore`](https://pypi.org/project/truststore/)), the same source `git` and `curl` use. This applies to `apm install` (in-process) and to the Python-based `llm` child runtime that `apm run` spawns: `apm runtime setup llm` installs `truststore` into the runtime's virtual environment and drops a self-contained bootstrap into it, so that interpreter also verifies against the OS store. So if your corporate CA or TLS-proxy root is installed in the OS trust store (Keychain on macOS, `update-ca-certificates`/`update-ca-trust` on Linux, the Trusted Root store on Windows), APM picks it up with **no configuration** -- importing the CA at the OS level is the recommended fix. The standalone frozen binary honours the system store as well, falling back to its bundled `certifi` set only when the OS store is unavailable.

**Scope caveat:** only the Python-based paths are covered. The Node-based (Copilot) and Rust-based (Codex) child runtimes are **not yet** wired to the OS store (tracked in #2034). Behind a TLS-proxy today, export `NODE_EXTRA_CA_CERTS=/path/to/org-ca-bundle.pem` for the Node runtime (and configure the Codex/Rust runtime's own trust) until #2034 lands.

You only need the steps below when the CA is *not* in the OS store, or you want to pin a specific bundle:

- Setting `REQUESTS_CA_BUNDLE` or `CURL_CA_BUNDLE` makes APM's HTTP layer verify against that bundle instead of the OS store. (`SSL_CERT_FILE` configures the stdlib `ssl` layer but is *not* read by `requests`, so on its own it does not override the HTTP path -- use `REQUESTS_CA_BUNDLE` for that.)
- `APM_DISABLE_TRUSTSTORE=1` restores the legacy behaviour (verify against APM's bundled `certifi` set only).

### Known limitations

- The Node-based (Copilot) and Rust-based (Codex) child runtimes are **not yet covered** by OS-trust propagation and continue to use their own default trust; behind a TLS-inspecting proxy, configure those runtimes' own trust or export `REQUESTS_CA_BUNDLE`/`NODE_EXTRA_CA_CERTS` for them. Tracked in #2034.
- The `llm` child runtime's OS-trust bootstrap needs the runtime venv's interpreter to be **Python 3.10+** (the `truststore` library requires 3.10). On systems where `apm runtime setup llm` builds the venv from a stock **Python 3.9** (for example Apple's `/usr/bin/python3`), `truststore` cannot install and the `llm` child silently falls back to its bundled `certifi` set behind a proxy. Use a Python 3.10+ `python3` on your `PATH` before running setup.
- The initial `pip install` run *during* `apm runtime setup llm` uses pip's **own** certificate resolution, not APM's OS-trust path. Behind a MITM proxy, `pip` may fail to fetch `llm`/`truststore` before the bootstrap is even in place. Export `PIP_CERT=/path/to/org-ca-bundle.pem` (or run `pip config set global.cert /path/to/org-ca-bundle.pem`) before running setup so pip trusts your proxy CA.
- An additive `APM_EXTRA_CA_BUNDLE` (trust the OS store *and* an extra bundle) is not yet available -- use `REQUESTS_CA_BUNDLE` to pin a single bundle for now.
- On Windows, the `schannel` backend has trust caveats.

## Configure trust

APM uses `requests` for HTTP and shells out to `git` for repository operations. Both honour standard environment variables. Set them at the shell or in your profile (`~/.zshrc`, `~/.bashrc`, or the Windows user environment).

### Python HTTP layer

```bash
export REQUESTS_CA_BUNDLE=/path/to/ca-bundle.pem
```

`REQUESTS_CA_BUNDLE` wins for `requests`. `SSL_CERT_FILE` / `SSL_CERT_DIR` cover parts of the stdlib TLS stack, but on their own they are not reliable overrides for the `requests` HTTP path APM uses.

### Git operations

```bash
export GIT_SSL_CAINFO=/path/to/ca-bundle.pem
```

For one host only, prefer per-host git config so you don't widen trust globally:

```bash
git config --global http.https://github.example.com/.sslCAInfo /path/to/internal-ca.pem
```

The trailing slash matters - it scopes the setting to that origin.

### Windows (PowerShell)

```powershell
$env:REQUESTS_CA_BUNDLE = "C:\certs\corporate-ca.pem"
$env:GIT_SSL_CAINFO     = "C:\certs\corporate-ca.pem"

# Persist for the current user:
[Environment]::SetEnvironmentVariable("REQUESTS_CA_BUNDLE", "C:\certs\corporate-ca.pem", "User")
```

### Where do I get the CA file?

Your IT or platform team owns it. Ask for the PEM bundle for the proxy or internal PKI. Do not export it yourself from a browser unless that is the documented procedure - you may capture an intermediate, not the root.

## GHES and GitLab self-managed

Trust alone is not enough for self-hosted forges. APM also needs to know which host to talk to.

**GHES:**

```bash
export GITHUB_HOST=github.example.com
export GITHUB_APM_PAT=<token>
export GIT_SSL_CAINFO=/path/to/internal-ca.pem
```

**GitLab self-managed:**

```bash
export GITLAB_HOST=gitlab.example.com
export APM_GITLAB_HOSTS=gitlab.example.com,gitlab-eu.example.com
export GITLAB_APM_PAT=<token>
export GIT_SSL_CAINFO=/path/to/internal-ca.pem
```

See [environment variables](../reference/environment-variables/) for the full list and [authentication](../getting-started/authentication/) for token scopes.

## Proxies

APM does not implement its own proxy logic. It honours the standard variables, which `requests` and `git` both read:

```bash
export HTTPS_PROXY=http://proxy.example.com:8080
export HTTP_PROXY=http://proxy.example.com:8080
export NO_PROXY=localhost,127.0.0.1,.internal.example.com
```

If the proxy performs TLS interception, you also need the proxy's signing CA in the trust store - see [Configure trust](#configure-trust). Importing the CA into the OS trust store (Keychain on macOS, `update-ca-certificates` on Debian/Ubuntu, `update-ca-trust` on RHEL, the Trusted Root store on Windows) is the most durable fix; consult your OS documentation rather than copying steps from here.

## Verify the fix

```bash
# Python side
python -c "import requests; print(requests.get('https://api.github.com').status_code)"

# Git side
GIT_CURL_VERBOSE=1 git ls-remote https://github.example.com/org/repo.git 2>&1 | grep -i 'ssl\|cert'

# APM end-to-end
apm install --verbose
```

A `200` from `requests`, a successful `ls-remote`, and a clean install confirm trust is wired through every layer APM uses.

## Development-only escape hatches

:::caution[Development only]
The settings below disable certificate verification. They expose every request to trivial man-in-the-middle attacks and **must never be used in CI, on shared machines, or against production data**. Trusting the right CA is always the correct fix.
:::

If you are isolated on a laptop, debugging a local server with a self-signed cert, and you accept the risk:

```bash
export GIT_SSL_NO_VERIFY=true       # git only
export PYTHONHTTPSVERIFY=0          # Python stdlib only; requests ignores this
```

What you lose: any guarantee that the host you reached is the host you intended to reach. Tokens you send may be captured. Packages you download may be tampered with - APM's [built-in security scanning](../enterprise/security/) still runs on the bytes received, but it cannot detect substitution upstream of itself.

Unset both as soon as you are done:

```bash
unset GIT_SSL_NO_VERIFY PYTHONHTTPSVERIFY
```

## Still failing?

[>] Re-run with `--verbose` and capture the full exception chain.
[>] Check `curl -v https://<host>` from the same shell - if it fails, the problem is the system trust store, not APM.
[>] Confirm `REQUESTS_CA_BUNDLE` and `GIT_SSL_CAINFO` point at a readable PEM file (`openssl x509 -in $REQUESTS_CA_BUNDLE -noout -subject` should print a subject line). Note `REQUESTS_CA_BUNDLE` *replaces* the OS store rather than augmenting it (like `git`'s `http.sslCAInfo` and `curl --cacert`), so a bundle missing your proxy root will still fail even though the OS store has it.
[>] If `git`/`curl` succeed but `apm` does not, suspect a **stale `REQUESTS_CA_BUNDLE`** (or `CURL_CA_BUNDLE`) pinning APM to an old bundle that predates the OS store. `unset REQUESTS_CA_BUNDLE CURL_CA_BUNDLE` and retry to let APM fall back to the OS trust store.
[>] If only one host fails, see [GHES and GitLab self-managed](#ghes-and-gitlab-self-managed) and the per-host `git config` recipe above.
[>] If the install proceeds past TLS but then fails, continue at [install failures](./install-failures/).
