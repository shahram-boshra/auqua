# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""Tests for the configuration loader (BP2 Y1-Y11; ADRs 0018-0020; BP6 section 4.11, T16).

Every fixture is created in a temporary directory: secrets need mode 0600,
which Git does not store, and no deployable configuration lives in the
repository.
"""

import datetime
import hashlib
import json
import os
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from auqua.config.base import ConfigModel, Environment
from auqua.config.errors import (
    ConfigError,
    ConfigFileError,
    ConfigValidationError,
    EnvironmentMismatchError,
    OverrideError,
    SecretError,
    UnsupportedVersionError,
)
from auqua.config.identity import config_id
from auqua.config.loader import LoadedConfig, load
from auqua.config.migrate import Migrations
from auqua.config.portfolio import MarginBasis, PortfolioConfig
from auqua.config.root import CONFIG_MIGRATIONS
from auqua.config.secret_files import SecretsDirectory
from auqua.config.storage import StorageConfig

PRIVATE_DIR = 0o700
PRIVATE_FILE = 0o600


class Limits(ConfigModel):
    """Stand-in for the BP7 risk-limits model."""

    max_notional: Decimal
    max_orders_per_minute: int


CONFIG = """\
schema_version = 1
environment = "{env}"

[paths]
data_root = "/srv/auqua/data"
output_root = "/srv/auqua/out"
"""

LIMITS = """\
schema_version = 1
environment = "{env}"

