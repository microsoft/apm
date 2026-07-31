"""Secret redaction helpers and script audit-log writer.

Extracted from ``script_executors.py`` to keep that module under the
800-line source budget. All symbols that unit tests import or access via
``apm_cli.core.script_executors.*`` are re-exported from that module so
existing import paths are unchanged.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import stat
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX
    fcntl = None  # type: ignore[assignment]

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from apm_cli.core.command_logger import CommandLogger
    from apm_cli.core.lifecycle_scripts import LifecycleEvent

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
