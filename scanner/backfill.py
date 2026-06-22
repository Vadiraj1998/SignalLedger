"""
backfill.py
Backfills historical signals for the last 30 days.
Crypto: full filters (funding, OI, volume, momentum)
Stocks: price/volume/52w filters only (no OI history from Kite)

Usage:
  python scanner/backfill.py --market crypto
  python scanner/backfill.py --market stocks
  python scanner/backfill.py --market both
"""

import json
import os
import subprocess
import time
import logging
import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path
import requests

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = BASE_DIR / "config" / "filters.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger(__name__)

BINANCE_BASE = "https://fapi.binance.com"
KITE_BASE    = "https://api.kite.trade"

TOP_SYMBOLS = [
    "BTCUSDT","ETHUSDT","BNBUSDT","SOLUSDT","XRPUSDT",
    "DOGEUSDT","ADAUSDT","AVAXUSDT","LINKUSDT","DOTUSDT",
    "WIFUSDT","LTCUSDT","NEARUSDT","ATOMUSDT","UNIUSDT",
    "AAVEUSDT","JUPUSDT","SANDUSDT","MANAUSDT","INJUSDT",
    "SUIUSDT","ARBUSDT","OPUSDT","APTUSDT","SEIUSDT",
]

# ── Env + Kite token ──────────────────────────────────────────────────────────
def _load_dashboard_env():
    env_path = Path("/home/ubuntu/central_trading_dashboard/.env")
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())

_load_dashboard_env()

def _parse_env(text):
    r = {}
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            r[k.strip()] = v.strip()
    return r

def _get_kite_token():
    for p in [Path(os.getenv("SWING_ENV","/home/ubuntu/zerodha-swing-bot/.env")),
              Path(os.getenv("ALGO_ENV","/home/ubuntu/algo/.env"))]:
        if p.exists():
            cfg = _parse_env(p.read_text())
            if cfg.get("KITE_ACCESS_TOKEN"):
                return cfg.get("KITE_API_KEY","kitefront"), cfg["KITE_ACCESS_TOKEN"]
    host = os.getenv("OCI2_HOST")
    if host:
        try:
            res = subprocess.run(
                ["ssh","-i",os.getenv("OCI2_SSH_KEY","/home/ubuntu/.ssh/id_rsa"),
                 "-o","StrictHostKeyChecking=no","-o","ConnectTimeout=5",
                 f"{os.getenv('OCI2_USER','ubuntu')}@{host}",
                 f"cat {os.getenv('OCI2_SWING_ENV','/home/ubuntu/zerodha-swing-bot/.env')}"],
                capture_output=True, text=True, timeout=8)
            if res.returncode == 0:
                cfg = _parse_env(res.stdout)
                if cfg.get("KITE_ACCESS_TOKEN"):
                    return cfg.get("KITE_API_KEY","kitefront"), cfg["KITE_ACCESS_TOKEN"]
        except Exception as e:
            log.warning(f"SSH token fetch failed: {e}")
    return "", ""

def kite_hdrs(api_key, token):
    return {"X-Kite-Version":"3","Authorization":f"token {api_key}:{token}"}

# ── Helpers ───────────────────────────────────────────────────────────────────
def date_range(days=30):
    """Returns list of date strings for last N days, oldest first, skipping weekends for stocks."""
    today = datetime.now(timezone.utc).date()
    return [(today - timedelta(days=i)).isoformat() for i in range(days, 0, -1)]

def derive_bias_crypto(filters, cfg):
    biases = [cfg["crypto"][f]["bias"] for f in filters if f in cfg["crypto"]]
    lc, sc = biases.count("long"), biases.count("short")
    return "long" if lc > sc else "short" if sc > lc else "neutral"

def derive_bias_stock(filters):
    long_f  = {"OI_BUILDUP_BULLISH","NEAR_52W_HIGH","PCR_EXTREME_BULLISH"}
    short_f = {"OI_BUILDUP_BEARISH","NEAR_52W_LOW","PCR_EXTREME_BEARISH"}
    lc = sum(1 for f in filters if f in long_f)
    sc = sum(1 for f in filters if f in short_f)
    return "long" if lc > sc else "short" if sc > lc else "neutral"

# ── CRYPTO BACKFILL ───────────────────────────────────────────────────────────

def fetch_funding_history(symbol, start_ts, end_ts):
    """Funding rate at a specific time (closest available)."""
    try:
        r = requests.get(f"{BINANCE_BASE}/fapi/v1/fundingRate",
            params={"symbol":symbol,"startTime":start_ts,"endTime":end_ts,"limit":1}, timeout=10)
        r.raise_for_status()
        data = r.json()
        return float(data[0]["fundingRate"]) * 100 if data else 0.0
    except:
        return 0.0

