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

# --- tunable thresholds (see AUTOMATION-SETUP.md) ---
MIN_VOLUME = 50_000
RS_MIN = 70
VOL_MULTIPLE = 1.5
ATR_STOP_MULT = 1.5

DATA_DIR = "data"
HISTORY_PATH = os.path.join(DATA_DIR, "history.csv")
PREDICTIONS_PATH = os.path.join(DATA_DIR, "predictions.csv")
TRACK_RECORD_PATH = os.path.join(DATA_DIR, "track_record.csv")
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


def write_report(
    scan_date: str,
    candidates: list[dict[str, Any]],
    track: pd.DataFrame,
    symbols_scanned: int,
    trading_days: int,
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
        "## Today's candidates",
        "",
    ]

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
    trading_days = history["date"].nunique()
    write_report(scan_date, candidates, track, len(hist_dict), trading_days)


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
    args = parser.parse_args()

    session = get_session()
    if args.backfill:
        run_backfill(session, args.backfill)
    else:
        run_daily(session)


if __name__ == "__main__":
    main()
