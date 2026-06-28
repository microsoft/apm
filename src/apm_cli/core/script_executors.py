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
import os
import re
import signal
import socket
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse

if TYPE_CHECKING:
    from apm_cli.core.command_logger import CommandLogger
    from apm_cli.core.lifecycle_scripts import LifecycleEvent, ScriptEntry

_logger = logging.getLogger(__name__)

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
_CREDENTIAL_DENYLIST = re.compile(
    r"(?:(?:^|_)PASS|TOKEN|SECRET|PAT|KEY|PASSWORD|PASSWD|PASSPHRASE|PWD"
    r"|CREDENTIAL|AUTHTOKEN|MNEMONIC|SEED_PHRASE|RECOVERY_PHRASE|BACKUP_PHRASE)"
    r"S?(?:_IDS?)?(?:_?(?:OLD|NEW|PREV|CURRENT))?(?:_?V[0-9]+)?[_0-9]*$",
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
        "NPM_AUTH",
        "REGISTRY_AUTH",
        "SECRET_KEY_BASE",
        "DSN",
        "CONN_STR",
        "WALLET_SEED",
        "MASTER_SEED",
        "DERIVATION_SEED",
    }
)
_CREDENTIAL_BLOB_SUFFIX = re.compile(
    r"(?:_AUTH|_AUTH_CONFIG|_CONNECTION_STRING|CONNECTIONSTRING|_DSN|_CONN_STR)$",
    re.IGNORECASE,
)

# Minimum value length that is substring-masked in the audit log. Short
# values (e.g. a 4-char ``test``) are common substrings of ordinary words
# and masking them would corrupt unrelated log text; real credential
# values are long, so an 8-char floor catches secrets without false hits.
_MIN_REDACT_LEN = 8


# Connection-string / DSN passwords use a key=value form
# (libpq ``password=secret dbname=app``, JDBC ``...;password=secret``) that the
# URL-userinfo masker cannot see (there is no ``scheme://user:pass@``). Mask the
# value of a ``password=``/``passwd=`` key regardless of the env-var NAME, so a
# DSN-style secret carried by an UN-denylisted var (a bare ``DSN``, an echoed
# connection string) cannot persist in cleartext. The value runs to the next
# whitespace, ``;`` or end-of-string. A bare ``pwd=`` is handled separately by
# the path-guarded ``_CONN_STR_PWD_PATTERN`` below (the ODBC ``PWD=`` keyword),
# so the shell ``$PWD`` working-directory echo is preserved while a real DSN
# secret is still masked.
# The keyword group also matches libpq's ``sslpassword`` (the client-cert key
# passphrase, PostgreSQL >= 13). A bare ``\b(password)`` would NOT fire inside
# ``sslpassword`` because the ``l`` before ``password`` is a word char (no
# boundary), leaking the passphrase; ``(?:ssl)?password`` consumes the ``ssl``
# prefix while ``\b`` still anchors before it. The braced value class consumes an
# ODBC value that escapes a literal ``}`` by DOUBLING it (``}}``) -- the true
# terminator is the LAST single ``}``, so ``\{(?:[^}]|\}\})*\}`` walks past every
# doubled brace instead of stopping at the first ``}`` and leaking the tail.
_CONN_STR_PASSWORD_PATTERN = re.compile(
    r"(?i)\b((?:ssl)?password|passwd)(\s*=\s*)(\{(?:[^}]|\}\})*\}|[^\s;]+)"
)

