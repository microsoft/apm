"""Lifecycle script executors -- one per action type.

Each executor isolates failures: it catches all exceptions internally
and logs failures in verbose mode only (using ``[i]`` ASCII symbol).
``http`` scripts dispatch in a background daemon thread; ``command``
scripts run synchronously and can delay the operation up to their timeout.

Two script types (Copilot CLI aligned):

- ``command`` -- shell command via subprocess, event JSON on **stdin**
- ``http``    -- HTTPS POST with JSON body, env-var expansion in headers

Script output is appended to ``~/.apm/logs/scripts.log`` (with known
credential values redacted) so administrators can audit what scripts
produce without enabling verbose CLI output.
"""

from __future__ import annotations

import contextlib
import ipaddress
import logging
import math
import os
import re
import signal
import socket
import stat
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse, urlsplit, urlunsplit

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX
    fcntl = None  # type: ignore[assignment]

if TYPE_CHECKING:
    from apm_cli.core.command_logger import CommandLogger
    from apm_cli.core.lifecycle_scripts import LifecycleEvent, ScriptEntry

_logger = logging.getLogger(__name__)

# POSIX permission bits (group/other access) are only meaningful on POSIX.
# On Windows ``os.fstat`` reports 0o666/0o444-style modes whose 0o077 bits are
# always set, so the world-readable tamper check in ``_append_to_script_log``
# must be POSIX-gated -- otherwise every log write "self-heals" the file and
# then returns without writing, leaving an empty ``scripts.log``.
_POSIX_PERMS = os.name == "posix"

# Fallback timeouts when script entry does not specify one.
_DEFAULT_HTTP_TIMEOUT = 10
_DEFAULT_COMMAND_TIMEOUT = 30

# Command scripts slower than this (seconds) earn a visible warning, since
# they run synchronously and delay the user-facing operation.
_SLOW_SCRIPT_THRESHOLD_SEC = 5.0

# Pattern for $VAR or ${VAR} expansion in header values.
_ENV_VAR_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)")

# Credential variable denylist -- these must never be expanded into HTTP
# headers or leaked to script subprocesses. The credential token must end
# the name, but we also accept a trailing plural ``S`` and an optional
# ``_ID``/``_IDS`` qualifier so real-world families are caught:
#   - plurals: ...TOKENS, ...KEYS, ...SECRETS, ...CREDENTIALS, ...PATS
#   - qualified: AWS_ACCESS_KEY_ID, ..._KEY_IDS
#   - canonical: GOOGLE_APPLICATION_CREDENTIALS (CREDENTIAL + plural S)
#   - key passphrases: GPG_PASSPHRASE, SSH_KEY_PASSPHRASE (PASSPHRASE is the
#     same secret class as PASSWORD/PASSWD/PWD, which are already covered)
#   - bare PASS shorthand: DB_PASS, MYSQL_PASS, REDIS_PASS, SMTP_PASS,
#     ROOT_PASS (ubiquitous in docker-compose / 12-factor stacks). The bare
#     ``PASS`` token is anchored to a ``_``-or-start boundary (``(?:^|_)PASS``)
#     so the real ``*_PASS`` family is swept WITHOUT catching the common-
#     English suffix words SURPASS / BYPASS / COMPASS / TRESPASS / PASSAGE.
#   - one-time passcodes: MFA_PASSCODE, VPN_PASSCODE, *_PASSCODE (a dedicated
#     ``PASSCODE`` token; the ``(?:^|_)PASS`` arm cannot reach it because the
#     trailing ``CODE`` blocks the end anchor). Same password secret class.
#     Benign ``*_CODE`` names (BARCODE, ZIPCODE, QRCODE, STATUS_CODE,
#     COUNTRY_CODE, ERROR_CODE) never contain ``PASSCODE`` and stay benign.
#   - bare JWT bearer tokens: JWT, ACCESS_JWT, REFRESH_JWT, *_JWT (a trailing-
#     anchored ``JWT`` token). The ``eyJ...`` value matches no structural
#     masker, so a bare *_JWT name is the only signal; benign LEADING-JWT
#     config names (JWT_ALGORITHM, JWT_ISSUER, JWT_AUDIENCE) keep a non-token
#     tail and so stay benign. JWT_SECRET is already swept via SECRET.
#   - wallet seed phrases: MNEMONIC, *_MNEMONIC, *_SEED_PHRASE, *_RECOVERY_PHRASE,
#     *_BACKUP_PHRASE (the hardhat / foundry / truffle deploy lifecycle secret --
#     a single token that, if leaked, drains a wallet; same secret class as
#     PASSWORD). RECOVERY_PHRASE / BACKUP_PHRASE are the MetaMask / hardware-wallet
#     spellings; the curated *_SEED wallet names (WALLET_SEED, MASTER_SEED,
#     DERIVATION_SEED) live in _CREDENTIAL_BLOB_NAMES so a bare ``SEED`` token does
#     NOT sweep the legitimate RNG seeds a build needs (PYTHONHASHSEED, RANDOM_SEED).
# The trailing ``S?`` does not over-match unrelated names (e.g. PATH keeps
# a stray ``H`` after PAT and so never matches; TRACE_ID has no credential
# token before ``_ID`` and is left alone). The trailing rotation tail
# ``(?:_?(?:OLD|NEW|PREV|CURRENT))?(?:_?V[0-9]+)?[_0-9]*`` lets an ENUMERATED /
# ROTATED credential keep matching even when a digit, an underscore, a rotation
# word (DB_PASS_OLD, PASSWORD_NEW), or a version tag (DB_PASSV2, API_KEYV2) now
# follows the token -- without it a single trailing word/digit defeats the whole
# sweep. The rotation words and ``V<n>`` tag only ever apply AFTER a credential
# token, so RENEW / PREVIEW / CURRENT alone (no token) stay benign, and
# SURPASS / PASSAGE (letters after PASS) stay benign via the ``(?:^|_)PASS``
# boundary anchor.
# The looser tokens (KEY, PAT, ...) keep their suffix-match behaviour as a
# deliberate fail-safe: over-redacting a non-secret env var is harmless,
# under-redacting a secret is not.
# The trailing ENCODING tail closes a real, widely-deployed CI convention: a
# whole secret (a GCP service-account JSON, a TLS private key, a JWT signing
# secret, an RFC-6238 TOTP/MFA base32 seed, a Solana/web3 base58 key) is
# serialized or base-encoded into ONE single-line env var named ``<x>_BASE64`` /
# ``<x>_B64`` / ``<x>_BASE32`` / ``<x>_JSON`` / ``<x>_BASE58`` so it survives a
# flat secret store -- e.g. ``GCP_SA_KEY_BASE64``, ``TLS_PRIVATE_KEY_BASE64``,
# ``JWT_SECRET_B64``, ``GOOGLE_CREDENTIALS_JSON``, ``GCP_CREDENTIALS_YAML``,
# ``SOLANA_PRIVATE_KEY_BASE58``, ``TOTP_SECRET_BASE32``. The tail therefore spans
# three families: binary base encodings (BASE64/BASE32/BASE58/BASE62/B64/B32/HEX/
# ASCII85/A85/Z85/URLSAFE), key armor (PEM/DER/ASC), and raw structured-text
# serialization (JSON/YAML/YML/TOML -- how GCP/terraform inline a credentials
# blob). The credential token (KEY/TOKEN/SECRET/CREDENTIAL/AUTHORIZATION) is an
# INFIX and the name ENDS in the benign tail, which the bare suffix anchor could
# not express. The tail only ever applies AFTER a credential token, so a
# TOKEN-LESS asset (IMAGE_BASE64, LOGO_B64, CONFIG_JSON, PACKAGE_JSON, COLOR_HEX)
# never matches and still reaches the child env.
# ``(?:^|_)COOKIE`` is start-anchored like ``PASS``: a session/auth COOKIE is a
# bearer credential (SESSION_COOKIE / AUTH_COOKIE / COOKIE / COOKIES) but the
# benign cookie *config* a script reads (COOKIE_DOMAIN / COOKIE_NAME /
# COOKIE_PATH / COOKIE_SECURE -- COOKIE is a PREFIX there) must survive, so the
# token only matches when COOKIE is the trailing segment.
_CREDENTIAL_DENYLIST = re.compile(
    r"(?:(?:^|_)PASS|PASSCODE|TOKEN|SECRET|PAT|KEY|PASSWORD|PASSWD|PASSPHRASE|PWD"
    r"|CREDENTIAL|AUTHTOKEN|AUTHORIZATION|JWT|MNEMONIC|SEED_PHRASE|RECOVERY_PHRASE|BACKUP_PHRASE"
    r"|(?:^|_)COOKIE)"
    r"S?(?:_IDS?)?(?:_?(?:OLD|NEW|PREV|CURRENT))?(?:_?V[0-9]+)?"
    r"(?:_?(?:BASE64|BASE32|BASE58|BASE62|B64|B32|HEX|PEM|DER|ASCII85|A85|Z85|URL_?SAFE|JSON|YAML|YML|TOML|ASC))?"
    # A descriptive trailing word (AUTHORIZATION_HEADER, SECRET_VALUE,
    # TOKEN_VALUE, PRIVATE_KEY_DATA) must not break the end-anchor and let an
    # already-tokenised credential NAME escape redaction. Only appends after a
    # credential token already matched, so benign names that merely END in one
    # of these words (CONTENT_TYPE_HEADER, MAX_VALUE, USER_DATA) carry no token
    # to anchor on and stay unmatched.
    r"(?:_(?:HEADERS?|VALUES?|DATA))?[_0-9]*$",
    re.IGNORECASE,
)

# Bare shell variables that end in a denylist token (``PWD``) yet hold no
# secret -- they are the current/previous working directory. Without this
# exemption the ``PWD`` token would sweep the ubiquitous ``$PWD``/``$OLDPWD``
# out of every command env and corrupt logs that echo a path.
_DENYLIST_EXEMPT: frozenset[str] = frozenset({"PWD", "OLDPWD"})

