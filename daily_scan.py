#!/usr/bin/env python3
"""
daily_scan.py — EOD momentum scan with prediction tracking.

Fetches NSE bhavcopy, maintains price history, scans for momentum setups,
logs predictions, scores outcomes, and writes data/report.md.
"""

from __future__ import annotations

import argparse
import io
import os
import time
import zipfile
from datetime import datetime, timedelta
from typing import Any

import pandas as pd
import requests
import yfinance as yf

# --- tunable thresholds (see AUTOMATION-SETUP.md) ---
MIN_VOLUME = 50_000
RS_MIN = 70
VOL_MULTIPLE = 1.5
ATR_STOP_MULT = 1.5

DATA_DIR = "data"
HISTORY_PATH = os.path.join(DATA_DIR, "history.csv")
PREDICTIONS_PATH = os.path.join(DATA_DIR, "predictions.csv")
TRACK_RECORD_PATH = os.path.join(DATA_DIR, "track_record.csv")
NIFTY_PREDICTIONS_PATH = os.path.join(DATA_DIR, "nifty_predictions.csv")
REPORT_PATH = os.path.join(DATA_DIR, "report.md")

BASE_URL = "https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{date}_F_0000.csv.zip"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/all-reports",
}

HISTORY_COLS = ["date", "symbol", "open", "high", "low", "close", "volume"]
PREDICTION_COLS = [
    "prediction_id",
    "symbol",
    "scan_date",
    "entry_price",
    "stop_price",
    "rs",
    "volume_ratio",
    "status",
]
TRACK_COLS = [
    "prediction_id",
    "symbol",
    "scan_date",
    "entry_price",
    "stop_price",
    "latest_close",
    "return_pct",
    "outcome",
    "scored_date",
]
NIFTY_PRED_COLS = [
    "prediction_id",
    "signal_date",
    "signal_time_ist",
    "action",
    "instrument",
    "nifty_at_signal",
    "prev_high",
    "prev_low",
    "regime",
    "bias",
    "status",
    "eod_nifty",
    "day_return_pct",
    "outcome",
    "scored_date",
    "notes",
]
MORNING_SECTION = "## Morning trade confirmation (today)"


def get_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(HEADERS)
    session.get("https://www.nseindia.com", timeout=10)
    session.get("https://www.nseindia.com/all-reports", timeout=10)
    return session


def fetch_bhavcopy(session: requests.Session, date_obj: datetime) -> pd.DataFrame | None:
    date_str = date_obj.strftime("%Y%m%d")
    url = BASE_URL.format(date=date_str)
    resp = session.get(url, timeout=20)
    if resp.status_code != 200 or len(resp.content) < 500:
        print(f"[miss] {date_obj.strftime('%Y-%m-%d')} — no file (weekend/holiday or not published)")
        return None

    try:
        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            csv_name = next(n for n in zf.namelist() if n.lower().endswith(".csv"))
            with zf.open(csv_name) as src:
                df = pd.read_csv(src)
    except (zipfile.BadZipFile, StopIteration) as exc:
        print(f"[error] {date_obj.strftime('%Y-%m-%d')} — {exc}")
        return None

    rows = []
    trade_date = date_obj.strftime("%Y-%m-%d")
    for _, row in df.iterrows():
        sym = str(row.get("TckrSymb", "")).strip()
        close = row.get("ClsPric")
        if not sym or pd.isna(close):
            continue
        if str(row.get("SctySrs", "")).strip() != "EQ":
            continue
        if pd.notna(row.get("OptnTp")) and str(row.get("OptnTp")).strip():
            continue

        rows.append(
            {
                "date": trade_date,
                "symbol": sym,
                "open": float(row.get("OpnPric") or close),
                "high": float(row.get("HghPric") or close),
                "low": float(row.get("LwPric") or close),
                "close": float(close),
                "volume": float(row.get("TtlTradgVol") or 0),
            }
        )

    if not rows:
        print(f"[miss] {date_obj.strftime('%Y-%m-%d')} — empty bhavcopy")
        return None

    print(f"[ok]   fetched {len(rows)} symbols for {trade_date}")
    return pd.DataFrame(rows)


