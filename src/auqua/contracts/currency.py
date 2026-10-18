# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""Currencies (BP1 section 4.5).

The repository bundles no currency list: currencies arrive as reference data
with provenance (BP4).
"""

import re
from enum import StrEnum

import msgspec

from auqua.contracts.errors import InvalidFieldError
from auqua.contracts.validation import require_match

#: ISO 4217 alphabetic codes: three upper-case letters.
FIAT_CODE = re.compile(r"[A-Z]{3}")
#: Exchange asset codes, e.g. ``BTC``, ``USDT``, ``1INCH``.
CRYPTO_CODE = re.compile(r"[A-Z0-9]{2,15}")
#: Digital Token Identifier (ISO 24165) characters; the format is checked by its source.
DTI_CODE = re.compile(r"[A-Z0-9]{1,20}")
MAX_MINOR_UNITS = 18


class CurrencyKind(StrEnum):
    """Fiat currency (ISO 4217) or crypto asset."""

    FIAT = "fiat"
    CRYPTO = "crypto"


class Currency(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """A currency and the decimal digits used for money in it."""

    code: str
    kind: CurrencyKind
    minor_units: int
    dti: str | None = None

    def __post_init__(self) -> None:
        """Validate the code for its kind, the minor units and the DTI."""
        pattern = FIAT_CODE if self.kind is CurrencyKind.FIAT else CRYPTO_CODE
        require_match(self.code, pattern, "currency code")
        if not 0 <= self.minor_units <= MAX_MINOR_UNITS:
            raise InvalidFieldError(
                f"minor_units must be 0..{MAX_MINOR_UNITS}, got {self.minor_units}"
            )
        if self.dti is not None:
            if self.kind is CurrencyKind.FIAT:
                raise InvalidFieldError("a DTI identifies digital tokens, not fiat currencies")
            require_match(self.dti, DTI_CODE, "dti")
