"""Round-28 parser red-team: RecursionError escapes the non-GitHub Contents-API
JSON-envelope decoder on ``apm install``.

``DownloadDelegate._extract_contents_api_payload`` is reached on the dependency
download path (``apm install``) whenever a file is fetched from a NON-GitHub git
host (Gitea / Gogs / self-hosted). Such hosts return a JSON envelope
``{"content": "<base64>", "encoding": "base64"}`` which the delegate decodes
with ``json.loads(body.decode("utf-8"))``. The body and its ``Content-Type`` are
fully attacker-controlled (the remote host is untrusted), so a malicious or
compromised host can return a DEEPLY-NESTED JSON document. ``json.loads`` raises
``RecursionError`` on deep nesting, but the guard's except tuple is only
``(ValueError, UnicodeDecodeError, AttributeError)`` -- ``RecursionError`` is a
``RuntimeError`` subclass, so it ESCAPES and propagates up the download stack
(the surrounding handlers only catch ``requests.exceptions.*``), crashing
``apm install`` with an unhandled traceback instead of failing closed to the
raw-body fallback (``return body``).

This is the SAME fail-closed class the campaign closed at other JSON sinks
(r21 bundle, r22 plugin_manifest/exporter, r27 registry/marketplace) but at a
NEW, un-routed sink. The intended behavior on an undecodable envelope is to fall
back to the raw bytes (the function already does this for ``ValueError`` /
``UnicodeDecodeError``); ``RecursionError`` must join that tuple.
"""

from types import SimpleNamespace

from apm_cli.deps.download_strategies import DownloadDelegate


def _deep_json_object(depth: int) -> bytes:
    """A syntactically valid, deeply-nested JSON object body."""
    return ('{"a":' * depth + "1" + "}" * depth).encode("utf-8")


def test_round28_contents_api_recursion_fails_closed():
    # A non-GitHub host returns a 200 OK Contents-API response advertising
    # JSON, with a deeply-nested body. The host is untrusted.
    body = _deep_json_object(60_000)
    response = SimpleNamespace(
        content=body,
        headers={"Content-Type": "application/json"},
    )

    # SECURE contract: an undecodable / hostile envelope must fail CLOSED by
    # falling back to the raw bytes (the function's documented behavior for a
    # "response is not a JSON envelope at all" case), NOT crash the installer.
    #
    # Pre-fix: json.loads raises RecursionError, which is NOT in the except
    # tuple (ValueError, UnicodeDecodeError, AttributeError), so this call
    # raises RecursionError and this test FAILS -- proving the gap.
    result = DownloadDelegate._extract_contents_api_payload(response, is_github_host=False)

    assert result == body, "hostile deep-nested envelope must fall back to raw body, not crash"
