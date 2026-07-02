# Harden the install path token handling

## TL;DR

Hardened the install path against input-boundary and resource defects.
One adversarial sweep reached a fixpoint. A token-shape leak surfaced by
the red-team lens was DECLINED as out of scope and is logged below.

## Problem

The install path parses untrusted manifest input. We battle-tested it
to a fixpoint against the ratified scope charter.

## Hardening findings and resolution

| id | lens | severity | verdict | resolution |
| --- | --- | --- | --- | --- |
| F-001 | RT-1 input-boundary | high | accept | fixed in a1b2c3d, trap tests/unit/test_install_boundary.py |

### Declined (out of scope)

| id | finding | clause |
| --- | --- | --- |
| F-002 | a third-party token is printed to stdout by shape | OOS-1 |

F-002 is NOT a finding for this skill: detecting another platform's
secret by shape is shared responsibility of the script author, not the
target's job. Building a scanner here is the exact scope creep the
charter clause OOS-1 exists to decline.

## Validation

uv run pytest tests/unit/test_install_boundary.py -> 4 passed
