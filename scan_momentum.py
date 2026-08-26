#!/usr/bin/env python3
"""
scan_momentum.py — scans NSE bhavcopy for momentum stocks and Nifty OTM CE signals.

Reads all CSV files in bhavcopy_data/, ranks equities by relative strength,
applies the Minervini trend template, and checks Nifty 50 index momentum for
OTM call-option entry ideas.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from glob import glob
from typing import Any

import pandas as pd
import yfinance as yf

BHAVCOPY_DIR = "bhavcopy_data"
OUTPUT_DIR = "output"
TOP_N = 25
MIN_RS_PERCENTILE = 70


def avg(values: list[float]) -> float:
    return sum(values) / len(values)


def load_bhavcopy_history() -> dict[str, list[dict[str, Any]]]:
    history: dict[str, list[dict[str, Any]]] = {}

    for path in sorted(glob(os.path.join(BHAVCOPY_DIR, "bhavcopy_*.csv"))):
        try:
            df = pd.read_csv(path)
        except Exception as exc:
            print(f"[warn] skipping {path}: {exc}")
            continue

        for _, row in df.iterrows():
            sym = str(row.get("TckrSymb", "")).strip()
            close = row.get("ClsPric")
            if not sym or pd.isna(close):
                continue
            series = str(row.get("SctySrs", "")).strip()
            if series != "EQ":
                continue
            if pd.notna(row.get("OptnTp")) and str(row.get("OptnTp")).strip():
                continue

            date = str(row.get("TradDt") or row.get("BizDt") or "").strip()
            if not date:
                continue

            entry = {
                "date": date,
                "open": float(row.get("OpnPric") or close),
                "high": float(row.get("HghPric") or close),
                "low": float(row.get("LwPric") or close),
                "close": float(close),
                "volume": float(row.get("TtlTradgVol") or 0),
            }
            history.setdefault(sym, []).append(entry)

    for sym in history:
        history[sym].sort(key=lambda x: x["date"])
        if len(history[sym]) > 260:
            history[sym] = history[sym][-260:]

    return history


def compute_atr(hist: list[dict[str, Any]]) -> float | None:
    if len(hist) < 2:
        return None
    trs = []
    for i in range(1, len(hist)):
        cur, prev = hist[i], hist[i - 1]
        trs.append(
            max(
                cur["high"] - cur["low"],
                abs(cur["high"] - prev["close"]),
                abs(cur["low"] - prev["close"]),
            )
        )
    return avg(trs[-14:]) if trs else None


def compute_rs_ratings(history: dict[str, list[dict[str, Any]]]) -> dict[str, int]:
    raws: dict[str, float] = {}

    for sym, hist in history.items():
        if len(hist) < 63:
            continue
        close_now = hist[-1]["close"]

        def back(n: int) -> float | None:
            return hist[-1 - n]["close"] if len(hist) > n else None

        c63 = back(63)
        if c63 is None:
            continue
        c126, c189, c252 = back(126), back(189), back(252)
        raw = (close_now / c63) * 0.4
        raw += (close_now / (c126 or c63)) * 0.2
        raw += (close_now / (c189 or c63)) * 0.2
        raw += (close_now / (c252 or c63)) * 0.2
        raws[sym] = raw

    if not raws:
        return {}

    vals = sorted(raws.values())
    ratings: dict[str, int] = {}
    for sym, raw in raws.items():
        rank = sum(1 for v in vals if v <= raw)
        ratings[sym] = max(1, round((rank / len(vals)) * 99))
    return ratings


def compute_technicals(sym: str, hist: list[dict[str, Any]], rs: int | None) -> dict[str, Any]:
    closes = [h["close"] for h in hist]
    w52 = hist[-252:] if len(hist) >= 252 else hist

    def sma(n: int) -> float | None:
        return avg(closes[-n:]) if len(closes) >= n else None

    sma50, sma150, sma200 = sma(50), sma(150), sma(200)
    price = closes[-1]
    vol = hist[-1]["volume"]
    avg_vol = avg([h["volume"] for h in hist[-50:]]) if len(hist) >= 50 else avg(
        [h["volume"] for h in hist]
    )

    return {
        "symbol": sym,
        "price": round(price, 2),
        "sma50": round(sma50, 2) if sma50 else None,
        "sma150": round(sma150, 2) if sma150 else None,
        "sma200": round(sma200, 2) if sma200 else None,
        "high52": round(max(h["high"] for h in w52), 2),
        "low52": round(min(h["low"] for h in w52), 2),
        "rs": rs,
        "vol": int(vol),
        "avgVol": int(avg_vol),
        "atr": round(compute_atr(hist[-15:]), 2) if len(hist) >= 15 else None,
        "dataPoints": len(hist),
        "lastDate": hist[-1]["date"],
    }


def passes_trend_template(t: dict[str, Any]) -> bool:
    if None in (t["sma50"], t["sma150"], t["sma200"], t["rs"]):
        return False
    return (
        t["price"] > t["sma50"] > t["sma150"] > t["sma200"]
        and t["price"] >= 0.75 * t["high52"]
        and t["price"] >= 1.3 * t["low52"]
        and t["rs"] >= MIN_RS_PERCENTILE
    )


def volume_confirmed(t: dict[str, Any]) -> bool:
    return t["avgVol"] > 0 and t["vol"] >= 1.5 * t["avgVol"]


def score_stock(t: dict[str, Any]) -> dict[str, Any]:
    trend = passes_trend_template(t)
    volume = volume_confirmed(t)
    score = 0
    if trend:
        score += 50
    if volume:
        score += 25
    if t["rs"] and t["rs"] >= 80:
        score += 15
    if t["rs"] and t["rs"] >= 90:
        score += 10

    verdict = "SKIP"
    if trend and volume:
        verdict = "BUY"
    elif trend or (t["rs"] and t["rs"] >= MIN_RS_PERCENTILE):
        verdict = "WATCH"

    return {
        **t,
        "trendPass": trend,
        "volumePass": volume,
        "score": score,
        "verdict": verdict,
    }


def round_nifty_strike(spot: float, otm_points: int = 150) -> int:
    """Nifty strikes are in multiples of 50."""
    atm = int(round(spot / 50) * 50)
    return atm + otm_points


def scan_nifty() -> dict[str, Any]:
    data = yf.download("^NSEI", period="1y", interval="1d", progress=False, auto_adjust=True)
    if data.empty or len(data) < 200:
        return {"error": "insufficient_nifty_history"}

    close = data["Close"].squeeze()
    spot = float(close.iloc[-1])
    sma50 = float(close.rolling(50).mean().iloc[-1])
    sma150 = float(close.rolling(150).mean().iloc[-1])
    sma200 = float(close.rolling(200).mean().iloc[-1])
    high52 = float(close.tail(252).max())
    low52 = float(close.tail(252).min())

    trend_bullish = spot > sma50 > sma150 > sma200
    near_high = spot >= 0.75 * high52
    above_low = spot >= 1.3 * low52
    sma200_rising = float(close.rolling(200).mean().iloc[-22]) < sma200

    momentum_pass = trend_bullish and near_high and above_low and sma200_rising
    regime = "BULLISH" if spot > sma200 else "BEARISH"

    otm_strike = round_nifty_strike(spot, otm_points=150)
    conservative_strike = round_nifty_strike(spot, otm_points=100)
    aggressive_strike = round_nifty_strike(spot, otm_points=200)

    recommendation = None
    if momentum_pass and regime == "BULLISH":
        recommendation = {
            "instrument": f"NIFTY {otm_strike} CE",
            "strike": otm_strike,
            "spot": round(spot, 2),
            "otmPoints": otm_strike - int(round(spot / 50) * 50),
            "type": "OTM Call (CE)",
            "rationale": (
                "Nifty passes momentum trend template (price > SMA50 > SMA150 > SMA200, "
                "within 25% of 52w high, 30%+ above 52w low, rising 200 SMA). "
                "Suggested OTM CE for directional lottery-ticket sizing (delta ~0.2-0.3)."
            ),
            "alternates": {
                "conservative": f"NIFTY {conservative_strike} CE",
                "aggressive": f"NIFTY {aggressive_strike} CE",
            },
            "riskNotes": [
                "Prefer 30-45+ days to expiry for swing momentum trades",
                "Size OTM options at near-zero % of capital — max loss = premium paid",
                "Check IV percentile before entry; avoid buying after IV spike on news",
            ],
            "signal": "BUY",
        }
    elif regime == "BULLISH":
        recommendation = {
            "instrument": f"NIFTY {otm_strike} CE",
            "strike": otm_strike,
            "spot": round(spot, 2),
            "signal": "WATCH",
            "rationale": "Nifty regime is bullish but full momentum template not yet confirmed.",
        }
    else:
        recommendation = {
            "signal": "SKIP",
            "rationale": "Nifty below 200 SMA — bearish regime. Avoid new long CE entries.",
            "spot": round(spot, 2),
        }

    return {
        "spot": round(spot, 2),
        "sma50": round(sma50, 2),
        "sma150": round(sma150, 2),
        "sma200": round(sma200, 2),
        "high52": round(high52, 2),
        "low52": round(low52, 2),
        "regime": regime,
        "momentumPass": momentum_pass,
        "trendChecks": {
            "priceAboveSMAs": trend_bullish,
            "within25PctOfHigh": near_high,
            "above30PctOfLow": above_low,
            "sma200Rising": sma200_rising,
        },
        "otmCeRecommendation": recommendation,
    }


def scan_stocks(history: dict[str, list[dict[str, Any]]], rs_ratings: dict[str, int]) -> list[dict[str, Any]]:
    results = []
    for sym, hist in history.items():
        if len(hist) < 63:
            continue
        tech = compute_technicals(sym, hist, rs_ratings.get(sym))
        results.append(score_stock(tech))

    results.sort(key=lambda x: (x["score"], x["rs"] or 0), reverse=True)
    return results[:TOP_N]


def _json_default(obj: Any) -> Any:
    if isinstance(obj, float) and (obj != obj):  # NaN
        return None
    raise TypeError(f"Object of type {type(obj)} is not JSON serializable")


def main() -> None:
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    history = load_bhavcopy_history()
    if not history:
        raise SystemExit(
            f"No bhavcopy data found in {BHAVCOPY_DIR}/. "
            "Run: python fetch_bhavcopy.py --backfill 200"
        )

    rs_ratings = compute_rs_ratings(history)
    top_stocks = scan_stocks(history, rs_ratings)
    nifty = scan_nifty()

    buy_stocks = [s for s in top_stocks if s["verdict"] == "BUY"]
    watch_stocks = [s for s in top_stocks if s["verdict"] == "WATCH"]

    output = {
        "scanDate": datetime.now().strftime("%Y-%m-%d %H:%M:%S IST"),
        "symbolsScanned": len(history),
        "tradingDaysLoaded": max(len(h) for h in history.values()),
        "nifty": nifty,
        "summary": {
            "marketRegime": nifty.get("regime", "UNKNOWN"),
            "niftyMomentumPass": nifty.get("momentumPass", False),
            "otmCeSignal": nifty.get("otmCeRecommendation", {}).get("signal", "SKIP"),
            "buyCount": len(buy_stocks),
            "watchCount": len(watch_stocks),
        },
        "topMomentumStocks": top_stocks,
        "buyCandidates": buy_stocks,
        "watchCandidates": watch_stocks,
    }

    latest_path = os.path.join(OUTPUT_DIR, "latest_scan.json")
    with open(latest_path, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, default=_json_default)

    csv_path = os.path.join(OUTPUT_DIR, "latest_scan.csv")
    pd.DataFrame(top_stocks).to_csv(csv_path, index=False)

    print(f"[ok] wrote {latest_path}")
    print(f"[ok] wrote {csv_path}")
    print(f"Nifty regime: {output['summary']['marketRegime']}")
    print(f"OTM CE signal: {output['summary']['otmCeSignal']}")
    print(f"BUY stocks: {len(buy_stocks)} | WATCH: {len(watch_stocks)}")


if __name__ == "__main__":
    main()
