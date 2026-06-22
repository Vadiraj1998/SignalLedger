"""
stock_scanner.py
Morning scan — NSE F&O universe + NIFTY / BankNifty / FinNifty
Runs weekdays at 6:00 AM IST via cron
Uses NSE public endpoints (no auth required)
"""

import json
import time
import logging
from datetime import datetime, timezone
from pathlib import Path
import requests

# ── Paths ─────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = BASE_DIR / "config" / "filters.json"
DATA_DIR = BASE_DIR / "data" / "stocks"
DATA_DIR.mkdir(parents=True, exist_ok=True)

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(BASE_DIR / "scanner" / "stock_scanner.log"),
    ],
)
log = logging.getLogger(__name__)

# ── NSE headers — required to avoid 403 ──────────────────────────────────────
NSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer": "https://www.nseindia.com/",
    "Connection": "keep-alive",
}

NSE_BASE = "https://www.nseindia.com"

# Indices to scan
INDICES = ["NIFTY", "BANKNIFTY", "FINNIFTY"]


def nse_session() -> requests.Session:
    """Create a session with cookies from NSE homepage (required)."""
    session = requests.Session()
    session.headers.update(NSE_HEADERS)
    # Hit homepage first to get cookies
    try:
        session.get(NSE_BASE, timeout=10)
        time.sleep(1)
    except Exception as e:
        log.warning(f"NSE homepage fetch failed: {e}")
    return session


def fetch_fno_universe(session: requests.Session) -> list[dict]:
    """Fetch all F&O stocks with quote data from NSE."""
    try:
        url = f"{NSE_BASE}/api/equity-stockIndices?index=Securities%20in%20F%26O"
        r = session.get(url, timeout=15)
        r.raise_for_status()
        data = r.json()
        return data.get("data", [])
    except Exception as e:
        log.error(f"F&O universe fetch failed: {e}")
        return []


