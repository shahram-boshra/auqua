# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""Futures, spot and perpetual effects; margin (BP6 sections 4.5 B, D, E, 4.6; T6, T8-T10, T21)."""

import datetime
import uuid
from decimal import Decimal

import msgspec
import pytest

from auqua.config.portfolio import PortfolioConfig
from auqua.contracts.events.account import Difference, FxBreak
from auqua.contracts.events.lifecycle import FundingPaymentV2, FuturesExpiry, FxSettlement
from auqua.contracts.events.trading import Fill, Side
from auqua.contracts.identity import InstrumentId
from auqua.contracts.instruments import (
    CryptoSpot,
    Future,
    FxDelivery,
    FxPair,
    Identifiers,
    Instrument,
    PerpetualSwap,
    PerpetualType,
    Settlement,
)
from auqua.contracts.portfolio import Posting, PostingKind
from auqua.portfolio.effects import (
    PendingFx,
    contract_value,
    fee_posting,
    funding_amount,
    funding_posting,
    futures_variation,
    increment,
    inverse_pnl,
    match_fx_settlement,
    pending_fx,
    require_cash_settled_expiry,
    spot_fill_postings,
)
from auqua.portfolio.errors import ConfigurationError, PortfolioError
from auqua.portfolio.margin import PerInstrumentMargin, Requirement, margin_rows
from auqua.portfolio.reconcile import fx_settlement_break

CENT = Decimal("0.01")
YEN = Decimal(1)
SATOSHI = Decimal("0.00000001")
HOUR = 3_600 * 10**9
ACCOUNT = "primary"
ES = InstrumentId("CME", "ESZ6")
BTC_USD = InstrumentId("COINBASE", "BTC-USD")
EUR_USD = InstrumentId("FX", "EUR/USD")
INVERSE = InstrumentId("DERIBIT", "BTC-PERPETUAL")
LINEAR = InstrumentId("DERIBIT", "BTC_USDC-PERPETUAL")
_READ = {"source": "s", "read_on": datetime.date(2026, 10, 29)}


def _future(settlement: Settlement = Settlement.CASH) -> Future:
    return Future(
        id=ES,
        quote_currency="USD",
        multiplier=Decimal(50),
        underlying="SPX",
        expiry_ns=10 * HOUR,
        last_trade_ns=9 * HOUR,
        settlement=settlement,
        price_increment=CENT,
        quantity_increment=SATOSHI,
        valid_from_ns=0,
        knowledge_ns=0,
        identifiers=Identifiers(),
    )


def _crypto() -> CryptoSpot:
    return CryptoSpot(
        id=BTC_USD,
        quote_currency="USD",
        multiplier=Decimal(1),
        base_currency="BTC",
        price_increment=CENT,
        quantity_increment=SATOSHI,
        valid_from_ns=0,
        knowledge_ns=0,
        identifiers=Identifiers(),
    )


def _fx(delivery: FxDelivery = FxDelivery.SPOT) -> FxPair:
    spot = delivery is FxDelivery.SPOT
    return FxPair(
        id=EUR_USD,
        quote_currency="USD",
        multiplier=Decimal(1),
        base_currency="EUR",
        delivery=delivery,
        settlement_lag_days=2 if spot else None,
        value_date_ns=None if spot else 5 * HOUR,
        price_increment=CENT,
        quantity_increment=SATOSHI,
        valid_from_ns=0,
        knowledge_ns=0,
        identifiers=Identifiers(),
    )


def _swap(contract_type: PerpetualType) -> PerpetualSwap:
    inverse = contract_type is PerpetualType.INVERSE
    return PerpetualSwap(
        id=INVERSE if inverse else LINEAR,
        quote_currency="USD" if inverse else "USDC",
        multiplier=Decimal(10) if inverse else Decimal(1),
        base_currency="BTC",
        settlement_currency="BTC" if inverse else "USDC",
        contract_type=contract_type,
        funding_interval_ns=8 * HOUR,
        price_increment=CENT,
        quantity_increment=SATOSHI,
        valid_from_ns=0,
        knowledge_ns=0,
        identifiers=Identifiers(),
    )


