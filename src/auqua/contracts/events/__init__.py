# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""Canonical events (ADR 0014).

:data:`CanonicalEvent` is the union of every current event type. A Blueprint
that adds event types places them in ``auqua.contracts.events.<domain>`` and
adds them to this union.
"""

from auqua.contracts.events.account import AccountStatement, CashTransfer, ReconciliationResult
from auqua.contracts.events.ingest import DataGap, IngestRun
from auqua.contracts.events.lifecycle import (
    CorporateAction,
    CorporateActionV2,
    CouponPayment,
    FundingPayment,
    FundingPaymentV2,
    FuturesExpiry,
    FxSettlement,
    OptionAssignment,
    OptionExercise,
    OptionExpiry,
)
from auqua.contracts.events.market import Bar, Quote, ReferenceRate, SettlementPrice, Trade
from auqua.contracts.events.reference import InstrumentDefined
from auqua.contracts.events.research import RunStartedV2, Trial
from auqua.contracts.events.trading import (
    Fill,
    OrderSubmitted,
    RiskDecision,
    RunStarted,
    Signal,
    Target,
)

CanonicalEvent = (
    Trade
    | Quote
    | Bar
    | ReferenceRate
    | SettlementPrice
    | CorporateActionV2
    | FuturesExpiry
    | OptionExercise
    | OptionAssignment
    | OptionExpiry
    | FxSettlement
    | FundingPaymentV2
    | CouponPayment
    | InstrumentDefined
    | RunStartedV2
    | Trial
    | Signal
    | Target
    | OrderSubmitted
    | RiskDecision
    | Fill
    | IngestRun
    | DataGap
    | CashTransfer
    | AccountStatement
    | ReconciliationResult
)

__all__ = [
    "AccountStatement",
    "Bar",
    "CanonicalEvent",
    "CashTransfer",
    "CorporateAction",
    "CorporateActionV2",
    "CouponPayment",
    "DataGap",
    "Fill",
    "FundingPayment",
    "FundingPaymentV2",
    "FuturesExpiry",
    "FxSettlement",
    "IngestRun",
    "InstrumentDefined",
    "OptionAssignment",
    "OptionExercise",
    "OptionExpiry",
    "OrderSubmitted",
    "Quote",
    "ReconciliationResult",
    "ReferenceRate",
    "RiskDecision",
    "RunStarted",
    "RunStartedV2",
    "SettlementPrice",
    "Signal",
    "Target",
    "Trade",
    "Trial",
]
