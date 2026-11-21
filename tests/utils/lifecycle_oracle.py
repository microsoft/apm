"""Compatibility shim: the cross-cutting oracle lives in ``tests.lifecycle.oracles`` (#4093)."""

from tests.lifecycle.oracles import (
    ORACLE_LAWS,
    ExpectedCell,
    Observation,
    Operation,
    OracleModel,
    Violation,
    check_cross_cutting,
    ledger_property_ids,
    partition_expected,
    strict_misses,
)

__all__ = [
    "ORACLE_LAWS",
    "ExpectedCell",
    "Observation",
    "Operation",
    "OracleModel",
    "Violation",
    "check_cross_cutting",
    "ledger_property_ids",
    "partition_expected",
    "strict_misses",
]