def load_history() -> pd.DataFrame:
    if os.path.exists(HISTORY_PATH):
        df = pd.read_csv(HISTORY_PATH)
        for col in HISTORY_COLS:
            if col not in df.columns:
                df[col] = None
        return df[HISTORY_COLS]
    return pd.DataFrame(columns=HISTORY_COLS)


def append_history(history: pd.DataFrame, day_df: pd.DataFrame) -> pd.DataFrame:
    trade_date = day_df["date"].iloc[0]
    history = history[history["date"] != trade_date]
    return pd.concat([history, day_df[HISTORY_COLS]], ignore_index=True)


def save_history(history: pd.DataFrame) -> None:
    history = history.sort_values(["symbol", "date"]).drop_duplicates(["symbol", "date"], keep="last")
    history.to_csv(HISTORY_PATH, index=False)


def history_to_dict(history: pd.DataFrame) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for sym, grp in history.groupby("symbol"):
        records = grp.sort_values("date").to_dict("records")
        if len(records) > 260:
            records = records[-260:]
        out[sym] = records
    return out


def avg(values: list[float]) -> float:
    return sum(values) / len(values)


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
        and t["rs"] >= RS_MIN
    )


def volume_confirmed(t: dict[str, Any]) -> bool:
    return t["avgVol"] > 0 and t["vol"] >= VOL_MULTIPLE * t["avgVol"]


def scan_candidates(history: dict[str, list[dict[str, Any]]], rs_ratings: dict[str, int]) -> list[dict[str, Any]]:
    results = []
    for sym, hist in history.items():
        if len(hist) < 63:
            continue
        if hist[-1]["volume"] < MIN_VOLUME:
            continue
        tech = compute_technicals(sym, hist, rs_ratings.get(sym))
        if passes_trend_template(tech) and volume_confirmed(tech):
            stop = tech["price"] - ATR_STOP_MULT * (tech["atr"] or 0)
            vol_ratio = round(tech["vol"] / tech["avgVol"], 2) if tech["avgVol"] else 0
            results.append(
                {
                    **tech,
                    "stop": round(max(stop, 0.01), 2),
                    "volume_ratio": vol_ratio,
                }
            )

    results.sort(key=lambda x: (x["rs"] or 0, x["volume_ratio"]), reverse=True)
    return results


def load_predictions() -> pd.DataFrame:
    if os.path.exists(PREDICTIONS_PATH):
        df = pd.read_csv(PREDICTIONS_PATH)
        for col in PREDICTION_COLS:
            if col not in df.columns:
                df[col] = None
        return df[PREDICTION_COLS]
    return pd.DataFrame(columns=PREDICTION_COLS)


def log_predictions(candidates: list[dict[str, Any]], scan_date: str) -> pd.DataFrame:
    preds = load_predictions()
    existing_today = set(preds[preds["scan_date"] == scan_date]["symbol"].tolist()) if not preds.empty else set()

    new_rows = []
    for c in candidates:
        if c["symbol"] in existing_today:
            continue
        pid = f"{scan_date}_{c['symbol']}"
        new_rows.append(
            {
                "prediction_id": pid,
                "symbol": c["symbol"],
                "scan_date": scan_date,
                "entry_price": c["price"],
                "stop_price": c["stop"],
                "rs": c["rs"],
                "volume_ratio": c["volume_ratio"],
                "status": "open",
            }
        )

    if new_rows:
        preds = pd.concat([preds, pd.DataFrame(new_rows)], ignore_index=True)
        preds.to_csv(PREDICTIONS_PATH, index=False)
        print(f"[ok]   logged {len(new_rows)} new predictions")

    return preds


