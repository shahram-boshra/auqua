# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""Load one environment's configuration, risk limits and secrets (BP2 section 4).

All or nothing: either every part is read, migrated, validated and checked, or
a :class:`~auqua.config.errors.ConfigError` is raised and nothing is returned.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

from pydantic import BaseModel, SecretStr, ValidationError

from auqua.config.base import ConfigModel, Environment
from auqua.config.errors import ConfigValidationError, EnvironmentMismatchError
from auqua.config.identity import config_id, sha256_hex
from auqua.config.migrate import Migrations
from auqua.config.overrides import apply_overrides
from auqua.config.root import (
    CONFIG_MIGRATIONS,
    RISK_LIMITS_MIGRATIONS,
    AuquaConfig,
    RiskLimitsFile,
)
from auqua.config.secret_files import SecretsDirectory
from auqua.config.tomlfile import read_toml

CONFIG_FILE = "config.toml"
RISK_LIMITS_FILE = "risk_limits.toml"


@dataclass(frozen=True)
class LoadedConfig[L: ConfigModel]:
    """Everything one environment needs, validated.

    Attributes:
        config: The root configuration.
        config_id: Identity of ``config`` (secrets excluded).
        risk_limits: The risk-limits file.
        risk_limits_sha256: SHA-256 of the risk-limits file bytes.
        secrets: Requested secrets by name (masked values).
    """

    config: AuquaConfig
    config_id: str
    risk_limits: RiskLimitsFile[L]
    risk_limits_sha256: str
    secrets: Mapping[str, SecretStr]


def _validate[M: BaseModel](model: type[M], document: dict[str, Any], source: Path) -> M:
    try:
        return model.model_validate(document)
    except ValidationError as error:
        details = "; ".join(
            f"{'.'.join(str(p) for p in e['loc']) or '<root>'}: {e['msg']}" for e in error.errors()
        )
        raise ConfigValidationError(f"{source}: {details}") from None


def _read[M: BaseModel](
    path: Path, model: type[M], migrations: Migrations, overrides: Mapping[str, str] | None
) -> tuple[M, bytes]:
    document, data = read_toml(path)
    document = migrations.upgrade(document)
    if overrides is not None:
        document = apply_overrides(model, document, overrides)
    return _validate(model, document, path), data


def load_root(
    environment: Environment, config_root: Path, *, environ: Mapping[str, str]
) -> AuquaConfig:
    """Load only the root configuration of ``environment`` (tools that need no risk limits).

    Raises:
        ConfigError: missing, invalid or of another environment.
    """
    path = config_root / environment.value / CONFIG_FILE
    config, _ = _read(path, AuquaConfig, CONFIG_MIGRATIONS, environ)
    if config.environment is not environment:
        raise EnvironmentMismatchError(
            f"{path}: environment {config.environment.value!r}, requested {environment.value!r}"
        )
    return config


def load[L: ConfigModel](  # noqa: PLR0913
    environment: Environment,
    config_root: Path,
    secrets_root: Path,
    *,
    limits_model: type[L],
    secret_names: tuple[str, ...],
    environ: Mapping[str, str],
) -> LoadedConfig[L]:
    """Load ``environment`` from ``config_root/<env>/`` and ``secrets_root/<env>/``.

    Args:
        environment: The environment to load.
        config_root: Directory holding one subdirectory per environment.
        secrets_root: Directory holding one private subdirectory per environment.
        limits_model: The risk-limits model (BP7).
        secret_names: Secrets the caller needs; each must exist.
        environ: Environment variables (``AUQUA_`` overrides for the root configuration).

    Raises:
        ConfigError: any part is missing, invalid, unsafe or inconsistent.
    """
    directory = config_root / environment.value
    config, _ = _read(directory / CONFIG_FILE, AuquaConfig, CONFIG_MIGRATIONS, environ)
    limits, limits_bytes = _read(
        directory / RISK_LIMITS_FILE,
        RiskLimitsFile[limits_model],  # type: ignore[valid-type]  # concrete at run time
        RISK_LIMITS_MIGRATIONS,
        None,
    )
    for name, found in ((CONFIG_FILE, config.environment), (RISK_LIMITS_FILE, limits.environment)):
        if found is not environment:
            raise EnvironmentMismatchError(
                f"{directory / name}: environment {found.value!r}, requested {environment.value!r}"
            )
    secrets_dir = SecretsDirectory(secrets_root / environment.value)
    secrets = MappingProxyType({name: secrets_dir.read(name) for name in secret_names})
    return LoadedConfig(
        config=config,
        config_id=config_id(config),
        risk_limits=limits,
        risk_limits_sha256=sha256_hex(limits_bytes),
        secrets=secrets,
    )
