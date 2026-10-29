# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""Field checks shared by the contract types (raise :class:`InvalidFieldError`)."""

import re
from decimal import Decimal

from auqua.contracts.errors import InvalidFieldError
from auqua.contracts.numeric import require_finite


def require_time_ns(value: int, name: str) -> None:
    """Refuse times before the Unix epoch."""
    if value < 0:
        raise InvalidFieldError(f"{name} must be >= 0 ns since the Unix epoch, got {value}")


def require_positive_int(value: int, name: str) -> None:
    """Refuse integers that are not strictly positive."""
    if value <= 0:
        raise InvalidFieldError(f"{name} must be > 0, got {value}")


def require_match(value: str, pattern: re.Pattern[str], name: str) -> None:
    """Refuse strings that do not fully match ``pattern``."""
    if pattern.fullmatch(value) is None:
        raise InvalidFieldError(f"{name} {value!r} does not match {pattern.pattern}")


def require_decimals(**values: Decimal | None) -> None:
    """Refuse non-finite or negative-zero decimals among keyword values (``None`` skipped)."""
    for name, value in values.items():
        if value is not None:
            require_finite(value, name)


def require_positive(value: Decimal, name: str) -> None:
    """Refuse decimals that are not finite and strictly positive."""
    require_finite(value, name)
    if value <= 0:
        raise InvalidFieldError(f"{name} must be > 0, got {value}")


def require_non_negative(value: Decimal, name: str) -> None:
    """Refuse decimals that are not finite and >= 0."""
    require_finite(value, name)
    if value < 0:
        raise InvalidFieldError(f"{name} must be >= 0, got {value}")


def require_sha256(value: str, name: str) -> None:
    """Refuse text that is not 64 lower-case hexadecimal digits."""
    if len(value) != 64 or value.strip("0123456789abcdef"):  # noqa: PLR2004 - SHA-256 hex length
        raise InvalidFieldError(f"{name} must be 64 lower-case hex digits")


def require_label(value: str, name: str) -> None:
    """Refuse empty text and text with leading or trailing whitespace."""
    if not value.strip() or value != value.strip():
        raise InvalidFieldError(f"{name} {value!r} must be non-empty and trimmed")