def score_predictions(history: dict[str, list[dict[str, Any]]], scored_date: str) -> pd.DataFrame:
    preds = load_predictions()
    if preds.empty:
        return pd.DataFrame(columns=TRACK_COLS)

    rows = []
    for _, p in preds.iterrows():
        sym = p["symbol"]
        if sym not in history:
            continue
        latest = history[sym][-1]
        latest_close = latest["close"]
        entry = float(p["entry_price"])
        stop = float(p["stop_price"])
        ret_pct = round((latest_close - entry) / entry * 100, 2)

        if latest_close <= stop:
            outcome = "stopped"
        elif ret_pct >= 10:
            outcome = "winner"
        elif ret_pct <= -5:
            outcome = "loser"
        else:
            outcome = "open"

        rows.append(
            {
                "prediction_id": p["prediction_id"],
                "symbol": sym,
                "scan_date": p["scan_date"],
                "entry_price": entry,
                "stop_price": stop,
                "latest_close": round(latest_close, 2),
                "return_pct": ret_pct,
                "outcome": outcome,
                "scored_date": scored_date,
            }
        )

    track = pd.DataFrame(rows)
    if not track.empty:
        track.to_csv(TRACK_RECORD_PATH, index=False)
        print(f"[ok]   scored {len(track)} predictions")
    return track


def round_nifty_strike(spot: float) -> int:
    """Nifty strikes are in multiples of 50."""
    return int(round(spot / 50) * 50)


def build_nifty_daily_plan() -> dict[str, Any]:
    """Build tomorrow's Nifty PE/CE plan from index OHLC."""
    data = yf.download("^NSEI", period="1y", interval="1d", progress=False, auto_adjust=True)
    if data.empty or len(data) < 5:
        return {"error": "Could not fetch Nifty data"}

    close = data["Close"].squeeze()
    high = data["High"].squeeze()
    low = data["Low"].squeeze()

    spot = float(close.iloc[-1])
    prev_high = float(high.iloc[-2]) if len(high) > 1 else float(high.iloc[-1])
    prev_low = float(low.iloc[-2]) if len(low) > 1 else float(low.iloc[-1])
    prev_close = float(close.iloc[-2]) if len(close) > 1 else spot
    day_change_pct = round((spot - prev_close) / prev_close * 100, 2)

    sma50 = float(close.rolling(50).mean().iloc[-1]) if len(close) >= 50 else None
    sma200 = float(close.rolling(200).mean().iloc[-1]) if len(close) >= 200 else None

    regime = "BULLISH" if sma200 and spot > sma200 else "BEARISH"
    if sma200 is None:
        regime = "NEUTRAL"

    atm = round_nifty_strike(spot)
    pe_strike = atm
    ce_strike = atm

    # --- bias from trend + today's close ---
    if regime == "BEARISH":
        bias = "PE"
        bias_reason = "Nifty below 200-day average — favour puts on weakness."
    elif regime == "BULLISH":
        bias = "CE"
        bias_reason = "Nifty above 200-day average — favour calls on strength."
    else:
        bias = "NEUTRAL"
        bias_reason = "Not enough history for 200-day trend — trade only clear breakouts."

    down_day = day_change_pct < 0
    up_day = day_change_pct > 0
    broke_low = spot < prev_low
    broke_high = spot > prev_high

    if bias == "PE" and down_day and broke_low:
        signal = "PE CONFIRMED"
        signal_note = "Down day and closed below yesterday's low — PE buyers had the edge today."
    elif bias == "CE" and up_day and broke_high:
        signal = "CE CONFIRMED"
        signal_note = "Up day and closed above yesterday's high — CE buyers had the edge today."
    elif bias == "PE" and down_day:
        signal = "PE WATCH"
        signal_note = "Down day with bearish bias — wait for a clean break below yesterday's low tomorrow."
    elif bias == "CE" and up_day:
        signal = "CE WATCH"
        signal_note = "Up day with bullish bias — wait for a clean break above yesterday's high tomorrow."
    else:
        signal = "SKIP"
        signal_note = "No clean directional setup today — only trade if tomorrow's open confirms."

    pe_trigger = f"Nifty breaks below **{prev_low:.0f}** (yesterday's low) in the first 30–60 min"
    ce_trigger = f"Nifty breaks above **{prev_high:.0f}** (yesterday's high) in the first 30–60 min"

    if bias == "PE":
        tomorrow_plan = (
            f"**Bias: PE** — {pe_trigger}. "
            f"Strike: **NIFTY {pe_strike} PE** (ATM) or **{pe_strike - 50} PE** (slightly OTM). "
            "Book half at +50% premium, rest at +80–100%. Cut at -40%."
        )
    elif bias == "CE":
        tomorrow_plan = (
            f"**Bias: CE** — {ce_trigger}. "
            f"Strike: **NIFTY {ce_strike} CE** (ATM) or **{ce_strike + 50} CE** (slightly OTM). "
            "Book half at +50% premium, rest at +80–100%. Cut at -40%."
        )
    else:
        tomorrow_plan = (
            f"**No default bias** — PE if {pe_trigger.lower()}; "
            f"CE if {ce_trigger.lower()}. Otherwise skip."
        )

    return {
        "spot": round(spot, 2),
        "prev_high": round(prev_high, 2),
        "prev_low": round(prev_low, 2),
        "day_change_pct": day_change_pct,
        "sma50": round(sma50, 2) if sma50 else None,
        "sma200": round(sma200, 2) if sma200 else None,
        "regime": regime,
        "bias": bias,
        "bias_reason": bias_reason,
        "signal": signal,
        "signal_note": signal_note,
        "pe_trigger": pe_trigger,
        "ce_trigger": ce_trigger,
        "pe_strike": pe_strike,
        "ce_strike": ce_strike,
        "tomorrow_plan": tomorrow_plan,
        "dont_chase": (
            "Do **not** chase options already up 80–100%. "
            "Enter only on tomorrow's trigger, or book profits if you are already in."
        ),
    }


