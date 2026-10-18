# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""Hash chain over the event log (ADR 0021; Strategy sections 9 and 10).

``record_hash = SHA-256(previous record_hash || sequence as 8-byte big-endian ||
payload)``, with 32 zero bytes before record 0. The hash of the last record
(the head) identifies the whole log up to that record: changing, removing,
inserting or reordering any earlier record changes it.
"""

import hashlib

HASH_SIZE = 32
GENESIS = bytes(HASH_SIZE)
_SEQUENCE_BYTES = 8


def record_hash(previous: bytes, sequence: int, payload: bytes) -> bytes:
    """Return the chained hash of one record.

    Raises:
        ValueError: ``previous`` is not 32 bytes or ``sequence`` is outside 0..2**63-1.
    """
    if len(previous) != HASH_SIZE:
        raise ValueError(f"previous hash must be {HASH_SIZE} bytes, got {len(previous)}")
    if not 0 <= sequence < 2**63:
        raise ValueError(f"sequence out of range: {sequence}")
    digest = hashlib.sha256(previous)
    digest.update(sequence.to_bytes(_SEQUENCE_BYTES, "big"))
    digest.update(payload)
    return digest.digest()
