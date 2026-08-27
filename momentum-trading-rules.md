# Momentum + News Trading Rules — Reference for Trade Decision Engine

Synthesized from: *Trade Like a Stock Market Wizard* (Minervini), *How to Make Money in Stocks* (O'Neil/CANSLIM), *Technical Analysis of the Financial Markets* (Murphy), *Options as a Strategic Investment* (McMillan), *Reminiscences of a Stock Operator* (Lefèvre).

This is a probability-tilting framework, not a certainty system. Every rule below exists to cut bad trades and size good ones — not to predict direction with certainty.

---

## 1. Universe & Fundamental Filter (pre-screen, run daily/weekly)

Only momentum-scan stocks that pass this filter — momentum without fundamentals is mostly noise:

- **EPS growth**: last quarter YoY EPS growth ≥ 20-25% (CANSLIM "C")
- **Sales growth**: YoY revenue growth ≥ 15-20%
- **ROE** ≥ 15%
- **Debt/Equity** < 1 (sector-dependent — relax for financials/infra)
- **Float / free-float market cap**: prefer smaller float — moves faster on news (matches your existing small/mid-cap thesis in Blueprint Screener)
- **Institutional ownership trend**: increasing QoQ (proxy: rising delivery % on NSE, or DII/FII shareholding delta from quarterly filings)
- **Sector leadership**: stock should be top quartile relative strength within its own sector, not just the index

Output: a "tradeable universe" list, re-generated weekly. This becomes the input to the technical layer.

---

## 2. Technical / Momentum Rules (entry filter)

### 2.1 Trend template (Minervini, adapted)
A stock qualifies for a momentum entry only if ALL of these hold:
1. Price > 50-day SMA > 150-day SMA > 200-day SMA
2. 200-day SMA trending up for at least 1 month
3. Price within 25% of its 52-week high
4. Price at least 30% above its 52-week low
5. Relative Strength (RS) rating vs. index in top 20-30% (formula below)

**RS Rating formula** (simplified O'Neil version):
```
RS_raw = (P_now / P_63days_ago) * 0.4
       + (P_now / P_126days_ago) * 0.2
       + (P_now / P_189days_ago) * 0.2
       + (P_now / P_252days_ago) * 0.2
RS_rating = percentile_rank(RS_raw) across universe, scaled 1-99
```

### 2.2 Volume confirmation
- Breakout day volume ≥ **1.5-2x** the 50-day average volume
- Reject breakouts on below-average volume — high failure rate

### 2.3 Volatility Contraction Pattern (VCP)
- Look for a base where each pullback is shallower than the last (e.g., -25% → -15% → -8%)
- Entry trigger: breakout above the base's pivot high, on volume spike
- Tighter final contraction (<10%) = higher-quality setup

### 2.4 ATR for stop placement (Murphy)
```
ATR(14) = 14-day average of True Range
True Range = max(High-Low, |High-PrevClose|, |Low-PrevClose|)
Stop-loss = Entry - (1.5 to 2 * ATR)   [long trade]
```
Use ATR instead of a fixed % — adapts stop distance to the stock's actual volatility.

---

## 3. News/Catalyst Rules (the "why now")

- **Freshness**: news must be ≤ 24-48 hours old. Stale news is priced in — this is what separates your friend's IDEA/NIFTY-CE trades from a random momentum entry.
- **Materiality filter**: classify news as High/Medium/Low impact:
  - High: earnings beat/miss, regulatory relief (like AGR dues), large order win, M&A, credit rating change
  - Medium: management commentary, sector policy news, analyst upgrade/downgrade
  - Low: routine board meeting notices, generic press mentions — filter these out
- **Price/volume reaction confirms materiality**: if news is High-impact but volume/price barely move, the market has already discounted it — skip.
- **Gap rule**: a gap-up ≥ 3% on News + Volume trigger = priority candidate for same-day or next-day entry, NOT for chasing 3+ days later.

This is the layer where an LLM (Claude) adds real value over pure price/volume screens: summarizing the announcement and classifying it Fresh/Stale, Material/Immaterial — a judgment call, not just a number.

---

## 4. Risk Management & Position Sizing (non-negotiable layer)

```
Risk per trade = 1-2% of total capital  (Lefèvre/Minervini consensus)
Position size = (Capital * Risk%) / (Entry - StopLoss)
```

- Max concentration: no single trade > 8-10% of capital, even if the stop distance formula allows more
- Max sector/theme exposure: cap correlated positions (e.g., don't stack 3 telecom momentum bets at once)
- **R-multiple tracking**: measure every trade in units of initial risk (R), not ₹. A momentum system can be right only 35-45% of the time and still be profitable if winners average 2.5-3R and losers are capped at -1R.
- Trail stop once trade is +1R in profit (move stop to breakeven), then trail using ATR or the rising 20-day SMA.

---

## 5. Options-Specific Rules (McMillan)

Options add leverage but also two extra failure modes: **IV crush** and **theta decay**. Rules to control for both:

- **Don't buy options where IV is already elevated by the news itself** — check IV percentile (IV relative to its own 6-month range) before entry. Buying a call the day of an earnings beat, after IV has already spiked, means you're paying inflated premium that collapses (IV crush) even if direction is right.
- **Prefer options with 30-45+ days to expiry** for a momentum/news swing trade — enough time for the thesis to play out without theta dominating. Weekly/near-expiry options are for very high-conviction, short-duration catalysts only (this is closer to what produced the NIFTY 24200 CE result in your screenshot — high risk, high variance).
- **Delta as a directional proxy**: for a "stock substitute" trade, use options with delta 0.6-0.7 (deep enough ITM to track the stock, less exposed to pure theta/vega noise). For a lottery-ticket high-leverage play (accepting most will expire worthless), OTM low-delta (0.2-0.3) is the explicit tradeoff — size these at near-zero % of capital.
- **Max loss = premium paid** — always define this as the "stop" for a long option; there's no ATR-based stop needed since the structure itself caps risk.
- **Never average down on a losing option** — time decay makes this fundamentally different from averaging down on stock.

---

## 6. Trade Execution Checklist (what the app should evaluate per candidate)

For each stock in the tradeable universe, the engine should score:

| Check | Pass condition |
|---|---|
| Fundamental filter | Section 1 passed |
| Trend template | All 5 Minervini conditions met |
| Volume | ≥1.5x 50-day avg on trigger day |
| News freshness | ≤48h, classified High/Medium |
| News materiality vs. reaction | Price/volume move confirms it wasn't priced in |
| Risk-defined entry | ATR-based stop calculable, position size ≤10% capital |
| Options (if used) | IV percentile checked, expiry ≥30 days unless explicit lottery-ticket bucket |

A candidate should pass ALL rows to generate a "Buy suggestion." Partial passes = "Watch" list, not a trade signal.

---

## 7. Backtest Before Live Use

Before wiring this into buy/sell execution:
- Backtest the rule set on 12+ months of NSE bhavcopy + historical corporate announcements
- Track: win rate, average R-multiple, max drawdown, and whether the News layer actually improved returns vs. Technical-only
- Paper trade for at least 4-8 weeks before connecting to real order execution

---

## Notes for integration into Blueprint Screener / Cursor_Stock_News

- Section 1 (fundamentals) extends the existing Screener tab funnel
- Section 2 (technical/RS/ATR) extends the existing Swing tab's bhavcopy momentum logic
- Section 3 (news) extends the existing News/Flags tabs — this is the layer where an LLM call (Claude) classifying Fresh/Stale + Material/Immaterial adds the most value over pure numeric screens
- Sections 4-5 (risk/options sizing) are new — currently not represented in the app based on existing architecture, and are the layer most directly responsible for whether the system survives long-term vs. blows up on a bad string of trades
