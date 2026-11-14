"""Currency-exchange cost for a NOK investor, as a sensitivity (P2-5).

Shared by gap_risk.py and hold_exit_study.py so both use the same scenarios
and the same arithmetic. Research only: the bot does not import this.

The model. A stock priced in another currency needs two conversions: NOK to
that currency to buy, and back to NOK after the sale. Each conversion loses
`bps` hundredths of a percent of the amount converted. With c = bps / 10,000
and a return of r (as a fraction) in the stock's own currency:

    P NOK in  ->  P(1-c) invested  ->  P(1-c)(1+r) proceeds  ->  P(1-c)^2 (1+r) NOK out

Without the cost the investor would hold P(1+r), so the cost, in percent of
the NOK put in, is

    fx_cost_pct = (1 + r) * (2c - c^2) * 100

It is a little more than 2 x bps on a winning trade and a little less on a
losing one, because the second conversion is on the sale proceeds.

What it is not:
- Not a broker's price list. The four scenarios are round numbers for a
  sensitivity table. Nothing here is DNB's or anyone's verified pricing.
- Not the exchange rate. Both scripts measure returns in the stock's own
  currency and ignore how the rate moved during the hold; that is unchanged.
- Not a commission, minimum fee, spread or slippage. Those stay wherever the
  script already has them, and this is subtracted separately, once.

A stock priced in NOK has no conversion and no cost. A stock whose currency
is unknown is charged as foreign, the cautious assumption, and both scripts
print how many trades that was.
"""
import numpy as np

HOME_CURRENCY = "NOK"

# One-way cost per conversion, in basis points. Illustrative, not verified pricing.
FX_SCENARIOS = (("zero-cost baseline", 0.0), ("low", 10.0), ("medium", 25.0), ("high", 50.0))

# Yahoo ticker suffix -> trading currency, for lists that carry no currency
# column (evaluation_universe.csv). No suffix is a US listing. As
# newsbot.fills._SUFFIX_CURRENCY, plus India for the evaluation basket.
SUFFIX_CURRENCY = {
    "OL": "NOK", "ST": "SEK", "CO": "DKK",
    "DE": "EUR", "F": "EUR", "PA": "EUR", "AS": "EUR", "MI": "EUR", "MC": "EUR",
    "HE": "EUR", "BR": "EUR", "LS": "EUR", "VI": "EUR", "IR": "EUR",
    "NS": "INR", "BO": "INR",
}
UNKNOWN = "unknown"


def currency_of(symbol, listed=None):
    """The stock's trading currency: the list's own column if it has one, else by suffix.

    `UNKNOWN` when neither says; never a guess.
    """
    listed = str(listed or "").strip().upper()
    if listed:
        return listed
    if "." not in symbol:
        return "USD"
    return SUFFIX_CURRENCY.get(symbol.rsplit(".", 1)[1].upper(), UNKNOWN)


def is_converted(currency):
    """True unless the stock is priced in NOK. Unknown counts as converted."""
    return currency != HOME_CURRENCY


def fx_cost_pct(return_pct, converted, bps):
    """Cost of the buy and sell conversions, in percent of the NOK put in.

    `return_pct` is the return in the stock's own currency before this cost;
    `converted` says which trades needed a conversion. Scalars or arrays.
    """
    if not np.isfinite(bps) or bps < 0:
        raise ValueError(f"FX cost must be zero or more basis points, got {bps!r}")
    c = bps / 10_000.0
    cost = (1.0 + np.asarray(return_pct, dtype=float) / 100.0) * (2.0 * c - c * c) * 100.0
    return np.where(np.asarray(converted, dtype=bool), cost, 0.0)


def column(kind, bps):
    return f"{kind}_{bps:g}bps"


def add_fx_columns(frame, own_currency_return, before_fx):
    """Add `fx_cost_<bps>bps` and `after_fx_<bps>bps` for every scenario. Returns the frame.

    `own_currency_return` is the column the conversions are sized by (the
    return in the stock's currency); `before_fx` is the column the cost comes
    off (the same, or that return after the script's own trading costs). The
    frame needs a `currency` column. Every scenario starts from `before_fx`,
    so scenarios never stack, and a frame that already has these columns is
    refused rather than charged twice.
    """
    existing = [name for name in frame.columns if name.startswith(("fx_cost_", "after_fx_"))]
    if existing:
        raise ValueError(f"FX cost was already applied to this frame: {existing}")
    if frame["currency"].isna().any():
        raise ValueError("every trade needs a currency; use currency_of, which says 'unknown'")
    converted = frame["currency"].map(is_converted).to_numpy(dtype=bool)
    for _name, bps in FX_SCENARIOS:
        cost = fx_cost_pct(frame[own_currency_return].to_numpy(), converted, bps)
        frame[column("fx_cost", bps)] = cost
        frame[column("after_fx", bps)] = frame[before_fx].to_numpy() - cost
    return frame


def describe(frame):
    """Lines saying which trades were charged, for the top of an FX section."""
    counts = frame["currency"].value_counts()
    home = int(counts.get(HOME_CURRENCY, 0))
    unknown = int(counts.get(UNKNOWN, 0))
    lines = [
        "Assumption, not a broker's price list: each conversion (NOK to the stock's currency to buy,",
        "and back to NOK after the sale) costs the stated basis points of the amount converted.",
        "Returns are in the stock's own currency; the exchange rate's own move is not modelled.",
        f"Trades by currency: {', '.join(f'{name} {count}' for name, count in counts.items())}.",
        f"Charged: {len(frame) - home} of {len(frame)} trades. Not charged: {home} priced in {HOME_CURRENCY}.",
    ]
    if unknown:
        symbols = sorted(frame.loc[frame["currency"] == UNKNOWN, "symbol"].unique())
        lines.append(f"Currency unknown for {unknown} trades ({', '.join(symbols)}): charged as foreign.")
    else:
        lines.append("Currency unknown: 0 trades.")
    return lines
