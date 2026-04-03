"""
Parse ctrader-cli backtest report output into a flat metrics dict.

Designed to be resilient: if the report structure differs from expectations,
we do a recursive key search and always return a valid dict (never crash).
The raw JSON is also preserved so you can inspect it if parsing is incomplete.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def parse_report(report_dir: Path) -> Dict[str, Any]:
    """
    Read the report.json produced by ctrader-cli and extract key metrics.

    Falls back to scanning for any .json file in the directory if
    report.json is not found.

    Returns a flat dict suitable for storing as result_json.
    """
    report_path = report_dir / "report.json"

    # Fallback: look for any JSON file in the results directory
    if not report_path.exists():
        json_files = list(report_dir.glob("*.json"))
        if json_files:
            report_path = json_files[0]
            logger.info("Using fallback report file: %s", report_path)
        else:
            logger.warning("No report JSON found in %s", report_dir)
            return _empty_result("No report file found")

    try:
        raw_text = report_path.read_text(encoding="utf-8")
        raw = json.loads(raw_text)
    except (json.JSONDecodeError, OSError) as exc:
        logger.error("Failed to read report: %s", exc)
        return _empty_result(str(exc))

    try:
        metrics = _extract_metrics(raw)
    except Exception as exc:
        logger.error("Failed to extract metrics: %s", exc, exc_info=True)
        # Preserve the raw JSON as-is so it's not lost
        metrics = _empty_result(f"Parse error: {exc}")
        metrics["_raw_keys"] = list(raw.keys()) if isinstance(raw, dict) else str(type(raw))

    # Save a dump of the first report we encounter so we can inspect the
    # actual ctrader-cli structure and refine parsing later.
    debug_path = report_dir / "_parsed_debug.json"
    if not debug_path.exists():
        try:
            debug_path.write_text(json.dumps({
                "parsed_metrics": metrics,
                "raw_report_keys": _recursive_keys(raw) if isinstance(raw, dict) else [],
            }, indent=2, default=str))
        except Exception:
            pass

    return metrics


def _recursive_keys(d: Any, prefix: str = "") -> List[str]:
    """Return a flat list of all key paths in a nested dict."""
    keys: List[str] = []
    if isinstance(d, dict):
        for k, v in d.items():
            full = f"{prefix}.{k}" if prefix else k
            keys.append(full)
            keys.extend(_recursive_keys(v, full))
    elif isinstance(d, list) and d and isinstance(d[0], dict):
        keys.extend(_recursive_keys(d[0], f"{prefix}[0]"))
    return keys


def _find_in_dict(d: Any, target_keys: List[str], default: Any = None) -> Any:
    """
    Recursively search a nested dict for the first matching key.

    This is the safe fallback: if ctrader-cli nests values in an
    unexpected location, we'll still find them.
    """
    if not isinstance(d, dict):
        return default

    # Check direct keys first
    for tk in target_keys:
        if tk in d:
            return d[tk]

    # Recurse into nested dicts
    for v in d.values():
        if isinstance(v, dict):
            result = _find_in_dict(v, target_keys, None)
            if result is not None:
                return result

    return default


def _extract_metrics(raw: Dict[str, Any]) -> Dict[str, Any]:
    """
    Extract a normalised metrics dict from the ctrader-cli report structure.

    Strategy:
    1. Try known top-level wrappers (BacktestResult, result, etc.)
    2. Try known nested stats keys (statistics, Statistics)
    3. Fall back to recursive search across the entire dict
    """
    # Try common top-level wrappers
    data: Dict[str, Any] = {}
    for key in ("BacktestResult", "backtestResult", "result", "Result"):
        if key in raw:
            data = raw[key]
            break
    if not data:
        data = raw

    # Nested stats
    stats = data.get("statistics", data.get("Statistics", data))

    def _g(keys: list, default: Any = None) -> Any:
        """Get the first matching key from stats, then data, then full recursive search."""
        # Fast path: check stats and data directly
        for k in keys:
            if k in stats:
                return stats[k]
            if k in data:
                return data[k]
        # Slow path: recursive search in the full raw dict
        return _find_in_dict(raw, keys, default)

    net_profit = _g(["netProfit", "NetProfit", "net_profit"], 0.0)
    gross_profit = _g(["grossProfit", "GrossProfit", "gross_profit"], 0.0)
    gross_loss = _g(["grossLoss", "GrossLoss", "gross_loss"], 0.0)
    profit_factor = _g(["profitFactor", "ProfitFactor", "profit_factor"], 0.0)
    sharpe_ratio = _g(["sharpeRatio", "SharpeRatio", "sharpe_ratio"], 0.0)
    max_drawdown_pct = _g(
        ["maxEquityDrawdownPercentage", "MaxEquityDrawdownPercentage",
         "MaxBalanceDrawdownPercentage", "maxBalanceDrawdownPercentage",
         "maxDrawdownPct", "max_drawdown_pct"],
        0.0,
    )
    max_drawdown_abs = _g(
        ["maxEquityDrawdown", "MaxEquityDrawdown",
         "MaxBalanceDrawdown", "maxBalanceDrawdown",
         "maxDrawdownAbs", "max_drawdown_abs"],
        0.0,
    )
    total_trades = _g(["totalTrades", "TotalTrades", "total_trades"], 0)

    # Win rate — prefer explicit win rate, otherwise compute from winning trades
    winning = _g(["winningTrades", "WinningTrades", "winning_trades"], 0)
    if total_trades and _to_int(total_trades) > 0:
        win_rate = round((_to_int(winning) / _to_int(total_trades)) * 100, 2)
    else:
        win_rate = _g(["winRate", "WinRate", "win_rate"], 0.0)

    avg_trade_duration = _g(
        ["averageTradeDuration", "AverageTradeDuration", "avg_trade_duration"], ""
    )
    start_balance = _g(["startBalance", "StartBalance", "start_balance", "balance", "Balance"], 0.0)
    end_balance = _g(["endBalance", "EndBalance", "end_balance"], 0.0)

    # If end_balance is 0 but we have start_balance and net_profit, compute it
    if _to_float(end_balance) == 0.0 and _to_float(start_balance) > 0 and _to_float(net_profit) != 0:
        end_balance = _to_float(start_balance) + _to_float(net_profit)

    return {
        "net_profit": _to_float(net_profit),
        "gross_profit": _to_float(gross_profit),
        "gross_loss": _to_float(gross_loss),
        "profit_factor": _to_float(profit_factor),
        "sharpe_ratio": _to_float(sharpe_ratio),
        "max_drawdown_pct": _to_float(max_drawdown_pct),
        "max_drawdown_abs": _to_float(max_drawdown_abs),
        "win_rate": _to_float(win_rate),
        "total_trades": _to_int(total_trades),
        "avg_trade_duration": str(avg_trade_duration),
        "start_balance": _to_float(start_balance),
        "end_balance": _to_float(end_balance),
    }


def _to_float(val: Any) -> float:
    try:
        return round(float(val), 4)
    except (TypeError, ValueError):
        return 0.0


def _to_int(val: Any) -> int:
    try:
        return int(float(val))
    except (TypeError, ValueError):
        return 0


def _empty_result(error: str) -> Dict[str, Any]:
    return {
        "net_profit": 0.0,
        "gross_profit": 0.0,
        "gross_loss": 0.0,
        "profit_factor": 0.0,
        "sharpe_ratio": 0.0,
        "max_drawdown_pct": 0.0,
        "max_drawdown_abs": 0.0,
        "win_rate": 0.0,
        "total_trades": 0,
        "avg_trade_duration": "",
        "start_balance": 0.0,
        "end_balance": 0.0,
        "error": error,
    }
