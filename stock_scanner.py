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
    """Grab api_key + access_token — tries local paths first, then OCI-2 via SSH."""
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

    # Fall back to OCI-2 over SSH
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

def kite_get(endpoint: str, api_key: str, token: str, params: dict = None) -> dict | list:
    headers = {
        "X-Kite-Version": "3",
        "Authorization": f"token {api_key}:{token}",
    }
    r = requests.get(f"{KITE_BASE}{endpoint}", headers=headers, params=params, timeout=10)
    r.raise_for_status()
    return r.json()["data"]


def fetch_fno_instruments(api_key: str, token: str) -> list[dict]:
    """Fetch full NFO instruments list — contains all F&O contracts."""
    try:
        headers = {
            "X-Kite-Version": "3",
            "Authorization": f"token {api_key}:{token}",
        }
        r = requests.get(f"{KITE_BASE}/instruments/NFO", headers=headers, timeout=30)
        r.raise_for_status()
        # Returns CSV
        lines = r.text.strip().splitlines()
        keys = lines[0].split(",")
        instruments = []
        for line in lines[1:]:
            values = line.split(",")
            if len(values) == len(keys):
                instruments.append(dict(zip(keys, values)))
        return instruments
    except Exception as e:
        log.error(f"Instruments fetch failed: {e}")
        return []


def fetch_quotes(api_key: str, token: str, instrument_tokens: list[str]) -> dict:
    """Fetch quotes for a list of instruments (max 500 per call)."""
    try:
        result = {}
        # Kite allows up to 500 symbols per quote call
        for i in range(0, len(instrument_tokens), 500):
            batch = instrument_tokens[i:i+500]
            data = kite_get("/quote", api_key, token, params={"i": batch})
            result.update(data)
            time.sleep(0.3)
        return result
    except Exception as e:
        log.error(f"Quote fetch failed: {e}")
        return {}


def get_fno_stock_universe(instruments: list[dict]) -> list[str]:
    """Extract unique underlying stock symbols from NFO instruments."""
    symbols = set()
    for inst in instruments:
        if inst.get("instrument_type") == "FUT" and inst.get("segment") == "NFO-FUT":
            symbols.add(inst.get("name", ""))
    return sorted(symbols - {""})


# ── Filter logic ──────────────────────────────────────────────────────────────
INDICES = {
    "NIFTY 50":    "NSE:NIFTY 50",
    "NIFTY BANK":  "NSE:NIFTY BANK",
    "NIFTY FIN SERVICE": "NSE:NIFTY FIN SERVICE",
}

def apply_stock_filters(symbol: str, price: float, price_chg_pct: float,
                         oi_chg_pct: float | None, volume: float,
                         avg_volume: float | None, high_52w: float,
                         low_52w: float, filters_cfg: dict) -> list[str]:
    triggered = []
    cfg = filters_cfg["stocks"]

    if oi_chg_pct is not None:
        if oi_chg_pct >= cfg["OI_BUILDUP_BULLISH"]["oi_threshold"] and price_chg_pct >= cfg["OI_BUILDUP_BULLISH"]["price_threshold"]:
            triggered.append("OI_BUILDUP_BULLISH")
        if oi_chg_pct >= cfg["OI_BUILDUP_BEARISH"]["oi_threshold"] and price_chg_pct <= cfg["OI_BUILDUP_BEARISH"]["price_threshold"]:
            triggered.append("OI_BUILDUP_BEARISH")

    if avg_volume and avg_volume > 0:
        if (volume / avg_volume) >= cfg["VOLUME_SURGE"]["threshold"]:
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
    if long_count > short_count:
        return "long"
    if short_count > long_count:
        return "short"
    return "neutral"


