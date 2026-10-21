# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""Source profiles and permitted use (BP4 section 4.2; ADR 0027; decision log a28).

A profile records what a provider's published terms allow, as read on a given
date: the purposes the data may serve, whether it may be used for machine
learning, whether derived outputs may be shared, and the attribution the terms
require. Profiles are facts about public documents, written by the project;
they ship no data. The platform states the terms and enforces the profile; the
operator decides, by naming the source and the terms version they accept in
the configuration. A newer shipped terms date stops ingestion until the
operator acknowledges it.
"""

from dataclasses import dataclass
from enum import StrEnum

from auqua.config.base import Environment
from auqua.config.ingestion import IngestionConfig, SourceAcknowledgement
from auqua.ingestion.errors import PermitError, TermsNotAcknowledgedError


class Purpose(StrEnum):
    """What a run uses the data for."""

    RESEARCH = "research"
    BACKTEST = "backtest"
    PAPER = "paper"
    LIVE = "live"


class MlUse(StrEnum):
    """Whether the terms allow use in machine-learning development."""

    ALLOWED = "allowed"
    PROHIBITED = "prohibited"


class Sharing(StrEnum):
    """Whether outputs derived from the data may be shared with third parties."""

    ALLOWED = "allowed"
    PROHIBITED = "prohibited"
    SHARE_ALIKE = "share_alike"


#: Strictness order of :class:`Sharing` (outputs carry the strictest of their inputs).
_SHARING_ORDER = {Sharing.ALLOWED: 0, Sharing.SHARE_ALIKE: 1, Sharing.PROHIBITED: 2}
_ALL_PURPOSES = frozenset(Purpose)
#: The run purpose of each environment (BP2 environments; Strategy section 12).
ENVIRONMENT_PURPOSE = {
    Environment.DEV: Purpose.RESEARCH,
    Environment.TEST: Purpose.RESEARCH,
    Environment.RESEARCH: Purpose.RESEARCH,
    Environment.PAPER: Purpose.PAPER,
    Environment.LIVE: Purpose.LIVE,
}


@dataclass(frozen=True)
class SourceProfile:
    """Permitted use of one source, from its terms as read on ``read_on``.

    Attributes:
        name: Profile name, e.g. ``coinbase/exchange/candles``.
        provider: Provider name.
        terms_url: Where the terms are published.
        terms: Version or date of the terms the profile reflects.
        read_on: Date the project read them (ISO 8601).
        purposes: Purposes the terms allow.
        ml_use: Machine-learning use.
        sharing: Sharing of derived outputs.
        attribution: Attribution the terms require, if any.
        basis: Clauses the profile rests on.
    """

    name: str
    provider: str
    terms_url: str
    terms: str
    read_on: str
    purposes: frozenset[Purpose]
    ml_use: MlUse
    sharing: Sharing
    attribution: str | None
    basis: str


COINBASE_CANDLES = SourceProfile(
    name="coinbase/exchange/candles",
    provider="Coinbase",
    terms_url="https://www.coinbase.com/legal/market_data",
    terms="2026-08-07",
    read_on="2026-10-21",
    purposes=_ALL_PURPOSES,
    ml_use=MlUse.PROHIBITED,
    sharing=Sharing.PROHIBITED,
    attribution=None,
    basis=(
        "Market Data Terms: 'exclusively for you or your entity's personal or research "
        "purposes'; no redistribution of data or derived works; section 3.5: no use to "
        "develop, train, fine-tune, teach, validate or benchmark any artificial intelligence"
    ),
)
BINANCE_VISION_KLINES = SourceProfile(
    name="binance/vision/klines",
    provider="Binance",
    terms_url="https://data.binance.vision/Binance_Vision-Terms_of_Use.pdf",
    terms="v1.0 2026-08-26",
    read_on="2026-10-21",
    purposes=frozenset({Purpose.RESEARCH, Purpose.BACKTEST}),
    ml_use=MlUse.ALLOWED,
    sharing=Sharing.SHARE_ALIKE,
    attribution="Binance Vision",
    basis=(
        "Vision Terms of Use v1.0: CC BY-NC-SA 4.0 (cl. 3.1); personal non-production "
        "backtesting (cl. 4.1); no live proprietary trading execution (cl. 4.2); no "
        "hosting (cl. 4.3); derivatives share-alike with attribution (cl. 4.5)"
    ),
)
FRB_H10 = SourceProfile(
    name="frb/h10/daily",
    provider="Board of Governors of the Federal Reserve System",
    terms_url="https://www.federalreserve.gov/disclaimer.htm",
    terms="undated, read 2026-10-21",
    read_on="2026-10-21",
    purposes=_ALL_PURPOSES,
    ml_use=MlUse.ALLOWED,
    sharing=Sharing.ALLOWED,
    attribution="Board of Governors of the Federal Reserve System",
    basis=(
        "Board website policies (disclaimer page): Board information may be copied and "
        "distributed without permission unless otherwise indicated; the Board asks to be "
        "cited as the source"
    ),
)
USTREASURY_PAR = SourceProfile(
    name="ustreasury/par-yield",
    provider="U.S. Department of the Treasury",
    terms_url="https://www.law.cornell.edu/uscode/text/17/105",
    terms="17 U.S.C. 105",
    read_on="2026-10-21",
    purposes=_ALL_PURPOSES,
    ml_use=MlUse.ALLOWED,
    sharing=Sharing.ALLOWED,
    attribution=None,
    basis="Works of the U.S. Government are not subject to copyright (17 U.S.C. 105)",
)
PROFILES: dict[str, SourceProfile] = {
    p.name: p for p in (COINBASE_CANDLES, BINANCE_VISION_KLINES, FRB_H10, USTREASURY_PAR)
}


def acknowledged(profile: SourceProfile, config: IngestionConfig) -> SourceAcknowledgement:
    """The operator's declaration for ``profile``.

    Raises:
        TermsNotAcknowledgedError: the source is not configured, or the
            acknowledged terms differ from the profile's terms.
    """
    declaration = config.sources.get(profile.name)
    if declaration is None:
        raise TermsNotAcknowledgedError(
            f'{profile.name} is not enabled: add [ingestion.sources."{profile.name}"] with '
            f'use_class and acknowledged_terms = "{profile.terms}" after reading '
            f"{profile.terms_url}"
        )
    if declaration.acknowledged_terms != profile.terms:
        raise TermsNotAcknowledgedError(
            f"{profile.name}: acknowledged terms {declaration.acknowledged_terms!r}, current "
            f"profile terms {profile.terms!r}; read {profile.terms_url} and update the "
            "acknowledgement"
        )
    return declaration


def permit(profile: SourceProfile, purpose: Purpose, *, ml: bool = False) -> None:
    """Refuse a use the profile does not allow (BP9 calls this with ``ml=True``).

    Raises:
        PermitError: ``purpose`` is not allowed, or ``ml`` is requested and prohibited.
    """
    if purpose not in profile.purposes:
        allowed = ", ".join(sorted(profile.purposes))
        raise PermitError(
            f"{profile.name}: purpose {purpose.value!r} not permitted (allowed: {allowed}); "
            f"{profile.basis}"
        )
    if ml and profile.ml_use is MlUse.PROHIBITED:
        raise PermitError(f"{profile.name}: machine-learning use prohibited; {profile.basis}")


def strictest_sharing(profiles: list[SourceProfile]) -> Sharing:
    """The sharing rule an output derived from ``profiles`` must carry."""
    if not profiles:
        return Sharing.ALLOWED
    return max((p.sharing for p in profiles), key=_SHARING_ORDER.__getitem__)


def attributions(profiles: list[SourceProfile]) -> list[str]:
    """Attributions required by ``profiles``, without repeats, in order."""
    return list(dict.fromkeys(p.attribution for p in profiles if p.attribution))