def _fill(
    instrument_id: InstrumentId,
    side: Side,
    quantity: str,
    price: str,
    fee: tuple[str, str] = ("0", "USD"),
) -> Fill:
    return Fill(
        id=uuid.UUID(int=11),
        source="execution/sim",
        ts_event_ns=HOUR,
        ts_ingest_ns=HOUR,
        causation_id=uuid.UUID(int=10),
        instrument_id=instrument_id,
        side=side,
        price=Decimal(price),
        quantity=Decimal(quantity),
        fee=Decimal(fee[0]),
        fee_currency=fee[1],
    )


def _config(**sections: object) -> PortfolioConfig:
    currencies = {
        "USD": {"kind": "fiat", "minor_units": 2, **_READ},
        "JPY": {"kind": "fiat", "minor_units": 0, **_READ},
        "EUR": {"kind": "fiat", "minor_units": 2, **_READ},
        "BTC": {"kind": "crypto", "minor_units": 8, **_READ},
        "USDC": {"kind": "crypto", "minor_units": 6, **_READ},
    }
    return PortfolioConfig.model_validate({"currencies": currencies, **sections}, strict=False)


# --- T6: CME normal rounding; variation = trades + start-of-day position ------------------------
@pytest.mark.parametrize(
    ("value", "money_increment", "rounded"),
    [
        ("32.5649", CENT, "32.56"),
        ("32.5650", CENT, "32.57"),
        ("32.5651", CENT, "32.57"),
        ("-32.5649", CENT, "-32.56"),
        ("-32.5650", CENT, "-32.57"),
        ("-32.5651", CENT, "-32.57"),
        ("3256.49", YEN, "3256"),
        ("3256.50", YEN, "3257"),
        ("3256.51", YEN, "3257"),
        ("-3256.49", YEN, "-3256"),
        ("-3256.50", YEN, "-3257"),
        ("-3256.51", YEN, "-3257"),
    ],
)
def test_cme_normal_rounding(value: str, money_increment: Decimal, rounded: str) -> None:
    """One contract's value is rounded to the currency's precision, ties away from zero."""
    assert contract_value(Decimal(value), Decimal(1), money_increment) == Decimal(rounded)


def test_variation_is_trades_plus_the_start_of_day_position() -> None:
    """Start of day +2 from 4,000.00, a buy of 1 at 4,005.50; settlement 4,010.25, multiplier 50."""
    variation = futures_variation(
        multiplier=Decimal(50),
        money_increment=CENT,
        settlement_price=Decimal("4010.25"),
        start=(Decimal(2), Decimal("4000.00")),
        trades=[(Decimal(1), Decimal("4005.50"))],
    )
    assert variation == 2 * (Decimal("200512.50") - Decimal("200000.00")) + (
        Decimal("200512.50") - Decimal("200275.00")
    )
    assert variation == Decimal("1262.50")


def test_variation_rounds_each_contract_value_before_the_quantity() -> None:
    """Rounding the contract value first differs from rounding the product (CME)."""
    variation = futures_variation(
        multiplier=Decimal("0.5"),
        money_increment=CENT,
        settlement_price=Decimal("65.1299"),
        start=None,
        trades=[(Decimal(-3), Decimal("65.1300"))],
    )
    assert variation == -3 * (Decimal("32.56") - Decimal("32.57"))
    assert variation == Decimal("0.03")


def test_physical_futures_expiry_is_refused() -> None:
    """Only cash-settled expiry is supported in phase 1 (BP6 R3)."""
    expiry = FuturesExpiry(
        id=uuid.UUID(int=12),
        source="ingest/test",
        ts_event_ns=10 * HOUR,
        ts_ingest_ns=10 * HOUR,
        instrument_id=ES,
        settlement=Settlement.PHYSICAL,
        settlement_price=Decimal("4010.25"),
    )
    with pytest.raises(PortfolioError, match="R3"):
        require_cash_settled_expiry(expiry, _future(Settlement.PHYSICAL))
    cash = msgspec.structs.replace(expiry, settlement=Settlement.CASH)
    require_cash_settled_expiry(cash, _future())
    with pytest.raises(PortfolioError, match="R3"):
        require_cash_settled_expiry(cash, _future(Settlement.PHYSICAL))


