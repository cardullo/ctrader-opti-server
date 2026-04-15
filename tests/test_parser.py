from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from server.parser import parse_report


class ParseReportTests(unittest.TestCase):
    def _parse(self, payload: dict) -> dict:
        with tempfile.TemporaryDirectory() as tmpdir:
            report_path = Path(tmpdir) / "report.json"
            report_path.write_text(json.dumps(payload), encoding="utf-8")
            return parse_report(Path(tmpdir))

    def test_parses_nested_average_trade_metric(self) -> None:
        result = self._parse(
            {
                "BacktestResult": {
                    "statistics": {
                        "netProfit": {"all": 120.0},
                        "totalTrades": {"all": 4},
                        "averageTrade": {"all": 30.0},
                        "profitFactor": {"all": 1.6},
                        "maxEquityDrawdownPercentage": {"all": 12.5},
                    }
                }
            }
        )

        self.assertEqual(result["average_trade"], 30.0)
        self.assertEqual(result["profit_factor"], 1.6)
        self.assertEqual(result["max_drawdown_pct"], 12.5)

    def test_falls_back_to_net_profit_divided_by_total_trades(self) -> None:
        result = self._parse(
            {
                "BacktestResult": {
                    "statistics": {
                        "netProfit": {"all": 100.0},
                        "totalTrades": {"all": 4},
                    }
                }
            }
        )

        self.assertEqual(result["average_trade"], 25.0)

    def test_zero_trade_reports_keep_average_trade_at_zero(self) -> None:
        result = self._parse(
            {
                "BacktestResult": {
                    "statistics": {
                        "netProfit": {"all": 100.0},
                        "totalTrades": {"all": 0},
                    }
                }
            }
        )

        self.assertEqual(result["average_trade"], 0.0)


if __name__ == "__main__":
    unittest.main()