def build_morning_confirmation() -> dict[str, Any]:
    """Check live Nifty vs yesterday's levels (~10 AM IST) and return trade signal."""
    now = datetime.now()
    signal_date = now.strftime("%Y-%m-%d")
    signal_time = now.strftime("%H:%M IST")

    daily = yf.download("^NSEI", period="10d", interval="1d", progress=False, auto_adjust=True)
    if daily.empty or len(daily) < 2:
        return {"error": "Could not fetch Nifty daily data", "signal_date": signal_date}

    high = daily["High"].squeeze()
    low = daily["Low"].squeeze()
    close = daily["Close"].squeeze()

    prev_high = float(high.iloc[-2])
    prev_low = float(low.iloc[-2])
    prev_close = float(close.iloc[-2])

    intraday = yf.download("^NSEI", period="1d", interval="5m", progress=False, auto_adjust=True)
    if intraday.empty:
        intraday = yf.download("^NSEI", period="1d", interval="15m", progress=False, auto_adjust=True)

    if not intraday.empty:
        nifty_now = float(intraday["Close"].squeeze().iloc[-1])
        day_open = float(intraday["Open"].squeeze().iloc[0])
    else:
        nifty_now = float(close.iloc[-1])
        day_open = nifty_now

    gap_pct = round((day_open - prev_close) / prev_close * 100, 2)
    move_from_open_pct = round((nifty_now - day_open) / day_open * 100, 2)

    plan = build_nifty_daily_plan()
    regime = plan.get("regime", "NEUTRAL")
    bias = plan.get("bias", "NEUTRAL")

    if nifty_now < prev_low:
        action = "TRADE PE"
        instrument = f"NIFTY {round_nifty_strike(nifty_now)} PE"
        reason = (
            f"Nifty **{nifty_now:.0f}** is below yesterday's low **{prev_low:.0f}** — "
            "breakdown confirmed. PE setup is active."
        )
    elif nifty_now > prev_high:
        action = "TRADE CE"
        instrument = f"NIFTY {round_nifty_strike(nifty_now)} CE"
        reason = (
            f"Nifty **{nifty_now:.0f}** is above yesterday's high **{prev_high:.0f}** — "
            "breakout confirmed. CE setup is active."
        )
    else:
        action = "SKIP"
        instrument = "—"
        reason = (
            f"Nifty **{nifty_now:.0f}** is between yesterday's low **{prev_low:.0f}** "
            f"and high **{prev_high:.0f}** — no breakout yet. **Do not trade.** "
            "Wait or skip today."
        )

    return {
        "signal_date": signal_date,
        "signal_time": signal_time,
        "action": action,
        "instrument": instrument,
        "nifty_now": round(nifty_now, 2),
        "day_open": round(day_open, 2),
        "prev_high": round(prev_high, 2),
        "prev_low": round(prev_low, 2),
        "gap_pct": gap_pct,
        "move_from_open_pct": move_from_open_pct,
        "regime": regime,
        "bias": bias,
        "reason": reason,
        "exit_reminder": (
            "Book half at +50% premium, rest at +80–100%. Cut at -40%. "
            "Week-1 options: exit today or tomorrow."
        ),
    }


