---
title: "SSL / TLS issues"
description: "Diagnose and fix TLS verification failures during apm install and apm audit."
sidebar:
  order: 4
---

`apm install` and `apm audit` reach out to GitHub, GHES, GitLab, Azure DevOps, and package archives over HTTPS. When the system can't verify the server certificate, the operation fails. This page maps the failure modes to fixes.

Related: [environment variables](../../reference/environment-variables/), [install failures](../install-failures/), [security model](../../enterprise/security/), [authentication](../../getting-started/authentication/).

## Symptoms

Typical errors APM surfaces or passes through from the underlying HTTP/git stack:

```text
[!] TLS verification failed -- APM uses the system trust store by default.
    If you're behind a corporate proxy or firewall, make sure your
    organisation's CA is installed in the OS trust store, or set
    APM_EXTRA_CA_BUNDLE to a readable PEM bundle and retry.
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

**Fastest fix:** install your corporate CA in the OS trust store and retry. APM picks it up automatically on its Python paths.

APM verifies HTTPS against the **operating-system trust store** by default (via [`truststore`](https://pypi.org/project/truststore/)), the same source `git` and `curl` use. This covers in-process commands such as `apm install` and the standalone frozen binary, with bundled `certifi` as a fallback.

For the Python-based `llm` child runtime, `apm runtime setup llm` installs `truststore` in its virtual environment and adds a self-contained bootstrap. APM refreshes that bootstrap before managed `llm` launches, including after an APM upgrade. Corporate CAs installed in Keychain on macOS, through `update-ca-certificates`/`update-ca-trust` on Linux, or in the Windows Trusted Root store then work without APM-specific configuration. If `APM_EXTRA_CA_BUNDLE` is selected, both the APM parent and this managed Python child retain those native roots and add the certificates from the PEM bundle when `truststore` is available.

For ordinary Python/Requests children launched by `apm run`, APM creates an APM-owned merged snapshot containing bundled `certifi` roots plus the validated extra CA. For Node-based children such as Copilot, APM derives `NODE_EXTRA_CA_CERTS` from an extra-only snapshot. If you explicitly set `NODE_EXTRA_CA_CERTS`, APM preserves your value. Rust-based Codex retains its runtime-owned trust configuration.

You only need the settings below when the CA is *not* in the OS store, or you intentionally want to pin a replacement bundle:

- `APM_EXTRA_CA_BUNDLE` adds a readable PEM bundle to APM's active defaults: OS roots while `truststore` is active, or bundled `certifi` for the Requests fallback. This is the recommended per-shell corporate CA setting.
- `REQUESTS_CA_BUNDLE` or `CURL_CA_BUNDLE` makes APM's Python HTTP layer verify against that bundle instead of the OS store. (`SSL_CERT_FILE` configures the stdlib `ssl` layer but is *not* read by `requests`, so on its own it does not override the HTTP path -- use `REQUESTS_CA_BUNDLE` for that.)
- `APM_DISABLE_TRUSTSTORE=1` disables APM's OS/additive propagation. It does not unset `REQUESTS_CA_BUNDLE` or `CURL_CA_BUNDLE`; an explicit replacement still controls Requests.

The exact order is `REQUESTS_CA_BUNDLE`, `CURL_CA_BUNDLE`, `APM_DISABLE_TRUSTSTORE`, `APM_EXTRA_CA_BUNDLE`, the OS trust store, then bundled `certifi` as the final Requests fallback. APM derives Python and Node child settings only when `APM_EXTRA_CA_BUNDLE` wins that precedence.

### Runtime coverage

| Path | Behaviour when `APM_EXTRA_CA_BUNDLE` is selected |
|---|---|
| APM parent Requests HTTP | Retains native OS roots and adds the selected PEM certificates; falls back to `certifi` plus the extra CA if OS injection is unavailable. |
| Other parent stdlib HTTPS | Retains OS-plus-extra trust while truststore is active; on injection failure it uses the stdlib's own fallback and does not consult `REQUESTS_CA_BUNDLE`. |
| Python/Requests child | Receives an APM-owned merged snapshot containing `certifi` roots plus the validated extra CA. |
| Managed Python `llm` child | Also retains native OS roots through the refreshed bootstrap when `truststore` is available. |
| Node/Copilot child | Receives the validated extra-only snapshot as `NODE_EXTRA_CA_CERTS` unless that native Node variable is already set. |
| Git | Unchanged; configure `GIT_SSL_CAINFO` or Git's native trust settings separately. |
| Rust/Codex | Unchanged; configure the runtime's own trust settings. |

### Known limitations

- Git uses its own trust configuration; `APM_EXTRA_CA_BUNDLE` does not change `git clone`, `git fetch`, or `git ls-remote`. Configure `GIT_SSL_CAINFO` separately when Git needs the same CA.
- Rust-based Codex uses its own runtime trust configuration. APM does not translate `APM_EXTRA_CA_BUNDLE` into a Rust/OpenSSL setting.
- If parent truststore injection is unavailable, the merged `certifi`-plus-extra fallback applies to Requests-based HTTPS. Parent code that directly uses the stdlib `urllib` stack does not read `REQUESTS_CA_BUNDLE` and retains its own default trust in that fallback mode.
- An abnormal termination can leave an `apm_tls_*` snapshot directory beneath `~/.apm/tls/`. After confirming no APM process is using that directory, it can be removed.
- The `llm` child runtime's OS-trust bootstrap needs the runtime venv's interpreter to be **Python 3.10+** (the `truststore` library requires 3.10). On systems where `apm runtime setup llm` builds the venv from a stock **Python 3.9** (for example Apple's `/usr/bin/python3`), `truststore` cannot install, so CAs present only in the OS store are unavailable to that child. Requests-based HTTPS still receives bundled `certifi` plus `APM_EXTRA_CA_BUNDLE`. Use a Python 3.10+ `python3` on your `PATH` before running setup when the child must use native OS trust.
- The initial `pip install` run *during* `apm runtime setup llm` uses pip's **own** certificate resolution, not APM's OS-trust path. Behind a MITM proxy, `pip` may fail to fetch `llm`/`truststore` before the bootstrap is even in place. Export `PIP_CERT=/path/to/org-ca-bundle.pem` (or run `pip config set global.cert /path/to/org-ca-bundle.pem`) before running setup so pip trusts your proxy CA.

## Configure trust

APM's primary Python HTTP paths use `requests`, a small number of metadata paths use the stdlib `urllib` stack, and repository operations shell out to `git`. Their fallback trust settings differ as described above. Set them at the shell or in your profile (`~/.zshrc`, `~/.bashrc`, or the Windows user environment).

### Python HTTP layer

```bash
export APM_EXTRA_CA_BUNDLE=/path/to/corporate-ca.pem
```

This retains native OS roots and adds the selected PEM certificates. If the APM parent cannot inject OS trust, Requests-based HTTPS retains the additive certificates over its `certifi` fallback. Set the variable in the environment that launches `apm`; an inline assignment inside an `apm.yml` shell command remains shell-owned and is not translated into `NODE_EXTRA_CA_CERTS`. APM validates the bundle before using it: the file must be regular, readable, non-empty, no larger than 8 MiB, certificate-only ASCII PEM, and contain at least one certificate. Private-key blocks are rejected before any child snapshot is created. Before child launch, APM copies the validated bytes into an APM-owned per-process directory beneath `~/.apm/tls/`, so replacing the source file after validation cannot change the child's trust. Those snapshots are removed when the APM process exits normally. An invalid selected bundle fails closed, so the command or child launch stops rather than silently weakening or bypassing certificate verification.

Use a replacement bundle only when you intend to pin the entire Python trust set:

```bash
export REQUESTS_CA_BUNDLE=/path/to/ca-bundle.pem
```

`REQUESTS_CA_BUNDLE` wins for `requests`. `SSL_CERT_FILE` / `SSL_CERT_DIR` cover parts of the stdlib TLS stack, but on their own they are not reliable overrides for the `requests` HTTP path APM uses.

### Node children

Normally, set only `APM_EXTRA_CA_BUNDLE`; APM passes its validated extra-only snapshot to Node children through `NODE_EXTRA_CA_CERTS`. To use a different Node-specific bundle, set it explicitly:

```bash
export NODE_EXTRA_CA_CERTS=/path/to/node-ca-bundle.pem
```

APM never overwrites this explicit native Node setting.

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
$env:APM_EXTRA_CA_BUNDLE = "C:\certs\corporate-ca.pem"
$env:GIT_SSL_CAINFO     = "C:\certs\corporate-ca.pem"

# Persist for the current user:
[Environment]::SetEnvironmentVariable("APM_EXTRA_CA_BUNDLE", "C:\certs\corporate-ca.pem", "User")
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

See [environment variables](../../reference/environment-variables/) for the full list and [authentication](../../getting-started/authentication/) for token scopes.

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
# APM Python HTTPS path
APM_LOG_LEVEL=DEBUG apm install

# Git side
GIT_CURL_VERBOSE=1 git ls-remote https://github.example.com/org/repo.git 2>&1 | grep -i 'ssl\|cert'
```