# ── Main ──────────────────────────────────────────────────────────────────────
def run():
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out_path = DATA_DIR / f"{today}.json"

    if out_path.exists():
        log.info(f"Today's file already exists: {out_path}. Skipping.")
        return

    # Get Kite token
    api_key, token = _get_kite_token()
    if not token:
        log.error("Could not get Kite token. Aborting.")
        return
    log.info("Kite token acquired.")

    filters_cfg = json.loads(CONFIG_PATH.read_text())
    signals = []

    # ── Index quotes ───────────────────────────────────────────────────────────
    log.info("Fetching index quotes...")
    index_quotes = fetch_quotes(api_key, token, list(INDICES.values()))
    for index_name, instrument in INDICES.items():
        q = index_quotes.get(instrument, {})
        if not q:
            log.warning(f"No quote for {index_name}")
            continue
        price     = float(q.get("last_price", 0))
        price_chg = float(q.get("net_change", 0))
        price_chg_pct = (price_chg / (price - price_chg) * 100) if (price - price_chg) > 0 else 0
        ohlc      = q.get("ohlc", {})
        high_52w  = float(q.get("upper_circuit_limit", 0) or 0)
        low_52w   = float(q.get("lower_circuit_limit", 0) or 0)

        triggered = apply_stock_filters(
            index_name, price, price_chg_pct, None,
            0, None, high_52w, low_52w, filters_cfg
        )
        if not triggered:
            log.info(f"  {index_name} — no filters triggered")
            continue

        signal = {
            "date": today, "symbol": index_name, "market": "index",
            "filters": triggered, "bias": derive_bias(triggered),
            "price_at_scan": price, "price_change_pct": round(price_chg_pct, 2),
            "oi_change_pct": None, "pcr": None,
            "volume": None, "high_52w": high_52w, "low_52w": low_52w,
            "eod_price": None, "move_pct": None, "success": None, "updated_at": None,
        }
        signals.append(signal)
        log.info(f"  {index_name} → {triggered}")

    # ── F&O stock universe ─────────────────────────────────────────────────────
    log.info("Fetching NFO instruments...")
    instruments = fetch_fno_instruments(api_key, token)
    fno_symbols = get_fno_stock_universe(instruments)
    log.info(f"  {len(fno_symbols)} F&O stocks found")

    # Build NSE instrument keys for quotes
    nse_keys = [f"NSE:{sym}" for sym in fno_symbols]

    log.info("Fetching stock quotes...")
    quotes = fetch_quotes(api_key, token, nse_keys)

    for sym in fno_symbols:
        key = f"NSE:{sym}"
        q = quotes.get(key)
        if not q:
            continue
        try:
            price         = float(q.get("last_price", 0))
            net_change    = float(q.get("net_change", 0))
            price_chg_pct = (net_change / (price - net_change) * 100) if (price - net_change) > 0 else 0
            volume        = float(q.get("volume", 0))
            ohlc          = q.get("ohlc", {})
            high_52w      = float(q.get("upper_circuit_limit", 0) or 0)
            low_52w       = float(q.get("lower_circuit_limit", 0) or 0)

            # OI from futures — find nearest expiry future instrument token
            fut = next(
                (i for i in instruments
                 if i.get("name") == sym and i.get("instrument_type") == "FUT"),
                None
            )
            oi_chg_pct = None
            if fut:
                fut_key = f"NFO:{fut['tradingsymbol']}"
                fut_quotes = fetch_quotes(api_key, token, [fut_key])
                fq = fut_quotes.get(fut_key, {})
                if fq:
                    oi_now  = float(fq.get("oi", 0))
                    oi_day  = float(fq.get("oi_day_high", 0))  # proxy for prev OI
                    if oi_day > 0:
                        oi_chg_pct = round(((oi_now - oi_day) / oi_day) * 100, 2)
                time.sleep(0.1)

            triggered = apply_stock_filters(
                sym, price, price_chg_pct, oi_chg_pct,
                volume, None, high_52w, low_52w, filters_cfg
            )
            if not triggered:
                continue

            signal = {
                "date": today, "symbol": sym, "market": "stock",
                "filters": triggered, "bias": derive_bias(triggered),
                "price_at_scan": price, "price_change_pct": round(price_chg_pct, 2),
                "oi_change_pct": oi_chg_pct, "pcr": None,
                "volume": volume, "high_52w": high_52w, "low_52w": low_52w,
                "eod_price": None, "move_pct": None, "success": None, "updated_at": None,
            }
            signals.append(signal)
            log.info(f"  {sym} → {triggered} | bias: {derive_bias(triggered)}")

        except Exception as e:
            log.warning(f"Error processing {sym}: {e}")
            continue

    out_path.write_text(json.dumps(signals, indent=2))
    log.info(f"Saved {len(signals)} stock signals to {out_path}")


if __name__ == "__main__":
    run()
