# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""Configuration errors (BP2 section 7).

No message ever contains a secret value.
"""


class ConfigError(Exception):
    """Configuration could not be loaded; nothing is returned."""


class ConfigFileError(ConfigError):
    """A configuration file is missing, not a regular file, or not valid TOML."""


class ConfigValidationError(ConfigError):
    """A configuration document does not match its model."""


class OverrideError(ConfigError):
    """An ``AUQUA_`` environment variable is unknown or not allowed to override."""


class SecretError(ConfigError):
    """A secret is missing or its file or directory has unsafe ownership or permissions."""


class EnvironmentMismatchError(ConfigError):
    """The requested environment, its directory and the file contents disagree."""


class UnsupportedVersionError(ConfigError):
    """A file's ``schema_version`` has no migration path to the current version."""
