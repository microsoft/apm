"""Why does the in-process fetch decline on default Git for Windows (schannel)? Diagnostics only."""
import logging, os, socket, ssl, subprocess, sys, tempfile, time, traceback
from pathlib import Path

logging.basicConfig(level=logging.DEBUG, stream=sys.stdout, format="LOG %(name)s %(levelname)s %(message)s")
for noisy in ("urllib3", "charset_normalizer"):
    logging.getLogger(noisy).setLevel(logging.INFO)
URL = "https://github.com/microsoft/apm"


def sh(cmd, env=None, tail=4000):
    print("\n$", " ".join(cmd), flush=True)
    t = time.monotonic()
    r = subprocess.run(cmd, capture_output=True, text=True, env=env, errors="replace")
    print(f"rc={r.returncode} {time.monotonic() - t:.2f}s")
    print(r.stdout[-tail:])
    print(r.stderr[-tail:], flush=True)
    return r


print("python", sys.version, "platform", sys.platform)
sh(["git", "--version"])
sh(["git", "config", "--show-origin", "--list"])
for key in ("http.sslBackend", "http.schannelCheckRevoke", "http.schannelUseSSLCAInfo", "http.sslCAInfo", "core.autocrlf", "credential.helper"):
    sh(["git", "config", "--show-origin", "--get-all", key])

from apm_cli.cache import inprocess_fetch as f  # noqa: E402
from apm_cli.core.invocation_memo import invocation_scope  # noqa: E402
from apm_cli.deps import anonymous_ref_listing as anon  # noqa: E402

import truststore  # noqa: E402

print("truststore", truststore.__version__)


def handshake(label, context):
    t = time.monotonic()
    try:
        with socket.create_connection(("github.com", 443), timeout=30) as raw:
            with context.wrap_socket(raw, server_hostname="github.com") as tls:
                print(f"TLS {label}: OK {tls.version()} {time.monotonic() - t:.2f}s", flush=True)
    except Exception as exc:
        print(f"TLS {label}: FAIL {type(exc).__name__}: {exc} after {time.monotonic() - t:.2f}s", flush=True)


handshake("os_trust_context (VERIFY_CRL_CHECK_CHAIN)", anon.os_trust_context())
plain = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
handshake("truststore default (no CRL flag)", plain)
leaf_only = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
leaf_only.verify_flags |= ssl.VERIFY_CRL_CHECK_LEAF
handshake("truststore VERIFY_CRL_CHECK_LEAF", leaf_only)


def probe(label, env):
    print(f"\n===== PROBE {label}", flush=True)
    for key in ("GIT_CONFIG_NOSYSTEM", "GIT_CONFIG_GLOBAL", "HOME", "GIT_SSL_CAINFO", "SSL_CERT_FILE"):
        print(f"  {key}={env.get(key)}")
    with invocation_scope():
        try:
            values, child_env, prefixes, entries = f._effective(env)
            print("  effective values:", values)
            print("  entries:", [(e.key, e.value) for e in entries])
        except Exception as exc:
            print("  effective:", type(exc).__name__, exc)
        try:
            print("  GATE OK:", f._gate(URL, env))
        except f._Refuse as exc:
            print("  GATE REFUSE:", exc)
        except Exception:
            traceback.print_exc()
        r = sh(["git", "ls-remote", URL, "HEAD"], env=env)
        sha = (r.stdout.split() or ["0" * 40])[0]
        staged = Path(tempfile.mkdtemp())
        t = time.monotonic()
        try:
            got = f.fetch_worktree(URL, sha, staged, env, ())
            print(f"  FETCH {'HIT' if got else 'NONE'} files={len(got or {})} {time.monotonic() - t:.2f}s", flush=True)
        except Exception:
            traceback.print_exc()


probe("runner env (a default GfW user)", dict(os.environ))
try:
    sys.path.insert(0, os.environ["HARNESS_DIR"])
    from tests.fixtures.perf.offline import isolated_environment

    _, env = isolated_environment(Path(tempfile.mkdtemp()) / "env")
    probe("bench real-fixture isolated env", env)
except Exception:
    traceback.print_exc()

verbose = dict(os.environ, GIT_CURL_VERBOSE="1", GIT_TRACE="1")
r = sh(["git", "ls-remote", URL, "HEAD"], env=verbose, tail=200)
print("\n".join(line for line in r.stderr.splitlines() if any(w in line.lower() for w in ("schannel", "revoc", "ssl", "tls", "certificate")))[-6000:])

pem = ssl.get_server_certificate(("github.com", 443))
cer = Path(tempfile.mkdtemp()) / "github.cer"
cer.write_text(pem)
r = sh(["certutil", "-urlfetch", "-verify", str(cer)], tail=12000)
