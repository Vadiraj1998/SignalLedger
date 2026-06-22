"""
stock_scanner.py
Morning scan — NSE F&O universe + NIFTY / BankNifty / FinNifty
Runs weekdays at 6:00 AM IST (UTC 00:30) via cron
Uses Kite Connect API — token fetched same way as analytics.py
"""

import json
import os
import subprocess
import time
import logging
from datetime import datetime, timedelta, timezone
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

# ── Load dashboard .env for OCI2 SSH config ───────────────────────────────────
def _load_dashboard_env():
    env_path = Path("/home/ubuntu/central_trading_dashboard/.env")
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())

_load_dashboard_env()

# ── Kite token — same logic as analytics.py ───────────────────────────────────
def _parse_env_text(text: str) -> dict:
    result = {}
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            result[k.strip()] = v.strip()
    return result


def _get_kite_token() -> tuple[str, str]:
    local_candidates = [
        Path(os.getenv("SWING_ENV", "/home/ubuntu/zerodha-swing-bot/.env")),
        Path(os.getenv("ALGO_ENV",  "/home/ubuntu/algo/.env")),
    ]
    for env_path in local_candidates:
        if not env_path.exists():
            continue
        cfg    = _parse_env_text(env_path.read_text())
        token  = cfg.get("KITE_ACCESS_TOKEN", "")
        apikey = cfg.get("KITE_API_KEY", "kitefront")
        if token:
            log.info(f"Kite token found at {env_path}")
            return apikey, token

    host   = os.getenv("OCI2_HOST")
    user   = os.getenv("OCI2_USER", "ubuntu")
    key    = os.getenv("OCI2_SSH_KEY", "/home/ubuntu/.ssh/id_rsa")
    remote = os.getenv("OCI2_SWING_ENV", "/home/ubuntu/zerodha-swing-bot/.env")
    if host:
        try:
            result = subprocess.run(
                ["ssh", "-i", key, "-o", "StrictHostKeyChecking=no",
                 "-o", "ConnectTimeout=5", f"{user}@{host}", f"cat {remote}"],
                capture_output=True, text=True, timeout=8
            )
            if result.returncode == 0:
                cfg    = _parse_env_text(result.stdout)
                token  = cfg.get("KITE_ACCESS_TOKEN", "")
                apikey = cfg.get("KITE_API_KEY", "kitefront")
                if token:
                    log.info("Kite token fetched via SSH from OCI-2")
                    return apikey, token
        except Exception as e:
            log.warning(f"SSH token fetch failed: {e}")
    return "", ""


# ── Kite API helpers ──────────────────────────────────────────────────────────
KITE_BASE = "https://api.kite.trade"

def kite_headers(api_key: str, token: str) -> dict:
    return {
        "X-Kite-Version": "3",
        "Authorization": f"token {api_key}:{token}",
    }


def fetch_nse_instruments(api_key: str, token: str) -> dict[str, str]:
    """Fetch NSE EQ instruments — returns {tradingsymbol: instrument_token}."""
    try:
        r = requests.get(
            f"{KITE_BASE}/instruments/NSE",
            headers=kite_headers(api_key, token),
            timeout=30
        )
        r.raise_for_status()
        lines = r.text.strip().splitlines()
        keys = lines[0].split(",")
        result = {}
        for line in lines[1:]:
            vals = line.split(",")
            if len(vals) == len(keys):
                row = dict(zip(keys, vals))
                if row.get("instrument_type") == "EQ":
                    result[row["tradingsymbol"]] = row["instrument_token"]
        log.info(f"Loaded {len(result)} NSE EQ instruments")
        return result
    except Exception as e:
        log.error(f"NSE instruments fetch failed: {e}")
        return {}


def fetch_nfo_instruments(api_key: str, token: str) -> list[dict]:
    """Fetch NFO instruments list for F&O universe + futures OI."""
    try:
        r = requests.get(
            f"{KITE_BASE}/instruments/NFO",
            headers=kite_headers(api_key, token),
            timeout=30
        )
        r.raise_for_status()
        lines = r.text.strip().splitlines()
        keys = lines[0].split(",")
        instruments = []
        for line in lines[1:]:
            vals = line.split(",")
            if len(vals) == len(keys):
                instruments.append(dict(zip(keys, vals)))
        return instruments
    except Exception as e:
        log.error(f"NFO instruments fetch failed: {e}")
        return []


def get_fno_symbols(nfo_instruments: list[dict]) -> list[str]:
    """Unique underlying stock symbols in F&O."""
    symbols = set()
    for i in nfo_instruments:
        if i.get("instrument_type") == "FUT" and i.get("segment") == "NFO-FUT":
            symbols.add(i.get("name", ""))
    return sorted(symbols - {""})


