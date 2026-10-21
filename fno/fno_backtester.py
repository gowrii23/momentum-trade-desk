"""
fno_backtester.py — NIFTY monthly index-option BUYING backtest, cost model,
Monte Carlo projection and NIFTYBEES / NIFTY 50 buy-and-hold comparison.

Buying only. No option selling, no futures, no leverage beyond the premium paid.
Never invents price data: real runs read only NSE CSVs you download yourself.

Commands
    python fno_backtester.py test                      # accounting tests (sample data)
    python fno_backtester.py checklist                 # NSE download checklist (3y + 5y)
    python fno_backtester.py inspect --options F... --index F...
    python fno_backtester.py run --options F... --index F... [--benchmark F...]
    python fno_backtester.py suggest --index F...      # 12-month honest sample picker

Sections
    1. CONFIG            lot sizes (cited), Zerodha charges (dated), slippage
    2. DATA LOADER       the only code that knows NSE file formats
    3. COST MODEL        per-order + exercise charges with breakdown
    4. SIMULATOR         cash account, positions, expiry settlement, bust rule
    5. STRATEGY          baseline momentum signal + strike picker (not optimised)
    6. METRICS           returns, drawdown, win rate, costs, affordability
    7. MONTE CARLO       bootstrap of real per-trade returns
    8. TESTS             hand-calculated accounting checks
    9. REPORTING / CLI
"""

from __future__ import annotations

import argparse
import glob
import math
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import date, timedelta

import numpy as np
import pandas as pd

# =============================================================================
# 1. CONFIG
# =============================================================================

STARTING_CAPITAL = 25_000.0
SLIPPAGE = 0.01            # 1% adverse on every fill (assumption, configurable)
RISK_PCT = 0.10            # max premium at risk per trade, share of equity
SIGNAL_THRESHOLD = 0.005   # Nifty close-to-close move that triggers a trade
MIN_CONTRACTS = 1000       # liquidity filter on the entry day
MIN_DAYS_TO_EXPIRY = 3     # trading days left (after entry day, incl. expiry)
TARGET = 0.60              # +60% on entry fill
STOP = 0.40                # -40% on entry fill
SEED = 42


@dataclass(frozen=True)
class Charges:
    """Zerodha F&O OPTIONS charges, checked on zerodha.com/charges on 2026-10-20.

    Brokerage           Flat Rs 20 per executed order
    STT                 0.15% on sell side (on premium)
                        0.15% of intrinsic value on options bought and exercised
    Transaction (NSE)   0.03553% on premium
    GST                 18% on (brokerage + SEBI charges + transaction charges)
    SEBI                Rs 10 / crore
    Stamp duty          0.003% (Rs 300 / crore) on buy side
    These current rates are applied to ALL years (conservative: older STT and
    NSE fees were lower).
    """
    brokerage_per_order: float = 20.0
    stt_sell: float = 0.0015
    stt_exercise: float = 0.0015
    txn: float = 0.0003553
    sebi_per_crore: float = 10.0
    gst: float = 0.18
    stamp_buy: float = 0.00003


ZERODHA = Charges()
NO_CHARGES = Charges(0, 0, 0, 0, 0, 0, 0)
CHARGES_CHECKED_ON = "2026-10-20 (zerodha.com/charges, F&O Options column)"

# NIFTY lot size by MONTHLY expiry. Each row: first monthly expiry, last
# monthly expiry, lot size, source. Weekly contracts switched on different
# dates (e.g. weekly 02-Jan-2025 was already 75 while monthly 30-Jan-2025 was
# still 25); this table is for monthly contracts only.
LOT_SIZE_SCHEDULE = [
    # (first expiry month, last expiry month, lot, source)
    ("2021-07", "2024-04", 50,
     "NSE/FAOP/47854 (31-Mar-2021): 75->50 from the Jul-2021 monthly; "
     "NSE/FAOP/61415 (02-Apr-2024): Apr-2024 monthly is the last at 50"),
    ("2024-05", "2025-01", 25,
     "NSE/FAOP/61415 (02-Apr-2024): 50->25, first monthly 30-May-2024; "
     "NSE/FAOP/64625 (18-Oct-2024): last monthly at 25 is 30-Jan-2025"),
    ("2025-02", "2025-12", 75,
     "NSE/FAOP/64625 (18-Oct-2024): 25->75, first monthly 27-Feb-2025; "
     "NSE/FAOP/67372 (28-Mar-2025): NIFTY unchanged at 75"),
    ("2026-01", "2026-12", 65,
     "NSE/FAOP/70616 (03-Oct-2025): 75->65, last monthly at 75 is "
     "30-Dec-2025, first at 65 is 27-Jan-2026"),
]


def lot_size_for_expiry(expiry) -> int:
    """Lot size of a NIFTY MONTHLY contract, by expiry month. Raises outside the
    verified range (Jul-2021 .. Dec-2026 expiries)."""
    m = pd.Timestamp(expiry).to_period("M")
    for first, last, lot, _ in LOT_SIZE_SCHEDULE:
        if pd.Period(first, "M") <= m <= pd.Period(last, "M"):
            return lot
    raise ValueError(
        f"NIFTY lot size for expiry month {m} is not verified in LOT_SIZE_SCHEDULE "
        "(verified: Jul-2021 to Dec-2026 monthly expiries). Ask before using.")


# =============================================================================
# 2. DATA LOADER  (only section that knows file formats)
# =============================================================================

SAMPLE_LABEL = "SAMPLE_DATA_FOR_ACCOUNTING_TESTS_ONLY"

OPTION_COLS = {
    "symbol": ["symbol"],
    "date": ["date"],
    "expiry": ["expiry", "expiry date"],
    "option_type": ["option type", "option_type", "optiontype"],
    "strike": ["strike price", "strike", "strike_price"],
    "close": ["close", "close price"],
    "contracts": ["no. of contracts", "no of contracts", "contracts"],
    "underlying": ["underlying value", "underlying"],
}
INDEX_COLS = {
    "date": ["date", "index date"],
    "close": ["close", "close price", "closing index value"],
}


def _norm(c: str) -> str:
    return re.sub(r"\s+", " ", str(c).replace("﻿", "")).strip().lower()


def _pick(df: pd.DataFrame, aliases: dict, path: str, optional=()) -> pd.DataFrame:
    cols = {_norm(c): c for c in df.columns}
    out = {}
    for key, names in aliases.items():
        hit = next((cols[n] for n in names if n in cols), None)
        if hit is None:
            if key in optional:
                continue
            raise ValueError(f"{path}: no column for '{key}' (have {list(df.columns)})")
        out[key] = df[hit]
    return pd.DataFrame(out)


