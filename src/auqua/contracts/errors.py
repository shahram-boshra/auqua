# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""Errors raised by the core domain contracts."""


class ContractError(ValueError):
    """A value violates a core domain contract.

    Subclasses :class:`ValueError` so that msgspec reports a violation found
    in ``__post_init__`` during decoding as a ``msgspec.ValidationError``.
    """


class InvalidDecimalError(ContractError):
    """A decimal is not finite, is negative zero, or is not a positive increment."""


class OffGridError(ContractError):
    """A value is not an exact multiple of its instrument increment."""


class InvalidFieldError(ContractError):
    """A field value is outside its allowed domain."""


class EventDecodeError(ContractError):
    """Bytes do not decode into a known, valid canonical event."""
