# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""auqua: open-core, low-frequency multi-asset quantitative trading platform.

The package version is read from the installed distribution metadata, so
``pyproject.toml`` remains the single version source.
"""

from importlib.metadata import version

__all__ = ["__version__"]

__version__: str = version("auqua")