# --- T8 (trade date), T2 (fee currency): spot pairs ---------------------------------------------
def test_crypto_spot_buy_posts_base_quote_and_fee() -> None:
    """A buy posts +base, -quote and the fee in its own currency (BP6 section 4.2)."""
    fill = _fill(BTC_USD, Side.BUY, "0.5", "60000.00", fee=("3.00", "USD"))
    postings = spot_fill_postings(fill, _crypto(), ACCOUNT, held=Decimal(0))
    assert postings == (
        Posting(ACCOUNT, "BTC", Decimal("0.5"), PostingKind.TRADE, fill.id),
        Posting(ACCOUNT, "USD", Decimal("-30000.000"), PostingKind.TRADE, fill.id),
        Posting(ACCOUNT, "USD", Decimal("-3.00"), PostingKind.FEE, fill.id),
    )


def test_fee_is_posted_in_the_fee_currency() -> None:
    """A fee charged in the base currency is posted in it, not in the quote currency (T2)."""
    fill = _fill(BTC_USD, Side.SELL, "0.5", "60000.00", fee=("0.0005", "BTC"))
    postings = spot_fill_postings(fill, _crypto(), ACCOUNT, held=Decimal("0.5"))
    assert [(p.currency, p.amount, p.kind) for p in postings] == [
        ("BTC", Decimal("-0.5"), PostingKind.TRADE),
        ("USD", Decimal("30000.000"), PostingKind.TRADE),
        ("BTC", Decimal("-0.0005"), PostingKind.FEE),
    ]
    assert fee_posting(_fill(BTC_USD, Side.BUY, "1", "1"), ACCOUNT) is None


def test_fx_spot_trade_date_postings() -> None:
    """EUR/USD spot: +EUR and -USD on the trade date (T8, first half)."""
    fill = _fill(EUR_USD, Side.BUY, "1000000", "1.0850")
    postings = spot_fill_postings(fill, _fx(), ACCOUNT, held=Decimal(0))
    assert [(p.currency, p.amount) for p in postings] == [
        ("EUR", Decimal(1000000)),
        ("USD", Decimal("-1085000.0000")),
    ]


def test_spot_refusals() -> None:
    """Spot is long or flat; forwards and multipliers other than 1 are not supported."""
    with pytest.raises(PortfolioError, match="held"):
        spot_fill_postings(_fill(BTC_USD, Side.SELL, "1", "1"), _crypto(), ACCOUNT, Decimal("0.5"))
    with pytest.raises(PortfolioError, match="forward"):
        spot_fill_postings(
            _fill(EUR_USD, Side.BUY, "1", "1"), _fx(FxDelivery.FORWARD), ACCOUNT, Decimal(0)
        )
    with pytest.raises(PortfolioError, match="instrument"):
        spot_fill_postings(_fill(EUR_USD, Side.BUY, "1", "1"), _crypto(), ACCOUNT, Decimal(0))


# --- T8 (settlement): matching an FX settlement to one pending fill (amendment 3) ----------------
DAY = 24 * HOUR


def _fx_fill(fill_id: int, side: Side, at_ns: int, quantity: str = "1000000") -> Fill:
    fill = _fill(EUR_USD, side, quantity, "1.0850")
    return msgspec.structs.replace(fill, id=uuid.UUID(int=fill_id), ts_event_ns=at_ns)


def _settlement(
    base: str = "1000000", quote: str = "1085000.0000", reference: int | None = None
) -> FxSettlement:
    return FxSettlement(
        id=uuid.UUID(int=99),
        source="ingest/test",
        ts_event_ns=2 * DAY,
        ts_ingest_ns=2 * DAY,
        instrument_id=EUR_USD,
        value_date_ns=2 * DAY,
        base_amount=Decimal(base),
        quote_amount=Decimal(quote),
        causation_id=None if reference is None else uuid.UUID(int=reference),
    )