# The canonical Microsoft ODBC / SQL Server password keyword is ``PWD=`` (e.g.
# ``Driver={ODBC Driver 18};Server=db;UID=sa;PWD=secret;``). We mask ``PWD=<value>``
# too, but the shell ``$PWD`` is routinely echoed as ``PWD=/home/user`` (or
# ``PWD=.``, ``PWD=~/x``, ``PWD=C:\Users``), and masking that would corrupt the
# benign working-directory path. So the value is preserved ONLY for the
# standalone ``$PWD`` echo form: a ``PWD=`` that is preceded by a ``;`` DSN
# delimiter (``UID=sa;PWD=...``) is ALWAYS a connection-string credential and is
# masked even when its value starts with ``/``, ``.`` or ``~`` -- otherwise an
# attacker (or a real ODBC password that legitimately starts with ``/``) could
# prefix a path char to dodge the guard. The value class also consumes an ODBC
# brace-escaped value (``PWD={p;w@d}``) through the closing ``}`` so an embedded
# ``;`` cannot leak the password tail.
# (``\bpwd`` cannot match inside ``OLDPWD`` -- the ``D`` before ``PWD`` is a word
# char, so there is no word boundary -- leaving ``OLDPWD=/x`` untouched as well.)
_CONN_STR_PWD_PATTERN = re.compile(r"(?i)\b(pwd)(\s*=\s*)(\{(?:[^}]|\}\})*\}|[^\s;]+)")
_PWD_PATH_VALUE = re.compile(r"^(?:[/~.]|[A-Za-z]:[\\/])")
# A ``;`` (optionally trailing whitespace) immediately before ``PWD=`` marks an
# ODBC connection-string delimiter, distinguishing ``UID=sa;PWD=/secret`` (a DSN
# credential, masked) from a standalone ``PWD=/home/user`` shell echo (preserved).
_PWD_DSN_DELIMITER = re.compile(r";\s*$")


def _redact_connection_string_password(text: str) -> str:
    """Mask ``password=``/``passwd=``/``PWD=`` values in connection strings / DSNs."""
    if not text:
        return text
    masked = _CONN_STR_PASSWORD_PATTERN.sub(lambda m: m.group(1) + m.group(2) + "[REDACTED]", text)

    def _mask_pwd(match: re.Match[str]) -> str:
        # A ';' delimiter before PWD= marks an ODBC DSN credential -- mask it
        # unconditionally (an attacker, or a real password, may start the value
        # with '/' to dodge the path guard). Only a standalone PWD= echo with a
        # filesystem-path value is the benign shell $PWD and is preserved.
        if _PWD_DSN_DELIMITER.search(match.string[: match.start()]):
            return match.group(1) + match.group(2) + "[REDACTED]"
        if _PWD_PATH_VALUE.match(match.group(3)):
            return match.group(0)  # filesystem path -- the $PWD echo, leave intact
        return match.group(1) + match.group(2) + "[REDACTED]"

    return _CONN_STR_PWD_PATTERN.sub(_mask_pwd, masked)


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


# Incoming-webhook URLs (Slack, Discord, Teams, generic O365) carry a
# bearer-grade secret in the URL PATH -- no userinfo, no query -- so neither the
# URL-userinfo masker nor the DSN masker sees it, and the env-var NAME
# (``SLACK_WEBHOOK``, ``*_WEBHOOK_URL``) ends in a benign token the suffix
# denylist does not list. Adding ``WEBHOOK`` to the denylist would strip the URL
# from the post-install child env too (a functional regression for a script that
# legitimately posts to the hook), so we mask the SECRET PATH SEGMENT
# structurally in log output instead, keyed on the known webhook hosts. The host
# and a short routing prefix stay readable; the token tail is redacted.
_WEBHOOK_URL_PATTERN = re.compile(
    r"(?i)(https://"
    r"(?:hooks\.slack\.com/(?:services|triggers)"
    r"|(?:ptb\.|canary\.)?discord(?:app)?\.com/api/webhooks"
    r"|[a-z0-9.\-]+\.webhook\.office\.com/webhookb2"
    r"|outlook\.office\.com/webhook)"
    r"/)[^\s\"'<>]+"
)


def _redact_webhook_urls(text: str) -> str:
    """Mask the secret path/token of known incoming-webhook URLs in *text*."""
    if not text:
        return text
    return _WEBHOOK_URL_PATTERN.sub(lambda m: m.group(1) + "[REDACTED]", text)


