# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""Canonical events (ADR 0014).

:data:`CanonicalEvent` is the union of every current event type. A Blueprint
that adds event types places them in ``auqua.contracts.events.<domain>`` and
adds them to this union.
"""

from auqua.contracts.events.lifecycle import (
    CorporateAction,
    CouponPayment,
    FundingPayment,
    FuturesExpiry,
    FxSettlement,
    OptionAssignment,
    OptionExercise,
    OptionExpiry,
)
from auqua.contracts.events.market import Bar, Quote, Trade

CanonicalEvent = (
    Trade
    | Quote
    | Bar
    | CorporateAction
    | FuturesExpiry
    | OptionExercise
    | OptionAssignment
    | OptionExpiry
    | FxSettlement
    | FundingPayment
    | CouponPayment
)

__all__ = [
    "Bar",
    "CanonicalEvent",
    "CorporateAction",
    "CouponPayment",
    "FundingPayment",
    "FuturesExpiry",
    "FxSettlement",
    "OptionAssignment",
    "OptionExercise",
    "OptionExpiry",
    "Quote",
    "Trade",
]
