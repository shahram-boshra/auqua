# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""Alpha plugin boundary.

Public part only: the alpha provider contract, entry-point loading (group
``auqua.alpha``), the isolated worker host and the public baseline plugin. The private
engine is never imported here (ADR 0003).

Owned by BP9, Alpha Plugin Boundary. Modules are added by that Blueprint's
implementation slice.
"""