def load_nifty_predictions() -> pd.DataFrame:
    if os.path.exists(NIFTY_PREDICTIONS_PATH):
        df = pd.read_csv(NIFTY_PREDICTIONS_PATH, dtype=str)
        for col in NIFTY_PRED_COLS:
            if col not in df.columns:
                df[col] = None
        for col in ("nifty_at_signal", "prev_high", "prev_low", "eod_nifty", "day_return_pct"):
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        return df[NIFTY_PRED_COLS]
    return pd.DataFrame(columns=NIFTY_PRED_COLS)


def log_nifty_prediction(confirmation: dict[str, Any]) -> pd.DataFrame:
    preds = load_nifty_predictions()
    action = confirmation.get("action", "SKIP")
    if action == "SKIP":
        return preds

    signal_date = confirmation["signal_date"]
    existing = (
        set(preds[preds["signal_date"] == signal_date]["action"].tolist()) if not preds.empty else set()
    )
    if action in existing:
        print(f"[skip] {action} already logged for {signal_date}")
        return preds

    row = {
        "prediction_id": f"{signal_date}_{action.replace(' ', '_')}",
        "signal_date": signal_date,
        "signal_time_ist": confirmation["signal_time"],
        "action": action,
        "instrument": confirmation["instrument"],
        "nifty_at_signal": confirmation["nifty_now"],
        "prev_high": confirmation["prev_high"],
        "prev_low": confirmation["prev_low"],
        "regime": confirmation["regime"],
        "bias": confirmation["bias"],
        "status": "open",
        "eod_nifty": None,
        "day_return_pct": None,
        "outcome": "pending",
        "scored_date": None,
        "notes": confirmation["reason"].replace("**", ""),
    }
    preds = pd.concat([preds, pd.DataFrame([row])], ignore_index=True)
    preds.to_csv(NIFTY_PREDICTIONS_PATH, index=False)
    print(f"[ok]   logged Nifty prediction: {action} → {confirmation['instrument']}")
    return preds


def score_nifty_predictions(scored_date: str | None = None) -> pd.DataFrame:
    preds = load_nifty_predictions()
    if preds.empty:
        return preds

    scored_date = scored_date or datetime.now().strftime("%Y-%m-%d")
    daily = yf.download("^NSEI", period="5d", interval="1d", progress=False, auto_adjust=True)
    if daily.empty:
        return preds

    eod_nifty = float(daily["Close"].squeeze().iloc[-1])
    updated_rows = []
    scored_count = 0

    for _, row in preds.iterrows():
        rec = row.to_dict()
        if rec.get("status") == "open" and rec.get("action") != "SKIP" and str(rec.get("signal_date")) == scored_date:
            entry = float(rec["nifty_at_signal"])
            ret_pct = round((eod_nifty - entry) / entry * 100, 2)
            if rec["action"] == "TRADE PE":
                outcome = "winner" if ret_pct <= -0.2 else "loser" if ret_pct >= 0.2 else "flat"
            else:
                outcome = "winner" if ret_pct >= 0.2 else "loser" if ret_pct <= -0.2 else "flat"
            rec["eod_nifty"] = round(eod_nifty, 2)
            rec["day_return_pct"] = ret_pct
            rec["outcome"] = outcome
            rec["status"] = "closed"
            rec["scored_date"] = scored_date
            scored_count += 1
        updated_rows.append(rec)

    preds = pd.DataFrame(updated_rows, columns=NIFTY_PRED_COLS)
    preds.to_csv(NIFTY_PREDICTIONS_PATH, index=False)
    if scored_count:
        print(f"[ok]   scored {scored_count} Nifty predictions")
    return preds