def _num(s: pd.Series) -> pd.Series:
    s = s.astype(str).str.replace(",", "", regex=False).str.strip()
    s = s.mask(s.isin(["-", "", "nan", "NaN", "None"]))
    return pd.to_numeric(s, errors="coerce")


def _dates(s: pd.Series) -> pd.Series:
    s = s.astype(str).str.strip()
    out = pd.to_datetime(s, format="%d-%b-%Y", errors="coerce")
    miss = out.isna()
    if miss.any():
        out[miss] = pd.to_datetime(s[miss], format="mixed", dayfirst=True, errors="coerce")
    return out.dt.normalize()


def _read_csv(path: str) -> pd.DataFrame:
    return pd.read_csv(path, dtype=str, skipinitialspace=True, encoding="utf-8-sig")


def load_nse_option_csv(paths) -> pd.DataFrame:
    """Raw NSE 'Contract-wise Price Volume Data' CSVs -> one clean table."""
    frames = []
    for p in paths:
        raw = _read_csv(p)
        df = _pick(raw, OPTION_COLS, p, optional=("symbol", "underlying"))
        for c in ("date", "expiry"):
            df[c] = _dates(df[c])
        for c in ("strike", "close", "contracts", "underlying"):
            if c in df:
                df[c] = _num(df[c])
        df["option_type"] = df["option_type"].astype(str).str.strip().str.upper()
        if "symbol" in df:
            df = df[df["symbol"].astype(str).str.strip().str.upper() == "NIFTY"]
        if "underlying" not in df:
            df["underlying"] = np.nan
        df["source_file"] = os.path.basename(p)
        frames.append(df.drop(columns=["symbol"], errors="ignore"))
    if not frames:
        raise ValueError("no option files given")
    df = pd.concat(frames, ignore_index=True)
    df = df.dropna(subset=["date", "expiry", "strike"])
    df = df[df["option_type"].isin(["CE", "PE"])]
    df["contracts"] = df["contracts"].fillna(0)
    df = (df.sort_values(["date", "expiry", "option_type", "strike", "source_file"])
            .drop_duplicates(["date", "expiry", "option_type", "strike"], keep="last"))
    return df[["date", "expiry", "option_type", "strike", "close", "contracts",
               "underlying", "source_file"]].reset_index(drop=True)


def load_nse_index_csv(paths) -> pd.DataFrame:
    """NIFTY 50 'Historical Index Data' or NIFTYBEES price files -> date, close."""
    frames = []
    for p in paths:
        raw = _read_csv(p)
        df = _pick(raw, INDEX_COLS, p)
        df["date"] = _dates(df["date"])
        df["close"] = _num(df["close"])
        frames.append(df)
    if not frames:
        raise ValueError("no index files given")
    df = pd.concat(frames, ignore_index=True).dropna()
    return (df.sort_values("date").drop_duplicates("date", keep="last")
              .reset_index(drop=True))


def load_sample_data():
    """Tiny INVENTED data for accounting tests. Not market data."""
    idx = pd.DataFrame({
        "date": pd.to_datetime(["2025-03-03", "2025-03-04", "2025-03-05",
                                "2025-03-06", "2025-03-07"]),
        "close": [22000.0, 22220.0, 22230.0, 22500.0, 22510.0],
    })
    exp = pd.Timestamp("2025-03-27")  # lot 75 era
    rows = []
    ce = {22200: [20, 25, 30, 50, 52], 22250: [28, 34, 40, 62, 64],
          22300: [12, 15, 20, 36, 37]}
    pe = {22200: [60, 45, 44, 30, 29], 22250: [80, 62, 60, 42, 41]}
    for kind, book in (("CE", ce), ("PE", pe)):
        for k, closes in book.items():
            for d, c, u in zip(idx["date"], closes, idx["close"]):
                rows.append((d, exp, kind, float(k), float(c), 5000.0, u))
    opts = pd.DataFrame(rows, columns=["date", "expiry", "option_type", "strike",
                                       "close", "contracts", "underlying"])
    opts["source_file"] = SAMPLE_LABEL
    return opts, idx


def report_data(opts: pd.DataFrame, idx: pd.DataFrame) -> dict:
    """What the files actually contain. Run before any backtest."""
    rep = {}
    print("\n=== DATA REPORT (from file contents, not file names) ===")
    print(f"Index rows {len(idx)}: {idx['date'].min().date()} -> {idx['date'].max().date()}")
    gaps = idx["date"].diff().dt.days
    big = idx.loc[gaps > 5, "date"]
    for d in big:
        prev = idx.loc[idx["date"] < d, "date"].max()
        print(f"  index gap: {prev.date()} -> {d.date()} ({(d - prev).days} days)")
    rep["index_gaps"] = [str(d.date()) for d in big]

    print(f"Option rows {len(opts)}: {opts['date'].min().date()} -> {opts['date'].max().date()}")
    summ = (opts.groupby(["expiry", "option_type"])
                .agg(first=("date", "min"), last=("date", "max"),
                     days=("date", "nunique"), strikes=("strike", "nunique"),
                     liquid_rows=("contracts", lambda c: int((c >= MIN_CONTRACTS).sum())))
                .reset_index())
    with pd.option_context("display.width", 140, "display.max_rows", 500):
        print(summ.to_string(index=False))
    rep["expiries"] = summ

    have = {(e.to_period("M"), t) for e, t in zip(summ["expiry"], summ["option_type"])}
    months = pd.period_range(opts["expiry"].min().to_period("M"),
                             opts["expiry"].max().to_period("M"), freq="M")
    missing = [(str(m), t) for m in months for t in ("CE", "PE") if (m, t) not in have]
    print("Missing expiry-month x type:", missing if missing else "none")
    rep["missing"] = missing

    multi = summ.groupby(summ["expiry"].dt.to_period("M"))["expiry"].nunique()
    weekly = list(multi[multi > 1].index.astype(str))
    if weekly:
        print("Months with >1 expiry (weekly contracts present; only the last "
              "expiry of each month is used):", weekly)
    rep["months_with_weeklies"] = weekly

    cal = idx["date"]
    by_day = {d: g for d, g in monthly_only(opts).groupby("date")}
    no_cov = []
    for d in cal[(cal >= opts["date"].min()) & (cal <= opts["date"].max())]:
        if d not in by_day or pick_expiry(by_day[d], d, cal) is None:
            no_cov.append(d)
    print(f"Trading days with no usable expiry (>= {MIN_DAYS_TO_EXPIRY} days left): "
          f"{len(no_cov)}" + (f" e.g. {[str(x.date()) for x in no_cov[:8]]}" if no_cov else ""))
    rep["days_without_contract"] = len(no_cov)
    return rep


