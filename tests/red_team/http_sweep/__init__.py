"""Round-2 adversarial sweep of the lifecycle HTTP/SSRF executor.

These suites probe DEEPER than the round-1 ``tests/red_team/http`` suite:
DNS-rebinding TOCTOU (resolve-and-pin), IPv6 / userinfo host confusion,
streamed-body non-buffering, and the bounded worker pool under a hostile
never-responding entry. All network is mocked at the boundary; no socket
is ever opened against a real host.
"""