def fetch_quotes(api_key: str, token: str, instrument_keys: list[str]) -> dict:
    """Fetch quotes in batches of 500."""
    result = {}
    for i in range(0, len(instrument_keys), 500):
        batch = instrument_keys[i:i+500]
        try:
            r = requests.get(
                f"{KITE_BASE}/quote",
                headers=kite_headers(api_key, token),
                params={"i": batch},
                timeout=15
            )
            r.raise_for_status()
            result.update(r.json().get("data", {}))
            time.sleep(0.3)
        except Exception as e:
            log.warning(f"Quote batch failed: {e}")
    return result


def fetch_52w(api_key: str, token: str, instrument_token: str) -> tuple[float, float]:
    """Fetch real 52-week high and low via daily historical data."""
    try:
        to_date   = datetime.now().strftime("%Y-%m-%d")
        from_date = (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d")
        r = requests.get(
            f"{KITE_BASE}/instruments/historical/{instrument_token}/day",
            headers=kite_headers(api_key, token),
            params={"from": from_date, "to": to_date},
            timeout=15
        )
        r.raise_for_status()
        candles = r.json()["data"]["candles"]
        if not candles:
            return 0.0, 0.0
        high_52w = max(c[2] for c in candles)
        low_52w  = min(c[3] for c in candles)
        return float(high_52w), float(low_52w)
    except Exception as e:
        log.warning(f"52w fetch failed for token {instrument_token}: {e}")
        return 0.0, 0.0


# ── Filter logic ──────────────────────────────────────────────────────────────
INDICES = {
    "NIFTY 50":          "NSE:NIFTY 50",
    "NIFTY BANK":        "NSE:NIFTY BANK",
    "NIFTY FIN SERVICE": "NSE:NIFTY FIN SERVICE",
}

def apply_stock_filters(price: float, price_chg_pct: float, oi_chg_pct: float | None,
                         volume: float, avg_volume: float | None,
                         high_52w: float, low_52w: float,
                         filters_cfg: dict) -> list[str]:
    triggered = []
    cfg = filters_cfg["stocks"]

    if oi_chg_pct is not None:
        if oi_chg_pct >= cfg["OI_BUILDUP_BULLISH"]["oi_threshold"] and price_chg_pct >= cfg["OI_BUILDUP_BULLISH"]["price_threshold"]:
            triggered.append("OI_BUILDUP_BULLISH")
        if oi_chg_pct >= cfg["OI_BUILDUP_BEARISH"]["oi_threshold"] and price_chg_pct <= cfg["OI_BUILDUP_BEARISH"]["price_threshold"]:
            triggered.append("OI_BUILDUP_BEARISH")

    if avg_volume and avg_volume > 0 and (volume / avg_volume) >= cfg["VOLUME_SURGE"]["threshold"]:
        triggered.append("VOLUME_SURGE")

    if high_52w > 0 and price > 0:
        pct_from_high = ((high_52w - price) / high_52w) * 100
        if pct_from_high <= cfg["NEAR_52W_HIGH"]["threshold"]:
            triggered.append("NEAR_52W_HIGH")

    if low_52w > 0 and price > 0:
        pct_from_low = ((price - low_52w) / low_52w) * 100
        if pct_from_low <= cfg["NEAR_52W_LOW"]["threshold"]:
            triggered.append("NEAR_52W_LOW")

    return triggered


def derive_bias(filters: list[str]) -> str:
    long_filters  = {"OI_BUILDUP_BULLISH", "NEAR_52W_HIGH", "PCR_EXTREME_BULLISH"}
    short_filters = {"OI_BUILDUP_BEARISH", "NEAR_52W_LOW",  "PCR_EXTREME_BEARISH"}
    long_count  = sum(1 for f in filters if f in long_filters)
    short_count = sum(1 for f in filters if f in short_filters)
    if long_count > short_count:   return "long"
    if short_count > long_count:   return "short"
    return "neutral"


# ── Main ──────────────────────────────────────────────────────────────────────
def run():
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out_path = DATA_DIR / f"{today}.json"

    if out_path.exists():
        log.info(f"Today's file already exists: {out_path}. Skipping.")
        return

    api_key, token = _get_kite_token()
    if not token:
        log.error("Could not get Kite token. Aborting.")
        return
    log.info("Kite token acquired.")

    filters_cfg = json.loads(CONFIG_PATH.read_text())
    signals = []

    # ── Load instruments ───────────────────────────────────────────────────────
    log.info("Loading instruments...")
    nse_instruments = fetch_nse_instruments(api_key, token)   # {symbol: token}
    nfo_instruments = fetch_nfo_instruments(api_key, token)   # list of dicts
    fno_symbols     = get_fno_symbols(nfo_instruments)
    log.info(f"F&O universe: {len(fno_symbols)} stocks")

    # Nearest expiry futures map: {symbol: tradingsymbol}
    nearest_fut = {}
    for inst in sorted(nfo_instruments, key=lambda x: x.get("expiry", "")):
        if inst.get("instrument_type") == "FUT" and inst.get("segment") == "NFO-FUT":
            sym = inst.get("name", "")
            if sym and sym not in nearest_fut:
                nearest_fut[sym] = inst["tradingsymbol"]

    # ── Index quotes ───────────────────────────────────────────────────────────
    log.info("Fetching index quotes...")
    index_quotes = fetch_quotes(api_key, token, list(INDICES.values()))

    for index_name, instrument_key in INDICES.items():
        q = index_quotes.get(instrument_key, {})
        if not q:
            log.warning(f"No quote for {index_name}")
            continue

        price      = float(q.get("last_price", 0))
        close_prev = float(q.get("ohlc", {}).get("close", 0))
        price_chg_pct = round(((price - close_prev) / close_prev * 100), 2) if close_prev > 0 else 0.0

        # 52w for indices via instrument token
        inst_token = nse_instruments.get(index_name.replace(" ", "_"), "")
        high_52w, low_52w = 0.0, 0.0
        # Skip 52w for indices — circuit limits not meaningful, skip for now

        triggered = apply_stock_filters(
            price, price_chg_pct, None, 0, None, high_52w, low_52w, filters_cfg
        )
        if not triggered:
            log.info(f"  {index_name} — no filters triggered (chg: {price_chg_pct}%)")
            continue

        signals.append({
            "date": today, "symbol": index_name, "market": "index",
            "filters": triggered, "bias": derive_bias(triggered),
            "price_at_scan": price, "price_change_pct": price_chg_pct,
            "oi_change_pct": None, "pcr": None,
            "volume": None, "high_52w": None, "low_52w": None,
            "eod_price": None, "move_pct": None, "success": None, "updated_at": None,
        })
        log.info(f"  {index_name} → {triggered}")

    # ── F&O stocks ─────────────────────────────────────────────────────────────
    log.info("Fetching stock quotes...")
    nse_keys  = [f"NSE:{sym}" for sym in fno_symbols if sym in nse_instruments]
    all_quotes = fetch_quotes(api_key, token, nse_keys)

    # Futures quotes for OI
    fut_keys = [f"NFO:{nearest_fut[sym]}" for sym in fno_symbols if sym in nearest_fut]
    fut_quotes = fetch_quotes(api_key, token, fut_keys)

    log.info(f"Fetching 52w high/low for {len(fno_symbols)} stocks (this takes ~3-4 mins)...")
    processed = 0
    for sym in fno_symbols:
        nse_key = f"NSE:{sym}"
        q = all_quotes.get(nse_key)
        if not q:
            continue

        try:
            price      = float(q.get("last_price", 0))
            close_prev = float(q.get("ohlc", {}).get("close", 0))
            price_chg_pct = round(((price - close_prev) / close_prev * 100), 2) if close_prev > 0 else 0.0
            volume     = float(q.get("volume", 0))

            # OI from nearest future
            oi_chg_pct = None
            fut_sym = nearest_fut.get(sym)
            if fut_sym:
                fq = fut_quotes.get(f"NFO:{fut_sym}", {})
                if fq:
                    oi_now     = float(fq.get("oi", 0))
                    oi_day_low = float(fq.get("oi_day_low", 0))
                    if oi_day_low > 0:
                        oi_chg_pct = round(((oi_now - oi_day_low) / oi_day_low) * 100, 2)

            # Real 52w high/low from historical data
            inst_token = nse_instruments.get(sym, "")
            high_52w, low_52w = 0.0, 0.0
            if inst_token:
                high_52w, low_52w = fetch_52w(api_key, token, inst_token)
                time.sleep(0.35)  # ~3 req/sec to stay within Kite limits

            triggered = apply_stock_filters(
                price, price_chg_pct, oi_chg_pct,
                volume, None, high_52w, low_52w, filters_cfg
            )

            processed += 1
            if processed % 20 == 0:
                log.info(f"  Progress: {processed}/{len(fno_symbols)} stocks processed...")

            if not triggered:
                continue

            signals.append({
                "date": today, "symbol": sym, "market": "stock",
                "filters": triggered, "bias": derive_bias(triggered),
                "price_at_scan": price, "price_change_pct": price_chg_pct,
                "oi_change_pct": oi_chg_pct, "pcr": None,
                "volume": volume, "high_52w": high_52w, "low_52w": low_52w,
                "eod_price": None, "move_pct": None, "success": None, "updated_at": None,
            })
            log.info(f"  ✓ {sym} → {triggered} | price_chg={price_chg_pct}% oi_chg={oi_chg_pct}% 52wH={high_52w} 52wL={low_52w}")

        except Exception as e:
            log.warning(f"Error processing {sym}: {e}")
            continue

    out_path.write_text(json.dumps(signals, indent=2))
    log.info(f"Done. Saved {len(signals)} signals to {out_path}")


if __name__ == "__main__":
    run()