def fetch_klines_day(symbol, date_str):
    """OHLCV for a specific day."""
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        start = int(dt.timestamp() * 1000)
        end   = int((dt + timedelta(days=1)).timestamp() * 1000) - 1
        r = requests.get(f"{BINANCE_BASE}/fapi/v1/klines",
            params={"symbol":symbol,"interval":"1d","startTime":start,"endTime":end,"limit":1}, timeout=10)
        r.raise_for_status()
        data = r.json()
        if data:
            return {
                "open": float(data[0][1]),
                "high": float(data[0][2]),
                "low":  float(data[0][3]),
                "close":float(data[0][4]),
                "volume": float(data[0][7]),  # quote volume
            }
    except:
        pass
    return None

def fetch_oi_change_history(symbol, date_str):
    """OI change % for a specific date using hourly OI history."""
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        start = int(dt.timestamp() * 1000)
        end   = int((dt + timedelta(days=1)).timestamp() * 1000)
        r = requests.get(f"{BINANCE_BASE}/futures/data/openInterestHist",
            params={"symbol":symbol,"period":"1h","startTime":start,"endTime":end,"limit":24}, timeout=10)
        r.raise_for_status()
        data = r.json()
        if len(data) >= 2:
            oldest = float(data[0]["sumOpenInterest"])
            latest = float(data[-1]["sumOpenInterest"])
            if oldest > 0:
                return round(((latest - oldest) / oldest) * 100, 2)
    except:
        pass
    return None

def fetch_vol_avg_before(symbol, date_str, days=7):
    """Average volume for N days before date."""
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        end   = int(dt.timestamp() * 1000)
        start = int((dt - timedelta(days=days+1)).timestamp() * 1000)
        r = requests.get(f"{BINANCE_BASE}/fapi/v1/klines",
            params={"symbol":symbol,"interval":"1d","startTime":start,"endTime":end,"limit":days+1}, timeout=10)
        r.raise_for_status()
        data = r.json()
        if len(data) >= days:
            return sum(float(k[7]) for k in data[:days]) / days
    except:
        pass
    return None

def backfill_crypto(filters_cfg, days=30):
    data_dir = BASE_DIR / "data" / "crypto"
    data_dir.mkdir(parents=True, exist_ok=True)
    dates = date_range(days)
    cfg   = filters_cfg["crypto"]

    for date_str in dates:
        out_path = data_dir / f"{date_str}.json"
        if out_path.exists():
            log.info(f"  {date_str} already exists, skipping.")
            continue

        log.info(f"Processing crypto {date_str}...")
        signals = []

        for symbol in TOP_SYMBOLS:
            try:
                # Day candle
                candle = fetch_klines_day(symbol, date_str)
                if not candle:
                    continue

                price       = candle["close"]
                price_chg   = round(((candle["close"] - candle["open"]) / candle["open"]) * 100, 3)
                vol_24h     = candle["volume"]

                # OI change
                oi_chg = fetch_oi_change_history(symbol, date_str)
                time.sleep(0.05)

                # Volume avg
                vol_avg = fetch_vol_avg_before(symbol, date_str)
                time.sleep(0.05)

                # Funding rate (morning of that day)
                dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                start_ts = int(dt.timestamp() * 1000)
                end_ts   = int((dt + timedelta(hours=8)).timestamp() * 1000)
                funding  = fetch_funding_history(symbol, start_ts, end_ts)
                time.sleep(0.05)

                # Apply filters
                triggered = []
                if funding <= cfg["FUNDING_EXTREME_LONG"]["threshold"]:
                    triggered.append("FUNDING_EXTREME_LONG")
                if funding >= cfg["FUNDING_EXTREME_SHORT"]["threshold"]:
                    triggered.append("FUNDING_EXTREME_SHORT")
                if oi_chg is not None and oi_chg >= cfg["OI_SURGE"]["threshold"]:
                    triggered.append("OI_SURGE")
                if vol_avg and vol_avg > 0 and (vol_24h / vol_avg) >= cfg["VOLUME_SPIKE"]["threshold"]:
                    triggered.append("VOLUME_SPIKE")
                if oi_chg is not None:
                    if price_chg >= cfg["MOMENTUM_OI_CONFIRM"]["price_threshold"] and oi_chg >= cfg["MOMENTUM_OI_CONFIRM"]["oi_threshold"]:
                        triggered.append("MOMENTUM_OI_CONFIRM")
                    if price_chg <= cfg["DUMP_OI_CONFIRM"]["price_threshold"] and oi_chg >= cfg["DUMP_OI_CONFIRM"]["oi_threshold"]:
                        triggered.append("DUMP_OI_CONFIRM")

                if not triggered:
                    continue

                bias = derive_bias_crypto(triggered, filters_cfg)

                # EOD price = close of that day
                eod_price = candle["close"]
                move_pct  = round(((eod_price - candle["open"]) / candle["open"]) * 100, 3)
                success   = move_pct > 0 if bias == "long" else move_pct < 0 if bias == "short" else abs(move_pct) > 1.0

                signals.append({
                    "date": date_str, "symbol": symbol, "market": "crypto",
                    "filters": triggered, "bias": bias,
                    "price_at_scan": candle["open"],
                    "price_change_pct_24h": price_chg,
                    "funding_rate": funding,
                    "oi_change_pct_24h": oi_chg,
                    "volume_usdt_24h": vol_24h,
                    "volume_avg_7d": vol_avg,
                    "eod_price": eod_price,
                    "move_pct": move_pct,
                    "success": success,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                })
                log.info(f"    {symbol} → {triggered} | move={move_pct}% {'✓' if success else '✗'}")

            except Exception as e:
                log.warning(f"    Error {symbol}: {e}")
                continue

        out_path.write_text(json.dumps(signals, indent=2))
        log.info(f"  Saved {len(signals)} crypto signals for {date_str}")
        time.sleep(0.5)

