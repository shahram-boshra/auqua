# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""Portfolio and capital.

Authoritative cash, positions, P&L, fees, margin and funding state.

Owned by BP6, Portfolio & Capital. Walking-skeleton (thin) module: ``book`` (ADR 0025),
replaced by the BP6 slice. BP6 modules so far: ``ledger`` (postings, balances) and
``lots`` (first-in, first-out lots); ``errors``.
"""