# Credential *blobs* whose NAME ends in a benign suffix (CONFIG / AUTH /
# STRING / BASE / DSN) that the suffix-token regex cannot express, yet whose
# VALUE is a secret: base64 registry auth (DOCKER_AUTH_CONFIG), a basic-auth
# header (BASIC_AUTH), a DSN with an embedded password (*_CONNECTION_STRING,
# *_DSN, *_CONN_STR), or a framework master secret whose credential token is
# an infix (SECRET_KEY_BASE -- the Rails master secret; the suffix anchor sees
# only the benign _BASE tail and its SECRET_KEY sibling is masked, so the exact
# name is curated here). Exact-name membership keeps benign siblings (KEYBASE_*,
# CODEBASE_*, DATABASE, RELEASE_BASE) unaffected. The ``_AUTH`` suffix sweeps
# the real npm/registry auth vars (NPM_CONFIG__AUTH -- the actual ``npm_config__auth``
# env name -- and ARTIFACTORY_AUTH) that the curated NPM_AUTH/REGISTRY_AUTH
# names alone miss; over-redacting a non-secret ``*_AUTH`` name is harmless.
# The curated wallet-seed names (WALLET_SEED, MASTER_SEED, DERIVATION_SEED) are
# listed here -- rather than as a bare ``SEED`` regex token -- precisely so the
# legitimate RNG seeds a build legitimately needs (PYTHONHASHSEED, RANDOM_SEED,
# TEST_SEED) are NOT stripped from the child env (which would break reproducible
# builds); exact-name membership masks the wallet secret without that collision.
_CREDENTIAL_BLOB_NAMES: frozenset[str] = frozenset(
    {
        "DOCKER_AUTH_CONFIG",
        "BASIC_AUTH",
        # ``AUTH`` alone is intentionally NOT a denylist token (too FP-prone),
        # so the bare-stem Authorization-header carriers AUTH_HEADER /
        # AUTH_HEADERS -- whose VALUE is a Basic/Bearer/opaque header secret --
        # have no token to anchor the descriptive-suffix regex on. Curate the
        # exact names so the header value is masked in scripts.log and stripped
        # from the child env. Benign siblings (AUTH_HEADER_NAME, CONTENT_TYPE_HEADER)
        # are not exact members and survive.
        "AUTH_HEADER",
        "AUTH_HEADERS",
        "NPM_AUTH",
        "REGISTRY_AUTH",
        "SECRET_KEY_BASE",
        "DSN",
        "CONN_STR",
        "WALLET_SEED",
        "MASTER_SEED",
        "DERIVATION_SEED",
        # Secret-manager CLI unlock-session keys. ``bw unlock``/``op signin``/
        # fastlane emit an opaque session blob (BW_SESSION / OP_SESSION /
        # FASTLANE_SESSION) that grants vault/keychain access for the shell's
        # lifetime -- a bearer to every other secret, with no token marker in the
        # value (no structural masker fires). Exact-name membership strips it from
        # the child env (a lifecycle script should not silently inherit an unlocked
        # vault session; opt in via allowedEnvVars), masks it in scripts.log, and
        # refuses it for $VAR header expansion. ``OP_SESSION_<account>`` (1Password
        # keys the session by account suffix) is handled by _CREDENTIAL_NAME_PREFIX.
        "BW_SESSION",
        "OP_SESSION",
        "FASTLANE_SESSION",
    }
)
# A base64/hex-encoded CONFIG blob keyed by the bare ``KUBE_CONFIG`` /
# ``KUBECONFIG`` stem (no credential token, so the denylist cannot see it) is
# the kubeconfig content secret -- it embeds a client cert / bearer token. The
# encoding tail is REQUIRED here so the bare ``KUBECONFIG`` *path* var (which
# merely names a file, like PWD) is NOT stripped from the child env and break
# ``kubectl``. The existing blob suffixes also accept an optional encoding tail
# so ``DOCKER_AUTH_CONFIG_BASE64`` / ``*_DSN_B64`` are caught the same way.
_CREDENTIAL_BLOB_SUFFIX = re.compile(
    r"(?:"
    r"(?:_AUTH|_AUTH_CONFIG|_CONNECTION_STRING|CONNECTIONSTRING|_DSN|_CONN_STR)"
    r"(?:_?(?:BASE64|BASE32|B64|B32|HEX|PEM|DER|ASC))?"
    r"|KUBE_?CONFIG_?(?:BASE64|BASE32|B64|B32|HEX)"
    # Binary private-key CONTAINER names (Android app-signing + JVM/Windows
    # code-signing): the signing key lives inside a keystore/PKCS#12 blob that
    # CI base64-encodes into one var (ANDROID_KEYSTORE_BASE64, SIGNING_KEYSTORE,
    # WINDOWS_PFX_BASE64, APPLE_CERT_P12, SERVER_JKS_BASE64). ``KEY`` is a token
    # but only as the compound ``KEY+STORE`` -- the denylist tail cannot consume
    # ``STORE`` so the token never reaches ``$``; PFX/P12/JKS carry no token and
    # the value is binary base64 (no PEM armor / ``=`` key / URL) so no value-
    # shape masker catches it either. The KEY_?STORE arm matches a bare keystore
    # tail (RELEASE_KEYSTORE, SIGNING_KEY_STORE) OR a keystore token followed by
    # an encoding tail anywhere to the end (KEYSTORE_FILE_BASE64), so the encoded
    # blob is caught while the benign PATH/FILE/ALIAS file vars (no encoding
    # tail) and TRUSTSTORE_* (public certs, no KEY) keep their suffixes and stay
    # in the child env.
    r"|(?:_PFX|_P12|_PKCS12|_JKS|KEY_?STORE)(?:[A-Z0-9_]*?(?:BASE64|BASE32|B64|B32|HEX|DER))?"
    r")$",
    re.IGNORECASE,
)

# Some ecosystems key a credential by HOST as a NAME SUFFIX, so the credential
# token sits in a fixed PREFIX rather than the suffix the denylist anchors on.
# Terraform Cloud / Enterprise reads ``TF_TOKEN_<host>`` (dots -> ``_``, e.g.
# ``TF_TOKEN_app_terraform_io``) as the API bearer for ``terraform init``; the
# ``_TOKEN`` is an infix, so the suffix-anchored denylist misses it and the
# bearer both leaks to scripts.log AND expands into an outbound HTTP header with
# no warning. A START-anchored prefix match closes both: nothing benign begins
# with ``TF_TOKEN_`` (Terraform's other vars are ``TF_VAR_*`` / ``TF_CLI_*`` /
# ``TF_LOG*``), so there is zero false-positive risk. The 1Password CLI keys its
# unlock session by account as ``OP_SESSION_<account>``; the ``OP_SESSION_``
# namespace is 1Password-owned (nothing benign begins with it), so the same
# START-anchored treatment strips/masks/refuses every per-account session blob.
_CREDENTIAL_NAME_PREFIX = re.compile(r"^(?:TF_TOKEN_|OP_SESSION_)", re.IGNORECASE)

# Minimum value length that is substring-masked in the audit log. Short
# values (e.g. a 4-char ``test``) are common substrings of ordinary words
# and masking them would corrupt unrelated log text; real credential
# values are long, so an 8-char floor catches secrets without false hits.
_MIN_REDACT_LEN = 8


def _matches_credential(name: str) -> bool:
    """True if *name* conventionally holds a credential value.

    Combines the suffix-token regex with a curated set of credential-blob
    names whose benign suffix the regex cannot express. Bare shell
    working-directory vars (``PWD`` / ``OLDPWD``) are exempt.
    """
    upper = name.upper()
    if upper in _DENYLIST_EXEMPT:
        return False
    if _CREDENTIAL_DENYLIST.search(name):
        return True
    if _CREDENTIAL_NAME_PREFIX.search(name):
        return True
    if upper in _CREDENTIAL_BLOB_NAMES:
        return True
    return bool(_CREDENTIAL_BLOB_SUFFIX.search(name))


# Known APM auth variables that must NEVER be expanded even when listed in
# allowedEnvVars -- these are the credentials APM itself uses and must not
# leak to HTTP endpoints or subprocess stdin regardless of opt-in.
_NEVER_EXPAND: frozenset[str] = frozenset(
    {
        "GITHUB_APM_PAT",
        "GITHUB_TOKEN",
        "GH_TOKEN",
        "ADO_APM_PAT",
    }
)


def _is_denylisted(name: str, allowed: frozenset[str]) -> bool:
    """True if *name* is a credential var NOT explicitly allowlisted.

    _NEVER_EXPAND vars are always blocked regardless of allowedEnvVars.
    """
    if name in _NEVER_EXPAND:
        return True
    if name in allowed:
        return False
    return _matches_credential(name)


def _redact_secrets(text: str) -> str:
    """Mask any denylisted env-var *values* appearing in script output.

    Scripts frequently echo their environment; without this, a command
    that prints ``$ANALYTICS_TOKEN`` would persist the cleartext secret
    into ``~/.apm/logs/scripts.log``. We replace raw occurrences of every
    denylisted variable's value with ``[REDACTED]``.

    Replacement is a raw substring match (not boundary-aware): the value
    is a KNOWN secret, so it must be masked even when glued to adjacent
    word characters. A length floor (``_MIN_REDACT_LEN``) keeps short,
    common values from corrupting unrelated text.

    Values are redacted LONGEST-FIRST so a shorter credential that is a
    prefix/substring of a longer one cannot fragment the longer value and
    leak its tail (e.g. token ``abcd1234`` and ``abcd1234efgh5678`` -- if
    the short one ran first it would split the long one, leaving
    ``efgh5678`` in cleartext).
    """
    if not text:
        return text
    secrets: list[str] = []
    for name, value in os.environ.items():
        if not value or len(value) < _MIN_REDACT_LEN or not _matches_credential(name):
            continue
        secrets.append(value)
        # subprocess text=True runs in universal-newline mode, which rewrites
        # CRLF and lone CR to LF in captured stdout/stderr. A credential whose
        # value carries a carriage return (a CRLF-sourced .env var, a Windows
        # PEM/base64 blob) therefore diverges from this raw os.environ needle,
        # so the exact str.replace below would miss and leak the cleartext to
        # scripts.log. Mask the newline-normalized form too (same transform the
        # subprocess applies); keep the raw form so the command/target string,
        # which is never newline-translated, still matches.
        normalized = value.replace("\r\n", "\n").replace("\r", "\n")
        if normalized != value and len(normalized) >= _MIN_REDACT_LEN:
            secrets.append(normalized)
    redacted = text
    for value in sorted(set(secrets), key=len, reverse=True):
        redacted = redacted.replace(value, "[REDACTED]")
    return redacted


