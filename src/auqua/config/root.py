# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""Root configuration and risk-limits file models (ADR 0018, ADR 0019).

Each component Blueprint adds its own typed section to :class:`AuquaConfig`;
BP7 supplies the risk-limits model carried by :class:`RiskLimitsFile`.
"""

from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, field_validator

from auqua.config.base import ConfigModel, Environment, Overridable
from auqua.config.migrate import Migrations

#: Enumerations and paths arrive as TOML strings; their fields accept strings.
EnvironmentField = Annotated[Environment, Field(strict=False)]
OverridablePath = Annotated[Path, Overridable(), Field(strict=False)]

CONFIG_MIGRATIONS = Migrations("config", current=1)
RISK_LIMITS_MIGRATIONS = Migrations("risk_limits", current=1)


class PathsConfig(ConfigModel):
    """Data and output roots (absolute paths)."""

    data_root: OverridablePath
    output_root: OverridablePath

    @field_validator("data_root", "output_root")
    @classmethod
    def _absolute(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("must be an absolute path")
        return value


class AuquaConfig(ConfigModel):
    """Root configuration of one environment (``config.toml``)."""

    schema_version: Literal[1]
    environment: EnvironmentField
    paths: PathsConfig


class RiskLimitsFile[L: ConfigModel](ConfigModel):
    """Risk limits of one environment (``risk_limits.toml``); never overridable."""

    schema_version: Literal[1]
    environment: EnvironmentField
    limits: L