def monthly_only(opts: pd.DataFrame) -> pd.DataFrame:
    """Keep only the last expiry of each calendar month (the monthly contract)."""
    exp = pd.Series(opts["expiry"].unique())
    keep = exp.groupby(exp.dt.to_period("M")).max()
    return opts[opts["expiry"].isin(keep.values)].reset_index(drop=True)


# =============================================================================
# 3. COST MODEL
# =============================================================================

def order_costs(turnover: float, side: str, ch: Charges = ZERODHA) -> dict:
    """Charges for one executed option order. turnover = premium x quantity."""
    brokerage = ch.brokerage_per_order if turnover > 0 else 0.0
    stt = ch.stt_sell * turnover if side == "SELL" else 0.0
    txn = ch.txn * turnover
    sebi = ch.sebi_per_crore * turnover / 1e7
    gst = ch.gst * (brokerage + txn + sebi)
    stamp = ch.stamp_buy * turnover if side == "BUY" else 0.0
    total = brokerage + stt + txn + sebi + gst + stamp
    return dict(brokerage=brokerage, stt=stt, txn=txn, sebi=sebi, gst=gst,
                stamp=stamp, total=total)


def exercise_costs(intrinsic_value: float, ch: Charges = ZERODHA) -> dict:
    """ITM option held to expiry: STT on intrinsic value. Index options are
    cash-settled; no brokerage order is placed."""
    stt = ch.stt_exercise * intrinsic_value
    return dict(brokerage=0.0, stt=stt, txn=0.0, sebi=0.0, gst=0.0, stamp=0.0,
                total=stt)


# =============================================================================
# 4. SIMULATOR
# =============================================================================

class DepositBlocked(Exception):
    pass


class InsufficientFunds(Exception):
    pass


@dataclass
class Position:
    option_type: str
    strike: float
    expiry: pd.Timestamp
    qty: int
    lot_size: int
    entry_date: pd.Timestamp
    entry_close: float
    entry_fill: float
    entry_costs: float
    last_price: float


@dataclass
class Account:
    cash: float = STARTING_CAPITAL
    charges: Charges = ZERODHA
    slippage: float = SLIPPAGE
    position: Position | None = None
    busted: bool = False
    bust_date: pd.Timestamp | None = None

    def deposit(self, amount):
        raise DepositBlocked(f"deposit of {amount} refused: capital is fixed at "
                             f"Rs {STARTING_CAPITAL:,.0f}, no top-ups")

    def equity(self) -> float:
        p = self.position
        return self.cash + (p.last_price * p.qty if p else 0.0)

    def buy_cost(self, close: float, qty: int) -> tuple[float, float, dict]:
        fill = close * (1 + self.slippage)
        c = order_costs(fill * qty, "BUY", self.charges)
        return fill * qty + c["total"], fill, c

    def buy(self, d, option_type, strike, expiry, close, lot_size, lots=1) -> Position:
        if self.position is not None:
            raise RuntimeError("one position at a time")
        qty = lot_size * lots
        cost, fill, c = self.buy_cost(close, qty)
        if cost > self.cash + 1e-9:
            raise InsufficientFunds(f"order needs Rs {cost:,.2f}, cash Rs {self.cash:,.2f}")
        self.cash -= cost
        self.position = Position(option_type, strike, pd.Timestamp(expiry), qty, lot_size,
                                 pd.Timestamp(d), close, fill, c["total"], close)
        return self.position

    def sell(self, close) -> dict:
        p = self.position
        fill = close * (1 - self.slippage)
        value = fill * p.qty
        c = order_costs(value, "SELL", self.charges)
        self.cash += value - c["total"]
        self.position = None
        return dict(exit_close=close, exit_fill=fill, exit_value=value,
                    exit_costs=c["total"], exit_cost_detail=c)

    def settle_expiry(self, spot) -> dict:
        p = self.position
        intrinsic = max(spot - p.strike, 0.0) if p.option_type == "CE" else max(p.strike - spot, 0.0)
        value = intrinsic * p.qty
        c = exercise_costs(value, self.charges)
        self.cash += value - c["total"]
        self.position = None
        return dict(exit_close=intrinsic, exit_fill=intrinsic, exit_value=value,
                    exit_costs=c["total"], exit_cost_detail=c)


# =============================================================================
# 5. STRATEGY  (baseline, NOT optimised)
# =============================================================================

def trading_days_left(cal: pd.Series, d, expiry) -> int:
    """Trading days after d up to and including expiry (from the index calendar;
    business days beyond the end of the calendar)."""
    d, expiry = pd.Timestamp(d), pd.Timestamp(expiry)
    known = int(((cal > d) & (cal <= expiry)).sum())
    last = cal.max()
    if expiry > last:
        known += len(pd.bdate_range(max(last, d) + pd.Timedelta(days=1), expiry))
    return known


def pick_expiry(day_rows, d, cal):
    """Nearest expiry with >= MIN_DAYS_TO_EXPIRY trading days left on day d.
    day_rows: option rows for day d only."""
    exps = sorted(day_rows.loc[day_rows["expiry"] >= d, "expiry"].unique())
    for e in exps:
        if trading_days_left(cal, d, e) >= MIN_DAYS_TO_EXPIRY:
            return pd.Timestamp(e)
    return None


def liquid_chain(day_rows, d, cal):
    e = pick_expiry(day_rows, d, cal)
    if e is None:
        return None, day_rows.iloc[0:0]
    ch = day_rows[(day_rows["expiry"] == e) & (day_rows["contracts"] >= MIN_CONTRACTS) &
                  (day_rows["close"] > 0)]
    return e, ch


def choose_strike(acct: Account, chain, side, spot, budget):
    """Nearest-to-the-money strike of `side` whose 1-lot cost <= budget and cash."""
    rows = chain[chain["option_type"] == side]
    if rows.empty:
        return None, "NO_LIQUID_CONTRACT"
    best = None
    for r in rows.itertuples():
        lot = lot_size_for_expiry(r.expiry)
        cost, _, _ = acct.buy_cost(r.close, lot)
        if cost <= budget and cost <= acct.cash:
            key = (abs(r.strike - spot), cost)
            if best is None or key < best[0]:
                best = (key, r, lot, cost)
    if best is None:
        return None, "UNAFFORDABLE"
    return best, "OK"


def cheapest_liquid_lot(acct, chain):
    if chain.empty:
        return np.nan
    return min(acct.buy_cost(r.close, lot_size_for_expiry(r.expiry))[0]
               for r in chain.itertuples())