[limits]
max_notional = 250000.50
max_orders_per_minute = 30
"""


@dataclass
class Roots:
    """Configuration and secrets roots of one test."""

    config: Path
    secrets: Path


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _secret(roots: Roots, env: str, name: str, value: str) -> Path:
    directory = roots.secrets / env
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(PRIVATE_DIR)
    path = directory / name
    path.write_text(value + "\n", encoding="utf-8")
    path.chmod(PRIVATE_FILE)
    return path


@pytest.fixture
def roots(tmp_path: Path) -> Roots:
    """A valid ``paper`` environment with one secret."""
    result = Roots(tmp_path / "config", tmp_path / "secrets")
    _write(result.config / "paper" / "config.toml", CONFIG.format(env="paper"))
    _write(result.config / "paper" / "risk_limits.toml", LIMITS.format(env="paper"))
    _secret(result, "paper", "BROKER_API_KEY", "k-123")
    return result


def _load(roots: Roots, environ: dict[str, str] | None = None) -> LoadedConfig[Limits]:
    return load(
        Environment.PAPER,
        roots.config,
        roots.secrets,
        limits_model=Limits,
        secret_names=("BROKER_API_KEY",),
        environ=environ or {},
    )


def test_load_valid_environment(roots: Roots) -> None:
    """A valid environment loads completely; TOML floats are exact decimals (Y1)."""
    loaded = _load(roots)
    assert loaded.config.environment is Environment.PAPER
    assert loaded.config.paths.data_root == Path("/srv/auqua/data")
    assert loaded.risk_limits.limits.max_notional == Decimal("250000.50")
    assert isinstance(loaded.risk_limits.limits.max_notional, Decimal)
    assert loaded.secrets["BROKER_API_KEY"].get_secret_value() == "k-123"


def test_float_refused_for_decimal() -> None:
    """A binary float is never accepted for a decimal field (Y1)."""
    with pytest.raises(ValidationError):
        Limits.model_validate({"max_notional": 1.5, "max_orders_per_minute": 1})


def test_unknown_key_refused(roots: Roots) -> None:
    """Unknown keys are refused with their path (Y2)."""
    path = roots.config / "paper" / "config.toml"
    _write(path, CONFIG.format(env="paper") + 'extra_root = "/x"\n')
    with pytest.raises(ConfigValidationError, match="extra_root"):
        _load(roots)


def test_loaded_config_is_frozen(roots: Roots) -> None:
    """A loaded configuration cannot be modified (Y2)."""
    loaded = _load(roots)
    with pytest.raises(ValidationError):
        loaded.config.paths.data_root = Path("/elsewhere")  # type: ignore[misc]


def test_override_precedence(roots: Roots) -> None:
    """An ``AUQUA_`` variable overrides the file for an overridable field (Y3)."""
    loaded = _load(roots, {"AUQUA_PATHS__DATA_ROOT": "/mnt/data", "HOME": "/home/x"})
    assert loaded.config.paths.data_root == Path("/mnt/data")
    assert loaded.config.paths.output_root == Path("/srv/auqua/out")


@pytest.mark.parametrize(
    ("variable", "value", "match"),
    [
        ("AUQUA_PATHS__DATA_ROT", "/x", "no configuration field"),
        ("AUQUA_ENVIRONMENT", "live", "not overridable"),
        ("AUQUA_SCHEMA_VERSION", "2", "not overridable"),
        ("AUQUA_LIMITS__MAX_NOTIONAL", "1", "no configuration field"),
        ("AUQUA_BROKER_API_KEY", "x", "no configuration field"),
    ],
)
def test_overrides_refused(roots: Roots, variable: str, value: str, match: str) -> None:
    """Unknown, non-overridable, risk-limit and secret variables are refused (Y4)."""
    with pytest.raises(OverrideError, match=match):
        _load(roots, {variable: value})


def test_relative_path_refused(roots: Roots) -> None:
    """Roots must be absolute, also when overridden."""
    with pytest.raises(ConfigValidationError, match="absolute"):
        _load(roots, {"AUQUA_PATHS__DATA_ROOT": "data"})


def test_secret_masked_and_permissions(roots: Roots) -> None:
    """Secrets are masked; group or other access is refused (Y5)."""
    secret = _load(roots).secrets["BROKER_API_KEY"]
    assert "k-123" not in repr(secret)
    assert "k-123" not in str(secret)
    path = roots.secrets / "paper" / "BROKER_API_KEY"
    path.chmod(0o640)
    with pytest.raises(SecretError, match="mode 640"):
        _load(roots)
    path.chmod(PRIVATE_FILE)
    (roots.secrets / "paper").chmod(0o750)
    with pytest.raises(SecretError, match="mode 750"):
        _load(roots)


def test_secret_symlink_and_missing_refused(roots: Roots, tmp_path: Path) -> None:
    """A symbolic link or a missing secret is refused (Y5)."""
    path = roots.secrets / "paper" / "BROKER_API_KEY"
    target = _write(tmp_path / "elsewhere", "k-999\n")
    target.chmod(PRIVATE_FILE)
    path.unlink()
    path.symlink_to(target)
    with pytest.raises(SecretError, match="not a regular file"):
        _load(roots)
    path.unlink()
    with pytest.raises(SecretError, match="missing"):
        _load(roots)


def test_invalid_secret_name(roots: Roots) -> None:
    """Secret names are upper-case identifiers."""
    with pytest.raises(SecretError, match="invalid secret name"):
        SecretsDirectory(roots.secrets / "paper").read("../config")


def test_risk_limits_hash(roots: Roots) -> None:
    """The risk-limits hash is the SHA-256 of the file bytes (Y6)."""
    path = roots.config / "paper" / "risk_limits.toml"
    assert _load(roots).risk_limits_sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    _write(path, LIMITS.format(env="paper") + "\n")
    assert _load(roots).risk_limits_sha256 == hashlib.sha256(path.read_bytes()).hexdigest()


def test_config_id_is_canonical(roots: Roots) -> None:
    """The id ignores key order and secrets, and changes with any value (Y7)."""
    first = _load(roots).config_id
    reordered = (
        'environment = "paper"\nschema_version = 1\n\n[paths]\n'
        'output_root = "/srv/auqua/out"\ndata_root = "/srv/auqua/data"\n'
    )
    _write(roots.config / "paper" / "config.toml", reordered)
    _secret(roots, "paper", "BROKER_API_KEY", "a different secret")
    assert _load(roots).config_id == first
    assert _load(roots, {"AUQUA_PATHS__OUTPUT_ROOT": "/srv/other"}).config_id != first
    loaded = _load(roots)
    canonical = json.dumps(
        loaded.config.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    )
    assert config_id(loaded.config) == hashlib.sha256(canonical.encode()).hexdigest()


def test_migrations() -> None:
    """Old versions migrate step by step; unknown and future versions are refused (Y8)."""
    migrations = Migrations("example", current=3)

    def v1_to_v2(doc: dict[str, Any]) -> dict[str, Any]:
        return {**doc, "schema_version": 2, "renamed": doc["old"]}

    def v2_to_v3(doc: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in doc.items() if k != "old"} | {"schema_version": 3}

    migrations.register(1, v1_to_v2)
    migrations.register(2, v2_to_v3)
    assert migrations.upgrade({"schema_version": 1, "old": "x"}) == {
        "schema_version": 3,
        "renamed": "x",
    }
    for bad in ({"schema_version": 4}, {"schema_version": "1"}, {}):
        with pytest.raises(UnsupportedVersionError):
            migrations.upgrade(bad)
    with pytest.raises(ValueError, match="duplicate"):
        migrations.register(1, v1_to_v2)


def test_future_config_version_refused(roots: Roots) -> None:
    """A configuration newer than the code is refused (Y8)."""
    future = f"= {CONFIG_MIGRATIONS.current + 1}"
    _write(
        roots.config / "paper" / "config.toml", CONFIG.format(env="paper").replace("= 1", future)
    )
    with pytest.raises(UnsupportedVersionError):
        _load(roots)


def test_config_version_1_migrates_to_storage_defaults(roots: Roots) -> None:
    """Version 1 has no ``storage`` section; it migrates to version 2 with defaults (BP3 Z15)."""
    migrated = _load(roots).config
    assert migrated.schema_version == CONFIG_MIGRATIONS.current
    assert migrated.storage == StorageConfig()
    explicit = CONFIG.format(env="paper").replace("= 1", "= 2") + (
        "\n[storage]\nbuffer_rows = 1000\nduckdb_memory_mib = 512\n"
    )
    _write(roots.config / "paper" / "config.toml", explicit)
    storage = _load(roots).config.storage
    assert (storage.buffer_rows, storage.duckdb_memory_mib) == (1000, 512)
    assert storage.rows_per_group == StorageConfig().rows_per_group
    for bad in ("rows_per_file = 0", "duckdb_threads = 1.5", "unknown = 1"):
        _write(roots.config / "paper" / "config.toml", explicit + bad + "\n")
        with pytest.raises(ConfigValidationError):
            _load(roots)


@pytest.mark.parametrize("file_name", ["config.toml", "risk_limits.toml"])
def test_environment_mismatch_refused(roots: Roots, file_name: str) -> None:
    """A file naming another environment is refused (Y9)."""
    template = CONFIG if file_name == "config.toml" else LIMITS
    _write(roots.config / "paper" / file_name, template.format(env="live"))
    with pytest.raises(EnvironmentMismatchError):
        _load(roots)


def test_missing_or_invalid_file_refused(roots: Roots) -> None:
    """A missing file or invalid TOML is refused."""
    path = roots.config / "paper" / "config.toml"
    _write(path, "schema_version = [")
    with pytest.raises(ConfigFileError):
        _load(roots)
    path.unlink()
    with pytest.raises(ConfigFileError):
        _load(roots)


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture], max_examples=30)
@given(st.text(alphabet=st.characters(categories=["L", "N"]), min_size=8, max_size=40))
def test_secret_never_in_error_messages(roots: Roots, value: str) -> None:
    """No error raised while loading contains a secret value (Y10)."""
    path = _secret(roots, "paper", "BROKER_API_KEY", value)
    path.chmod(0o644)
    with pytest.raises(ConfigError) as error:
        _load(roots)
    assert value not in str(error.value)
    path.chmod(PRIVATE_FILE)
    _write(roots.config / "paper" / "config.toml", "not = [valid")
    with pytest.raises(ConfigError) as error:
        _load(roots)
    assert value not in str(error.value)
    _write(roots.config / "paper" / "config.toml", CONFIG.format(env="paper"))


def test_secrets_owned_by_the_running_user(roots: Roots) -> None:
    """The ownership rule compares with the effective user id."""
    info = (roots.secrets / "paper" / "BROKER_API_KEY").stat()
    assert info.st_uid == os.geteuid()


# --- BP6 section 4.11: the portfolio section (schema 5) -----------------------------------------
PORTFOLIO = """
[portfolio]
account_id = "paper-1"