def fetch_index_option_chain(session: requests.Session, symbol: str) -> dict | None:
    """Fetch option chain for NIFTY / BANKNIFTY / FINNIFTY."""
    try:
        url = f"{NSE_BASE}/api/option-chain-indices?symbol={symbol}"
        r = session.get(url, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.error(f"Option chain fetch failed for {symbol}: {e}")
        return None


def compute_pcr(option_chain: dict) -> float | None:
    """Compute Put-Call Ratio from option chain data."""
    try:
        records = option_chain["records"]["data"]
        total_put_oi = sum(
            r["PE"]["openInterest"] for r in records if "PE" in r and r["PE"]
        )
        total_call_oi = sum(
            r["CE"]["openInterest"] for r in records if "CE" in r and r["CE"]
        )
        if total_call_oi > 0:
            return round(total_put_oi / total_call_oi, 3)
    except Exception as e:
        log.warning(f"PCR computation failed: {e}")
    return None


def fetch_stock_oi_data(session: requests.Session, symbol: str) -> dict | None:
    """Fetch OI data for individual F&O stock."""
    try:
        url = f"{NSE_BASE}/api/quote-derivative?symbol={symbol}"
        r = session.get(url, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.warning(f"OI fetch failed for {symbol}: {e}")
    return None


def apply_stock_filters(symbol: str, price: float, price_chg_pct: float, oi_chg_pct: float | None, volume: float, avg_volume: float | None, high_52w: float, low_52w: float, filters_cfg: dict) -> list[str]:
    triggered = []
    cfg = filters_cfg["stocks"]

    # OI buildup filters
    if oi_chg_pct is not None:
        if oi_chg_pct >= cfg["OI_BUILDUP_BULLISH"]["oi_threshold"] and price_chg_pct >= cfg["OI_BUILDUP_BULLISH"]["price_threshold"]:
            triggered.append("OI_BUILDUP_BULLISH")
        if oi_chg_pct >= cfg["OI_BUILDUP_BEARISH"]["oi_threshold"] and price_chg_pct <= cfg["OI_BUILDUP_BEARISH"]["price_threshold"]:
            triggered.append("OI_BUILDUP_BEARISH")

    # Volume surge
    if avg_volume and avg_volume > 0:
        if (volume / avg_volume) >= cfg["VOLUME_SURGE"]["threshold"]:
            triggered.append("VOLUME_SURGE")

    # 52-week proximity
    if high_52w > 0 and price > 0:
        pct_from_high = ((high_52w - price) / high_52w) * 100
        if pct_from_high <= cfg["NEAR_52W_HIGH"]["threshold"]:
            triggered.append("NEAR_52W_HIGH")

    if low_52w > 0 and price > 0:
        pct_from_low = ((price - low_52w) / low_52w) * 100
        if pct_from_low <= cfg["NEAR_52W_LOW"]["threshold"]:
            triggered.append("NEAR_52W_LOW")

    return triggered


def apply_index_filters(symbol: str, pcr: float | None, filters_cfg: dict) -> list[str]:
    triggered = []
    cfg = filters_cfg["stocks"]

    if pcr is not None:
        if pcr >= cfg["PCR_EXTREME_BULLISH"]["threshold"]:
            triggered.append("PCR_EXTREME_BULLISH")
        if pcr <= cfg["PCR_EXTREME_BEARISH"]["threshold"]:
            triggered.append("PCR_EXTREME_BEARISH")

    return triggered


def derive_bias(filters: list[str]) -> str:
    long_filters = {"OI_BUILDUP_BULLISH", "NEAR_52W_HIGH", "PCR_EXTREME_BULLISH"}
    short_filters = {"OI_BUILDUP_BEARISH", "NEAR_52W_LOW", "PCR_EXTREME_BEARISH"}
    long_count = sum(1 for f in filters if f in long_filters)
    short_count = sum(1 for f in filters if f in short_filters)
    if long_count > short_count:
        return "long"
    if short_count > long_count:
        return "short"
    return "neutral"


def run():
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out_path = DATA_DIR / f"{today}.json"

    if out_path.exists():
        log.info(f"Today's file already exists: {out_path}. Skipping.")
        return

    filters_cfg = json.loads(CONFIG_PATH.read_text())
    session = nse_session()
    signals = []

    # ── Index scans (NIFTY, BankNifty, FinNifty) ──────────────────────────────
    log.info("Scanning indices...")
    for index in INDICES:
        log.info(f"  Fetching option chain: {index}")
        chain = fetch_index_option_chain(session, index)
        time.sleep(1)

        if not chain:
            continue

        pcr = compute_pcr(chain)
        triggered = apply_index_filters(index, pcr, filters_cfg)

        if not triggered:
            log.info(f"  {index} — no filters triggered (PCR: {pcr})")
            continue

        # Get underlying price
        try:
            underlying = chain["records"]["underlyingValue"]
        except Exception:
            underlying = None

        signal = {
            "date": today,
            "symbol": index,
            "market": "index",
            "filters": triggered,
            "bias": derive_bias(triggered),
            "price_at_scan": underlying,
            "price_change_pct": None,
            "oi_change_pct": None,
            "pcr": pcr,
            "volume": None,
            "high_52w": None,
            "low_52w": None,
            # EOD fields
            "eod_price": None,
            "move_pct": None,
            "success": None,
            "updated_at": None,
        }
        signals.append(signal)
        log.info(f"  {index} → triggered: {triggered} | PCR: {pcr}")

    # ── F&O stock scans ────────────────────────────────────────────────────────
    log.info("Fetching F&O universe...")
    fno_stocks = fetch_fno_universe(session)
    time.sleep(1)
    log.info(f"  Got {len(fno_stocks)} stocks")

    for stock in fno_stocks:
        try:
            symbol = stock.get("symbol", "")
            price = float(stock.get("lastPrice", 0) or 0)
            price_chg_pct = float(stock.get("pChange", 0) or 0)
            volume = float(stock.get("totalTradedVolume", 0) or 0)
            high_52w = float(stock.get("yearHigh", 0) or 0)
            low_52w = float(stock.get("yearLow", 0) or 0)

            # OI data needs separate call — do for all but throttle
            oi_chg_pct = None
            oi_data = fetch_stock_oi_data(session, symbol)
            if oi_data:
                try:
                    # Look for futures OI change
                    fut = next(
                        (x for x in oi_data.get("stocks", []) if x.get("metadata", {}).get("instrumentType") == "Stock Futures"),
                        None,
                    )
                    if fut:
                        oi_chg_pct = float(fut.get("marketDeptOrderBook", {}).get("tradeInfo", {}).get("changeinOpenInterest", 0) or 0)
                except Exception:
                    pass
            time.sleep(0.3)

            # No avg volume from this endpoint — use volume > 500k as proxy for now
            avg_volume = None

            triggered = apply_stock_filters(
                symbol, price, price_chg_pct, oi_chg_pct,
                volume, avg_volume, high_52w, low_52w, filters_cfg
            )

            if not triggered:
                continue

            signal = {
                "date": today,
                "symbol": symbol,
                "market": "stock",
                "filters": triggered,
                "bias": derive_bias(triggered),
                "price_at_scan": price,
                "price_change_pct": price_chg_pct,
                "oi_change_pct": oi_chg_pct,
                "pcr": None,
                "volume": volume,
                "high_52w": high_52w,
                "low_52w": low_52w,
                # EOD fields
                "eod_price": None,
                "move_pct": None,
                "success": None,
                "updated_at": None,
            }
            signals.append(signal)
            log.info(f"  {symbol} → {triggered} | bias: {derive_bias(triggered)}")

        except Exception as e:
            log.warning(f"Error processing {stock.get('symbol', '?')}: {e}")
            continue

    out_path.write_text(json.dumps(signals, indent=2))
    log.info(f"Saved {len(signals)} stock signals to {out_path}")


if __name__ == "__main__":
    run()