def atm_lot_cost(chain, spot):
    """Average CE/PE premium x lot at the strike nearest spot (no costs)."""
    if chain.empty:
        return np.nan
    k = chain.iloc[(chain["strike"] - spot).abs().argsort()].iloc[0]["strike"]
    at = chain[chain["strike"] == k]
    return float((at["close"] * at["expiry"].map(lot_size_for_expiry)).mean())


@dataclass
class Params:
    risk_pct: float = RISK_PCT
    entry: str = "next_close"   # next_close: no look-ahead; same_close: optimistic
    slippage: float = SLIPPAGE
    charges: Charges = ZERODHA
    threshold: float = SIGNAL_THRESHOLD
    name: str = "baseline"


def run_backtest(opts, idx, prm: Params = Params()):
    opts = monthly_only(opts)
    cal = idx["date"].reset_index(drop=True)
    start, end = opts["date"].min(), opts["date"].max()
    days = cal[(cal >= start) & (cal <= end)].reset_index(drop=True)
    close = idx.set_index("date")["close"]
    by_day = {d: g for d, g in opts.groupby("date")}
    px = opts.set_index(["date", "expiry", "option_type", "strike"])["close"]

    acct = Account(charges=prm.charges, slippage=prm.slippage)
    trades, events, curve = [], [], []
    pending = None   # (signal_date, side) waiting for next close
    tid = 0
    open_meta = {}

    for i, d in enumerate(days):
        chain_today = by_day.get(d, opts.iloc[0:0])
        spot = float(close.get(d, np.nan))

        # --- manage open position -------------------------------------------
        p = acct.position
        if p is not None:
            key = (d, p.expiry, p.option_type, p.strike)
            price = px.get(key, np.nan)
            if not np.isnan(price):
                p.last_price = float(price)
            exit_reason = None
            if d >= p.expiry:
                exit_reason = "EXPIRY_SETTLED"
            elif not np.isnan(price):
                nxt = cal[cal > d]
                if price >= p.entry_fill * (1 + TARGET):
                    exit_reason = "TARGET"
                elif price <= p.entry_fill * (1 - STOP):
                    exit_reason = "STOP"
                elif nxt.empty or nxt.iloc[0] >= p.expiry:
                    exit_reason = "DAY_BEFORE_EXPIRY"
            if exit_reason:
                if exit_reason == "EXPIRY_SETTLED":
                    s = float(close.get(p.expiry, spot))
                    res = acct.settle_expiry(s)
                else:
                    res = acct.sell(float(price))
                trades.append(_trade_row(open_meta, p, d, exit_reason, res, acct))

        # --- signal ---------------------------------------------------------
        side = None
        pos = close.index.searchsorted(d)
        if pos > 0:
            chg = spot / close.iloc[pos - 1] - 1
            if chg > prm.threshold:
                side = "CE"
            elif chg < -prm.threshold:
                side = "PE"

        todo = None
        if prm.entry == "same_close" and side:
            todo = (d, side)
        elif prm.entry == "next_close":
            todo, pending = pending, ((d, side) if side else None)

        # --- entry ----------------------------------------------------------
        exp, chain = liquid_chain(chain_today, d, cal)
        if todo and not acct.busted:
            sig_date, s_side = todo
            if acct.position is not None:
                events.append(dict(date=d, signal_date=sig_date, side=s_side, event="IN_POSITION"))
            else:
                eq = acct.equity()
                budget = prm.risk_pct * eq
                pick, status = choose_strike(acct, chain, s_side, spot, budget)
                if status != "OK":
                    events.append(dict(date=d, signal_date=sig_date, side=s_side, event=status,
                                       budget=round(budget, 2), equity=round(eq, 2)))
                else:
                    (_, r, lot, cost) = pick
                    acct.buy(d, s_side, r.strike, r.expiry, r.close, lot)
                    tid += 1
                    otm = ((r.strike - spot) if s_side == "CE" else (spot - r.strike)) / spot
                    open_meta = dict(trade_id=tid, signal_date=sig_date, side=s_side,
                                     spot_at_entry=spot, pct_otm=100 * otm,
                                     budget=budget, equity_before=eq, entry_outlay=cost)
                    events.append(dict(date=d, signal_date=sig_date, side=s_side, event="ENTERED",
                                       budget=round(budget, 2), equity=round(eq, 2)))

        # --- mark to market + bust rule -------------------------------------
        eq = acct.equity()
        cheapest = cheapest_liquid_lot(acct, chain)
        curve.append(dict(date=d, cash=acct.cash, equity=eq, nifty=spot,
                          in_position=acct.position is not None,
                          cheapest_liquid_lot=cheapest, atm_lot_cost=atm_lot_cost(chain, spot)))
        if not acct.busted and not np.isnan(cheapest) and eq < cheapest:
            acct.busted, acct.bust_date = True, d
            if acct.position is not None:
                price = px.get((d, acct.position.expiry, acct.position.option_type,
                                acct.position.strike), acct.position.last_price)
                p = acct.position
                res = acct.sell(float(price))
                trades.append(_trade_row(open_meta, p, d, "BUSTED_LIQUIDATION", res, acct))
            events.append(dict(date=d, event="BUSTED", equity=round(eq, 2),
                               cheapest_lot=round(cheapest, 2)))
            curve[-1]["equity"] = acct.equity()
            curve[-1]["cash"] = acct.cash

    if acct.position is not None:   # data ended with a position open: mark, not sell
        events.append(dict(date=days.iloc[-1], event="OPEN_AT_END_MARKED"))
    return dict(trades=pd.DataFrame(trades), events=pd.DataFrame(events),
                curve=pd.DataFrame(curve), account=acct, params=prm)