def _pending(*fills: Fill) -> list[PendingFx]:
    return [pending_fx(fill, _fx()) for fill in fills]


def test_fx_settlement_matches_its_referenced_fill() -> None:
    """A trade reference selects that fill only, even when an older one has equal amounts."""
    pending = _pending(_fx_fill(1, Side.BUY, HOUR), _fx_fill(2, Side.BUY, 2 * HOUR))
    match = match_fx_settlement(_settlement(reference=2), pending)
    assert (match.matched, match.fill_id) == (True, uuid.UUID(int=2))
    assert match_fx_settlement(_settlement(reference=3), pending).reason is FxBreak.UNMATCHED


def test_fx_settlement_matches_the_oldest_economic_candidate() -> None:
    """Without a reference: equal amounts, executed before the value date, the oldest first."""
    pending = _pending(
        _fx_fill(1, Side.BUY, HOUR, quantity="500000"),
        _fx_fill(2, Side.BUY, 2 * HOUR),
        _fx_fill(3, Side.BUY, 3 * HOUR),
        _fx_fill(4, Side.BUY, 2 * DAY),
    )
    match = match_fx_settlement(_settlement(), pending)
    assert (match.matched, match.fill_id, match.differences) == (True, uuid.UUID(int=2), ())
    late = _pending(_fx_fill(4, Side.BUY, 2 * DAY))
    assert match_fx_settlement(_settlement(), late).reason is FxBreak.UNMATCHED


def test_fx_settlement_amount_break_lists_the_differences() -> None:
    """The referenced fill exchanges other amounts: a break with expected and reported values."""
    pending = _pending(_fx_fill(1, Side.BUY, HOUR))
    match = match_fx_settlement(_settlement(quote="1084999.0000", reference=1), pending)
    assert (match.matched, match.reason, match.fill_id) == (
        False,
        FxBreak.AMOUNTS,
        uuid.UUID(int=1),
    )
    assert match.differences == (
        Difference("quote_amount", Decimal("1085000.0000"), Decimal("1084999.0000")),
    )
    assert match_fx_settlement(_settlement(quote="1084999.0000"), pending).reason is (
        FxBreak.UNMATCHED
    )


def test_fx_settlement_with_candidates_on_both_sides_is_ambiguous() -> None:
    """A buy and a sell of equal amounts: the event carries no direction, so no guess."""
    pending = _pending(_fx_fill(1, Side.BUY, HOUR), _fx_fill(2, Side.SELL, 2 * HOUR))
    match = match_fx_settlement(_settlement(), pending)
    assert (match.matched, match.reason, match.fill_id) == (False, FxBreak.AMBIGUOUS, None)
    assert match_fx_settlement(_settlement(reference=2), pending).matched


def test_fx_settlement_breaks_are_recorded_by_reason() -> None:
    """Each reason becomes one fx_settlement_break.v1; a match records nothing (amendment 5)."""
    settlement = _settlement(quote="1084999.0000", reference=1)
    amounts = match_fx_settlement(settlement, _pending(_fx_fill(1, Side.BUY, HOUR)))
    recorded = fx_settlement_break(settlement, amounts, recorded_ns=2 * DAY + 5)
    assert recorded is not None
    assert (recorded.reason, recorded.fill_id, recorded.settlement_id) == (
        FxBreak.AMOUNTS,
        uuid.UUID(int=1),
        settlement.id,
    )
    assert recorded.differences == amounts.differences
    assert (recorded.ts_event_ns, recorded.ts_ingest_ns) == (2 * DAY, 2 * DAY + 5)
    unmatched = fx_settlement_break(
        _settlement(), match_fx_settlement(_settlement(), []), recorded_ns=2 * DAY
    )
    assert unmatched is not None
    assert (unmatched.reason, unmatched.fill_id, unmatched.differences) == (
        FxBreak.UNMATCHED,
        None,
        (),
    )
    both = _pending(_fx_fill(1, Side.BUY, HOUR), _fx_fill(2, Side.SELL, 2 * HOUR))
    ambiguous = fx_settlement_break(
        _settlement(), match_fx_settlement(_settlement(), both), recorded_ns=2 * DAY
    )
    assert ambiguous is not None
    assert ambiguous.reason is FxBreak.AMBIGUOUS
    assert unmatched.id == ambiguous.id  # derived from the settlement's source and id
    matched = match_fx_settlement(_settlement(reference=1), _pending(_fx_fill(1, Side.BUY, HOUR)))
    assert fx_settlement_break(_settlement(reference=1), matched, recorded_ns=2 * DAY) is None
    other = msgspec.structs.replace(_settlement(), id=uuid.UUID(int=98))
    with pytest.raises(PortfolioError, match="match of"):
        fx_settlement_break(other, matched, recorded_ns=2 * DAY)


