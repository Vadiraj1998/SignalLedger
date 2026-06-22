"""
crypto_scanner.py
Morning scan — Binance USDT perpetual futures
Runs daily at 5:30 AM IST via cron
"""

import json
import os
import time
import logging
from datetime import datetime, timezone
from pathlib import Path
import requests

# ── Paths ─────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = BASE_DIR / "config" / "filters.json"
DATA_DIR = BASE_DIR / "data" / "crypto"
DATA_DIR.mkdir(parents=True, exist_ok=True)

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(BASE_DIR / "scanner" / "crypto_scanner.log"),
    ],
)
log = logging.getLogger(__name__)

# ── Binance endpoints (public, no auth needed) ────────────────────────────────
BINANCE_BASE = "https://fapi.binance.com"

# Top coins to scan — extend as needed
TOP_SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT",
    "DOGEUSDT", "ADAUSDT", "AVAXUSDT", "LINKUSDT", "DOTUSDT",
    "MATICUSDT", "LTCUSDT", "NEARUSDT", "ATOMUSDT", "UNIUSDT",
    "AAVEUSDT", "FTMUSDT", "SANDUSDT", "MANAUSDT", "INJUSDT",
    "SUIUSDT", "ARBUSDT", "OPUSDT", "APTUSDT", "SEIUSDT",
]


def binance_get(endpoint: str, params: dict = None) -> dict | list:
    url = f"{BINANCE_BASE}{endpoint}"
    r = requests.get(url, params=params, timeout=10)
    r.raise_for_status()
    return r.json()


def fetch_funding_rates() -> dict:
    """Latest funding rate per symbol."""
    data = binance_get("/fapi/v1/premiumIndex")
    return {
        item["symbol"]: float(item["lastFundingRate"])
        for item in data
        if item["symbol"] in TOP_SYMBOLS
    }


def fetch_ticker_24h() -> dict:
    """24h price change, volume per symbol."""
    data = binance_get("/fapi/v1/ticker/24hr")
    result = {}
    for item in data:
        if item["symbol"] in TOP_SYMBOLS:
            result[item["symbol"]] = {
                "price": float(item["lastPrice"]),
                "price_change_pct": float(item["priceChangePercent"]),
                "volume_usdt": float(item["quoteVolume"]),
                "high_24h": float(item["highPrice"]),
                "low_24h": float(item["lowPrice"]),
            }
    return result


def fetch_open_interest() -> dict:
    """Current OI per symbol."""
    oi = {}
    for symbol in TOP_SYMBOLS:
        try:
            data = binance_get("/fapi/v1/openInterest", {"symbol": symbol})
            oi[symbol] = float(data["openInterest"]) * float(data.get("openInterestValue", 1) or 1)
            # Use OI in contracts; value needs price multiply later
            oi[symbol] = float(data["openInterest"])
            time.sleep(0.05)  # gentle rate limit
        except Exception as e:
            log.warning(f"OI fetch failed for {symbol}: {e}")
    return oi


def fetch_oi_history(symbol: str) -> float | None:
    """OI 24h ago via histOpenInterest endpoint (5m candles, go back 288 periods)."""
    try:
        data = binance_get(
            "/futures/data/openInterestHist",
            {"symbol": symbol, "period": "1h", "limit": 25},
        )
        if len(data) >= 2:
            oldest = float(data[0]["sumOpenInterest"])
            latest = float(data[-1]["sumOpenInterest"])
            if oldest > 0:
                return ((latest - oldest) / oldest) * 100
    except Exception as e:
        log.warning(f"OI history failed for {symbol}: {e}")
    return None


def fetch_volume_avg(symbol: str, days: int = 7) -> float | None:
    """Average daily volume over last N days."""
    try:
        data = binance_get(
            "/fapi/v1/klines",
            {"symbol": symbol, "interval": "1d", "limit": days + 1},
        )
        if len(data) >= days:
            volumes = [float(k[7]) for k in data[:-1]]  # quoteAssetVolume
            return sum(volumes) / len(volumes)
    except Exception as e:
        log.warning(f"Volume avg failed for {symbol}: {e}")
    return None