def _trade_row(meta, p: Position, d, reason, res, acct):
    entry_prem = p.entry_fill * p.qty
    gross = res["exit_value"] - entry_prem
    costs = p.entry_costs + res["exit_costs"]
    slip = (p.entry_fill - p.entry_close) * p.qty
    if reason not in ("EXPIRY_SETTLED",):
        slip += (res["exit_close"] - res["exit_fill"]) * p.qty
    net = gross - costs
    return dict(**{k: meta.get(k) for k in ("trade_id", "signal_date", "side")},
                entry_date=p.entry_date.date(), expiry=p.expiry.date(), strike=p.strike,
                lot_size=p.lot_size, qty=p.qty, spot_at_entry=meta.get("spot_at_entry"),
                pct_otm=round(meta.get("pct_otm", np.nan), 2),
                equity_before=round(meta.get("equity_before", np.nan), 2),
                budget=round(meta.get("budget", np.nan), 2),
                entry_close=p.entry_close, entry_fill=round(p.entry_fill, 4),
                entry_premium=round(entry_prem, 2), entry_costs=round(p.entry_costs, 4),
                exit_date=pd.Timestamp(d).date(), exit_reason=reason,
                exit_close=res["exit_close"], exit_fill=round(res["exit_fill"], 4),
                exit_value=round(res["exit_value"], 2), exit_costs=round(res["exit_costs"], 4),
                slippage_cost=round(slip, 2), gross_pnl=round(gross, 2),
                total_costs=round(costs, 4), net_pnl=round(net, 4),
                ret_on_premium_pct=round(100 * net / (entry_prem + p.entry_costs), 3),
                ret_on_equity_pct=round(100 * net / meta.get("equity_before", np.nan), 4),
                cash_after=round(acct.cash, 2),
                days_held=(pd.Timestamp(d) - p.entry_date).days)


# =============================================================================
# 6. METRICS
# =============================================================================

def max_drawdown(equity) -> float:
    e = np.asarray(equity, dtype=float)
    if e.size == 0:
        return 0.0
    peak = np.maximum.accumulate(e)
    return float(((peak - e) / peak).max() * 100)


def compute_metrics(res, start_capital=STARTING_CAPITAL) -> dict:
    t, ev, c, acct = res["trades"], res["events"], res["curve"], res["account"]
    end = float(c["equity"].iloc[-1]) if len(c) else start_capital
    yrs = (c["date"].iloc[-1] - c["date"].iloc[0]).days / 365.25 if len(c) > 1 else 0
    wins = t[t["net_pnl"] > 0] if len(t) else t
    losses = t[t["net_pnl"] <= 0] if len(t) else t
    skipped = ev["event"].value_counts().to_dict() if len(ev) else {}
    return dict(
        variant=res["params"].name,
        start=c["date"].iloc[0].date() if len(c) else None,
        end=c["date"].iloc[-1].date() if len(c) else None,
        years=round(yrs, 2),
        starting_capital=start_capital,
        ending_capital=round(end, 2),
        total_return_pct=round(100 * (end / start_capital - 1), 2),
        # CAGR is meaningless (and explodes) on less than a year of data.
        cagr_pct=round(100 * ((end / start_capital) ** (1 / yrs) - 1), 2) if yrs >= 1 and end > 0 else None,
        max_drawdown_pct=round(max_drawdown(c["equity"]), 2),
        trades=len(t),
        win_rate_pct=round(100 * len(wins) / len(t), 1) if len(t) else None,
        avg_win=round(wins["net_pnl"].mean(), 2) if len(wins) else None,
        avg_loss=round(losses["net_pnl"].mean(), 2) if len(losses) else None,
        total_charges=round(t["total_costs"].sum(), 2) if len(t) else 0.0,
        total_slippage=round(t["slippage_cost"].sum(), 2) if len(t) else 0.0,
        charges_pct_of_start=round(100 * t["total_costs"].sum() / start_capital, 2) if len(t) else 0.0,
        unaffordable_skips=int(skipped.get("UNAFFORDABLE", 0)),
        no_liquid_contract_skips=int(skipped.get("NO_LIQUID_CONTRACT", 0)),
        busy_skips=int(skipped.get("IN_POSITION", 0)),
        busted="yes" if acct.busted else "no",
        bust_date=acct.bust_date.date() if acct.bust_date is not None else None,
        avg_pct_otm_traded_neg_is_itm=round(t["pct_otm"].mean(), 2) if len(t) else None,
    )


def benchmark_curve(bench: pd.DataFrame, dates, capital=STARTING_CAPITAL) -> pd.Series:
    """Buy-and-hold: all capital at the first close, fractional units, no costs."""
    b = bench.set_index("date")["close"].reindex(pd.DatetimeIndex(dates)).ffill().bfill()
    return capital * b / b.iloc[0]


def affordability(curve: pd.DataFrame) -> pd.DataFrame:
    m = curve.assign(month=curve["date"].dt.to_period("M").astype(str))
    out = m.groupby("month").agg(avg_equity=("equity", "mean"),
                                 avg_atm_lot_cost=("atm_lot_cost", "mean"),
                                 avg_cheapest_liquid_lot=("cheapest_liquid_lot", "mean"))
    out["atm_lot_pct_of_equity"] = 100 * out["avg_atm_lot_cost"] / out["avg_equity"]
    return out.round(2).reset_index()


# =============================================================================
# 7. MONTE CARLO  (projection from limited data, NOT a forecast)
# =============================================================================

def monte_carlo(trade_returns, trades_per_month, months, bust_level,
                bench_monthly=None, runs=10_000, start=STARTING_CAPITAL, seed=SEED):
    """Resample real per-trade returns on equity (fractions, e.g. -0.04).

    Each run: months x Poisson(trades_per_month) trades, each drawing one real
    per-trade return with replacement; equity *= (1 + r). Once equity drops below
    bust_level (cost of the cheapest liquid lot seen in the backtest) the run is
    BUSTED and frozen. Benchmark runs resample real monthly benchmark returns
    over the same months, paired run by run.
    """
    rng = np.random.default_rng(seed)
    r = np.asarray(trade_returns, dtype=float)
    ends = np.empty(runs)
    busted = np.zeros(runs, dtype=bool)
    for k in range(runs):
        eq = start
        n = rng.poisson(trades_per_month * months)
        for x in rng.choice(r, size=n) if n and r.size else []:
            eq *= 1 + x
            if eq < bust_level:
                busted[k] = True
                break
        ends[k] = eq
    bench = None
    if bench_monthly is not None and len(bench_monthly):
        bm = np.asarray(bench_monthly, dtype=float)
        bench = start * np.prod(1 + rng.choice(bm, size=(runs, months)), axis=1)
    pct = np.percentile(ends, [5, 25, 50, 75, 95])
    row = dict(horizon_months=months, runs=runs,
               p5=pct[0], p25=pct[1], p50=pct[2], p75=pct[3], p95=pct[4],
               prob_bust_pct=100 * busted.mean(),
               prob_reach_1_lakh_pct=100 * (ends >= 1e5).mean(),
               prob_reach_10_lakh_pct=100 * (ends >= 1e6).mean())
    if bench is not None:
        bp = np.percentile(bench, [5, 50, 95])
        row.update(prob_beat_benchmark_pct=100 * (ends > bench).mean(),
                   bench_p5=bp[0], bench_p50=bp[1], bench_p95=bp[2])
    return {k: (round(v, 2) if isinstance(v, float) else v) for k, v in row.items()}


