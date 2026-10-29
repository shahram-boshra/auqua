# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""Errors raised by the portfolio (BP6 section 7)."""


class PortfolioError(Exception):
    """The portfolio cannot apply a record or compute a value."""


class LotError(PortfolioError):
    """A lot operation is impossible, e.g. relieving more units than the lots hold."""


class DayCountNotSupportedError(PortfolioError):
    """A day-count convention without a verified primary source (BP6 P9)."""


class RoundingTieError(PortfolioError):
    """An exact tie under a rule whose tie-breaking the source does not define (BP6 R11)."""


class ConfigurationError(PortfolioError):
    """The portfolio configuration lacks an entry a computation needs (e.g. the base currency)."""


class CorporateActionLateError(PortfolioError):
    """A cash dividend recorded at or after its ex-date: never back-dated (BP6 section 4.5 A)."""


class CorporateActionUnsupportedError(PortfolioError):
    """A corporate action phase 1 does not apply, e.g. a dividend paid before its ex-date (F-G)."""


class ReconciliationConflictError(PortfolioError):
    """A statement re-sent with the same id and other content (BP6 section 4.7)."""