def format_morning_section(conf: dict[str, Any]) -> list[str]:
    if conf.get("error"):
        return [MORNING_SECTION, "", f"_{conf['error']}_", ""]

    action_emoji = {"TRADE PE": "🔴", "TRADE CE": "🟢", "SKIP": "⚪"}.get(conf["action"], "")

    lines = [
        MORNING_SECTION,
        "",
        f"**Checked at:** {conf['signal_time']}  ",
        f"**Decision:** {action_emoji} **{conf['action']}**  ",
    ]
    if conf["action"] != "SKIP":
        lines.append(f"**Instrument:** {conf['instrument']}  ")
    lines.extend(
        [
            "",
            conf["reason"],
            "",
            "### Live context",
            "",
            f"- Nifty now: **{conf['nifty_now']}**",
            f"- Day open: {conf['day_open']} (gap {conf['gap_pct']:+.2f}%)",
            f"- Move from open: {conf['move_from_open_pct']:+.2f}%",
            f"- PE trigger (below): **{conf['prev_low']}**",
            f"- CE trigger (above): **{conf['prev_high']}**",
            f"- Regime: {conf['regime']} | Bias: {conf['bias']}",
            "",
        ]
    )
    if conf["action"] != "SKIP":
        lines.extend(["### Exit reminder", "", f"- {conf['exit_reminder']}", ""])
    else:
        lines.append("_No trade today unless levels break later — but prefer to skip choppy days._")
        lines.append("")
    return lines


def format_nifty_tracker_section(preds: pd.DataFrame, days: int = 30) -> list[str]:
    lines = ["## Nifty trade tracker (review)", ""]
    if preds.empty:
        lines.append("_No Nifty trade signals logged yet. TRADE PE/CE entries appear here after morning scans._")
        lines.append("")
        return lines

    preds = preds.copy()
    preds["signal_date"] = pd.to_datetime(preds["signal_date"])
    cutoff = datetime.now() - timedelta(days=days)
    recent = preds[preds["signal_date"] >= cutoff].sort_values("signal_date", ascending=False)

    trades = recent[recent["action"].isin(["TRADE PE", "TRADE CE"])]
    closed = trades[trades["status"] == "closed"]
    winners = closed[closed["outcome"] == "winner"]

    lines.append(f"**Last {days} days** — use this to review and tune the setup.")
    lines.append("")
    lines.append(f"- **Trade signals:** {len(trades)}")
    lines.append(f"- **Closed:** {len(closed)} | **Wins:** {len(winners)}")
    if len(closed) > 0:
        win_rate = round(len(winners) / len(closed) * 100, 1)
        avg_ret = round(closed["day_return_pct"].mean(), 2)
        lines.append(f"- **Win rate (direction correct by EOD):** {win_rate}%")
        lines.append(f"- **Avg Nifty move from signal:** {avg_ret:+.2f}%")
    lines.append("")

    if not trades.empty:
        lines.append("### Recent Nifty signals")
        lines.append("")
        lines.append("| Date | Time | Action | Instrument | Nifty @ signal | EOD | Move % | Outcome |")
        lines.append("|------|------|--------|------------|---------------:|----:|-------:|---------|")
        for _, r in trades.head(20).iterrows():
            eod = r["eod_nifty"] if pd.notna(r["eod_nifty"]) else "—"
            move = r["day_return_pct"] if pd.notna(r["day_return_pct"]) else "—"
            sig_date = r["signal_date"]
            if hasattr(sig_date, "strftime"):
                sig_date = sig_date.strftime("%Y-%m-%d")
            lines.append(
                f"| {sig_date} | {r['signal_time_ist']} | {r['action']} | {r['instrument']} "
                f"| {r['nifty_at_signal']} | {eod} | {move} | {r['outcome']} |"
            )
        lines.append("")

    return lines


def remove_report_section(content: str, section_header: str) -> str:
    if section_header not in content:
        return content
    before, after = content.split(section_header, 1)
    next_header = after.find("\n## ")
    if next_header != -1:
        after = after[next_header + 1 :]
    else:
        after = ""
    return (before.rstrip() + "\n\n" + after.lstrip()).strip() + "\n"


