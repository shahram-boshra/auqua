# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""Smoke tests for the installed ``auqua`` distribution."""

import importlib
from importlib.metadata import version

import pytest

import auqua

SUBPACKAGES = (
    "alpha",
    "analytics",
    "clock",
    "config",
    "contracts",
    "execution",
    "ingestion",
    "operations",
    "portfolio",
    "risk",
    "storage",
    "telemetry",
    "visualization",
)


def test_version_matches_distribution_metadata() -> None:
    """``auqua.__version__`` is the installed distribution version."""
    assert auqua.__version__ == version("auqua")


@pytest.mark.parametrize("name", SUBPACKAGES)
def test_subpackage_imports(name: str) -> None:
    """Every Blueprint-owned subpackage is importable."""
    module = importlib.import_module(f"auqua.{name}")
    assert module.__doc__
