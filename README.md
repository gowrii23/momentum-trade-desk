# Momentum Trade Desk

Daily NSE momentum scanner with **Nifty OTM CE (Call Option)** signals and a browser-based trade scoring desk.

Automatically fetches NSE bhavcopy data via GitHub Actions, ranks momentum stocks using the Minervini trend template + O'Neil relative strength, and recommends out-of-the-money Nifty call strikes when index momentum is bullish.

## What it does

1. **`fetch_bhavcopy.py`** — Downloads NSE's free daily bhavcopy (end-of-day prices for all listed stocks)
2. **`scan_momentum.py`** — Scans bhavcopy history for top momentum stocks and checks Nifty 50 momentum for OTM CE entry ideas
3. **`index.html`** — Browser trade desk to view daily scan results, import bhavcopy, and score individual candidates with news/fundamentals

## Quick start (local)

```bash
pip install -r requirements.txt
python fetch_bhavcopy.py --backfill 200   # ~3 min, builds SMA/RS history
python scan_momentum.py                   # writes output/latest_scan.json
python -m http.server 8080                # open http://localhost:8080
```

## GitHub deployment

### 1. Create a public repo

On GitHub, create a new **public** repository named `momentum-trade-desk` (empty, no README).

### 2. Push this code

```bash
cd momentum-trade-desk
git init
git add .
git commit -m "Initial commit: momentum scanner + Nifty OTM CE signals"
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/momentum-trade-desk.git
git push -u origin main
```

### 3. Enable GitHub Pages

1. Go to **Settings → Pages**
2. Under **Build and deployment**, set Source to **GitHub Actions**
3. The `daily-scan.yml` workflow deploys the site automatically

### 4. Run the first scan

Go to **Actions → Daily Momentum Scan → Run workflow**.  
On first run, set `backfill_days` to `200` to build price history, then run again with `0` for daily updates.

The workflow runs automatically **Mon–Fri at ~7:00 PM IST** after NSE publishes bhavcopy.

## Nifty OTM CE logic

When Nifty passes the momentum trend template:

- Price > SMA50 > SMA150 > SMA200
- Within 25% of 52-week high
- At least 30% above 52-week low
- 200 SMA trending up

The scanner suggests an **OTM CE strike** ~150 points above ATM (rounded to nearest 50). Example: Nifty at 24,350 → suggests `NIFTY 24550 CE`.

| Signal | Meaning |
|--------|---------|
| **BUY** | Full momentum template confirmed — OTM CE candidate |
| **WATCH** | Bullish regime but template not fully confirmed |
| **SKIP** | Bearish regime (below 200 SMA) — avoid new long CE |

See `docs/momentum-trading-rules.md` for the full rule set (position sizing, IV checks, expiry preferences).

## Output files

| File | Description |
|------|-------------|
| `output/latest_scan.json` | Full scan results (Nifty + top stocks) |
| `output/latest_scan.csv` | Top momentum stocks as CSV |
| `bhavcopy_data/` | Cached daily NSE bhavcopy (gitignored locally, cached in Actions) |

## Disclaimer

Scoring tool only — **not order execution**. Backtest and paper-trade before live use. Options involve significant risk; OTM calls are high-variance bets — size accordingly.