def upsert_report_section(section_header: str, section_lines: list[str]) -> None:
    """Replace or insert a markdown section in report.md."""
    os.makedirs(DATA_DIR, exist_ok=True)
    if os.path.exists(REPORT_PATH):
        with open(REPORT_PATH, encoding="utf-8") as f:
            content = f.read()
    else:
        content = "# EOD Momentum Scan Report\n\n"

    content = remove_report_section(content, section_header)
    new_section = "\n".join(section_lines).rstrip() + "\n"

    if content.strip() == "# EOD Momentum Scan Report":
        content = "# EOD Momentum Scan Report\n\n" + new_section
    elif content.startswith("# EOD Momentum Scan Report\n\n"):
        rest = content[len("# EOD Momentum Scan Report\n\n") :]
        content = "# EOD Momentum Scan Report\n\n" + new_section + rest
    else:
        content = content.rstrip() + "\n\n" + new_section

    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"[ok]   updated {REPORT_PATH}")


def run_morning() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    confirmation = build_morning_confirmation()
    log_nifty_prediction(confirmation)
    preds = load_nifty_predictions()

    if os.path.exists(REPORT_PATH):
        with open(REPORT_PATH, encoding="utf-8") as f:
            content = f.read()
    else:
        content = "# EOD Momentum Scan Report\n\n"

    content = remove_report_section(content, MORNING_SECTION)
    content = remove_report_section(content, "## Nifty trade tracker (review)")

    morning_text = "\n".join(format_morning_section(confirmation)).rstrip() + "\n"
    tracker_text = "\n".join(format_nifty_tracker_section(preds)).rstrip() + "\n"

    if content.startswith("# EOD Momentum Scan Report\n\n"):
        rest = content[len("# EOD Momentum Scan Report\n\n") :]
    elif content.startswith("# EOD Momentum Scan Report"):
        rest = content[len("# EOD Momentum Scan Report") :].lstrip()
    else:
        rest = content

    content = "# EOD Momentum Scan Report\n\n" + morning_text + "\n" + tracker_text + "\n" + rest.lstrip()

    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"[ok]   updated {REPORT_PATH}")
    print(f"[ok]   morning decision: {confirmation.get('action', 'ERROR')}")


def format_nifty_plan_section(plan: dict[str, Any]) -> list[str]:
    if plan.get("error"):
        return ["## Nifty daily plan (tomorrow)", "", f"_{plan['error']}_", ""]

    lines = [
        "## Nifty daily plan (tomorrow)",
        "",
        f"**Spot:** {plan['spot']} ({plan['day_change_pct']:+.2f}% today)  ",
        f"**Regime:** {plan['regime']}  ",
        f"**Bias:** {plan['bias']} — {plan['bias_reason']}  ",
        f"**Today's signal:** {plan['signal']}  ",
        "",
        f"_{plan['signal_note']}_",
        "",
        "### Key levels",
        "",
        f"- Yesterday's high: **{plan['prev_high']}** (CE trigger above this)",
        f"- Yesterday's low: **{plan['prev_low']}** (PE trigger below this)",
        f"- 50-day average: {plan['sma50'] or '—'}",
        f"- 200-day average: {plan['sma200'] or '—'}",
        "",
        "### Tomorrow's plan",
        "",
        plan["tomorrow_plan"],
        "",
        "### Exit rules (weeklies)",
        "",
        "- Book **half** at **+50%** on premium",
        "- Book **rest** at **+80–100%** or trail",
        "- **Exit** if premium is down **40%**",
        "- Week-1 options: treat as **today/tomorrow** trade, not hold till expiry",
        "",
        f"⚠️ {plan['dont_chase']}",
        "",
    ]
    return lines


def extract_report_section(content: str, section_header: str) -> str | None:
    if section_header not in content:
        return None
    _, after = content.split(section_header, 1)
    next_header = after.find("\n## ")
    body = after[:next_header] if next_header != -1 else after
    return section_header + body.rstrip() + "\n"


