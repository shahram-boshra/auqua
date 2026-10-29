# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""Portfolio and capital.

Authoritative cash, positions, P&L, fees, margin and funding state.

Owned by BP6, Portfolio & Capital. Walking-skeleton (thin) module: ``book`` (ADR 0025),
replaced by the BP6 slice. BP6 modules so far: ``ledger`` (postings, balances),
``lots`` (first-in, first-out lots), ``daycount`` (sourced day counts, accrued interest),
``valuation`` (conversion into USD), ``effects`` (futures variation, spot pairs,
inverse perpetual P&L and funding), ``margin`` (configured requirements); ``errors``.
"""
