# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""Errors raised by the portfolio (BP6 section 7)."""


class PortfolioError(Exception):
    """The portfolio cannot apply a record or compute a value."""


class LotError(PortfolioError):
    """A lot operation is impossible, e.g. relieving more units than the lots hold."""