def _redact_url_credentials(url: str) -> str:
    """Strip ``user:password@`` userinfo from a URL before it is logged.

    Deterministic log hygiene for a field APM itself writes to
    ``scripts.log`` (the http event target): the URL is parsed with
    ``urllib`` and the authority rebuilt from host (+ port) only. This is
    NOT a shape-scan of script output -- it sanitizes one APM-owned value.
    The credential-bearing URL is still used for the actual dispatch; only
    the LOGGED form is sanitized.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    if "@" not in (parts.netloc or ""):
        return url
    try:
        host = parts.hostname or ""
        port = parts.port
    except ValueError:
        return url
    if host and ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = f"{host}:{port}" if port is not None else host
    return urlunsplit(parts._replace(netloc=netloc))


# -- Script output log -----------------------------------------------------

# Per-entry stdout/stderr is truncated to this many characters before being
# written, so a single lifecycle command that prints a large blob cannot
# bloat the audit log (or be used for a local disk-fill DoS).
_MAX_LOG_FIELD_CHARS = 4096

# When the log grows past this size it is rotated to ``scripts.log.1`` so it
# never grows without bound across many noisy events.
_MAX_LOG_BYTES = 5 * 1024 * 1024

# Hard ceiling on how much command-script stdout/stderr is read into memory.
# ``proc.communicate()`` would otherwise buffer the ENTIRE child output (a
# runaway or hostile lifecycle script printing GiBs OOMs the installer long
# before the per-field log truncation -- which runs only on already-resident
# text -- or the timeout/killpg reap can fire). The bounded reader caps each
# stream at this many characters, discards the rest, and SIGKILLs the process
# group so the installer's memory stays flat regardless of how much a script
# prints. Comfortably above ``_MAX_LOG_FIELD_CHARS`` so the audit log is never
# starved of legitimate output.
_MAX_CAPTURE_CHARS = 1024 * 1024

# Grace period (seconds) after a clean shell exit for the stdout/stderr drain
# threads to hit EOF on their own. A well-behaved script -- or one that
# backgrounds a daemon with redirected stdio -- closes the capture pipes well
# within this window, so we never reap it. Only a backgrounded GROUP MEMBER
# still holding the original capture pipes keeps a drain alive past the grace;
# that is the wedge we reap. Kept short so a real wedge costs a fraction of a
# second instead of the full 5s join budget.
_CAPTURE_DRAIN_GRACE = 0.5


def _get_scripts_log_path() -> Path:
    """Return the path to the scripts output log file."""
    apm_home = os.environ.get("APM_HOME")
    base = Path(apm_home) if apm_home else Path.home() / ".apm"
    return base / "logs" / "scripts.log"


_LINE_BREAK_ESCAPES = {
    "\r": "\\r",
    "\n": "\\n",
}
# Every code point str.splitlines() treats as a line boundary. Escaping only
# CR/LF is insufficient: a splitlines()-based log consumer (a very common
# Python idiom) also splits on VT, FF, the FS/GS/RS information separators,
# NEL, LS, and PS -- so an attacker field carrying any of these would still
# forge a column-0 audit record for such a parser. We neutralize the full set
# in one choke point so stdout/stderr (via _truncate_log_field) and target
# (via safe_target) are all covered.
_LINE_BREAK_PATTERN = re.compile("[\r\n\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029]")


def _neutralize_newlines(text: str) -> str:
    """Escape every line-boundary code point so a log field cannot forge a line.

    The scripts.log audit trail exists to catch malicious scripts, so a field
    derived from attacker-controlled stdout/stderr (or a multi-line command)
    must never contain a raw line break -- otherwise a script could emit output
    that, at column 0, is byte-indistinguishable from a genuine
    ``[ts] event=... status=ok`` entry and forge or bury audit records. The
    header path already strips CR/LF from expanded values; this closes the same
    gap for the output fields, covering the complete ``str.splitlines()``
    boundary set (not just CR/LF) so splitlines-based consumers cannot be
    fooled either.
    """

    def _escape(match: re.Match[str]) -> str:
        char = match.group()
        readable = _LINE_BREAK_ESCAPES.get(char)
        if readable is not None:
            return readable
        code = ord(char)
        return f"\\x{code:02x}" if code <= 0xFF else f"\\u{code:04x}"

    return _LINE_BREAK_PATTERN.sub(_escape, text)


def _escape_header_field(text: str) -> str:
    """Neutralize a ``key=value`` lookalike inside a header-line field.

    The scripts.log header is a single space-delimited ``key=value`` line
    (``[ts] event=... type=... target=<cmd> status=... exit_code=...``).
    ``target`` is the effective command -- attacker-controlled for a
    dependency-supplied entry -- and sits MID-LINE among the fixed fields.
    Without neutralization a command like ``evil status=ok event=deploy``
    smuggles forged ``status=``/``event=`` tokens onto the header line, so a
    first-match ``status=(\\S+)`` regex, a whitespace-tokenized key=value
    parser, or a logfmt consumer reads the attacker's value instead of the
    real one. Escaping ``=`` to ``\\x3d`` means no ``<word>=`` lookalike can
    appear in the field, while spaces (and thus benign multi-word commands
    like ``echo hi``) stay readable. Newlines are handled separately by
    :func:`_neutralize_newlines`.
    """
    return text.replace("=", "\\x3d")


def _truncate_log_field(text: str) -> str:
    """Clamp a stdout/stderr field to a bounded length for the log.

    Newlines are neutralized first so a single log event occupies a bounded,
    single logical region no attacker field can break out of.
    """
    text = _neutralize_newlines(text)
    if len(text) <= _MAX_LOG_FIELD_CHARS:
        return text
    return text[:_MAX_LOG_FIELD_CHARS] + " ...[truncated]"


def _rotate_log_if_large(log_path: Path) -> None:
    """Rotate the log to ``.1`` once it exceeds the size cap.

    Serialized across processes via an exclusive ``fcntl`` lock on a dedicated
    lock file, with a DOUBLE-CHECKED size test INSIDE the critical section.
    Without the lock two concurrent installers can both observe ``size >= cap``
    and both ``os.replace`` -- the second rename clobbers the freshly-rotated
    ``scripts.log.1`` and destroys ~5 MiB of audit trail (a hostile package can
    flood the log to force the crossing and race the rename to bury its own
    record). The re-stat under the lock makes the racing writer see the now-
    small log and skip the rename, so no record is lost below the two-file
    retention capacity. The unlocked pre-check keeps the common (under-cap)
    append path lock-free; only a genuine crossing pays for the lock.
    """
    with contextlib.suppress(OSError):
        st = log_path.stat()
        if not stat.S_ISREG(st.st_mode) or st.st_size < _MAX_LOG_BYTES:
            return

    rotated = log_path.with_name(log_path.name + ".1")
    if fcntl is None:  # pragma: no cover - non-POSIX best-effort
        with contextlib.suppress(OSError):
            if log_path.stat().st_size >= _MAX_LOG_BYTES:
                os.replace(log_path, rotated)
        return

    lock_path = log_path.with_name(log_path.name + ".lock")
    lock_flags = os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    try:
        lock_fd = os.open(lock_path, lock_flags, 0o600)
    except OSError:
        return
    try:
        # Non-blocking: rotation is best-effort and runs synchronously on the
        # install firing path, so it must NEVER block. A foreign holder of the
        # predictable lock path (or a genuine concurrent rotator) means another
        # process is already rotating -- skip this pass. The double-checked
        # re-stat below makes skipping safe: a later appender re-stats and
        # rotates if the log is still oversized.
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return
        with contextlib.suppress(OSError):
            if log_path.stat().st_size >= _MAX_LOG_BYTES:
                os.replace(log_path, rotated)
    except OSError:
        pass
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def _append_to_script_log(
    event_name: str,
    script_type: str,
    target: str,
    *,
    stdout: str = "",
    stderr: str = "",
    status: str = "ok",
    exit_code: int | None = None,
) -> None:
    """Append a timestamped entry to the scripts log file.

    Creates ``~/.apm/logs/`` (mode ``0700``) on first write and opens the
    log ``0600`` with ``O_NOFOLLOW`` so it cannot be world-readable nor
    redirected through a pre-planted symlink. Per-entry output is truncated
    and the file is size-rotated. Errors are silently swallowed -- logging
    must never break the CLI.
    """
    try:
        log_path = _get_scripts_log_path()
        log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        _rotate_log_if_large(log_path)

        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        safe_target = _escape_header_field(_neutralize_newlines(_redact_secrets(target)))
        lines = [
            f"[{ts}] event={event_name} type={script_type} target={safe_target} status={status}"
        ]
        if exit_code is not None:
            lines[0] += f" exit_code={exit_code}"
        if stdout and stdout.strip():
            lines.append(f"  stdout: {_truncate_log_field(_redact_secrets(stdout).strip())}")
        if stderr and stderr.strip():
            lines.append(f"  stderr: {_truncate_log_field(_redact_secrets(stderr).strip())}")
        lines.append("")  # blank line separator

        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_APPEND
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        excl_flags = flags | getattr(os, "O_EXCL", 0)
        payload = ("\n".join(lines) + "\n").encode("utf-8")
        try:
            fd = os.open(log_path, flags, 0o600)
        except OSError:
            # A no-reader FIFO (ENXIO) or a planted DIRECTORY (EISDIR/EPERM) at
            # the log path fails the open; self-heal by removing the hostile node
            # and O_EXCL-recreating a fresh 0600 file so the audit log is not
            # permanently blackholed. unlink clears a file/FIFO/symlink; rmdir
            # clears an empty dir (unlink raises IsADirectoryError/PermissionError
            # on a dir, so a ``mkdir scripts.log`` plant would otherwise blackout).
            with contextlib.suppress(FileNotFoundError):
                try:
                    os.unlink(log_path)
                except (IsADirectoryError, PermissionError):
                    with contextlib.suppress(OSError):
                        os.rmdir(log_path)
            fd = os.open(log_path, excl_flags, 0o600)
        try:
            # Fail closed if the log path is not a regular file OR a pre-planted
            # regular file carries group/other permission bits. ``O_NOFOLLOW``
            # rejects a symlink swap but NOT a FIFO, and an attacker who owns
            # ~/.apm/logs can also seed a world-readable/writable (0666) regular
            # ``scripts.log`` BEFORE the first append -- which would otherwise be
            # appended to forever, defeating the audit log's documented "cannot
            # be world-readable" tamper-evidence guarantee. Unlink + retry
            # (``O_EXCL``, 0600) so a tampered/wide-mode node self-heals to a
            # fresh 0600 file (also discarding any forged pre-seeded content)
            # instead of being trusted or silently dropped forever.
            _st = os.fstat(fd)
            if not stat.S_ISREG(_st.st_mode) or (_POSIX_PERMS and _st.st_mode & 0o077):
                os.close(fd)
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(log_path)
                fd = os.open(log_path, excl_flags, 0o600)
                _st_retry = os.fstat(fd)
                if not stat.S_ISREG(_st_retry.st_mode) or (
                    _POSIX_PERMS and _st_retry.st_mode & 0o077
                ):
                    # Same-instant adversarial re-plant on the retry: drop only
                    # THIS racing write (bounded), never a persistent blackout.
                    return
            os.write(fd, payload)
        finally:
            os.close(fd)
    except Exception:
        _logger.debug("Failed to write to scripts log", exc_info=True)


def execute_script(
    script: ScriptEntry,
    event: LifecycleEvent,
    *,
    logger: CommandLogger | None = None,
    verbose: bool = False,
    project_root: str | None = None,
) -> threading.Thread | None:
    """Dispatch to the correct executor based on script type.

    Returns the daemon thread for HTTP scripts (so callers can optionally
    join it), or None for command scripts and no-ops.
    """
    if script.script_type == "http":
        return _execute_http(script, event, logger=logger, verbose=verbose)
    elif script.script_type == "command":
        _execute_command(script, event, logger=logger, verbose=verbose, project_root=project_root)
    return None


# -- HTTP executor ---------------------------------------------------------


def _expand_env_vars(
    value: str,
    allowed: frozenset[str] = frozenset(),
    *,
    logger: CommandLogger | None = None,
    verbose: bool = False,
) -> str:
    """Expand ``$VAR`` and ``${VAR}`` references in *value*.

    Variables whose names match the credential denylist pattern
    (TOKEN, SECRET, PAT, KEY, PASSWORD, PASSPHRASE, CREDENTIAL, AUTHTOKEN)
    are NOT expanded unless their name is in *allowed* (the script's opt-in
    ``allowedEnvVars``). A blocked expansion emits a visible warning so
    the failure is never silent.
    """

    def _replace(match: re.Match) -> str:
        var_name = match.group(1) or match.group(2)
        if _is_denylisted(var_name, allowed):
            warning = (
                f"[!] Script: credential variable '{var_name}' will NOT be expanded. "
                f"If you must pass it, add it to the script's 'allowedEnvVars' -- "
                f"note this sends its value to the configured endpoint or subprocess."
            )
            if logger is not None:
                warn_fn = getattr(logger, "warning", None) or getattr(
                    logger, "verbose_detail", None
                )
                if warn_fn is not None:
                    warn_fn(warning)
            _logger.debug("Blocked credential variable expansion: %s", var_name)
            return ""
        # Strip CR/LF so a value carrying a smuggled "\r\nX-Evil: ..." cannot
        # inject extra HTTP headers when expanded into a header value.
        return os.environ.get(var_name, "").replace("\r", "").replace("\n", "")

    return _ENV_VAR_PATTERN.sub(_replace, value)


def _http_payload(event: LifecycleEvent) -> str:
    """Serialise *event* for HTTP delivery with PII minimisation.

    The full ``working_directory`` absolute path leaks the developer's
    username and local filesystem layout to a remote endpoint. For HTTP
    scripts we send only the final path component (the project folder
    name); command scripts -- which run locally -- still receive the full
    path on stdin.
    """
    from dataclasses import replace

    wd = event.working_directory
    safe_wd = Path(wd).name if wd else ""
    return replace(event, working_directory=safe_wd).to_json()


# Hostnames that resolve to cloud-metadata endpoints. Blocked by NAME
# (independent of DNS) because a guard that only classifies resolved IPs
# can be defeated when the host does not resolve in a sandbox yet routes
# to the metadata service in production.
_METADATA_HOSTNAMES = frozenset(
    {
        "metadata",
        "metadata.google.internal",
        "metadata.goog",
    }
)

# RFC 6598 carrier-grade NAT shared address space. Not flagged by the stdlib
# is_private/is_global predicates, but an SSRF guard must refuse it.
_CGNAT_NET = ipaddress.ip_network("100.64.0.0/10")

# RFC 3879 deprecated IPv6 site-local space. The stdlib reports it as
# is_global=True / is_private=False (only fe80::/10 link-local is flagged), so
# an SSRF guard built on the stdlib predicates would let it through. Refuse it
# explicitly for the same reason as the CGNAT block.
_SITE_LOCAL_NET = ipaddress.ip_network("fec0::/10")


def _host_to_ip_literal(host: str) -> ipaddress._BaseAddress | None:
    """Canonicalise *host* to an IP address if it denotes one literally.

    Handles dotted IPv4/IPv6, bracket-stripped IPv6, trailing-dot forms,
    and the decimal / hexadecimal integer encodings that defeat a naive
    ``ipaddress.ip_address(hostname)`` guard (e.g. ``2130706433`` and
    ``0x7f000001`` both denote ``127.0.0.1``). Returns ``None`` when the
    host is a DNS name rather than an address literal.
    """
    h = host.strip().rstrip(".")
    if not h:
        return None
    try:
        return ipaddress.ip_address(h)
    except ValueError:
        pass
    try:
        if h.lower().startswith("0x"):
            value = int(h, 16)
        elif h.isdigit():
            value = int(h, 10)
        else:
            return None
        if 0 <= value <= 0xFFFFFFFF:
            return ipaddress.ip_address(value)
    except ValueError:
        pass
    return None


def _ip_is_internal(ip: ipaddress._BaseAddress) -> bool:
    """Return True for any address an SSRF guard must refuse to reach."""
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    # RFC 6598 carrier-grade NAT (100.64.0.0/10) is neither is_private nor
    # is_global per the stdlib, yet it is shared ISP space an SSRF guard must
    # refuse (a request there can hit a sibling tenant behind the CGNAT).
    if isinstance(ip, ipaddress.IPv4Address) and ip in _CGNAT_NET:
        return True
    # RFC 3879 deprecated IPv6 site-local (fec0::/10): is_global per the stdlib,
    # so the predicates below miss it. Refuse it as the link-local sibling.
    if isinstance(ip, ipaddress.IPv6Address) and ip in _SITE_LOCAL_NET:
        return True
    return bool(
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def _ssrf_block_reason(host: str) -> str | None:
    """Classify *host*; return a reason string if it must be refused.

    Refuses cloud-metadata hostnames, then any literal/encoded address in
    a private, loopback, link-local, reserved, multicast, unspecified, or
    IPv4-mapped-internal range. For DNS names, every resolved address is
    classified -- if ANY resolves internal the destination is refused.
    A name that cannot be resolved is allowed to proceed (the request
    layer will fail to connect; no internal host is reachable).
    """
    if host.rstrip(".").lower() in _METADATA_HOSTNAMES:
        return "cloud-metadata hostname"

    literal = _host_to_ip_literal(host)
    if literal is not None:
        return "internal address" if _ip_is_internal(literal) else None

    try:
        infos = socket.getaddrinfo(host, None)
    except (OSError, ValueError):
        # OSError: name does not resolve. ValueError (covers its UnicodeError
        # subclass and the bare ValueError CPython raises for an embedded NUL
        # byte in the host): the host cannot be resolved or IDNA-encoded (empty
        # label, a label over 63 octets, a surrogate, or a NUL). Either way the
        # host is unreachable, so allowing it is SSRF-safe and the request layer
        # will fail to connect -- but it must fail CLOSED (return None) here, not
        # propagate out of _prepare_http and crash the public execute_script
        # caller, matching the _safe_urlparse fail-closed contract.
        return None
    for info in infos:
        sockaddr = info[4]
        try:
            resolved = ipaddress.ip_address(sockaddr[0])
        except ValueError:
            continue
        if _ip_is_internal(resolved):
            return "resolves to internal address"
    return None


class _SSRFConnectError(OSError):
    """Raised at connect time when a resolved address is internal.

    Subclasses ``OSError`` so the ``requests`` layer treats it as an
    ordinary connection failure (the dispatch is logged ``status=error``,
    never an internal host reached).
    """


def _ssrf_safe_connect(
    address: tuple[str, int],
    timeout: object = None,
    source_address: tuple[str, int] | None = None,
    socket_options: list | None = None,
) -> socket.socket:
    """Resolve *address* ONCE, refuse any internal result, then connect.

    Closes the DNS-rebinding TOCTOU left open by the up-front
    :func:`_ssrf_block_reason` guard: ``requests``/``urllib3`` would
    otherwise re-resolve the hostname independently at connect time, so a
    low-TTL name can answer a public A record to the guard and
    ``169.254.169.254`` to the socket. Here the SAME resolution that is
    validated is the one connected to. Raises :class:`_SSRFConnectError`
    when every resolved address is internal, or the last ``OSError`` when
    no permitted address could be reached.
    """
    host, port = address
    infos = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
    last_err: OSError | None = None
    blocked = False
    for family, socktype, proto, _canon, sockaddr in infos:
        try:
            resolved = ipaddress.ip_address(sockaddr[0])
        except ValueError:
            continue
        if _ip_is_internal(resolved):
            blocked = True
            continue
        sock = None
        try:
            sock = socket.socket(family, socktype, proto)
            if socket_options:
                for opt in socket_options:
                    sock.setsockopt(*opt)
            if isinstance(timeout, (int, float)):
                sock.settimeout(timeout)
            if source_address:
                sock.bind(source_address)
            sock.connect(sockaddr)
            return sock
        except OSError as exc:
            last_err = exc
            if sock is not None:
                sock.close()
    if blocked and last_err is None:
        raise _SSRFConnectError(f"blocked internal address for host {host}")
    if last_err is not None:
        raise last_err
    raise _SSRFConnectError(f"could not resolve {host} to a permitted address")


# Lazily-built, process-cached ``requests.Session`` whose HTTPS adapter
# pins each connection to the validated resolution (see _ssrf_safe_connect).
_GUARDED_SESSION = None
_GUARDED_SESSION_LOCK = threading.Lock()

# Per-thread handle to the in-flight dispatch's holder dict. The dispatch worker
# runs the blocking ``requests.post`` on its OWN thread and creates the urllib3
# connection on that same thread, so a recording connection mixin can stash the
# live socket into ``holder["sock"]`` here. On total-deadline ABANDONMENT the
# dispatcher then force-closes that socket (``_abort_dispatch_sock``) so the
# wedged ``post`` unblocks and the worker's ``finally`` releases its
# ``_HTTP_INFLIGHT`` permit PROMPTLY -- otherwise a CONTINUOUS slow-loris
# endpoint (one that dribbles under the per-recv read timeout forever) would pin
# its permit for the whole process lifetime, and 32 such endpoints would starve
# ALL legitimate outbound http for the rest of the install (availability DoS).
_DISPATCH_SOCK = threading.local()


def _record_dispatch_sock(sock: object) -> None:
    """Record the just-connected socket into the active dispatch holder.

    Called from a recording connection's ``connect()`` on the worker thread.
    Records only the FIRST socket of the dispatch (a redirect is disabled, so
    there is normally exactly one) and only when a holder is registered.
    """
    holder = getattr(_DISPATCH_SOCK, "holder", None)
    if holder is not None and sock is not None and "sock" not in holder:
        holder["sock"] = sock


def _abort_dispatch_sock(holder: dict) -> bool:
    """Force-close an abandoned worker's socket so its ``post`` unblocks.

    ``shutdown(SHUT_RDWR)`` (NOT ``close``) is deliberate: shutdown unblocks the
    worker's in-progress read without freeing the fd, so the worker's own urllib3
    cleanup closes it -- avoiding the fd-reuse race that a cross-thread ``close``
    would open. Returns True iff a socket was present to abort.
    """
    sock = holder.get("sock")
    if sock is None:
        return False
    with contextlib.suppress(OSError, AttributeError):
        sock.shutdown(socket.SHUT_RDWR)
    return True


class _SockRecordingMixin:
    """Connection mixin that records its socket after ``connect()`` completes.

    Mixed in BEFORE the urllib3 connection base so ``connect`` resolves here
    first, delegates to the real ``connect`` (which sets ``self.sock`` -- for
    pinned HTTPS that is the TLS-wrapped socket), then records it. Pure stdlib:
    references no urllib3 symbol, so it is safe to define at module import.
    """

    def connect(self):  # type: ignore[override]
        super().connect()
        _record_dispatch_sock(getattr(self, "sock", None))


def _build_guarded_session():
    """Build a ``requests.Session`` that resolve-and-pins every HTTPS conn.

    The custom ``urllib3`` connection overrides ``_new_conn`` to delegate
    to :func:`_ssrf_safe_connect`, so TLS SNI / certificate validation
    still runs against the ORIGINAL hostname while the socket only ever
    connects to a guard-approved address.
    """
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.connection import HTTPSConnection
    from urllib3.connectionpool import HTTPSConnectionPool
    from urllib3.poolmanager import PoolManager

    class _PinnedHTTPSConnection(_SockRecordingMixin, HTTPSConnection):
        def _new_conn(self):  # type: ignore[override]
            return _ssrf_safe_connect(
                (self._dns_host, self.port),
                self.timeout,
                source_address=self.source_address,
                socket_options=self.socket_options,
            )

    class _PinnedHTTPSConnectionPool(HTTPSConnectionPool):
        ConnectionCls = _PinnedHTTPSConnection

    class _PinnedPoolManager(PoolManager):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.pool_classes_by_scheme = {
                **self.pool_classes_by_scheme,
                "https": _PinnedHTTPSConnectionPool,
            }

    class _SSRFGuardAdapter(HTTPAdapter):
        def init_poolmanager(self, connections, maxsize, block=False, **pool_kwargs):
            self.poolmanager = _PinnedPoolManager(
                num_pools=connections,
                maxsize=maxsize,
                block=block,
                **pool_kwargs,
            )

    session = requests.Session()
    # trust_env=False is correct for THIS (direct-egress) session only: a
    # configured proxy uses urllib3's ProxyManager, NOT the resolve-and-pin
    # pool above, so honoring a proxy here would silently nullify the DNS pin.
    # Corporate-proxy egress is handled on a SEPARATE path in
    # _dispatch_http_request (which honors the operator's env proxy explicitly);
    # this session is used only when no env proxy applies to the destination.
    session.trust_env = False
    session.mount("https://", _SSRFGuardAdapter())
    return session


def _environ_proxies_for(url: str) -> dict:
    """Return the operator's env-configured proxies for *url*, or ``{}``.

    A non-empty result is the corporate-egress case: the environment mandates
    an outbound proxy for this destination (honoring ``NO_PROXY``), exactly as
    curl / pip / npm / git resolve proxies. Returns ``{}`` (direct egress) when
    no proxy applies or on any resolution error -- fail-closed to the DNS-pinned
    direct path rather than guessing a proxy.
    """
    try:
        import requests

        return requests.utils.get_environ_proxies(url, no_proxy=None) or {}
    except Exception:
        return {}


def _get_guarded_session():
    """Return the process-cached resolve-and-pin session, or None on failure.

    A None return means the custom adapter could not be constructed (e.g.
    an incompatible ``urllib3`` build); callers fall back to the up-front
    guard, which still blocks literal and pre-resolved internal targets.
    """
    global _GUARDED_SESSION
    if _GUARDED_SESSION is not None:
        return _GUARDED_SESSION
    with _GUARDED_SESSION_LOCK:
        if _GUARDED_SESSION is None:
            with contextlib.suppress(Exception):
                _GUARDED_SESSION = _build_guarded_session()
    return _GUARDED_SESSION


# Lazily-built capturing ``requests.Session`` used for the two egress paths the
# resolve-and-pin guarded session does NOT cover: the corporate-PROXY path
# (urllib3 routes through a ProxyManager, not the pinned pool) and the direct
# FALLBACK when the guarded session could not be built. It records the live
# socket of each dispatch (see _SockRecordingMixin) so an abandoned slow-loris
# worker can be force-closed at the deadline on these paths too -- WITHOUT the
# DNS pin (the destination was already vetted up-front by _ssrf_block_reason,
# and the proxy hop's rebind defense is delegated to the corporate proxy ACLs).
_CAPTURING_SESSION = None
_CAPTURING_SESSION_LOCK = threading.Lock()


def _build_capturing_session():
    """Build a non-pinning ``requests.Session`` that records each dispatch sock.

    Recording connection classes stash ``self.sock`` after ``connect()`` for
    BOTH schemes and BOTH the direct and proxy (ProxyManager) routings, so the
    dispatcher can force-close an abandoned worker on the proxy / fallback paths.
    """
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.connection import HTTPConnection, HTTPSConnection
    from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool
    from urllib3.poolmanager import PoolManager

    class _RecHTTPConnection(_SockRecordingMixin, HTTPConnection):
        pass

    class _RecHTTPSConnection(_SockRecordingMixin, HTTPSConnection):
        pass

    class _RecHTTPPool(HTTPConnectionPool):
        ConnectionCls = _RecHTTPConnection

    class _RecHTTPSPool(HTTPSConnectionPool):
        ConnectionCls = _RecHTTPSConnection

    rec_pools = {"http": _RecHTTPPool, "https": _RecHTTPSPool}

    class _CapturingAdapter(HTTPAdapter):
        def init_poolmanager(self, connections, maxsize, block=False, **pool_kwargs):
            self.poolmanager = PoolManager(
                num_pools=connections, maxsize=maxsize, block=block, **pool_kwargs
            )
            self.poolmanager.pool_classes_by_scheme = dict(rec_pools)

        def proxy_manager_for(self, proxy, **proxy_kwargs):
            if proxy in self.proxy_manager:
                return self.proxy_manager[proxy]
            manager = super().proxy_manager_for(proxy, **proxy_kwargs)
            with contextlib.suppress(Exception):
                manager.pool_classes_by_scheme = dict(rec_pools)
            return manager

    session = requests.Session()
    # Never auto-honor env proxies: the proxy is passed EXPLICITLY per-dispatch
    # in _run, so a stray env var cannot silently re-route a dispatch here.
    session.trust_env = False
    adapter = _CapturingAdapter()
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def _get_capturing_session():
    """Return the process-cached capturing session, or None on build failure."""
    global _CAPTURING_SESSION
    if _CAPTURING_SESSION is not None:
        return _CAPTURING_SESSION
    with _CAPTURING_SESSION_LOCK:
        if _CAPTURING_SESSION is None:
            with contextlib.suppress(Exception):
                _CAPTURING_SESSION = _build_capturing_session()
    return _CAPTURING_SESSION


# Upper bound on simultaneous HTTP dispatch worker threads started for a
# single lifecycle event. Without a cap, an event file with N http entries
# spawns N threads + sockets at once (resource exhaustion).
MAX_HTTP_DISPATCH_THREADS = 32

# Hard cap on the number of http dispatches that may be IN FLIGHT at once,
# counting BOTH live workers and abandoned-but-still-alive ones. The dispatch
# pool above bounds how many workers START concurrently, but a slow-loris
# endpoint makes ``_dispatch_http_request`` ABANDON its inner daemon worker on
# total-deadline expiry (the daemon keeps dribbling, holding one socket/fd). A
# malicious event file with thousands of http entries pointed at such an endpoint
# would otherwise leak O(N) abandoned daemons + fds and exhaust the process fd
# table within a single ``apm install``. This BoundedSemaphore is acquired
# NON-BLOCKING before each worker starts and released only when the worker TRULY
# finishes (its ``finally``), so an abandoned worker keeps holding its permit for
# its real lifetime -- capping live+abandoned http daemons to a constant
# regardless of attacker-chosen N. A dispatch that cannot claim a permit is
# dropped with a logged error (zero added stall); a legitimate install with
# <= MAX_HTTP_DISPATCH_THREADS fast http entries never hits the cap because each
# fast worker releases its permit before the next entry needs it.
_HTTP_INFLIGHT = threading.BoundedSemaphore(MAX_HTTP_DISPATCH_THREADS)

# Hard ceiling on the (attacker-influenced) per-event HTTP timeout. ``timeoutSec``
# in a project apm.yml is otherwise uncapped, so a malicious manifest could set it
# to days; a lifecycle http event is fire-and-forget telemetry, so a small finite
# ceiling is safe and bounds the worst-case hold on the dispatch worker.
_MAX_HTTP_TIMEOUT = 30.0
# Connect-phase timeout, bounded separately from the per-recv read timeout.
_HTTP_CONNECT_TIMEOUT = 10.0
# Grace given to an ABANDONED worker to finish after its socket is force-closed
# at the total deadline. The shutdown() unblocks the wedged read within ms, so
# this only needs to cover thread-scheduling jitter; if the worker somehow does
# not finish in the grace (it will, in practice), its permit is still released
# whenever it eventually does, and the in-flight cap bounds the residual.
_HTTP_ABANDON_GRACE = 1.0


def _coerce_http_deadline(timeout: float) -> float:
    """Clamp the per-event HTTP timeout to a finite, positive ceiling.

    ``ScriptEntry.effective_timeout`` is attacker-influenced (project apm.yml
    ``timeoutSec``) and uncapped; a non-finite or huge value would let a
    misbehaving endpoint hold a dispatch thread effectively forever.
    """
    try:
        value = float(timeout)
    except (TypeError, ValueError):
        return _MAX_HTTP_TIMEOUT
    if not math.isfinite(value) or value <= 0:
        return _MAX_HTTP_TIMEOUT
    return min(value, _MAX_HTTP_TIMEOUT)


def _connect_layer_host(url: str) -> str | None:
    """Return the host ``requests``/``urllib3`` would actually dial.

    The up-front SSRF guard validates ``urllib.parse.urlparse(url).hostname``,
    but the request layer connects to ``urllib3.util.parse_url(url).host``.
    The two parsers disagree on a hostile authority -- notably a backslash,
    which ``urllib3`` treats as an authority terminator while ``urllib.parse``
    folds it into the host -- so ``https://169.254.169.254\\.evil/`` can pass
    the guard (host looks public/unresolvable) yet dial bare
    ``169.254.169.254``. Returning urllib3's host lets the caller validate the
    SAME value the socket layer uses and reject any mismatch. Returns ``None``
    if urllib3 is unavailable or cannot parse the URL (the caller then falls
    back to the urllib.parse host alone).
    """
    try:
        from urllib3.util import parse_url

        return parse_url(url).host
    except Exception:
        return None


def _normalize_host(host: str | None) -> str:
    """Lower-case and strip IPv6 brackets for a parser-agnostic comparison."""
    if not host:
        return ""
    return host.strip("[]").lower()


def _safe_urlparse(url: str):
    """``urlparse`` that fails closed to ``None`` instead of raising.

    ``urllib.parse.urlparse`` raises ``ValueError`` on a malformed authority
    (e.g. ``https://[::1\\.evil/`` -> "Invalid IPv6 URL"). In the fire path
    that would be an uncaught crash, so callers treat a parse failure as a
    refused (skipped) script -- the same fail-closed posture ``validate`` took
    for this URL class.
    """
    try:
        return urlparse(url)
    except ValueError:
        return None


def _prepare_http(
    script: ScriptEntry,
    event: LifecycleEvent,
    *,
    logger: CommandLogger | None = None,
    verbose: bool = False,
) -> tuple[str, str, dict[str, str], float, str, str, str] | None:
    """Validate and build an HTTP dispatch, or return None if refused.

    Security gates (all enforced before any network call): HTTPS-only,
    hostname required, and an SSRF guard that refuses internal / metadata
    destinations (including encoded-IP bypasses). Returns the tuple
    ``(url, payload, headers, timeout, event_name, safe_url, hostname)``.
    """
    url = script.url
    if not url:
        _logger.debug("HTTP script has no URL, skipping")
        return None

    parsed = _safe_urlparse(url)
    if parsed is None:
        if verbose and logger:
            logger.verbose_detail("[i] HTTP script rejected: malformed URL")
        _logger.debug("Rejecting malformed script URL: %s", url)
        return None
    if parsed.scheme != "https":
        if verbose and logger:
            logger.verbose_detail(
                f"[i] HTTP script rejected: URL must use https (got {parsed.scheme}://)"
            )
        _logger.debug("Rejecting non-HTTPS script URL: %s", url)
        return None

    if not parsed.hostname:
        _logger.debug("HTTP script URL has no hostname: %s", url)
        return None

    # Validate the host the CONNECT layer (urllib3) will actually dial, not
    # just urllib.parse's view: a backslash or other authority-confusing
    # character makes them disagree, and the fallback request path would
    # otherwise reach an internal literal the guard was tricked into allowing.
    connect_host = _connect_layer_host(url)
    if connect_host is not None and _normalize_host(connect_host) != _normalize_host(
        parsed.hostname
    ):
        if verbose and logger:
            logger.verbose_detail("[i] HTTP script rejected: ambiguous URL authority")
        _logger.debug(
            "Rejecting URL: guard/connect host mismatch (%r vs %r)",
            parsed.hostname,
            connect_host,
        )
        return None

    # The mismatch gate above guarantees the connect-layer host equals the
    # guard host once we reach here, so a single SSRF resolution on
    # parsed.hostname covers both views -- resolving twice would needlessly
    # widen the DNS-rebind window.
    reason = _ssrf_block_reason(parsed.hostname)
    if reason is not None:
        if verbose and logger:
            logger.verbose_detail(f"[i] HTTP script rejected: {reason}")
        _logger.debug("Rejecting internal/SSRF script URL (%s)", reason)
        return None

    allowed = frozenset(script.allowed_env_vars or ())
    request_headers: dict[str, str] = {"Content-Type": "application/json"}
    if script.headers:
        for key, val in script.headers.items():
            request_headers[key] = _expand_env_vars(val, allowed, logger=logger, verbose=verbose)

    return (
        url,
        _http_payload(event),
        request_headers,
        script.effective_timeout,
        event.event,
        _redact_url_credentials(url),
        parsed.hostname,
    )


def _dispatch_http_request(
    url: str,
    payload: str,
    request_headers: dict[str, str],
    timeout: float,
    event_name: str,
    safe_url: str,
) -> None:
    """Send the prepared POST synchronously and log the outcome.

    ``stream=True`` keeps a malicious endpoint from forcing the whole
    response body into memory: only the status line is consumed.

    requests/urllib3 treat a scalar ``timeout`` as a PER-RECV socket timeout,
    which a slow-loris endpoint resets on every dribbled byte -- so the read of
    the response status/headers could otherwise hang far past the configured
    timeout. We therefore run the request on an inner daemon worker and enforce
    a TOTAL wall-clock deadline: past it, the worker is abandoned (a daemon never
    blocks process exit) and a timeout is logged, so a dribble can never hold the
    dispatch/batch worker -- or the ``apm lifecycle`` join that waits on it.
    """
    total_deadline = _coerce_http_deadline(timeout)
    connect_timeout = min(total_deadline, _HTTP_CONNECT_TIMEOUT)
    holder: dict[str, object] = {}

    # Bound live+abandoned http daemons to MAX_HTTP_DISPATCH_THREADS. A
    # non-blocking acquire means a fast endpoint (permit released in _run's
    # finally before the next entry needs it) never false-drops, while a flood
    # of slow-loris entries that abandon their workers cannot leak more than the
    # cap -- the (cap+1)th simply drops, converting unbounded fd-exhaustion into
    # the already-accepted <= cap residual, with zero added stall.
    if not _HTTP_INFLIGHT.acquire(blocking=False):
        _append_to_script_log(
            event_name,
            "http",
            safe_url,
            stderr="too many in-flight http dispatches; entry dropped",
            status="error",
        )
        return

    def _run() -> None:
        _DISPATCH_SOCK.holder = holder
        try:
            import requests

            # The DESTINATION host was already vetted by _ssrf_block_reason in
            # _prepare_http (internal / metadata / loopback targets are refused
            # before dispatch), and that gate is proxy-agnostic -- it runs whether
            # or not a proxy is configured. Egress ROUTING is a separate, operator-
            # owned concern handled here:
            env_proxies = _environ_proxies_for(url)
            if env_proxies:
                # Corporate-egress case: the operator's environment mandates an
                # outbound proxy for this destination (honoring NO_PROXY), exactly
                # as curl / pip / npm / git do -- in many corporate networks the
                # proxy is the ONLY outbound path. The resolve-and-pin adapter
                # cannot apply through a proxy (urllib3 uses a ProxyManager), so
                # rebind-TOCTOU defense for this hop is delegated to the corporate
                # proxy's own egress ACLs; the up-front destination gate still
                # bounds WHERE the request may be sent. Influencing the process
                # environment is RCE-equivalent here anyway (command-type lifecycle
                # scripts run in this same env), so env integrity is a precondition,
                # not a boundary we can meaningfully defend on this path. The
                # capturing session records the proxy socket so an abandoned
                # slow-loris (a dribbling proxy) is force-closed at the deadline.
                capt = _get_capturing_session()
                post = capt.post if capt is not None else requests.post
                proxies = env_proxies
            else:
                # Direct-egress case: no env proxy applies. Use the resolve-and-pin
                # guarded session (records its pinned socket) and explicitly refuse
                # any proxy so a stray env var cannot silently nullify the DNS pin.
                # If the guarded session is unavailable, fall back to the capturing
                # session (still records the socket; the up-front gate already
                # blocked internal targets) rather than bare requests.
                session = _get_guarded_session()
                if session is None:
                    session = _get_capturing_session()
                post = session.post if session is not None else requests.post
                proxies = {"http": None, "https": None}

            holder["resp"] = post(
                url,
                data=payload,
                headers=request_headers,
                timeout=(connect_timeout, total_deadline),
                allow_redirects=False,
                stream=True,
                proxies=proxies,
            )
        except BaseException as exc:
            holder["exc"] = exc
        finally:
            _DISPATCH_SOCK.holder = None
            # Release when the worker finishes. With the deadline force-close
            # below, an abandoned slow-loris worker finishes within the abandon
            # grace (its wedged read is unblocked), so its permit is reclaimed
            # promptly rather than pinned for the process lifetime.
            _HTTP_INFLIGHT.release()

    worker = threading.Thread(target=_run, name="apm-http-post", daemon=True)
    try:
        worker.start()
    except BaseException:
        # The thread never began, so _run's finally will not run; release the
        # permit here to avoid leaking it (e.g. "can't start new thread").
        _HTTP_INFLIGHT.release()
        raise
    worker.join(total_deadline)

    if worker.is_alive():
        # A dribbling endpoint is still feeding bytes under the per-recv read
        # timeout past the total deadline. Force-close the worker's captured
        # socket so its wedged read raises and the worker's finally releases its
        # _HTTP_INFLIGHT permit PROMPTLY (a CONTINUOUS dribble would otherwise
        # pin the permit for the whole process lifetime -> 32 such endpoints
        # starve all legit outbound http). shutdown(SHUT_RDWR) unblocks the read
        # within ms without freeing the fd (no cross-thread fd-reuse race); the
        # worker's own urllib3 cleanup then closes it. A short re-join lets the
        # worker run its finally before we return -- but ONLY when a socket was
        # actually recorded (guarded/capturing path). With no recorded socket
        # (bare-requests fallback) the force-close is a no-op, so the re-join
        # would gain nothing and merely add _HTTP_ABANDON_GRACE of latency per
        # serial dispatch; in that case we abandon at once (the worker is a
        # daemon, reaped at process exit) and reclaim the permit when it dies.
        if _abort_dispatch_sock(holder):
            worker.join(_HTTP_ABANDON_GRACE)
        _append_to_script_log(
            event_name,
            "http",
            safe_url,
            stderr=f"request exceeded total deadline {total_deadline:g}s",
            status="error",
        )
        return

    exc = holder.get("exc")
    if exc is not None:
        _logger.debug("HTTP POST failed for %s", safe_url, exc_info=True)
        _append_to_script_log(event_name, "http", safe_url, stderr=str(exc), status="error")
        return

    resp = holder.get("resp")
    _append_to_script_log(
        event_name,
        "http",
        safe_url,
        stdout=f"HTTP {resp.status_code}",
        status="ok" if resp.ok else "error",
    )
    with contextlib.suppress(Exception):
        resp.close()


def _execute_http(
    script: ScriptEntry,
    event: LifecycleEvent,
    *,
    logger: CommandLogger | None = None,
    verbose: bool = False,
) -> threading.Thread | None:
    """Send an HTTP POST to the script URL in a daemon thread.

    Returns the started thread so callers can optionally join it, or
    ``None`` when the destination is refused by a security gate.

    Security hardening:
    - HTTPS-only (rejects ``http://``)
    - SSRF guard (refuses internal / loopback / link-local / metadata)
    - No redirect following
    - ``stream=True`` (response body never buffered)
    - Configurable timeout (default 10s)
    - Header values support ``$ENV_VAR`` expansion (credential vars blocked)
    """
    prepared = _prepare_http(script, event, logger=logger, verbose=verbose)
    if prepared is None:
        return None

    url, payload, request_headers, timeout, event_name, safe_url, hostname = prepared

    thread = threading.Thread(
        target=_dispatch_http_request,
        args=(url, payload, request_headers, timeout, event_name, safe_url),
        daemon=True,
    )
    thread.start()

    if verbose and logger:
        logger.verbose_detail(f"[i] {event.event} event dispatched to {hostname}")

    return thread


def dispatch_http_batch(
    scripts: list[ScriptEntry],
    event: LifecycleEvent,
    *,
    logger: CommandLogger | None = None,
    verbose: bool = False,
) -> list[threading.Thread]:
    """Dispatch many HTTP scripts through a bounded worker pool.

    Starts at most ``MAX_HTTP_DISPATCH_THREADS`` worker threads that drain
    a shared queue, so an event with hundreds of http entries can never
    spawn hundreds of simultaneous threads/sockets. Returns the started
    worker threads so callers can join them. Per-entry SSRF/HTTPS gating
    is applied inside each worker via :func:`_prepare_http`.
    """
    import queue

    if not scripts:
        return []

    work: queue.Queue[ScriptEntry] = queue.Queue()
    for script in scripts:
        work.put(script)

    def _worker() -> None:
        while True:
            try:
                script = work.get_nowait()
            except queue.Empty:
                return
            try:
                prepared = _prepare_http(script, event, logger=logger, verbose=verbose)
                if prepared is not None:
                    url, payload, headers, timeout, event_name, safe_url, _host = prepared
                    _dispatch_http_request(url, payload, headers, timeout, event_name, safe_url)
            except Exception:
                _logger.debug("HTTP dispatch worker failed", exc_info=True)
            finally:
                work.task_done()

    pool_size = min(len(scripts), MAX_HTTP_DISPATCH_THREADS)
    workers = [threading.Thread(target=_worker, daemon=True) for _ in range(pool_size)]
    for worker in workers:
        worker.start()
    return workers


# -- Command executor ------------------------------------------------------


def _execute_command(
    script: ScriptEntry,
    event: LifecycleEvent,
    *,
    logger: CommandLogger | None = None,
    verbose: bool = False,
    project_root: str | None = None,
) -> None:
    """Execute a shell command with the event payload on stdin.

    Command scripts run synchronously (bounded by ``timeout``), unlike
    HTTP scripts which fire in a background thread.  The timeout default
    is 30s per script -- multiple slow scripts can accumulate, but each
    is capped.
    """
    cmd = script.effective_command
    if not cmd:
        _logger.debug("Command script has no command string, skipping")
        return

    env = _build_script_env(script)
    timeout = script.effective_timeout
    cwd = _resolve_cwd(script, project_root)

    start = time.monotonic()
    proc: subprocess.Popen[str] | None = None
    try:
        # start_new_session puts the shell (and every grandchild it
        # spawns) in its OWN process group, so a timeout can reap the
        # whole tree via killpg instead of orphaning backgrounded
        # grandchildren (subprocess.run's timeout kills only the shell).
        proc = subprocess.Popen(
            cmd,
            shell=True,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=cwd,
            start_new_session=True,
        )
        stdout, stderr, capped = _capture_bounded(proc, event.to_json(), timeout)
        returncode = proc.returncode
        if capped:
            note = " ...[output capped: exceeded in-memory capture limit]"
            stdout = stdout + note
            stderr = stderr + note
            if logger is not None:
                warn = getattr(logger, "warning", None) or getattr(logger, "verbose_detail", None)
                if warn is not None:
                    warn(
                        "[!] Lifecycle command script output exceeded the capture "
                        "limit and was truncated; the script was terminated."
                    )
        _append_to_script_log(
            event.event,
            "command",
            cmd,
            stdout=stdout,
            stderr=stderr,
            exit_code=returncode,
            status="ok" if returncode == 0 else "error",
        )
        elapsed = time.monotonic() - start
        if elapsed > _SLOW_SCRIPT_THRESHOLD_SEC and logger is not None:
            warn = getattr(logger, "warning", None) or getattr(logger, "verbose_detail", None)
            if warn is not None:
                warn(
                    f"[!] Lifecycle command script took {elapsed:.1f}s "
                    "(command scripts run synchronously and delay the operation)."
                )
    except subprocess.TimeoutExpired:
        _logger.debug("Command script timed out: %s", cmd)
        _kill_process_group(proc)
        _append_to_script_log(event.event, "command", cmd, status="timeout")
        if logger:
            warn = getattr(logger, "warning", None) or getattr(logger, "verbose_detail", None)
            if warn is not None:
                warn(
                    f"[!] Lifecycle command script timed out after {script.effective_timeout}s: {cmd}"
                )
    except Exception as exc:
        _logger.debug("Command script failed: %s", cmd, exc_info=True)
        _kill_process_group(proc)
        _append_to_script_log(event.event, "command", cmd, stderr=str(exc), status="error")
        if verbose and logger:
            logger.verbose_detail(f"[i] Lifecycle command script failed: {cmd}")


def _signal_kill_group(proc: subprocess.Popen | None) -> None:
    """Send SIGKILL to the script's whole process group (no reap).

    Split out of ``_kill_process_group`` so the bounded-capture reader can
    terminate a runaway/over-cap writer WITHOUT also calling
    ``proc.communicate`` -- the drain threads already own the stdout/stderr
    pipes, so a second reader there would race. Best-effort, never raises.
    """
    if proc is None:
        return
    try:
        if hasattr(os, "killpg"):
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except (ProcessLookupError, PermissionError, OSError):
        with contextlib.suppress(OSError):
            proc.kill()


def _drain_capped(stream, sink: list[str], state: dict[str, int], cap: int) -> None:
    """Read ``stream`` to EOF, retaining at most ``cap`` chars in ``sink``.

    Past the cap the bytes are discarded but reading continues so the child
    can never wedge on a full pipe -- the watchdog in ``_capture_bounded``
    SIGKILLs the group once ``state['over']`` trips. ``state`` carries ``n``
    (chars retained) and ``over`` (1 once the cap is hit).
    """
    try:
        while True:
            chunk = stream.read(65536)
            if not chunk:
                break
            remaining = cap - state["n"]
            if remaining > 0:
                piece = chunk[:remaining]
                sink.append(piece)
                state["n"] += len(piece)
                if state["n"] >= cap:
                    state["over"] = 1
            else:
                state["over"] = 1
    except (OSError, ValueError):
        pass
    finally:
        with contextlib.suppress(OSError):
            stream.close()


def _capture_bounded(
    proc: subprocess.Popen[str],
    stdin_text: str,
    timeout: float,
    cap: int = _MAX_CAPTURE_CHARS,
) -> tuple[str, str, bool]:
    """Drive ``proc`` to completion with a hard per-stream capture cap.

    A bounded replacement for ``proc.communicate(input=..., timeout=...)``:
    stdin is fed and stdout/stderr are drained on separate daemon threads so
    no single stream can deadlock, and each captured stream is clamped to
    ``cap`` characters. If either stream overflows the cap the process group
    is SIGKILLed promptly. Re-raises ``subprocess.TimeoutExpired`` on timeout
    (after killing the group + joining the readers) so the caller's existing
    timeout handling is unchanged. Returns ``(stdout, stderr, capped)``.
    """
    out: list[str] = []
    err: list[str] = []
    out_state = {"n": 0, "over": 0}
    err_state = {"n": 0, "over": 0}

    # Reject a non-finite / non-numeric deadline up front (NaN, inf, list,
    # dict, str): a NaN deadline would make every `remaining <= 0` and
    # `proc.wait(min(0.1, remaining))` comparison false-y and silently
    # DISABLE the timeout bound, letting a slow script run unbounded. This
    # mirrors the old `proc.communicate(timeout=...)` which raised promptly
    # on such values; the caller's isolation handler reaps the process.
    if not (
        isinstance(timeout, (int, float))
        and not isinstance(timeout, bool)
        and math.isfinite(timeout)
    ):
        raise ValueError(f"invalid capture timeout: {timeout!r}")

    def _feed() -> None:
        try:
            if proc.stdin is not None:
                proc.stdin.write(stdin_text)
                proc.stdin.close()
        except (OSError, ValueError):
            pass

    workers = [
        threading.Thread(target=_feed, daemon=True),
        threading.Thread(
            target=_drain_capped, args=(proc.stdout, out, out_state, cap), daemon=True
        ),
        threading.Thread(
            target=_drain_capped, args=(proc.stderr, err, err_state, cap), daemon=True
        ),
    ]
    for w in workers:
        w.start()

    deadline = time.monotonic() + timeout
    killed_over = False
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _signal_kill_group(proc)
            for w in workers:
                w.join(timeout=5)
            raise subprocess.TimeoutExpired(proc.args, timeout)
        if (out_state["over"] or err_state["over"]) and not killed_over:
            _signal_kill_group(proc)
            killed_over = True
        try:
            proc.wait(timeout=min(0.1, remaining))
            break
        except subprocess.TimeoutExpired:
            continue

    # Clean shell exit reaped the LEADER. If NOTHING else holds the capture
    # pipes, the drains hit EOF and finish within a short grace -- this is the
    # well-behaved case AND the legitimately-detached-daemon case (a lifecycle
    # script may background a service whose stdio is redirected away from our
    # pipes, e.g. ``nohup svc >svc.log 2>&1 &``: its drains EOF immediately, so
    # we never reap it -- mirroring npm/yarn, which let a redirected daemon
    # survive). Only if a backgrounded GROUP MEMBER still holds the capture
    # pipes open after the grace are the drains wedged on read() (leaking the
    # grandchild + its fds + the two drain daemons); reap the whole group so the
    # pipes hit EOF and the drains finish. This bounds the wedge to the grace
    # instead of the full 5s join budget, and -- unlike an unconditional reap --
    # does NOT kill a daemon that correctly redirected its stdio.
    grace_deadline = time.monotonic() + _CAPTURE_DRAIN_GRACE
    # Only the two stdout/stderr DRAINS (workers[1:]) decide the reap. workers[0]
    # is the stdin _feed writer: a still-alive _feed means stdin is merely
    # UNCONSUMED (e.g. a legitimately-detached daemon that inherited but never
    # reads stdin, and on a many-package install the event JSON exceeds the OS
    # pipe buffer so the write blocks). Abandoning that bounded daemon-thread
    # write is harmless -- it does NOT hold our capture pipes -- so it must not
    # drive the kill decision, else a redirected-stdio daemon (which DID EOF both
    # drains, the npm/yarn-parity survival case) is wrongly reaped by killpg.
    drains = workers[1:]
    for w in drains:
        w.join(timeout=max(0.0, grace_deadline - time.monotonic()))
    if any(w.is_alive() for w in drains):
        # A group member still holds the pipes after the grace. Reap the group so
        # the drains hit EOF. If a grandchild ESCAPED the group -- e.g. it called
        # ``os.setsid()`` to form its OWN session/group -- ``killpg(proc.pid)``
        # cannot reach it and the drains stay wedged; so bound the post-kill wait
        # to a short settle budget and RETURN rather than block the install ~5s
        # per wedged drain. The escapee is a deliberately self-detached daemon (it
        # severed its own group); npm/yarn cannot kill such a process either. Its
        # two drain daemons are daemon threads (reaped at process exit) and the
        # residual fds are bounded by scripts-per-install -- a bounded latency
        # cost, not an unbounded hang.
        _signal_kill_group(proc)
        reap_deadline = time.monotonic() + _CAPTURE_DRAIN_GRACE
        for w in drains:
            w.join(timeout=max(0.0, reap_deadline - time.monotonic()))
    return "".join(out), "".join(err), bool(out_state["over"] or err_state["over"])


def _kill_process_group(proc: subprocess.Popen | None) -> None:
    """Kill the script's whole process group, then reap it.

    Required after a timeout: shell=True + start_new_session means the
    shell leads its own process group, so a single SIGKILL to the group
    also reaps backgrounded grandchildren that would otherwise orphan
    (the shell may have already exited while a grandchild lives on).
    Best-effort -- never raises into the install flow.

    The group is signalled by ``proc.pid`` directly rather than via
    ``os.getpgid(proc.pid)``: under ``start_new_session`` the child's
    PGID equals its PID, and the group persists as long as ANY member
    is alive. If the shell leader has already exited (a zombie) while a
    ``&``-backgrounded grandchild lives on, ``os.getpgid`` would raise
    ``ProcessLookupError`` and strand the live grandchild;
    ``killpg(proc.pid, ...)`` reaps the whole group regardless.
    """
    if proc is None:
        return
    _signal_kill_group(proc)
    with contextlib.suppress(subprocess.TimeoutExpired, ValueError, OSError):
        proc.communicate(timeout=5)


# -- Helpers ---------------------------------------------------------------


def _build_script_env(script: ScriptEntry) -> dict[str, str]:
    """Build the environment dict for command scripts.

    Inherits the current process environment but strips any variables
    whose names match the credential denylist (TOKEN, SECRET, PAT, KEY,
    PASSWORD, PASSPHRASE, CREDENTIAL, AUTHTOKEN) to prevent accidental
    exfiltration via scripts. A script may opt specific names back in via
    ``allowedEnvVars`` (e.g. ``ANALYTICS_TOKEN``) -- this is best-effort
    convenience, NOT a security boundary: a command script can read any
    file it has permission to.
    """
    allowed = frozenset(script.allowed_env_vars or ())
    env = {k: v for k, v in os.environ.items() if not _is_denylisted(k, allowed)}
    if script.env:
        # script.env values are merged last and may reintroduce credential-named
        # variables deliberately set by the script author. This is intentional
        # best-effort convenience (the user configured it explicitly), NOT a
        # security boundary: a command script can read any file it has permission
        # to regardless of env filtering.
        env.update(script.env)
    return env


def _resolve_cwd(script: ScriptEntry, project_root: str | None) -> str | None:
    """Resolve the working directory for a command script.

    Rejects relative paths that escape project_root to prevent lateral
    movement (e.g. 'cwd: ../../.ssh').  Absolute cwd values are passed
    through unchanged because they are explicit and visible in apm.yml.
    """
    if not script.cwd:
        return project_root
    from pathlib import Path

    if Path(script.cwd).is_absolute():
        return script.cwd
    root = Path(project_root) if project_root else Path.cwd()
    resolved = (root / script.cwd).resolve()
    root_resolved = root.resolve()
    if not str(resolved).startswith(str(root_resolved) + os.sep) and resolved != root_resolved:
        _logger.warning(
            "Script cwd '%s' escapes project root -- using project root instead", script.cwd
        )
        return str(root_resolved)
    return str(resolved)
