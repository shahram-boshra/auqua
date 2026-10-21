# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""Market-data ingestion.

Historical and live market-data adapters producing canonical events.

Owned by BP4, Ingestion (ADRs 0026-0028). Raw files are stored immutably by
SHA-256 (``raw``), retrieved over HTTPS with rate limits and bounded retries
(``fetch``), checked against the permitted use of their source (``sources``),
parsed exactly (``parsers``), checked for gaps (``quality``), given an
availability time (``availability``) and appended to the event log with
revisions and a lineage record (``runner``). ``python -m auqua.ingestion``
runs it on the operator's workstation (``cli``). No third-party data is part
of the package or the repository.
"""