# ── STOCKS BACKFILL ───────────────────────────────────────────────────────────

def fetch_kite_historical(api_key, token, instrument_token, date_str):
    """Fetch OHLCV for a specific date from Kite."""
    try:
        r = requests.get(
            f"{KITE_BASE}/instruments/historical/{instrument_token}/day",
            headers=kite_hdrs(api_key, token),
            params={"from": date_str, "to": date_str},
            timeout=10
        )
        r.raise_for_status()
        candles = r.json()["data"]["candles"]
        if candles:
            c = candles[0]
            return {"open":float(c[1]),"high":float(c[2]),"low":float(c[3]),"close":float(c[4]),"volume":float(c[5])}
    except:
        pass
    return None

def fetch_52w_before(api_key, token, instrument_token, date_str):
    """52w high/low as of a specific date."""
    try:
        to_date   = date_str
        from_date = (datetime.strptime(date_str, "%Y-%m-%d") - timedelta(days=365)).strftime("%Y-%m-%d")
        r = requests.get(
            f"{KITE_BASE}/instruments/historical/{instrument_token}/day",
            headers=kite_hdrs(api_key, token),
            params={"from": from_date, "to": to_date},
            timeout=15
        )
        r.raise_for_status()
        candles = r.json()["data"]["candles"]
        if candles:
            return max(float(c[2]) for c in candles), min(float(c[3]) for c in candles)
    except:
        pass
    return 0.0, 0.0