# Microsoft retired the O365 connector webhooks (2024-2025); the replacement
# Teams "Workflows" hooks are Power Automate / Logic Apps URLs whose secret is a
# Shared Access Signature carried as a ``?sig=<HMAC>`` query token rather than a
# path segment, so the host-keyed webhook masker above misses them. The same
# ``sig=`` SAS token guards every Azure SAS URL (Storage, Service Bus, Event
# Grid), so mask the signature value structurally and host-independently -- it is
# unambiguously a secret in any URL query -- instead of chasing an ever-changing
# Microsoft host set. The ``sig=`` key stays so the log still shows a SAS was
# present; the value (to the next ``&``, whitespace or quote) is redacted.
_SAS_SIGNATURE_PATTERN = re.compile(r"(?i)([?&]sig=)[^\s\"'<>&]+")


def _redact_sas_signatures(text: str) -> str:
    """Mask the ``sig=`` SAS token of Azure / Teams-Workflows webhook URLs."""
    if not text:
        return text
    return _SAS_SIGNATURE_PATTERN.sub(lambda m: m.group(1) + "[REDACTED]", text)


# A private-key blob (an env var whose value is a PEM, an inline key a script
# echoes) is a multi-line secret with no ``=`` key and no URL, so neither the DSN
# nor the URL masker sees it; a name like ``SSH_PRIVATE_KEY_PEM`` also ends in a
# benign token the suffix-anchored denylist misses. Redact the key MATERIAL
# between the PEM armor markers structurally, independent of the env-var name, so
# it cannot persist in cleartext in scripts.log. The BEGIN/END armor lines stay
# (they carry no secret) so the log still records that a key was present. The
# trailing ``(?: BLOCK)?`` also covers OpenPGP/GnuPG armor
# (``-----BEGIN PGP PRIVATE KEY BLOCK-----``), whose marker ends in ``KEY BLOCK``
# rather than ``KEY`` -- a ``gpg --export-secret-keys --armor`` dump otherwise
# carries no ``=`` key and no denylisted name, so nothing else would mask it.
_PEM_PRIVATE_KEY_PATTERN = re.compile(
    r"(-----BEGIN [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----)(.*?)"
    r"(-----END [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----)",
    re.DOTALL,
)


def _redact_pem_private_keys(text: str) -> str:
    """Mask the key material inside any PEM PRIVATE KEY armor in *text*."""
    if not text or "PRIVATE KEY" not in text:
        return text
    return _PEM_PRIVATE_KEY_PATTERN.sub(lambda m: m.group(1) + "[REDACTED]" + m.group(3), text)


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
    return _redact_pem_private_keys(
        _redact_sas_signatures(_redact_webhook_urls(_redact_connection_string_password(redacted)))
    )


def _redact_url_credentials(url: str) -> str:
    """Strip ``user:password@`` from a URL before logging."""
    try:
        parsed = urlparse(url)
        if not parsed.netloc or "@" not in parsed.netloc:
            return url
        host = parsed.hostname or ""
        if parsed.port is not None:
            host = f"{host}:{parsed.port}"
        return parsed._replace(netloc=host).geturl()
    except (ValueError, TypeError):
        return url


# Matches ``scheme://userinfo@`` embedded anywhere in free text so a
# tokenized URL printed by a script (e.g. git progress echoing
# ``https://ci-bot:ghp_xxx@github.com/...``) cannot persist its credential
# to scripts.log in cleartext. The scheme prefix is required so a bare
# ``user@host`` (e.g. an email address) is never over-redacted, and the
# userinfo run stops at the first ``/`` so a ``?next=a@b`` query is ignored.
# The userinfo class is ``[^/\s]+`` (NOT ``[^/\s@]+``): a password may itself
# contain a literal ``@`` (e.g. ``svc:p@ssw0rd@host``), and git/curl treat the
# LAST ``@`` before the path as the separator, so the greedy class must anchor
# there too -- otherwise the secret tail after the first ``@`` would leak.
_EMBEDDED_URL_CRED_PATTERN = re.compile(r"([a-zA-Z][a-zA-Z0-9+.\-]*://)[^/\s]+@")


