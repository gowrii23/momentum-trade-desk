# EOD Momentum Scan

Automated end-of-day scan of NSE equities against a momentum rule set, with a
running prediction-vs-reality track record.

**👉 [Today's scan and track record](data/report.md)** — updated every weekday evening.

---

## What this does

Every weekday at 19:00 IST, a GitHub Actions workflow:

1. Fetches the latest NSE Bhavcopy (free, no API key)
2. Appends it to `data/history.csv`
3. Computes SMA50/150/200, 52-week high/low, ATR14, and an RS percentile
4. Scans every liquid symbol against Trend Template + Volume Confirmation
5. Logs qualifying names to `data/predictions.csv` with entry price and stop
6. Scores all past predictions against the latest close
7. Writes `data/report.md`

## Where to look

| File | What's in it |
|---|---|
| [`data/report.md`](data/report.md) | Today's candidates + running win rate — **start here** |
| [`data/track_record.csv`](data/track_record.csv) | Every prediction with its actual return |
| [`data/predictions.csv`](data/predictions.csv) | Raw prediction log |
| [`data/history.csv`](data/history.csv) | Stored price history |

The same report also appears in the **Actions** tab under each run's summary,
without needing to open any files.

## Other files

- `daily_scan.py` — the pipeline
- `.github/workflows/daily-scan.yml` — the schedule
- `AUTOMATION-SETUP.md` — setup instructions
- `momentum-trading-rules.md` — the rule set this implements, and the books behind it
- `trading-rules-mindmap.mermaid` — the rules as a visual map
- `trade-desk.html` — manual per-stock scoring tool (open in a browser)
- `index.html` — legacy trade desk with Nifty OTM CE signals (GitHub Pages)

## Legacy local tools

- `scan_momentum.py` + `fetch_bhavcopy.py` — original scanner writing to `output/latest_scan.json`
- Run locally: `python fetch_bhavcopy.py --backfill 200 && python scan_momentum.py`

## Important

The scan checks **technical rules only**. Fundamentals (EPS growth, ROE) and a fresh
news catalyst are not automated — no free source provides them at scale — and they're
the checks that separate "this chart qualifies" from "this is a trade." Verify those by
hand on the handful of names the scan surfaces.

This is a learning and tracking exercise, not trade execution. Momentum systems
typically win 35-45% of the time and profit on the size of winners, so judge the track
record over months and dozens of predictions, not over a good or bad week.