def fetch_vol_avg_kite(api_key, token, instrument_token, date_str, days=20):
    """Average volume for N days before date."""
    try:
        to_date   = (datetime.strptime(date_str, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
        from_date = (datetime.strptime(date_str, "%Y-%m-%d") - timedelta(days=days+5)).strftime("%Y-%m-%d")
        r = requests.get(
            f"{KITE_BASE}/instruments/historical/{instrument_token}/day",
            headers=kite_hdrs(api_key, token),
            params={"from": from_date, "to": to_date},
            timeout=15
        )
        r.raise_for_status()
        candles = r.json()["data"]["candles"]
        if len(candles) >= days:
            return sum(float(c[5]) for c in candles[-days:]) / days
    except:
        pass
    return None

def backfill_stocks(filters_cfg, days=30):
    data_dir = BASE_DIR / "data" / "stocks"
    data_dir.mkdir(parents=True, exist_ok=True)

    api_key, token = _get_kite_token()
    if not token:
        log.error("No Kite token. Aborting stocks backfill.")
        return

    log.info("Loading NSE instruments...")
    r = requests.get(f"{KITE_BASE}/instruments/NSE", headers=kite_hdrs(api_key, token), timeout=30)
    lines = r.text.strip().splitlines()
    keys  = lines[0].split(",")
    nse_instruments = {}
    for line in lines[1:]:
        vals = line.split(",")
        if len(vals) == len(keys):
            row = dict(zip(keys, vals))
            if row.get("instrument_type") == "EQ":
                nse_instruments[row["tradingsymbol"]] = row["instrument_token"]

    r2 = requests.get(f"{KITE_BASE}/instruments/NFO", headers=kite_hdrs(api_key, token), timeout=30)
    lines2 = r2.text.strip().splitlines()
    keys2  = lines2[0].split(",")
    nfo_instruments = []
    for line in lines2[1:]:
        vals = line.split(",")
        if len(vals) == len(keys2):
            nfo_instruments.append(dict(zip(keys2, vals)))

    fno_symbols = sorted(set(
        i.get("name","").strip('"') for i in nfo_instruments
        if i.get("instrument_type") == "FUT" and i.get("segment") == "NFO-FUT"
    ) - {""})
    log.info(f"F&O universe: {len(fno_symbols)} stocks")

    cfg = filters_cfg["stocks"]
    dates = date_range(days)
    # Only weekdays
    dates = [d for d in dates if datetime.strptime(d, "%Y-%m-%d").weekday() < 5]

    for date_str in dates:
        out_path = data_dir / f"{date_str}.json"
        if out_path.exists():
            log.info(f"  {date_str} already exists, skipping.")
            continue

        log.info(f"Processing stocks {date_str} ({len(fno_symbols)} stocks)...")
        signals  = []
        processed = 0

        for sym in fno_symbols:
            inst_token = nse_instruments.get(sym)
            if not inst_token:
                continue
            try:
                # Day candle
                candle = fetch_kite_historical(api_key, token, inst_token, date_str)
                if not candle or candle["open"] == 0:
                    time.sleep(0.2)
                    continue

                price         = candle["close"]
                price_chg_pct = round(((candle["close"] - candle["open"]) / candle["open"]) * 100, 3)
                volume        = candle["volume"]

                # 52w high/low as of that date
                high_52w, low_52w = fetch_52w_before(api_key, token, inst_token, date_str)
                time.sleep(0.3)

                # Volume avg
                vol_avg = fetch_vol_avg_kite(api_key, token, inst_token, date_str)
                time.sleep(0.2)

                # Apply filters
                triggered = []
                if high_52w > 0:
                    pct_from_high = ((high_52w - price) / high_52w) * 100
                    if pct_from_high <= cfg["NEAR_52W_HIGH"]["threshold"]:
                        triggered.append("NEAR_52W_HIGH")
                if low_52w > 0:
                    pct_from_low = ((price - low_52w) / low_52w) * 100
                    if pct_from_low <= cfg["NEAR_52W_LOW"]["threshold"]:
                        triggered.append("NEAR_52W_LOW")
                if vol_avg and vol_avg > 0 and (volume / vol_avg) >= cfg["VOLUME_SURGE"]["threshold"]:
                    triggered.append("VOLUME_SURGE")

                if not triggered:
                    processed += 1
                    if processed % 30 == 0:
                        log.info(f"    Progress: {processed}/{len(fno_symbols)}")
                    continue

                bias      = derive_bias_stock(triggered)
                eod_price = candle["close"]
                move_pct  = round(((eod_price - candle["open"]) / candle["open"]) * 100, 3)
                success   = move_pct > 0 if bias == "long" else move_pct < 0 if bias == "short" else abs(move_pct) > 1.0

                signals.append({
                    "date": date_str, "symbol": sym, "market": "stock",
                    "filters": triggered, "bias": bias,
                    "price_at_scan": candle["open"],
                    "price_change_pct": price_chg_pct,
                    "oi_change_pct": None,
                    "pcr": None,
                    "volume": volume,
                    "high_52w": high_52w,
                    "low_52w": low_52w,
                    "eod_price": eod_price,
                    "move_pct": move_pct,
                    "success": success,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                })
                log.info(f"    {sym} → {triggered} | move={move_pct}% {'✓' if success else '✗'}")

                processed += 1
                if processed % 30 == 0:
                    log.info(f"    Progress: {processed}/{len(fno_symbols)}")

            except Exception as e:
                log.warning(f"    Error {sym}: {e}")
                time.sleep(0.3)
                continue

        out_path.write_text(json.dumps(signals, indent=2))
        log.info(f"  Saved {len(signals)} stock signals for {date_str}")
        time.sleep(1)

# ── Main ──────────────────────────────────────────────────────────────────────

def run():
    parser = argparse.ArgumentParser()
    parser.add_argument("--market", choices=["crypto","stocks","both"], required=True)
    parser.add_argument("--days", type=int, default=30)
    args = parser.parse_args()

    filters_cfg = json.loads(CONFIG_PATH.read_text())

    if args.market in ("crypto","both"):
        log.info(f"=== CRYPTO BACKFILL ({args.days} days) ===")
        backfill_crypto(filters_cfg, args.days)

    if args.market in ("stocks","both"):
        log.info(f"=== STOCKS BACKFILL ({args.days} days) ===")
        backfill_stocks(filters_cfg, args.days)

    log.info("=== BACKFILL COMPLETE ===")
    log.info("Run: python aggregator/filter_stats.py")

if __name__ == "__main__":
    run()
