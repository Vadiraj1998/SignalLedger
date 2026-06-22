"""
eod_updater.py
EOD scan — patches move_pct, eod_price, success into today's signal files
Run after market close:
  Stocks  → 3:45 PM IST (UTC 10:15) weekdays
  Crypto  → 11:30 PM IST (UTC 18:00) daily
"""

import json
import os
import subprocess
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

BINANCE_BASE = "https://fapi.binance.com"
KITE_BASE    = "https://api.kite.trade"

# ── Load dashboard .env ────────────────────────────────────────────────────────
def _load_dashboard_env():
    env_path = Path("/home/ubuntu/central_trading_dashboard/.env")
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())

_load_dashboard_env()

# ── Kite token ────────────────────────────────────────────────────────────────
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
                    log.info("Kite token fetched via SSH")
                    return apikey, token
        except Exception as e:
            log.warning(f"SSH token fetch failed: {e}")
    return "", ""


# ── Price fetchers ─────────────────────────────────────────────────────────────

def get_crypto_price(symbol: str) -> float | None:
    try:
        r = requests.get(
            f"{BINANCE_BASE}/fapi/v1/ticker/price",
            params={"symbol": symbol}, timeout=10
        )
        r.raise_for_status()
        return float(r.json()["price"])
    except Exception as e:
        log.warning(f"Crypto price fetch failed for {symbol}: {e}")
        return None


def get_kite_prices(api_key: str, token: str, symbols: list[str], markets: list[str]) -> dict:
    """Bulk fetch EOD prices via Kite quote API. Returns {symbol: price}."""
    headers = {"X-Kite-Version": "3", "Authorization": f"token {api_key}:{token}"}

    # Build instrument keys
    keys = []
    for sym, mkt in zip(symbols, markets):
        if mkt == "index":
            # Map index names to Kite keys
            index_map = {
                "NIFTY 50":          "NSE:NIFTY 50",
                "NIFTY BANK":        "NSE:NIFTY BANK",
                "NIFTY FIN SERVICE": "NSE:NIFTY FIN SERVICE",
            }
            keys.append(index_map.get(sym, f"NSE:{sym}"))
        else:
            keys.append(f"NSE:{sym}")

    result = {}
    for i in range(0, len(keys), 500):
        batch_keys  = keys[i:i+500]
        batch_syms  = symbols[i:i+500]
        try:
            r = requests.get(
                f"{KITE_BASE}/quote",
                headers=headers,
                params={"i": batch_keys},
                timeout=15
            )
            r.raise_for_status()
            data = r.json().get("data", {})
            for key, sym in zip(batch_keys, batch_syms):
                q = data.get(key, {})
                if q:
                    # Use ohlc.close for EOD (last_price may be stale after hours)
                    close = q.get("ohlc", {}).get("close")
                    last  = q.get("last_price")
                    result[sym] = float(close or last or 0)
            time.sleep(0.2)
        except Exception as e:
            log.warning(f"Kite quote batch failed: {e}")

    return result


# ── Update logic ───────────────────────────────────────────────────────────────

def update_file(file_path: Path, market: str):
    signals = json.loads(file_path.read_text())
    pending = [s for s in signals if s.get("success") is None and s.get("price_at_scan")]

    if not pending:
        log.info("No pending signals to update.")
        return

    updated = 0

    if market == "crypto":
        for signal in pending:
            eod_price = get_crypto_price(signal["symbol"])
            time.sleep(0.1)
            if eod_price is None:
                log.warning(f"  Could not get price for {signal['symbol']}")
                continue
            _patch(signal, eod_price)
            updated += 1
    else:
        # Bulk fetch all stock/index prices via Kite
        api_key, token = _get_kite_token()
        if not token:
            log.error("Could not get Kite token. Aborting.")
            return

        symbols = [s["symbol"] for s in pending]
        markets = [s["market"] for s in pending]
        prices  = get_kite_prices(api_key, token, symbols, markets)

        for signal in pending:
            sym       = signal["symbol"]
            eod_price = prices.get(sym)
            if not eod_price:
                log.warning(f"  No EOD price for {sym}")
                continue
            _patch(signal, eod_price)
            updated += 1

    file_path.write_text(json.dumps(signals, indent=2))
    log.info(f"Updated {updated}/{len(pending)} signals in {file_path.name}")


def _patch(signal: dict, eod_price: float):
    entry_price = signal["price_at_scan"]
    move_pct    = round(((eod_price - entry_price) / entry_price) * 100, 3)
    bias        = signal.get("bias", "neutral")

    if bias == "long":
        success = move_pct > 0
    elif bias == "short":
        success = move_pct < 0
    else:
        success = abs(move_pct) > 1.0

    signal["eod_price"]  = eod_price
    signal["move_pct"]   = move_pct
    signal["success"]    = success
    signal["updated_at"] = datetime.now(timezone.utc).isoformat()

    log.info(f"  {signal['symbol']}: entry={entry_price} eod={eod_price} move={move_pct}% {'✓' if success else '✗'}")


# ── Main ──────────────────────────────────────────────────────────────────────

def run(market: str):
    today     = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    data_dir  = BASE_DIR / "data" / ("crypto" if market == "crypto" else "stocks")
    file_path = data_dir / f"{today}.json"

    if not file_path.exists():
        log.warning(f"No signal file found: {file_path}")
        return

    log.info(f"Running EOD update for {market} — {today}")
    update_file(file_path, market)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--market", choices=["crypto", "stocks"], required=True)
    args = parser.parse_args()
    run(args.market)
