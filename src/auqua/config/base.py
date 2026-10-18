# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""Base model, environments and the override marker (ADR 0018, ADR 0020)."""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class Environment(StrEnum):
    """Isolated environments (Strategy section 12); nothing is shared between them."""

    DEV = "dev"
    TEST = "test"
    RESEARCH = "research"
    PAPER = "paper"
    LIVE = "live"


class ConfigModel(BaseModel):
    """Base of every configuration model: immutable, closed and strict.

    Unknown keys are refused, values are never coerced across types (a float
    is not accepted for a decimal), and a loaded model cannot be modified.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class Overridable:
    """Field marker: the field may be overridden by an ``AUQUA_`` environment variable.

    Use as ``Annotated[T, Overridable()]``. Secrets and risk limits are never
    overridable.
    """

    def __eq__(self, other: object) -> bool:
        """All markers are equal."""
        return isinstance(other, Overridable)

    def __hash__(self) -> int:
        """All markers hash alike."""
        return hash(Overridable)