# =============================================================================
# 8. TESTS  (sample data validates ACCOUNTING, not profitability)
# =============================================================================

def run_tests() -> bool:
    results = []

    def check(name, got, want, tol=1e-6):
        ok = (got == want) if not isinstance(want, float) else abs(got - want) <= tol
        results.append((name, ok, got, want))

    # T1 zero-cost profit, end to end on sample data, no charges, no slippage.
    #   Signal 04-Mar: 22220/22000-1 = +1.0% > 0.5% -> buy CE at next close (05-Mar).
    #   Budget 10% x 25,000 = 2,500. Spot 22,230. Lot 75 (Mar-2025 expiry).
    #   22250 CE @40 -> 3,000 > 2,500 (unaffordable, though nearest).
    #   22200 CE @30 -> 2,250 OK, distance 30. 22300 CE @20 -> 1,500, distance 70.
    #   Picks 22200 CE @30. 06-Mar close 50 >= 30 x 1.6 = 48 -> TARGET.
    #   P&L = (50-30) x 75 = 1,500 -> cash 26,500.
    opts, idx = load_sample_data()
    res = run_backtest(opts, idx, Params(charges=NO_CHARGES, slippage=0.0, name="t1"))
    t = res["trades"]
    check("T1 one trade", len(t), 1)
    check("T1 strike 22200 chosen", float(t.iloc[0]["strike"]), 22200.0)
    check("T1 exit reason", t.iloc[0]["exit_reason"], "TARGET")
    check("T1 net P&L 1500", float(t.iloc[0]["net_pnl"]), 1500.0)
    check("T1 ending cash 26500", res["account"].cash, 26500.0)

    # T2 ITM expiry with full charges, slippage 0.
    #   Buy 1 lot (75) 22000 CE @100: premium 7,500.
    #   Buy charges: brokerage 20 + txn 7500x0.0003553=2.66475 + SEBI 0.0075
    #     + GST 0.18x22.67225=4.081005 + stamp 0.225 = 26.978255
    #   Expiry spot 22,150: intrinsic 150 x 75 = 11,250; STT 0.15% = 16.875
    #   Net = 11,250 - 7,500 - 26.978255 - 16.875 = 3,706.146745
    a = Account(slippage=0.0)
    a.buy("2025-03-20", "CE", 22000, "2025-03-27", 100.0, 75)
    r = a.settle_expiry(22150.0)
    check("T2 ITM exercise STT", r["exit_costs"], 16.875)
    check("T2 ITM cash", a.cash, 25000 + 3706.146745)

    # T3 worthless expiry: 22000 PE @100, spot 22,150 -> intrinsic 0, no STT.
    #   Net = -7,500 - 26.978255 = -7,526.978255
    a = Account(slippage=0.0)
    a.buy("2025-03-20", "PE", 22000, "2025-03-27", 100.0, 75)
    r = a.settle_expiry(22150.0)
    check("T3 worthless value", r["exit_value"], 0.0)
    check("T3 worthless cash", a.cash, 25000 - 7526.978255)

    # T4 max drawdown: peak 30,000 -> trough 21,000 = 9,000/30,000 = 30%.
    check("T4 max drawdown", max_drawdown([25000, 30000, 24000, 27000, 21000, 33000]), 30.0)

    # T5 full charges + 1% slippage, buy @100, sell @160, lot 75.
    #   Buy fill 101 -> 7,575: txn 2.6913975, SEBI 0.007575,
    #     GST 0.18x(20+2.6913975+0.007575)=4.08581505, stamp 0.22725
    #     total = 27.01203755
    #   Sell fill 158.4 -> 11,880: STT 17.82, txn 4.220964, SEBI 0.01188,
    #     GST 0.18x(20+4.220964+0.01188)=4.36191192 -> total 46.41475592
    #   Net = 11,880 - 7,575 - 27.01203755 - 46.41475592 = 4,231.57320653
    a = Account(slippage=0.01)
    a.buy("2025-03-20", "CE", 22000, "2025-03-27", 100.0, 75)
    check("T5 buy charges", a.position.entry_costs, 27.01203755)
    r = a.sell(160.0)
    check("T5 sell charges", r["exit_costs"], 46.41475592)
    check("T5 net cash", a.cash, 25000 + 4231.57320653)

    # T6 no borrowing: 1 lot of 65 @400 = 26,000 > 25,000 cash -> rejected.
    a = Account(slippage=0.0)
    try:
        a.buy("2026-03-02", "CE", 25000, "2026-03-30", 400.0, 65)
        check("T6 no-borrowing rejected", False, True)
    except InsufficientFunds:
        check("T6 no-borrowing rejected", True, True)
    check("T6 cash unchanged", a.cash, 25000.0)

    # T7 deposits blocked.
    try:
        Account().deposit(1000)
        check("T7 deposit blocked", False, True)
    except DepositBlocked:
        check("T7 deposit blocked", True, True)

    # T8 lot sizes from NSE circulars.
    for d, want in [("2024-04-25", 50), ("2024-05-30", 25), ("2025-01-30", 25),
                    ("2025-02-27", 75), ("2025-12-30", 75), ("2026-01-27", 65),
                    ("2023-01-25", 50), ("2026-03-30", 65)]:
        check(f"T8 lot {d}", lot_size_for_expiry(d), want)
    try:
        lot_size_for_expiry("2021-06-24")
        check("T8 unverified era raises", False, True)
    except ValueError:
        check("T8 unverified era raises", True, True)

    # T9 Monte Carlo sanity: all-zero returns stay at 25,000; all -100% bust.
    m0 = monte_carlo([0.0], 2, 12, 100, runs=200)
    check("T9 MC flat median", m0["p50"], 25000.0)
    m1 = monte_carlo([-1.0], 2, 12, 100, runs=200)
    check("T9 MC total loss busts", m1["prob_bust_pct"], 100.0)

    # T10 loader cleans raw NSE format (spaces, commas, '-', dates).
    import tempfile
    raw = ('"Symbol  ","Date  ","Expiry  ","Option type  ","Strike Price  ","Open  ",'
           '"High  ","Low  ","Close  ","LTP  ","Settle Price  ","No. of contracts  ",'
           '"Turnover * in   ₹ Lakhs  ","Premium Turnover ** in   ₹ Lakhs  ","Open Int  ",'
           '"Change in OI  ","Underlying Value  "\n'
           '"NIFTY","30-Jan-2025","30-Jan-2025","CE","23,000.00","1","1","1","1,234.50",'
           '"1","1","12,345","1","1","1","1","23,249.50"\n'
           '"NIFTY","30-Jan-2025","30-Jan-2025","CE","23,100.00","-","-","-","-","-","-",'
           '"0","-","-","-","-","23,249.50"\n'
           '"NIFTY","30-Jan-2025","30-Jan-2025","CE","23,000.00","1","1","1","1,234.50",'
           '"1","1","12,345","1","1","1","1","23,249.50"\n')
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "sample_raw.csv")
        with open(p, "w", encoding="utf-8") as f:
            f.write(raw)
        df = load_nse_option_csv([p, p])
    check("T10 dedupe rows", len(df), 2)
    check("T10 comma close", float(df.iloc[0]["close"]), 1234.5)
    check("T10 '-' is missing", bool(np.isnan(df.iloc[1]["close"])), True)
    check("T10 contracts", float(df.iloc[0]["contracts"]), 12345.0)
    check("T10 date parsed", str(df.iloc[0]["date"].date()), "2025-01-30")

    # T11 index loader: NSE 'Historical Index Data' style (trailing spaces,
    #   upper-case months) and NIFTYBEES 'Close Price' style.
    with tempfile.TemporaryDirectory() as tmp:
        p1 = os.path.join(tmp, "idx.csv")
        with open(p1, "w", encoding="utf-8") as f:
            f.write('"Date ","Open ","High ","Low ","Close ","Shares Traded ","Turnover (₹ Cr)"\n'
                    '"31-JAN-2025","1","1","1","23,508.40","1","1"\n'
                    '"30-JAN-2025","1","1","1","23,249.50","1","1"\n')
        p2 = os.path.join(tmp, "bees.csv")
        with open(p2, "w", encoding="utf-8") as f:
            f.write('"Symbol","Series","Date","Prev Close","Close Price"\n'
                    '"NIFTYBEES","EQ","30-Jan-2025","1","265.10"\n')
        i1, i2 = load_nse_index_csv([p1]), load_nse_index_csv([p2])
    check("T11 index sorted + parsed", str(i1.iloc[0]["date"].date()), "2025-01-30")
    check("T11 index close", float(i1.iloc[1]["close"]), 23508.4)
    check("T11 NIFTYBEES close", float(i2.iloc[0]["close"]), 265.1)

    print("\n=== TESTS (" + SAMPLE_LABEL + ") ===")
    for name, ok, got, want in results:
        print(f"[{'PASS' if ok else 'FAIL'}] {name}" + ("" if ok else f"  got={got} want={want}"))
    n_ok = sum(ok for _, ok, _, _ in results)
    print(f"{n_ok}/{len(results)} passed")
    return n_ok == len(results)


