# Harden the manifest loader

## TL;DR

Three adversarial findings on the manifest loader; two accepted and
fixed, one declined out of scope. Findings are rendered from the
persisted ledger below.

## Hardening findings and resolution

| id | lens | severity | verdict | root cause | fix commit | trap |
| --- | --- | --- | --- | --- | --- | --- |
| F-001 | RT-1 input-boundary | high | accept | unbounded key recursion | 9f3a1c2 | tests/unit/test_loader_depth.py |
| F-002 | CH-4 exhaustion-and-limits | medium | accept | no size cap on inline anchors | 4d7e8b1 | tests/unit/test_loader_limits.py |

### Declined / deferred

| id | finding | clause |
| --- | --- | --- |
| F-003 | reimplement the YAML parser's TLS fetch | OOS-2 |

## How to test

uv run pytest tests/unit/test_loader_depth.py tests/unit/test_loader_limits.py
