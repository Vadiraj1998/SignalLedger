"""
filter_stats.py
Reads all historical signal JSON files and computes per-filter performance.
Run manually anytime: python aggregator/filter_stats.py
Output: aggregator/filter_performance.json (also printed to console)
"""

import json
from collections import defaultdict
from pathlib import Path
from datetime import datetime

BASE_DIR = Path(__file__).resolve().parent.parent
CRYPTO_DIR = BASE_DIR / "data" / "crypto"
STOCKS_DIR = BASE_DIR / "data" / "stocks"
OUT_PATH = BASE_DIR / "aggregator" / "filter_performance.json"
OUT_PATH.parent.mkdir(parents=True, exist_ok=True)


def load_all_signals() -> list[dict]:
    signals = []
    for d in [CRYPTO_DIR, STOCKS_DIR]:
        for f in sorted(d.glob("*.json")):
            try:
                data = json.loads(f.read_text())
                signals.extend(data)
            except Exception as e:
                print(f"Warning: could not read {f}: {e}")
    return signals


def compute_stats(signals: list[dict]) -> dict:
    # Per filter stats
    filter_stats = defaultdict(lambda: {
        "signals": 0,
        "wins": 0,
        "losses": 0,
        "pending": 0,
        "total_move_pct": 0.0,
        "win_moves": [],
        "loss_moves": [],
    })

    # Per market stats
    market_stats = defaultdict(lambda: {
        "signals": 0,
        "wins": 0,
        "losses": 0,
        "pending": 0,
    })

    # Per symbol stats (top performers)
    symbol_stats = defaultdict(lambda: {"signals": 0, "wins": 0})

    for s in signals:
        filters = s.get("filters", [])
        success = s.get("success")
        move_pct = s.get("move_pct")
        market = s.get("market", "unknown")
        symbol = s.get("symbol", "")

        market_stats[market]["signals"] += 1
        symbol_stats[symbol]["signals"] += 1

        for f in filters:
            filter_stats[f]["signals"] += 1

            if success is None:
                filter_stats[f]["pending"] += 1
                market_stats[market]["pending"] += 1
            elif success:
                filter_stats[f]["wins"] += 1
                market_stats[market]["wins"] += 1
                symbol_stats[symbol]["wins"] += 1
                if move_pct is not None:
                    filter_stats[f]["win_moves"].append(move_pct)
                    filter_stats[f]["total_move_pct"] += abs(move_pct)
            else:
                filter_stats[f]["losses"] += 1
                market_stats[market]["losses"] += 1
                if move_pct is not None:
                    filter_stats[f]["loss_moves"].append(move_pct)

    # Build output
    result = {
        "generated_at": datetime.utcnow().isoformat(),
        "total_signals": len(signals),
        "filters": {},
        "markets": {},
        "top_symbols": [],
    }

    for fname, stats in filter_stats.items():
        evaluated = stats["wins"] + stats["losses"]
        win_rate = round((stats["wins"] / evaluated * 100), 1) if evaluated > 0 else None
        avg_win = round(sum(abs(m) for m in stats["win_moves"]) / len(stats["win_moves"]), 2) if stats["win_moves"] else None
        avg_loss = round(sum(abs(m) for m in stats["loss_moves"]) / len(stats["loss_moves"]), 2) if stats["loss_moves"] else None

        result["filters"][fname] = {
            "signals": stats["signals"],
            "evaluated": evaluated,
            "wins": stats["wins"],
            "losses": stats["losses"],
            "pending": stats["pending"],
            "win_rate_pct": win_rate,
            "avg_win_move_pct": avg_win,
            "avg_loss_move_pct": avg_loss,
        }

    for mname, stats in market_stats.items():
        evaluated = stats["wins"] + stats["losses"]
        result["markets"][mname] = {
            **stats,
            "win_rate_pct": round(stats["wins"] / evaluated * 100, 1) if evaluated > 0 else None,
        }

    # Top 10 symbols by win rate (min 5 signals)
    top = [
        {
            "symbol": sym,
            "signals": s["signals"],
            "wins": s["wins"],
            "win_rate_pct": round(s["wins"] / s["signals"] * 100, 1),
        }
        for sym, s in symbol_stats.items()
        if s["signals"] >= 5
    ]
    result["top_symbols"] = sorted(top, key=lambda x: x["win_rate_pct"], reverse=True)[:10]

    return result


def print_summary(result: dict):
    print(f"\n{'='*60}")
    print(f"  FILTER PERFORMANCE REPORT")
    print(f"  Generated: {result['generated_at']}")
    print(f"  Total signals: {result['total_signals']}")
    print(f"{'='*60}\n")

    print("PER FILTER:")
    for fname, stats in sorted(result["filters"].items(), key=lambda x: x[1].get("win_rate_pct") or 0, reverse=True):
        wr = stats["win_rate_pct"]
        wr_str = f"{wr}%" if wr is not None else "N/A"
        print(f"  {fname:<35} signals={stats['signals']:>4}  evaluated={stats['evaluated']:>4}  win_rate={wr_str:>6}  avg_win={stats['avg_win_move_pct']}%")

    print("\nPER MARKET:")
    for mname, stats in result["markets"].items():
        print(f"  {mname:<10} signals={stats['signals']}  wins={stats['wins']}  losses={stats['losses']}  win_rate={stats['win_rate_pct']}%")

    if result["top_symbols"]:
        print("\nTOP SYMBOLS (min 5 signals):")
        for s in result["top_symbols"]:
            print(f"  {s['symbol']:<15} signals={s['signals']}  wins={s['wins']}  win_rate={s['win_rate_pct']}%")
    print()


if __name__ == "__main__":
    signals = load_all_signals()
    result = compute_stats(signals)
    OUT_PATH.write_text(json.dumps(result, indent=2))
    print_summary(result)
    print(f"Saved to {OUT_PATH}")
