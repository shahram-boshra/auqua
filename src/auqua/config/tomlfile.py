# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""TOML reading with exact decimals (ADR 0018).

TOML floats are parsed as :class:`decimal.Decimal`, so no binary float ever
enters configuration.
"""

import tomllib
from decimal import Decimal
from pathlib import Path
from typing import Any

from auqua.config.errors import ConfigFileError


def read_toml(path: Path) -> tuple[dict[str, Any], bytes]:
    """Read a TOML file; return the document and the file's exact bytes.

    Raises:
        ConfigFileError: the path is missing, a symbolic link, not a regular
            file, not UTF-8, or not valid TOML.
    """
    if path.is_symlink() or not path.is_file():
        raise ConfigFileError(f"{path}: not a regular file")
    data = path.read_bytes()
    try:
        document = tomllib.loads(data.decode("utf-8"), parse_float=Decimal)
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise ConfigFileError(f"{path}: {error}") from error
    return document, data
