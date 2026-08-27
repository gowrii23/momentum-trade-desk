# Automating the EOD Scan (no laptop required)

The HTML app can't automate itself — browsers can't fetch NSE (bot protection + no CORS),
and it only runs while the page is open. Automation means moving the pipeline to a script
that runs on a schedule. Since you're on mobile, **GitHub Actions** is the best host:
free, cloud-based, and fully manageable from the GitHub mobile app or a browser.

---

## What gets automated

Every weekday evening, unattended:

1. Fetch the latest NSE Bhavcopy
2. Append it to stored price history
3. Recompute SMA50/150/200, 52-week high/low, ATR14, RS percentile
4. Scan every liquid symbol against Trend Template + Volume Confirmation
5. Log today's qualifying names as predictions (with entry price and stop)
6. Score every past prediction against the latest close
7. Write `data/report.md` with today's candidates and your running win rate

You open the GitHub app once a day and read the report. That's the whole workflow.

**Still not automated, by design:** fundamentals (EPS/ROE) and news catalyst. These
aren't available free at scale, and they're the checks that turn "this chart qualifies"
into "this is a trade." Check them by hand on the handful of names the scan surfaces.

---

## One-time setup (all doable from a phone browser)

### 1. Create a GitHub repo
- Sign in at github.com → **New repository**
- Name it anything (e.g. `eod-scan`), set it **Private**, tick "Add a README"

### 2. Add the script
- In the repo → **Add file** → **Create new file**
- Name it `daily_scan.py`
- Paste the contents of the `daily_scan.py` file, then **Commit**

### 3. Add the workflow
- **Add file** → **Create new file**
- Name it exactly: `.github/workflows/daily-scan.yml`
  (typing the slashes creates the folders automatically)
- Paste the contents of `daily-scan.yml`, then **Commit**

### 4. Allow the workflow to commit results
- Repo **Settings** → **Actions** → **General**
- Under "Workflow permissions", select **Read and write permissions** → **Save**

### 5. Build the history (one-time, ~10 minutes)
- Go to the **Actions** tab → **Daily EOD Scan** → **Run workflow**
- This runs the normal daily job. For the initial backfill, temporarily change the
  run line in the workflow to `python daily_scan.py --backfill 300`, commit, run it
  once, then change it back to `python daily_scan.py`.
- The backfill fetches ~200 trading days. It's slow on purpose (1.5s between requests,
  so NSE doesn't block it) — expect roughly 5-10 minutes.

### 6. Done
From then on it runs itself at 19:00 IST, Mon-Fri.

---

## Reading the results

- **`data/report.md`** in the repo — today's candidates + running track record. Tap it in
  the GitHub app to read it rendered.
- **Actions tab → latest run → summary** — the same report, shown without opening files.
- **`data/track_record.csv`** — every prediction with its actual return, for your own analysis.
- **`data/predictions.csv`** — the raw prediction log.

You can also download `data/history.csv` and feed it into the HTML app if you prefer that
interface for manual per-stock scoring.

---

## Tuning the rules

The thresholds live at the top of `daily_scan.py`:

```python
MIN_VOLUME = 50_000     # skip illiquid names
RS_MIN = 70             # relative strength floor
VOL_MULTIPLE = 1.5      # breakout volume vs 50-day average
```

Edit, commit, and the next run uses the new values. Changing these mid-experiment
resets the meaning of your track record, so it's worth letting a rule set run for a
few weeks before adjusting — otherwise you're measuring your tinkering, not the rules.

---

## Things that will go wrong eventually

- **NSE blocks the fetch.** Their bot protection changes periodically. The run logs will
  show `[error]` or `[miss]` lines. Usually resolves on the next run; if it persists, the
  URL format may have changed — check `nseindia.com/all-reports` for the current
  "CM - UDiFF Common Bhavcopy Final" link and update `BASE_URL`.
- **Holidays.** No file published. The script logs `[miss]` and exits cleanly.
- **Empty scan days.** Some days nothing passes. That's a real result, not a bug — clean
  momentum setups aren't present daily.
- **GitHub schedule lag.** Actions cron can run late under load. Fine for EOD work.

---

## Before this becomes real money

The track record is the point of this exercise. Let it accumulate for **at least 2-3 months**
before drawing conclusions — a handful of trades tells you nothing about a system whose
edge shows up over dozens. Momentum systems typically win 35-45% of the time and make
money on the size of the winners, so a run of losses is expected behaviour rather than
proof the rules are broken. Judge it on average return per pick and the win/loss size
ratio, not on whether the last few calls felt right.
