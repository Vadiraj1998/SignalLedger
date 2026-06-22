# market-watchlist

**Rule-based daily market watchlist with full historical accountability.**

No cherry-picking. Every signal is recorded at scan time. Every result is logged at EOD. The git history is the proof.

---

## What it does

**Morning (auto):**
- Scans Binance USDT perpetual futures (top 25 coins)
- Scans NSE F&O universe (~200 stocks) + NIFTY / BankNifty / FinNifty
- Applies rule-based filters (OI buildup, funding extremes, volume spikes, PCR, 52w levels)
- Saves signals as JSON with entry price and bias (long/short/neutral)
- Pushes to GitHub

**EOD (auto):**
- Fetches closing price for every signal
- Computes move % and success (did it go in the biased direction?)
- Patches the same JSON file
- Pushes to GitHub

**Weekly (auto):**
- Aggregates all historical data
- Computes win rate, avg move per filter
- Saves `aggregator/filter_performance.json`

---

## Filters

### Crypto
| Filter | Logic |
|---|---|
| `FUNDING_EXTREME_LONG` | Funding rate ≤ -0.05% → crowded shorts, long bias |
| `FUNDING_EXTREME_SHORT` | Funding rate ≥ 0.10% → crowded longs, short bias |
| `OI_SURGE` | OI up >20% in 24h → new money entering |
| `VOLUME_SPIKE` | 24h volume >2x 7-day average |
| `MOMENTUM_OI_CONFIRM` | Price >3% + OI >10% → trend with conviction |
| `DUMP_OI_CONFIRM` | Price <-3% + OI >10% → bearish with conviction |

### Stocks / Indices
| Filter | Logic |
|---|---|
| `OI_BUILDUP_BULLISH` | OI >15% + Price >1% → long buildup |
| `OI_BUILDUP_BEARISH` | OI >15% + Price <-1% → short buildup |
| `VOLUME_SURGE` | Volume >2x 20-day average |
| `NEAR_52W_HIGH` | Within 2% of 52-week high → breakout watch |
| `NEAR_52W_LOW` | Within 2% of 52-week low → breakdown watch |
| `PCR_EXTREME_BULLISH` | PCR >1.5 → oversold, potential bounce |
| `PCR_EXTREME_BEARISH` | PCR <0.5 → overbought, potential reversal |

---

## Data format

**Signal file** (`data/crypto/2026-06-22.json`):
```json
{
  "date": "2026-06-22",
  "symbol": "BTCUSDT",
  "market": "crypto",
  "filters": ["FUNDING_EXTREME_SHORT", "OI_SURGE"],
  "bias": "short",
  "price_at_scan": 104250.0,
  "funding_rate": 0.12,
  "oi_change_pct_24h": 23.4,
  "eod_price": 101800.0,
  "move_pct": -2.35,
  "success": true,
  "updated_at": "2026-06-22T18:00:00Z"
}
```

---

## Setup

```bash
git clone https://github.com/yourusername/market-watchlist
cd market-watchlist
pip install requests

# Configure git for auto-push
git config user.email "you@example.com"
git config user.name "market-watchlist-bot"

# Add cron jobs
crontab -e
# Paste contents of scanner/crontab.txt (update paths first)
```

---

## Run manually

```bash
# Morning scans
python scanner/crypto_scanner.py
python scanner/stock_scanner.py

# EOD updates
python scanner/eod_updater.py --market crypto
python scanner/eod_updater.py --market stocks

# Filter performance
python aggregator/filter_stats.py

# Push to GitHub
bash scanner/push_to_github.sh "Manual push"
```

---

## Stack
- Python 3.11+
- Binance public REST API (no auth)
- NSE public endpoints
- GitHub Pages (site — coming soon)
- OCI Ubuntu server (cron host)
