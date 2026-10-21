# ₹25,000 NIFTY option-buying test: status and plain-English summary

_Written 20-Oct-2026._

## Where things stand

**The real answer to "where does ₹25,000 land after 3 and 5 years?" is not known yet.** No real NSE option data has been loaded. This container's network policy blocks nseindia.com, nsearchives.nseindia.com, Yahoo and Stooq, so the files have to come from you (see `DATA_CHECKLIST.md`). The backtester, cost model, tests, Monte Carlo and checklist are built and tested. Once the CSVs arrive, one command produces every number.

Everything in `output/SAMPLE_RUN/` comes from tiny invented data labelled `SAMPLE_DATA_FOR_ACCOUNTING_TESTS_ONLY`. It shows that the accounting works. It says nothing about profit. On that sample, the Monte Carlo correctly refuses to project anything (`INSUFFICIENT_TRADES`).

## Honest prior, before any backtest

- **SEBI's own data:** [SEBI, 23-Sep-2024](https://www.sebi.gov.in/media-and-notifications/press-releases/sep-2024/updated-sebi-study-reveals-93-of-individual-traders-incurred-losses-in-equity-fando-between-fy22-and-fy24-aggregate-losses-exceed-1-8-lakh-crores-over-three-years_86906.html): 93% of individual F&O traders lost money between FY22 and FY24.
- **What the rules force you to buy** (lot 65 today):
  - 10% risk: ₹2,500 ÷ 65 = **at most ₹38.46 premium per unit**, before costs.
  - 2% risk: ₹500 ÷ 65 = **₹7.69 per unit**.
  - The code will measure how far out of the money that pushes the strike (`avg_pct_otm_traded`, `affordability.csv`). Cheap options need a big, fast move just to break even.
- **Fixed cost drag per round trip:**
  - Brokerage ₹20 × 2 plus 18% GST on it comes to ₹47.20, before STT, NSE fees or slippage.
  - That is **1.9% of a ₹2,500 trade and 9.4% of a ₹500 trade**.
  - 1% slippage on each side adds about 2% more.
  - Time decay works against the buyer every day on top of that.
- **Expectation:** an untuned momentum rule has to beat all of that. If the real backtest shows the account drifting toward zero, that is the expected result, not a bug. I'll report it plainly.

## Where every number will come from

| Item | Source |
|---|---|
| Lot size per expiry | NSE circulars: 50 (Jul-2021 to Apr-2024, [FAOP/47854](https://archives.nseindia.com/content/circulars/FAOP47854.pdf) and [FAOP/61415](https://nsearchives.nseindia.com/content/circulars/FAOP61415.pdf)); 25 (30-May-2024 to 30-Jan-2025, FAOP/61415 and [FAOP/64625](https://nsearchives.nseindia.com/content/circulars/FAOP64625.pdf)); 75 (27-Feb-2025 to 30-Dec-2025, FAOP/64625, NIFTY unchanged per [FAOP/67372](https://nsearchives.nseindia.com/content/circulars/FAOP67372.pdf)); 65 (from 27-Jan-2026, [FAOP/70616](https://nsearchives.nseindia.com/content/circulars/FAOP70616.pdf)). |
| Lot-size note 1 | The **monthly** contract stayed at 25 until 30-Jan-2025, not Nov-2024. Weekly contracts switched earlier. |
| Lot-size note 2 | The start and end of the 50-lot era are verified. My searches found no NIFTY revision between them. |
| Lot-size note 3 | Expiries outside Jul-2021 to Dec-2026 raise an error rather than guessing. |
| Charges | [zerodha.com/charges](https://zerodha.com/charges), F&O Options column, checked 20-Oct-2026. They match the figures you gave. Applied to all years, which is conservative because older STT and NSE fees were lower. |
| Slippage | 1% adverse on every fill (assumption). |
| Expiry dates in checklist | Computed (last Thursday to Aug-2025, last Tuesday from Sep-2025) and moved for holidays. The 2026 holidays come from NSE's holiday page. **The NSE dropdown is the final authority.** |
| Trades, P&L, equity | Only from your NSE CSVs, using each day's closing prices. |
| Monte Carlo | Resamples the backtest's real per-trade return on equity at the observed trades-per-month rate (Poisson), 10,000 runs, seed 42. A run is busted below the median cheapest liquid lot seen in the data. The benchmark resamples real monthly NIFTY 50 (or NIFTYBEES) returns over the same months. |

## Design choices (all shown, none tuned)

- **Baseline entry:** buy at the close of the day *after* the signal. Using the same day's close would mean trading on a price you only know after the market shuts (look-ahead). That optimistic version is still run and reported, at both 10% and 2% risk, so you get **4 variants side by side** in `variations.csv`.
- **Exits:** target and stop are checked on closing prices only. A −40% stop can fill much worse after a gap. That is realistic and isn't softened.
- **Missing prices:** if the held contract has no price that day, it is marked at the last price. If it is still open at expiry, it settles at intrinsic value, with 0.15% STT on any ITM value.
- **Benchmark:** buy-and-hold uses fractional units with no costs. If only NIFTY 50 is supplied, the benchmark excludes dividends, which is slightly unfair to the ETF.

## Tests: 35/35 pass

The hand-calculated checks cover zero-cost profit end-to-end (₹1,500), ITM expiry (+₹3,706.15), worthless expiry (−₹7,526.98), max drawdown (30%), full charges with slippage (net ₹4,231.57), the no-borrowing rejection, the deposit block, lot sizes by date, Monte Carlo sanity, and parsing of the raw NSE formats.

The hand calculations are written as comments in section 8 of the code. Run `python fno_backtester.py test` to repeat them.

## What even the full 5-year data cannot prove

- **Fills:** daily closes hide intraday fills, so real stops and targets will differ.
- **Market history:** one 5-year path is a single draw of market history.
- **Monte Carlo limits:** it assumes trades are independent and ignores lot granularity at small equity. Treat it as a projection from limited data, not a forecast.
- **Costs:** charges and lot sizes will change again.

## One honest next step

Download the **31 NIFTY 50 index files** first (rows 121–151 in the checklist). They are small, they give the benchmark, and `suggest` turns them into an honest 12-month option sample (24 more files) if 151 files is too many.