[portfolio.currencies.USD]
kind = "fiat"
minor_units = 2
source = "ISO 4217 amendment list"
read_on = 2026-10-28

[portfolio.currencies.EUR]
kind = "fiat"
minor_units = 2
source = "ISO 4217 amendment list"
read_on = 2026-10-28

[portfolio.currencies.BTC]
kind = "crypto"
minor_units = 8
source = "venue asset specification"
read_on = 2026-10-28

[portfolio.rates.EUR]
series = "FRB:H10/RXI$US_N.B.EU"

[portfolio.rates.BTC]
instrument = "COINBASE:BTC-USD"

[portfolio.margin."DERIBIT:BTC-PERPETUAL"]
basis = "notional_fraction"
initial = 0.02
maintenance = 0.01
currency = "BTC"
source = "venue margin page"
read_on = 2026-10-28

[portfolio.funding."DERIBIT:BTC-PERPETUAL"]
rate_period_ns = 28800000000000
source = "Deribit, Inverse Perpetual"
read_on = 2026-10-28
"""


def test_config_version_4_migrates_to_an_empty_portfolio(roots: Roots) -> None:
    """Version 4 has no ``portfolio`` section; it migrates with empty defaults (BP6 4.11)."""
    _write(roots.config / "paper" / "config.toml", CONFIG.format(env="paper").replace("= 1", "= 4"))
    portfolio = _load(roots).config.portfolio
    assert portfolio == PortfolioConfig()
    assert (portfolio.base_currency, portfolio.account_id) == ("USD", "primary")
    sections = (portfolio.currencies, portfolio.rates, portfolio.margin, portfolio.funding)
    assert [len(section) for section in sections] == [0, 0, 0, 0]


def test_portfolio_section_loads_exactly(roots: Roots) -> None:
    """Currencies, rates, margin and funding load with exact decimals and dates (BP6 4.11)."""
    text = CONFIG.format(env="paper").replace("= 1", "= 5") + PORTFOLIO
    _write(roots.config / "paper" / "config.toml", text)
    portfolio = _load(roots).config.portfolio
    assert portfolio.account_id == "paper-1"
    assert portfolio.currencies["BTC"].currency("BTC").minor_units == 8  # noqa: PLR2004
    assert portfolio.rates["EUR"].series == "FRB:H10/RXI$US_N.B.EU"
    margin = portfolio.margin["DERIBIT:BTC-PERPETUAL"]
    assert (margin.basis, margin.initial, margin.maintenance) == (
        MarginBasis.NOTIONAL_FRACTION,
        Decimal("0.02"),
        Decimal("0.01"),
    )
    assert margin.read_on == datetime.date(2026, 10, 28)
    assert portfolio.funding["DERIBIT:BTC-PERPETUAL"].rate_period_ns == 8 * 3_600 * 10**9


def _portfolio(**changes: Any) -> dict[str, Any]:  # noqa: ANN401 - test overrides
    entry = {"source": "s", "read_on": datetime.date(2026, 10, 28)}
    document: dict[str, Any] = {
        "currencies": {
            "USD": {"kind": "fiat", "minor_units": 2, **entry},
            "EUR": {"kind": "fiat", "minor_units": 2, **entry},
        },
        "rates": {"EUR": {"series": "FRB:H10/RXI$US_N.B.EU"}},
        "margin": {
            "XCME:ES-202612": {
                "basis": "per_contract",
                "initial": Decimal(20000),
                "maintenance": Decimal(18000),
                "currency": "USD",
                **entry,
            }
        },
    }
    for key, value in changes.items():
        document[key] = value
    return document


@pytest.mark.parametrize(
    ("changes", "match"),
    [
        ({"rates": {"USD": {"series": "FRB:H10/X"}}}, "base currency"),
        ({"rates": {"JPY": {"series": "FRB:H10/RXI_N.B.JA"}}}, "not among the currencies"),
        (
            {"rates": {"EUR": {"series": "FRB:H10/X", "instrument": "COINBASE:EUR-USD"}}},
            "exactly one",
        ),
        ({"rates": {"EUR": {}}}, "exactly one"),
        ({"rates": {"EUR": {"series": "no-separator"}}}, "series id"),
        ({"margin": {"bad key": {}}}, "Field required|instrument id"),
        (
            {
                "currencies": {
                    "USDT": {
                        "kind": "fiat",
                        "minor_units": 2,
                        "source": "s",
                        "read_on": datetime.date(2026, 1, 1),
                    }
                }
            },
            "currency code",
        ),
        (
            {
                "currencies": {
                    "USD": {
                        "kind": "fiat",
                        "minor_units": 2,
                        "source": " s",
                        "read_on": datetime.date(2026, 1, 1),
                    }
                }
            },
            "pattern",
        ),
    ],
)
def test_portfolio_section_refusals(changes: dict[str, Any], match: str) -> None:
    """References must resolve; each rate has exactly one source; codes fit their kind."""
    with pytest.raises(ValidationError, match=match):
        PortfolioConfig.model_validate(_portfolio(**changes), strict=False)


def test_margin_entry_refusals() -> None:
    """Maintenance <= initial; a notional fraction is at most 1; its currency is declared."""
    base = _portfolio()["margin"]["XCME:ES-202612"]
    for changes, match in (
        ({"maintenance": Decimal(30000)}, "exceed"),
        (
            {"basis": "notional_fraction", "initial": Decimal(2), "maintenance": Decimal(1)},
            "at most 1",
        ),
        ({"currency": "JPY"}, "not among the currencies"),
    ):
        document = _portfolio(margin={"XCME:ES-202612": base | changes})
        with pytest.raises(ValidationError, match=match):
            PortfolioConfig.model_validate(document, strict=False)
    assert PortfolioConfig.model_validate(_portfolio(), strict=False).margin