# --- T9: Deribit inverse P&L ---------------------------------------------------------------------
@pytest.mark.parametrize(
    ("notional", "exit_price", "pnl"),
    [
        ("8000", "10000", "0.2"),
        ("8000", "12000", "0.33333333"),
        ("-8000", "10000", "-0.2"),
        ("-8000", "12000", "-0.33333333"),
    ],
)
def test_inverse_pnl_reproduces_deribit(notional: str, exit_price: str, pnl: str) -> None:
    """8,000 x (1/8,000 - 1/exit) in BTC, rounded once to 0.00000001 (HALF_EVEN)."""
    increment_btc = increment(_config(), "BTC")
    assert increment_btc == SATOSHI
    result = inverse_pnl(Decimal(notional), Decimal(8000), Decimal(exit_price), increment_btc)
    assert result == Decimal(pnl)


def test_inverse_pnl_refuses_non_positive_prices() -> None:
    """Both prices are divisors."""
    with pytest.raises(PortfolioError, match="positive"):
        inverse_pnl(Decimal(8000), Decimal(0), Decimal(10000), SATOSHI)


# --- T10: Deribit funding ------------------------------------------------------------------------
def _funding(interval_ns: int, amount: Decimal | None = None) -> FundingPaymentV2:
    return FundingPaymentV2(
        id=uuid.UUID(int=13),
        source="ingest/test",
        ts_event_ns=8 * HOUR,
        ts_ingest_ns=8 * HOUR,
        instrument_id=INVERSE,
        funding_rate=Decimal("0.0005"),
        mark_price=Decimal("10007.50"),
        interval_ns=interval_ns,
        amount=amount,
    )


FUNDING_CONFIG = {"funding": {str(INVERSE): {"rate_period_ns": 8 * HOUR, **_READ}}}


def test_funding_formula_reproduces_deribit() -> None:
    """Long 1 BTC at 0.05 %: 8 h → pays 0.0005 BTC; 1 min → 0.00000104 BTC; shorts receive.

    The formula as printed, with the rate period and increment from the fixture's
    configuration; phase 1's fold does not call it (amendment 3, F-H).
    """
    config = _config(**FUNDING_CONFIG)
    period = config.funding[str(INVERSE)].rate_period_ns
    btc = increment(config, "BTC")
    rate = Decimal("0.0005")
    assert funding_amount(rate, Decimal(1), 8 * HOUR, period, btc) == Decimal("-0.0005")
    minute = 60 * 10**9
    assert funding_amount(rate, Decimal(1), minute, period, btc) == Decimal("-0.00000104")
    assert funding_amount(rate, Decimal(-1), minute, period, btc) == Decimal("0.00000104")


def test_reported_funding_is_posted_as_reported() -> None:
    """A reported amount is posted unchanged, never recomputed (E2)."""
    reported = Decimal("-0.00049999")
    swap = _swap(PerpetualType.INVERSE)
    posting = funding_posting(_funding(8 * HOUR, reported), swap, ACCOUNT)
    assert posting == Posting(ACCOUNT, "BTC", reported, PostingKind.FUNDING, uuid.UUID(int=13))
    assert funding_posting(_funding(HOUR, Decimal(0)), swap, ACCOUNT) is None


