from __future__ import annotations

import json
from pathlib import Path

import pytest

from server.export_importer import ValidationError, load_prepared_run


def _write_sample_run(
    root: Path,
    *,
    run_id: str = "run-001",
    rows: list[str] | None = None,
    manifest_overrides: dict | None = None,
    with_report_json: bool = True,
) -> Path:
    run_dir = root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    default_rows = rows or [
        "run_id,broker_code,symbol,timeframe,sequence_no,open_time_utc,open,high,low,close,tick_volume",
        f"{run_id},icmarkets,EURUSD,m1,1,2026-04-01T00:00:00Z,1.1000,1.2000,1.0000,1.1500,10",
        f"{run_id},icmarkets,EURUSD,m1,2,2026-04-01T00:01:00Z,1.1500,1.2500,1.1000,1.2000,12",
    ]
    (run_dir / "bars.csv").write_text("\n".join(default_rows) + "\n", encoding="utf-8")

    manifest = {
        "run_id": run_id,
        "broker_code": "icmarkets",
        "symbol": "EURUSD",
        "timeframe": "m1",
        "data_mode_label": "m1",
        "requested_start_utc": "2026-04-01T00:00:00Z",
        "requested_end_utc": "2026-04-01T00:02:00Z",
        "first_bar_utc": "2026-04-01T00:00:00Z",
        "last_bar_utc": "2026-04-01T00:01:00Z",
        "row_count": 2,
        "export_started_utc": "2026-04-01T00:02:00Z",
        "export_finished_utc": "2026-04-01T00:02:01Z",
        "ctrader_report_json_path": "ctrader-report.json",
        "ctrader_report_html_path": "ctrader-report.html",
    }
    if manifest_overrides:
        manifest.update(manifest_overrides)
    (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    if with_report_json:
        (run_dir / "ctrader-report.json").write_text(
            json.dumps({"summary": {"trades": 0}}, indent=2) + "\n",
            encoding="utf-8",
        )
    (run_dir / "ctrader-report.html").write_text("<html></html>\n", encoding="utf-8")
    return run_dir


def test_load_prepared_run_accepts_valid_non_empty_run(tmp_path: Path) -> None:
    run_dir = _write_sample_run(tmp_path)

    prepared = load_prepared_run(run_dir)

    assert prepared.manifest.run_id == "run-001"
    assert len(prepared.rows) == 2
    assert prepared.report_json_raw == {"summary": {"trades": 0}}


def test_load_prepared_run_accepts_zero_row_run(tmp_path: Path) -> None:
    run_dir = _write_sample_run(
        tmp_path,
        run_id="run-zero",
        rows=["run_id,broker_code,symbol,timeframe,sequence_no,open_time_utc,open,high,low,close,tick_volume"],
        manifest_overrides={
            "row_count": 0,
            "first_bar_utc": None,
            "last_bar_utc": None,
        },
    )

    prepared = load_prepared_run(run_dir)

    assert prepared.manifest.run_id == "run-zero"
    assert prepared.rows == ()


def test_load_prepared_run_rejects_invalid_header(tmp_path: Path) -> None:
    run_dir = _write_sample_run(
        tmp_path,
        run_id="bad-header",
        rows=[
            "wrong,broker_code,symbol,timeframe,sequence_no,open_time_utc,open,high,low,close,tick_volume",
            "bad-header,icmarkets,EURUSD,m1,1,2026-04-01T00:00:00Z,1.1000,1.2000,1.0000,1.1500,10",
        ],
        manifest_overrides={
            "row_count": 1,
            "last_bar_utc": "2026-04-01T00:00:00Z",
        },
    )

    with pytest.raises(ValidationError, match="invalid header"):
        load_prepared_run(run_dir)


def test_load_prepared_run_rejects_invalid_column_count(tmp_path: Path) -> None:
    run_dir = _write_sample_run(
        tmp_path,
        run_id="bad-columns",
        rows=[
            "run_id,broker_code,symbol,timeframe,sequence_no,open_time_utc,open,high,low,close,tick_volume",
            "bad-columns,icmarkets,EURUSD,m1,1,2026-04-01T00:00:00Z,1.1000,1.2000,1.0000,1.1500,10,extra",
        ],
        manifest_overrides={
            "row_count": 1,
            "last_bar_utc": "2026-04-01T00:00:00Z",
        },
    )

    with pytest.raises(ValidationError, match="Expected 11 columns"):
        load_prepared_run(run_dir)


def test_load_prepared_run_rejects_duplicate_or_out_of_order_timestamps(tmp_path: Path) -> None:
    run_dir = _write_sample_run(
        tmp_path,
        run_id="bad-order",
        rows=[
            "run_id,broker_code,symbol,timeframe,sequence_no,open_time_utc,open,high,low,close,tick_volume",
            "bad-order,icmarkets,EURUSD,m1,1,2026-04-01T00:01:00Z,1.1000,1.2000,1.0000,1.1500,10",
            "bad-order,icmarkets,EURUSD,m1,2,2026-04-01T00:01:00Z,1.1500,1.2500,1.1000,1.2000,12",
        ],
    )

    with pytest.raises(ValidationError, match="ordered by ascending open_time_utc|duplicate open_time_utc"):
        load_prepared_run(run_dir)


def test_load_prepared_run_rejects_manifest_identity_mismatch(tmp_path: Path) -> None:
    run_dir = _write_sample_run(
        tmp_path,
        run_id="manifest-mismatch",
        rows=[
            "run_id,broker_code,symbol,timeframe,sequence_no,open_time_utc,open,high,low,close,tick_volume",
            "other-run,icmarkets,EURUSD,m1,1,2026-04-01T00:00:00Z,1.1000,1.2000,1.0000,1.1500,10",
        ],
        manifest_overrides={
            "row_count": 1,
            "last_bar_utc": "2026-04-01T00:00:00Z",
        },
    )

    with pytest.raises(ValidationError, match="does not match manifest value"):
        load_prepared_run(run_dir)
