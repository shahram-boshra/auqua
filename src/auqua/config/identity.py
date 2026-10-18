# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""Configuration identity (ADR 0020)."""

import hashlib
import json

from pydantic import BaseModel


def config_id(config: BaseModel) -> str:
    """SHA-256 of the canonical JSON of a validated configuration.

    Canonical form: ``model_dump(mode="json")`` (decimals as strings), keys
    sorted, separators ``(",", ":")``. Secrets are never part of a configuration
    model, so they never affect the identity.
    """
    canonical = json.dumps(
        config.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def sha256_hex(data: bytes) -> str:
    """SHA-256 of raw bytes (used for the risk-limits file)."""
    return hashlib.sha256(data).hexdigest()