def _redact_embedded_url_credentials(text: str) -> str:
    """Mask ``user:pass@`` userinfo of any URL embedded in *text*."""
    if not text or "@" not in text:
        return text
    return _EMBEDDED_URL_CRED_PATTERN.sub(lambda m: m.group(1) + "[REDACTED]@", text)


# -- Script output log -----------------------------------------------------

# Per-entry stdout/stderr is truncated to this many characters before being
# written, so a single lifecycle command that prints a large blob cannot
# bloat the audit log (or be used for a local disk-fill DoS).
_MAX_LOG_FIELD_CHARS = 4096

# When the log grows past this size it is rotated to ``scripts.log.1`` so it
# never grows without bound across many noisy events.
_MAX_LOG_BYTES = 5 * 1024 * 1024


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
    """Rotate the log to ``.1`` once it exceeds the size cap."""
    with contextlib.suppress(OSError):
        if log_path.stat().st_size >= _MAX_LOG_BYTES:
            rotated = log_path.with_name(log_path.name + ".1")
            os.replace(log_path, rotated)


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
        safe_target = _escape_header_field(
            _neutralize_newlines(_redact_embedded_url_credentials(_redact_secrets(target)))
        )
        lines = [
            f"[{ts}] event={event_name} type={script_type} target={safe_target} status={status}"
        ]
        if exit_code is not None:
            lines[0] += f" exit_code={exit_code}"
        if stdout and stdout.strip():
            lines.append(
                f"  stdout: {_truncate_log_field(_redact_embedded_url_credentials(_redact_secrets(stdout)).strip())}"
            )
        if stderr and stderr.strip():
            lines.append(
                f"  stderr: {_truncate_log_field(_redact_embedded_url_credentials(_redact_secrets(stderr)).strip())}"
            )
        lines.append("")  # blank line separator

        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(log_path, flags, 0o600)
        try:
            os.write(fd, ("\n".join(lines) + "\n").encode("utf-8"))
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

    class _PinnedHTTPSConnection(HTTPSConnection):
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
    session.mount("https://", _SSRFGuardAdapter())
    return session


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


# Upper bound on simultaneous HTTP dispatch worker threads started for a
# single lifecycle event. Without a cap, an event file with N http entries
# spawns N threads + sockets at once (resource exhaustion).
MAX_HTTP_DISPATCH_THREADS = 32


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
    """
    try:
        import requests

        session = _get_guarded_session()
        post = session.post if session is not None else requests.post
        resp = post(
            url,
            data=payload,
            headers=request_headers,
            timeout=timeout,
            allow_redirects=False,
            stream=True,
        )
        _append_to_script_log(
            event_name,
            "http",
            safe_url,
            stdout=f"HTTP {resp.status_code}",
            status="ok" if resp.ok else "error",
        )
    except Exception as exc:
        _logger.debug("HTTP POST failed for %s", safe_url, exc_info=True)
        _append_to_script_log(event_name, "http", safe_url, stderr=str(exc), status="error")


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
        stdout, stderr = proc.communicate(input=event.to_json(), timeout=timeout)
        returncode = proc.returncode
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
    try:
        if hasattr(os, "killpg"):
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except (ProcessLookupError, PermissionError, OSError):
        with contextlib.suppress(OSError):
            proc.kill()
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
    if not str(resolved).startswith(str(root_resolved) + "/") and resolved != root_resolved:
        _logger.warning(
            "Script cwd '%s' escapes project root -- using project root instead", script.cwd
        )
        return str(root_resolved)
    return str(resolved)
