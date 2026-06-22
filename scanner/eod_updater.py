"""
eod_updater.py
EOD scan — patches move_pct, eod_price, success into today's signal files
Run after market close:
  Stocks  → 3:45 PM IST weekdays
  Crypto  → 11:30 PM IST daily (gives full 24h from morning scan)
"""

import json
import time
import logging
import argparse
from datetime import datetime, timezone
from pathlib import Path
import requests

# ── Paths ─────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent.parent

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(BASE_DIR / "scanner" / "eod_updater.log"),
    ],
)
log = logging.getLogger(__name__)

NSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "*/*",
    "Referer": "https://www.nseindia.com/",
}
BINANCE_BASE = "https://fapi.binance.com"


# ── Price fetchers ─────────────────────────────────────────────────────────────

def get_crypto_price(symbol: str) -> float | None:
    try:
        r = requests.get(f"{BINANCE_BASE}/fapi/v1/ticker/price", params={"symbol": symbol}, timeout=10)
        r.raise_for_status()
        return float(r.json()["price"])
    except Exception as e:
        log.warning(f"Crypto price fetch failed for {symbol}: {e}")
        return None


def get_nse_price(session: requests.Session, symbol: str) -> float | None:
    try:
        if symbol in ["NIFTY", "BANKNIFTY", "FINNIFTY"]:
            url = f"https://www.nseindia.com/api/option-chain-indices?symbol={symbol}"
            r = session.get(url, timeout=10)
            r.raise_for_status()
            return float(r.json()["records"]["underlyingValue"])
        else:
            url = f"https://www.nseindia.com/api/quote-equity?symbol={symbol}"
            r = session.get(url, timeout=10)
            r.raise_for_status()
            data = r.json()
            return float(data["priceInfo"]["lastPrice"])
    except Exception as e:
        log.warning(f"NSE price fetch failed for {symbol}: {e}")
        return None


def nse_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(NSE_HEADERS)
    try:
        session.get("https://www.nseindia.com", timeout=10)
        time.sleep(1)
    except Exception:
        pass
    return session


# ── Update logic ───────────────────────────────────────────────────────────────

def update_file(file_path: Path, market: str):
    signals = json.loads(file_path.read_text())
    updated = 0

    session = nse_session() if market in ("stock", "index") else None

    for signal in signals:
        if signal.get("success") is not None:
            continue  # already updated

        symbol = signal["symbol"]
        entry_price = signal.get("price_at_scan")

        if not entry_price:
            continue

        # Fetch EOD price
        if market == "crypto":
            eod_price = get_crypto_price(symbol)
            time.sleep(0.1)
        else:
            eod_price = get_nse_price(session, symbol)
            time.sleep(0.5)

        if eod_price is None:
            log.warning(f"Could not get EOD price for {symbol}")
            continue

        move_pct = round(((eod_price - entry_price) / entry_price) * 100, 3)

        # Determine success based on bias
        bias = signal.get("bias", "neutral")
        if bias == "long":
            success = move_pct > 0
        elif bias == "short":
            success = move_pct < 0
        else:
            success = abs(move_pct) > 1.0  # neutral: just moved significantly

        signal["eod_price"] = eod_price
        signal["move_pct"] = move_pct
        signal["success"] = success
        signal["updated_at"] = datetime.now(timezone.utc).isoformat()

        log.info(f"  {symbol}: entry={entry_price} eod={eod_price} move={move_pct}% success={success}")
        updated += 1

    file_path.write_text(json.dumps(signals, indent=2))
    log.info(f"Updated {updated} signals in {file_path.name}")


def run(market: str):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    if market == "crypto":
        data_dir = BASE_DIR / "data" / "crypto"
    else:
        data_dir = BASE_DIR / "data" / "stocks"

    file_path = data_dir / f"{today}.json"

    if not file_path.exists():
        log.warning(f"No signal file found for today: {file_path}")
        return

    log.info(f"Running EOD update for {market} — {today}")
    update_file(file_path, market)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--market", choices=["crypto", "stocks"], required=True, help="Which market to update")
    args = parser.parse_args()
    run(args.market)