The debug output identifies whether APM selected the OS trust store, additive bundle, replacement bundle, or `certifi` fallback. That line plus a clean install confirms APM's in-process Python path; a successful `ls-remote` confirms Git trust separately. Verify the managed `llm` child and Node child with their normal HTTPS-backed commands.

## Development-only escape hatches

:::caution[Development only]
The settings below disable certificate verification. They expose every request to trivial man-in-the-middle attacks and **must never be used in CI, on shared machines, or against production data**. Trusting the right CA is always the correct fix.
:::

If you are isolated on a laptop, debugging a local server with a self-signed cert, and you accept the risk:

```bash
export GIT_SSL_NO_VERIFY=true       # git only
export PYTHONHTTPSVERIFY=0          # Python stdlib only; requests ignores this
```

What you lose: any guarantee that the host you reached is the host you intended to reach. Tokens you send may be captured. Packages you download may be tampered with - APM's [built-in security scanning](../../enterprise/security/) still runs on the bytes received, but it cannot detect substitution upstream of itself.

Unset both as soon as you are done:

```bash
unset GIT_SSL_NO_VERIFY PYTHONHTTPSVERIFY
```

## Still failing?

[>] Re-run with `--verbose` and capture the full exception chain.
[>] Check `curl -v https://<host>` from the same shell - if it fails, the problem is the system trust store, not APM.
[>] Confirm `APM_EXTRA_CA_BUNDLE` points at a readable, non-empty regular file no larger than 8 MiB and contains certificate-only ASCII PEM. APM rejects malformed bundles and private-key blocks rather than silently ignoring them. Unset it to return to normal OS trust, or replace it with the correct CA file.
[>] Confirm `REQUESTS_CA_BUNDLE` and `GIT_SSL_CAINFO` point at a readable PEM file (`openssl x509 -in $REQUESTS_CA_BUNDLE -noout -subject` should print a subject line). Note `REQUESTS_CA_BUNDLE` *replaces* the OS store rather than augmenting it (like `git`'s `http.sslCAInfo` and `curl --cacert`), so a bundle missing your proxy root will still fail even though the OS store has it.
[>] If `git`/`curl` succeed but `apm` does not, check the precedence settings. `APM_DISABLE_TRUSTSTORE` bypasses OS and additive trust; a stale `REQUESTS_CA_BUNDLE` (or `CURL_CA_BUNDLE`) pins APM to a replacement bundle. Unset those variables and retry to let `APM_EXTRA_CA_BUNDLE`, or the OS store when it is unset, take effect.
[>] If only one host fails, see [GHES and GitLab self-managed](#ghes-and-gitlab-self-managed) and the per-host `git config` recipe above.
[>] If the install proceeds past TLS but then fails, continue at [install failures](../install-failures/).