def apply_filters(symbol: str, funding: float, ticker: dict, oi_change_pct: float | None, vol_avg: float | None, filters_cfg: dict) -> list[str]:
    """Return list of filter names triggered for this symbol."""
    triggered = []
    cfg = filters_cfg["crypto"]
    price_chg = ticker["price_change_pct"]
    vol_24h = ticker["volume_usdt"]

    # Funding rate filters
    if funding <= cfg["FUNDING_EXTREME_LONG"]["threshold"]:
        triggered.append("FUNDING_EXTREME_LONG")
    if funding >= cfg["FUNDING_EXTREME_SHORT"]["threshold"]:
        triggered.append("FUNDING_EXTREME_SHORT")

    # OI surge
    if oi_change_pct is not None and oi_change_pct >= cfg["OI_SURGE"]["threshold"]:
        triggered.append("OI_SURGE")

    # Volume spike
    if vol_avg and vol_avg > 0:
        vol_ratio = vol_24h / vol_avg
        if vol_ratio >= cfg["VOLUME_SPIKE"]["threshold"]:
            triggered.append("VOLUME_SPIKE")

    # Momentum + OI confirmation
    if oi_change_pct is not None:
        if price_chg >= cfg["MOMENTUM_OI_CONFIRM"]["price_threshold"] and oi_change_pct >= cfg["MOMENTUM_OI_CONFIRM"]["oi_threshold"]:
            triggered.append("MOMENTUM_OI_CONFIRM")
        if price_chg <= cfg["DUMP_OI_CONFIRM"]["price_threshold"] and oi_change_pct >= cfg["DUMP_OI_CONFIRM"]["oi_threshold"]:
            triggered.append("DUMP_OI_CONFIRM")

    return triggered


def derive_bias(filters: list[str], filters_cfg: dict) -> str:
    cfg = filters_cfg["crypto"]
    biases = [cfg[f]["bias"] for f in filters if f in cfg]
    long_count = biases.count("long")
    short_count = biases.count("short")
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

    log.info("Fetching Binance data...")
    filters_cfg = json.loads(CONFIG_PATH.read_text())

    funding_rates = fetch_funding_rates()
    tickers = fetch_ticker_24h()

    signals = []

    for symbol in TOP_SYMBOLS:
        if symbol not in tickers:
            log.warning(f"No ticker data for {symbol}, skipping.")
            continue

        ticker = tickers[symbol]
        funding = funding_rates.get(symbol, 0.0)

        log.info(f"Processing {symbol}...")
        oi_change_pct = fetch_oi_history(symbol)
        vol_avg = fetch_volume_avg(symbol)
        time.sleep(0.1)

        triggered = apply_filters(symbol, funding, ticker, oi_change_pct, vol_avg, filters_cfg)

        if not triggered:
            continue  # only log symbols with at least one filter hit

        bias = derive_bias(triggered, filters_cfg)

        signal = {
            "date": today,
            "symbol": symbol,
            "market": "crypto",
            "filters": triggered,
            "bias": bias,
            "price_at_scan": ticker["price"],
            "price_change_pct_24h": ticker["price_change_pct"],
            "funding_rate": funding,
            "oi_change_pct_24h": oi_change_pct,
            "volume_usdt_24h": ticker["volume_usdt"],
            "volume_avg_7d": vol_avg,
            # EOD fields — filled by eod_updater.py
            "eod_price": None,
            "move_pct": None,
            "success": None,
            "updated_at": None,
        }
        signals.append(signal)
        log.info(f"  → {symbol} triggered: {triggered} | bias: {bias}")

    out_path.write_text(json.dumps(signals, indent=2))
    log.info(f"Saved {len(signals)} signals to {out_path}")


if __name__ == "__main__":
    run()
