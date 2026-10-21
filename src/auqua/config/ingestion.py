# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""Ingestion section of the root configuration (BP4 section 4.8; ADRs 0026-0028).

Placed in ``auqua.config`` like the storage section, so that the root model
never imports a component. Nothing is ingested from a source the operator has
not named here with a declared use class and the terms version accepted
(decision log a28, I-2); the section is empty by default.
"""

from decimal import Decimal
from typing import Annotated

from pydantic import Field

from auqua.config.base import ConfigModel
from auqua.contracts.events.ingest import UseClass

PositiveInt = Annotated[int, Field(gt=0)]
PositiveDecimal = Annotated[Decimal, Field(gt=0)]
UseClassField = Annotated[UseClass, Field(strict=False)]
#: Coinbase states 10 requests/s per IP and that historical rates "should not be
#: polled frequently" (BP4 section 4.4); the default stays well below.
DEFAULT_COINBASE_RATE = Decimal(3)


def _default_rate_limits() -> dict[str, Decimal]:
    return {"coinbase/exchange/candles": DEFAULT_COINBASE_RATE}


class SourceAcknowledgement(ConfigModel):
    """The operator's declaration for one source.

    Attributes:
        use_class: How the operator uses the data (Q-BP4-1).
        acknowledged_terms: The terms version or date the operator accepted;
            must equal the shipped profile's ``terms``.
    """

    use_class: UseClassField
    acknowledged_terms: str = Field(min_length=1)


class IngestionConfig(ConfigModel):
    """Sources the operator enabled and the bounds of fetching and ingestion.

    Attributes:
        jurisdiction: ISO 3166-1 alpha-2 code of the country the operator resides
            in and accesses the providers from (decision log a32; ADR 0029);
            unset means no source is cleared.
        sources: Source profile name → declaration; empty means nothing runs.
        http_timeout_s: Timeout of one HTTP request.
        max_attempts: Attempts per request, the first included.
        backoff_base_ms: Base of the capped exponential backoff with full jitter.
        backoff_cap_ms: Cap of one backoff sleep.
        rate_limits: Source profile name → maximum requests per second.
        max_raw_bytes: Largest raw object accepted (bytes).
        batch_events: Events per append transaction.
        treasury_available_after_business_days: Declared availability of US
            Treasury par yields: 00:00 New York time of the N-th US business
            day after the observation date (BP4 section 4.6).
    """

    jurisdiction: str | None = Field(default=None, pattern=r"^[A-Z]{2}$")
    sources: dict[str, SourceAcknowledgement] = Field(default_factory=dict)
    http_timeout_s: PositiveInt = 30
    max_attempts: PositiveInt = 6
    backoff_base_ms: PositiveInt = 500
    backoff_cap_ms: PositiveInt = 30_000
    rate_limits: dict[str, PositiveDecimal] = Field(default_factory=_default_rate_limits)
    max_raw_bytes: PositiveInt = 1 << 30
    batch_events: PositiveInt = 10_000
    treasury_available_after_business_days: PositiveInt = 1