# =============================================================================
# 9. REPORTING / CHECKLIST / CLI
# =============================================================================

# Verified NSE equity-derivative holidays (nseindia.com holiday page, 2026 list,
# fetched 2026-10-20). For earlier years only fixed-date national holidays are
# applied; festival holidays must be checked in the NSE expiry dropdown.
NSE_HOLIDAYS_2026 = {date(2026, m, d) for m, d in [
    (1, 15), (1, 26), (3, 3), (3, 26), (3, 31), (4, 3), (4, 14), (5, 1), (5, 28),
    (6, 26), (9, 14), (10, 2), (10, 20), (11, 10), (11, 24), (12, 25)]}
FIXED_HOLIDAYS = {(1, 26), (5, 1), (8, 15), (10, 2), (12, 25)}


def _is_holiday(d: date) -> bool:
    return d.weekday() >= 5 or d in NSE_HOLIDAYS_2026 or (d.month, d.day) in FIXED_HOLIDAYS


def monthly_expiry(y: int, m: int) -> tuple[date, str]:
    """Last Thursday (to Aug-2025) / last Tuesday (from Sep-2025), moved to the
    previous trading day if that day is a known holiday."""
    wd = 3 if (y, m) <= (2025, 8) else 1
    nxt = date(y + (m == 12), m % 12 + 1, 1)
    d = nxt - timedelta(days=1)
    while d.weekday() != wd:
        d -= timedelta(days=1)
    note = ""
    while _is_holiday(d):
        d -= timedelta(days=1)
        note = "shifted for holiday"
    if y < 2026 and not note:
        note = "confirm in NSE dropdown (festival holidays before 2026 not checked)"
    if (y, m) in [(2025, 8), (2025, 9)]:
        note = "Thu->Tue expiry switch around here: confirm in NSE dropdown"
    return d, note


def build_checklist(first=(2021, 10), last=(2026, 9)) -> pd.DataFrame:
    rows, n = [], 0
    months = pd.period_range(f"{first[0]}-{first[1]:02d}", f"{last[0]}-{last[1]:02d}", freq="M")
    three_y_start = pd.Period(f"{last[0] - 3}-{last[1]:02d}", "M") + 1
    for per in months:
        exp, note = monthly_expiry(per.year, per.month)
        prev_exp, _ = monthly_expiry((per - 1).year, (per - 1).month)
        frm = prev_exp - timedelta(days=7)
        lot = lot_size_for_expiry(exp)
        for t in ("CE", "PE"):
            n += 1
            rows.append(dict(no=n, kind="OPTIONS", in_3y_test="yes" if per >= three_y_start else "no",
                             symbol="NIFTY", instrument="Index Options (OPTIDX)",
                             expiry=exp.strftime("%d-%b-%Y"), option_type=t, strike="(blank)",
                             from_date=frm.strftime("%d-%b-%Y"), to_date=exp.strftime("%d-%b-%Y"),
                             lot_size=lot, note=note))
    start = date(first[0], first[1], 1) - pd.DateOffset(months=1)
    s = pd.Timestamp(start).date()
    end = date(last[0], last[1], 1) + pd.DateOffset(months=1)
    e_all = (pd.Timestamp(end) - pd.Timedelta(days=1)).date()
    while s <= e_all:
        e = (pd.Timestamp(s) + pd.DateOffset(months=2) - pd.Timedelta(days=1)).date()
        e = min(e, e_all)
        n += 1
        rows.append(dict(no=n, kind="INDEX", in_3y_test="yes" if pd.Period(e, "M") >= three_y_start - 1 else "no",
                         symbol="NIFTY 50", instrument="Historical Index Data", expiry="",
                         option_type="", strike="", from_date=s.strftime("%d-%b-%Y"),
                         to_date=e.strftime("%d-%b-%Y"), lot_size="", note=""))
        s = (pd.Timestamp(e) + pd.Timedelta(days=1)).date()
    out = pd.DataFrame(rows)
    out["lot_size"] = out["lot_size"].astype(object)
    return out