def write_report(
    scan_date: str,
    candidates: list[dict[str, Any]],
    track: pd.DataFrame,
    symbols_scanned: int,
    trading_days: int,
    nifty_plan: dict[str, Any] | None = None,
) -> None:
    closed = track[track["outcome"].isin(["winner", "loser", "stopped"])] if not track.empty else track
    winners = closed[closed["outcome"] == "winner"] if not closed.empty else closed
    win_rate = round(len(winners) / len(closed) * 100, 1) if len(closed) > 0 else None
    avg_return = round(closed["return_pct"].mean(), 2) if len(closed) > 0 else None

    lines = [
        "# EOD Momentum Scan Report",
        "",
        f"**Scan date:** {scan_date}  ",
        f"**Symbols scanned:** {symbols_scanned}  ",
        f"**Trading days in history:** {trading_days}  ",
        "",
    ]

    preserved_morning = None
    if os.path.exists(REPORT_PATH):
        with open(REPORT_PATH, encoding="utf-8") as f:
            existing = f.read()
        if MORNING_SECTION in existing and scan_date in existing:
            preserved_morning = extract_report_section(existing, MORNING_SECTION)

    if preserved_morning:
        lines.extend(preserved_morning.splitlines())
        lines.append("")

    if nifty_plan:
        lines.extend(format_nifty_plan_section(nifty_plan))

    lines.extend(
        [
            "## Stock momentum candidates",
            "",
        ]
    )

    if candidates:
        lines.append("| Symbol | Price | RS | Vol x Avg | Stop |")
        lines.append("|--------|------:|---:|----------:|-----:|")
        for c in candidates[:25]:
            lines.append(
                f"| {c['symbol']} | {c['price']} | {c['rs']} | {c['volume_ratio']}x | {c['stop']} |"
            )
    else:
        lines.append("_No names passed Trend Template + Volume Confirmation today._")

    lines.extend(["", "## Track record", ""])
    if win_rate is not None:
        lines.append(f"- **Closed predictions:** {len(closed)}")
        lines.append(f"- **Win rate (≥10% gain):** {win_rate}%")
        lines.append(f"- **Average return (closed):** {avg_return}%")
        lines.append(f"- **Open predictions:** {len(track) - len(closed) if not track.empty else 0}")
    else:
        lines.append("_Track record builds as predictions close. Check back after a few weeks._")

    if not track.empty:
        lines.extend(["", "### Recent predictions", ""])
        lines.append("| Symbol | Scan date | Entry | Latest | Return % | Outcome |")
        lines.append("|--------|-----------|------:|-------:|---------:|---------|")
        for _, r in track.sort_values("scan_date", ascending=False).head(15).iterrows():
            lines.append(
                f"| {r['symbol']} | {r['scan_date']} | {r['entry_price']} | "
                f"{r['latest_close']} | {r['return_pct']} | {r['outcome']} |"
            )

    nifty_preds = load_nifty_predictions()
    lines.extend(["", *format_nifty_tracker_section(nifty_preds)])

    lines.extend(
        [
            "",
            "---",
            "_Technical rules only. Verify fundamentals and news catalysts by hand before trading._",
        ]
    )

    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"[ok]   wrote {REPORT_PATH}")


def run_daily(session: requests.Session, target: datetime | None = None) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    target = target or datetime.now()
    day_df = fetch_bhavcopy(session, target)
    if day_df is None:
        return

    history = load_history()
    history = append_history(history, day_df)
    save_history(history)

    hist_dict = history_to_dict(history)
    rs_ratings = compute_rs_ratings(hist_dict)
    candidates = scan_candidates(hist_dict, rs_ratings)
    scan_date = day_df["date"].iloc[0]

    log_predictions(candidates, scan_date)
    track = score_predictions(hist_dict, scan_date)
    score_nifty_predictions(scan_date)
    trading_days = history["date"].nunique()
    nifty_plan = build_nifty_daily_plan()
    write_report(scan_date, candidates, track, len(hist_dict), trading_days, nifty_plan)


def run_backfill(session: requests.Session, days: int) -> None:
    today = datetime.now()
    dates = [today - timedelta(days=i) for i in range(days)]
    dates = [d for d in dates if d.weekday() < 5]
    dates.reverse()

    for day in dates:
        run_daily(session, day)
        time.sleep(1.5)


def main() -> None:
    parser = argparse.ArgumentParser(description="EOD momentum scan with prediction tracking")
    parser.add_argument("--backfill", type=int, help="fetch and process the last N calendar weekdays")
    parser.add_argument(
        "--morning",
        action="store_true",
        help="run morning Nifty confirmation (~10 AM IST) and update report",
    )
    args = parser.parse_args()

    if args.morning:
        run_morning()
        return

    session = get_session()
    if args.backfill:
        run_backfill(session, args.backfill)
    else:
        run_daily(session)


if __name__ == "__main__":
    main()