def test_funding_without_an_amount_is_refused() -> None:
    """No amount: refused for inverse and linear swaps alike (amendment 3; R10 (c), R1)."""
    with pytest.raises(PortfolioError, match="R10"):
        funding_posting(_funding(HOUR), _swap(PerpetualType.INVERSE), ACCOUNT)
    event = msgspec.structs.replace(_funding(HOUR), instrument_id=LINEAR)
    with pytest.raises(PortfolioError, match="R1"):
        funding_posting(event, _swap(PerpetualType.LINEAR), ACCOUNT)
    with pytest.raises(PortfolioError, match="instrument"):
        funding_posting(event, _swap(PerpetualType.INVERSE), ACCOUNT)
    with pytest.raises(ConfigurationError, match="XRP"):
        increment(_config(), "XRP")


# --- T21: margin ---------------------------------------------------------------------------------
MARGIN_CONFIG = {
    "margin": {
        str(ES): {
            "basis": "per_contract",
            "initial": "12000",
            "maintenance": "11000",
            "currency": "USD",
            **_READ,
        },
        str(LINEAR): {
            "basis": "notional_fraction",
            "initial": "0.02",
            "maintenance": "0.01",
            "currency": "USDC",
            **_READ,
        },
        str(INVERSE): {
            "basis": "notional_fraction",
            "initial": "0.02",
            "maintenance": "0.01",
            "currency": "BTC",
            **_READ,
        },
    }
}


def test_margin_requirements_are_exact_products() -> None:
    """Per contract x |quantity|; fraction x |quantity| x multiplier x mark (T21)."""
    model = PerInstrumentMargin(_config(**MARGIN_CONFIG))
    assert model.requirement(_future(), Decimal(-3), None) == Requirement(
        "USD", Decimal(36000), Decimal(33000)
    )
    linear = _swap(PerpetualType.LINEAR)
    assert model.requirement(linear, Decimal("1.5"), Decimal("60000.10")) == Requirement(
        "USDC", Decimal("1800.0030"), Decimal("900.0015")
    )


def test_margin_unavailable_is_never_zero() -> None:
    """No entry, no mark, or an inverse swap: requirement unavailable, the instrument unmargined."""
    model = PerInstrumentMargin(_config(**MARGIN_CONFIG))
    linear = _swap(PerpetualType.LINEAR)
    inverse = _swap(PerpetualType.INVERSE)
    assert model.requirement(linear, Decimal(1), None) is None
    assert model.requirement(inverse, Decimal(100), Decimal(60000)) is None
    assert model.requirement(_crypto(), Decimal(1), Decimal(60000)) is None
    positions: list[tuple[Instrument, Decimal, Decimal | None]] = [
        (_future(), Decimal(2), None),
        (inverse, Decimal(100), Decimal(60000)),
        (_crypto(), Decimal(0), Decimal(60000)),
        (linear, Decimal(1), None),
    ]
    rows, unmargined = margin_rows(model, positions, {"USD": Decimal(30000)})
    assert unmargined == (INVERSE, LINEAR)
    (usd,) = rows
    assert (usd.currency, usd.initial, usd.maintenance) == ("USD", Decimal(24000), Decimal(22000))


@pytest.mark.parametrize(
    ("equity", "available", "deficit"),
    [("40000", "4000", "0"), ("30000", "-6000", "3000"), ("33000", "-3000", "0")],
)
def test_margin_used_available_and_deficit(equity: str, available: str, deficit: str) -> None:
    """Used = initial; available = equity - initial; deficit = maintenance above equity."""
    model = PerInstrumentMargin(_config(**MARGIN_CONFIG))
    rows, unmargined = margin_rows(
        model, [(_future(), Decimal(-3), None)], {"USD": Decimal(equity)}
    )
    assert unmargined == ()
    (usd,) = rows
    assert usd.initial == Decimal(36000)
    assert usd.available == Decimal(available)
    assert usd.deficit == Decimal(deficit)
    with pytest.raises(PortfolioError, match="equity"):
        margin_rows(model, [(_future(), Decimal(1), None)], {})