def suggest_sample_months(idx: pd.DataFrame, n_each=4) -> pd.DataFrame:
    """Pick up / down / sideways months from REAL index data for a 12-month sample."""
    m = idx.set_index("date")["close"].resample("ME").last().pct_change().dropna()
    df = m.rename("ret").to_frame()
    df["regime"] = np.where(df["ret"] > 0.02, "UP", np.where(df["ret"] < -0.02, "DOWN", "SIDEWAYS"))
    picks = []
    for reg, g in df.groupby("regime"):
        g = g.sort_index()
        take = np.linspace(0, len(g) - 1, min(n_each, len(g))).round().astype(int)
        picks.append(g.iloc[np.unique(take)])
    out = pd.concat(picks).sort_index()
    out["ret_pct"] = (100 * out["ret"]).round(2)
    return out.drop(columns="ret").reset_index().rename(columns={"date": "month_end"})


def plot_equity(curve, bench, path, title):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(9, 4.5))
    ax.plot(curve["date"], curve["equity"], label="Option-buying strategy", color="#c0392b")
    if bench is not None:
        ax.plot(curve["date"], bench.values, label="Buy & hold benchmark", color="#2c7fb8")
    ax.axhline(STARTING_CAPITAL, color="grey", lw=0.8, ls="--")
    ax.set_ylabel("Rs")
    ax.set_title(title)
    ax.legend()
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(path, dpi=120)
    plt.close(fig)


def full_run(opts, idx, bench, out_dir, label):
    os.makedirs(out_dir, exist_ok=True)
    report_data(opts, idx)
    variants = [Params(0.10, "next_close", name="risk10_next_close (BASELINE)"),
                Params(0.02, "next_close", name="risk2_next_close"),
                Params(0.10, "same_close", name="risk10_same_close (look-ahead, optimistic)"),
                Params(0.02, "same_close", name="risk2_same_close (look-ahead, optimistic)")]
    runs = [run_backtest(opts, idx, v) for v in variants]
    table = pd.DataFrame([compute_metrics(r) for r in runs])
    table.insert(0, "data", label)
    table.to_csv(os.path.join(out_dir, "variations.csv"), index=False)
    print("\n=== ALL VARIATIONS (none tuned) ===")
    print(table.T.to_string(header=False))

    base = runs[0]
    c = base["curve"].copy()
    bsrc = bench if bench is not None else idx
    bc = benchmark_curve(bsrc, c["date"])
    c["benchmark_equity"] = bc.values
    c.insert(0, "data", label)
    c.to_csv(os.path.join(out_dir, "equity_curve.csv"), index=False)
    tl = base["trades"].copy()
    tl.insert(0, "data", label)
    tl.to_csv(os.path.join(out_dir, "trade_log.csv"), index=False)
    base["events"].to_csv(os.path.join(out_dir, "signal_events.csv"), index=False)
    affordability(base["curve"]).to_csv(os.path.join(out_dir, "affordability.csv"), index=False)
    plot_equity(c, bc, os.path.join(out_dir, "equity_curve.png"), f"Rs 25,000: strategy vs buy & hold ({label})")

    # Monte Carlo per variant x horizon
    rows = []
    bm = bsrc.set_index("date")["close"].resample("ME").last().pct_change().dropna().values
    for r in runs:
        t = r["trades"]
        months_obs = max(1e-9, (r["curve"]["date"].iloc[-1] - r["curve"]["date"].iloc[0]).days / 30.44)
        bust_level = float(np.nanmedian(r["curve"]["cheapest_liquid_lot"]))
        for h in (36, 60):
            base_row = dict(data=label, variant=r["params"].name, horizon_months=h,
                            trades_observed=len(t),
                            months_observed=round(months_obs, 1),
                            trades_per_month=round(len(t) / months_obs, 3),
                            bust_level=round(bust_level, 2))
            if len(t) < 30:
                base_row["status"] = f"INSUFFICIENT_TRADES (<30); projection not meaningful"
            else:
                base_row["status"] = "PROJECTION_FROM_LIMITED_DATA_NOT_A_FORECAST"
            if len(t) >= 2:
                base_row.update(monte_carlo(t["ret_on_equity_pct"].values / 100,
                                            len(t) / months_obs, h, bust_level, bm))
            rows.append(base_row)
    mc = pd.DataFrame(rows)
    mc.to_csv(os.path.join(out_dir, "monte_carlo_summary.csv"), index=False)
    print("\n=== MONTE CARLO (projection, not forecast) ===")
    print(mc.to_string(index=False))

    if len(base["trades"]):
        t0 = base["trades"].iloc[0]
        print("\n=== HAND CHECK, trade 1 ===")
        print(f"entry fill {t0.entry_close} x 1.01 = {t0.entry_fill}; x qty {t0.qty} = {t0.entry_premium}")
        print(f"exit fill {t0.exit_close} -> {t0.exit_fill}; value {t0.exit_value}")
        print(f"gross {t0.exit_value} - {t0.entry_premium} = {t0.gross_pnl}")
        print(f"charges {t0.entry_costs} + {t0.exit_costs} = {t0.total_costs}; net {t0.net_pnl}")
    return table, mc


def _expand(pats):
    out = []
    for p in pats or []:
        out.extend(sorted(glob.glob(p)) or [p])
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["test", "checklist", "inspect", "run", "suggest", "sample"])
    ap.add_argument("--options", nargs="*")
    ap.add_argument("--index", nargs="*")
    ap.add_argument("--benchmark", nargs="*")
    ap.add_argument("--out", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "output"))
    a = ap.parse_args(argv)

    if a.cmd == "test":
        sys.exit(0 if run_tests() else 1)
    if a.cmd == "checklist":
        cl = build_checklist()
        os.makedirs(a.out, exist_ok=True)
        cl.to_csv(os.path.join(a.out, "nse_download_checklist.csv"), index=False)
        print(cl.to_string(index=False))
        return
    if a.cmd == "sample":
        opts, idx = load_sample_data()
        full_run(opts, idx, None, os.path.join(a.out, "SAMPLE_RUN"), SAMPLE_LABEL)
        return
    idx = load_nse_index_csv(_expand(a.index))
    if a.cmd == "suggest":
        print(suggest_sample_months(idx).to_string(index=False))
        return
    opts = load_nse_option_csv(_expand(a.options))
    if a.cmd == "inspect":
        report_data(opts, idx)
        return
    bench = load_nse_index_csv(_expand(a.benchmark)) if a.benchmark else None
    full_run(opts, idx, bench, os.path.join(a.out, "REAL_RUN"), "NSE_REAL_DATA")


if __name__ == "__main__":
    main()
