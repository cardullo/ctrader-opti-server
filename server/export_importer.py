from __future__ import annotations

import csv
import json
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import psycopg
from psycopg.types.json import Json

from server.config import settings

CSV_COLUMNS = (
    "run_id",
    "broker_code",
    "symbol",
    "timeframe",
    "sequence_no",
    "open_time_utc",
    "open",
    "high",
    "low",
    "close",
    "tick_volume",
)


class ValidationError(ValueError):
    """Raised when export artifacts do not match the expected contract."""


@dataclass(frozen=True)
class CandleRow:
    run_id: str
    broker_code: str
    symbol: str
    timeframe: str
    sequence_no: int
    open_time_utc: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    tick_volume: int

    @classmethod
    def from_csv_values(cls, values: list[str]) -> "CandleRow":
        if len(values) != len(CSV_COLUMNS):
            raise ValidationError(f"Expected {len(CSV_COLUMNS)} columns but found {len(values)}.")

        try:
            sequence_no = int(values[4])
        except ValueError as exc:
            raise ValidationError("sequence_no must be an integer.") from exc

        try:
            tick_volume = int(values[10])
        except ValueError as exc:
            raise ValidationError("tick_volume must be an integer.") from exc

        return cls(
            run_id=values[0],
            broker_code=values[1],
            symbol=values[2],
            timeframe=values[3],
            sequence_no=sequence_no,
            open_time_utc=parse_required_utc(values[5], "open_time_utc"),
            open=parse_decimal(values[6], "open"),
            high=parse_decimal(values[7], "high"),
            low=parse_decimal(values[8], "low"),
            close=parse_decimal(values[9], "close"),
            tick_volume=tick_volume,
        )

    def as_stage_tuple(self) -> tuple[Any, ...]:
        return (
            self.run_id,
            self.broker_code,
            self.symbol,
            self.timeframe,
            self.sequence_no,
            self.open_time_utc,
            self.open,
            self.high,
            self.low,
            self.close,
            self.tick_volume,
        )


@dataclass(frozen=True)
class RunManifest:
    run_id: str
    broker_code: str
    symbol: str
    timeframe: str
    data_mode_label: str
    requested_start_utc: datetime | None
    requested_end_utc: datetime | None
    first_bar_utc: datetime | None
    last_bar_utc: datetime | None
    row_count: int
    export_started_utc: datetime
    export_finished_utc: datetime
    ctrader_report_json_path: str | None
    ctrader_report_html_path: str | None

    @classmethod
    def from_raw(cls, raw: dict[str, Any]) -> "RunManifest":
        required_text_fields = (
            "run_id",
            "broker_code",
            "symbol",
            "timeframe",
            "data_mode_label",
        )

        for field_name in required_text_fields:
            if not isinstance(raw.get(field_name), str) or not raw[field_name].strip():
                raise ValidationError(f"{field_name} must be a non-empty string.")

        row_count = raw.get("row_count")
        if not isinstance(row_count, int) or row_count < 0:
            raise ValidationError("row_count must be a non-negative integer.")

        manifest = cls(
            run_id=raw["run_id"].strip(),
            broker_code=raw["broker_code"].strip(),
            symbol=raw["symbol"].strip(),
            timeframe=raw["timeframe"].strip(),
            data_mode_label=raw["data_mode_label"].strip(),
            requested_start_utc=parse_optional_utc(raw.get("requested_start_utc"), "requested_start_utc"),
            requested_end_utc=parse_optional_utc(raw.get("requested_end_utc"), "requested_end_utc"),
            first_bar_utc=parse_optional_utc(raw.get("first_bar_utc"), "first_bar_utc"),
            last_bar_utc=parse_optional_utc(raw.get("last_bar_utc"), "last_bar_utc"),
            row_count=row_count,
            export_started_utc=parse_required_utc(raw.get("export_started_utc"), "export_started_utc"),
            export_finished_utc=parse_required_utc(raw.get("export_finished_utc"), "export_finished_utc"),
            ctrader_report_json_path=normalize_optional_text(raw.get("ctrader_report_json_path")),
            ctrader_report_html_path=normalize_optional_text(raw.get("ctrader_report_html_path")),
        )

        if manifest.requested_start_utc and manifest.requested_end_utc:
            if manifest.requested_start_utc > manifest.requested_end_utc:
                raise ValidationError("requested_start_utc must be earlier than requested_end_utc.")

        if manifest.export_started_utc > manifest.export_finished_utc:
            raise ValidationError("export_started_utc must be earlier than export_finished_utc.")

        return manifest


@dataclass(frozen=True)
class PreparedRun:
    run_dir: Path
    manifest_path: Path
    csv_path: Path
    manifest: RunManifest
    manifest_raw: dict[str, Any]
    rows: tuple[CandleRow, ...]
    report_json_path: Path | None
    report_html_path: Path | None
    report_json_raw: Any | None


@dataclass(frozen=True)
class ImportCompletedExportResult:
    pass_id: str
    status: str
    detail: str


def parse_required_utc(value: Any, field_name: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field_name} must be a non-empty ISO-8601 UTC string.")

    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError(f"{field_name} must be a valid ISO-8601 timestamp.") from exc

    if parsed.tzinfo is None:
        raise ValidationError(f"{field_name} must include timezone information.")

    return parsed.astimezone(timezone.utc)


def parse_optional_utc(value: Any, field_name: str) -> datetime | None:
    if value in (None, ""):
        return None
    return parse_required_utc(value, field_name)


def parse_decimal(value: str, field_name: str) -> Decimal:
    try:
        return Decimal(value)
    except (InvalidOperation, TypeError) as exc:
        raise ValidationError(f"{field_name} must be a decimal string.") from exc


def normalize_optional_text(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise ValidationError("Optional report path fields must be strings when provided.")
    return value.strip() or None


def discover_report_path(run_dir: Path, manifest_value: str | None, fallbacks: tuple[str, ...]) -> Path | None:
    candidates: list[Path] = []
    if manifest_value:
        configured = Path(manifest_value)
        if not configured.is_absolute():
            configured = run_dir / configured
        candidates.append(configured)

    candidates.extend(run_dir / filename for filename in fallbacks)
    for candidate in candidates:
        if candidate.exists():
            return candidate.resolve()
    return None


def load_prepared_run(run_dir: Path) -> PreparedRun:
    run_dir = run_dir.expanduser().resolve()
    manifest_path = run_dir / "manifest.json"
    csv_path = run_dir / "bars.csv"

    if not manifest_path.is_file():
        raise ValidationError(f"Missing manifest.json in {run_dir}")
    if not csv_path.is_file():
        raise ValidationError(f"Missing bars.csv in {run_dir}")

    manifest_raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest_raw, dict):
        raise ValidationError(f"{manifest_path} must contain a JSON object.")

    manifest = RunManifest.from_raw(manifest_raw)
    rows = tuple(load_rows(csv_path, manifest))
    report_json_path = discover_report_path(
        run_dir,
        manifest.ctrader_report_json_path,
        ("ctrader-report.json", "report.json"),
    )
    report_html_path = discover_report_path(
        run_dir,
        manifest.ctrader_report_html_path,
        ("ctrader-report.html", "report.html"),
    )
    report_json_raw = None
    if report_json_path and report_json_path.is_file():
        report_json_raw = json.loads(report_json_path.read_text(encoding="utf-8"))

    return PreparedRun(
        run_dir=run_dir,
        manifest_path=manifest_path,
        csv_path=csv_path,
        manifest=manifest,
        manifest_raw=manifest_raw,
        rows=rows,
        report_json_path=report_json_path,
        report_html_path=report_html_path,
        report_json_raw=report_json_raw,
    )


def load_rows(csv_path: Path, manifest: RunManifest) -> list[CandleRow]:
    rows: list[CandleRow] = []
    seen_open_times = set()
    previous_open_time = None

    with csv_path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle)
        header = next(reader, None)
        if header != list(CSV_COLUMNS):
            raise ValidationError(f"{csv_path} has an invalid header. Expected {CSV_COLUMNS}.")

        for line_number, values in enumerate(reader, start=2):
            candle = CandleRow.from_csv_values(values)
            validate_row_identity(candle, manifest, line_number)

            expected_sequence = len(rows) + 1
            if candle.sequence_no != expected_sequence:
                raise ValidationError(
                    f"{csv_path}:{line_number} expected sequence_no {expected_sequence} but found {candle.sequence_no}."
                )

            if previous_open_time and candle.open_time_utc <= previous_open_time:
                raise ValidationError(f"{csv_path}:{line_number} bars must be ordered by ascending open_time_utc.")

            if candle.open_time_utc in seen_open_times:
                raise ValidationError(f"{csv_path}:{line_number} duplicate open_time_utc detected.")

            seen_open_times.add(candle.open_time_utc)
            previous_open_time = candle.open_time_utc
            rows.append(candle)

    validate_manifest_vs_rows(manifest, rows, csv_path)
    return rows


def validate_row_identity(candle: CandleRow, manifest: RunManifest, line_number: int) -> None:
    expected = {
        "run_id": manifest.run_id,
        "broker_code": manifest.broker_code,
        "symbol": manifest.symbol,
        "timeframe": manifest.timeframe,
    }
    actual = {
        "run_id": candle.run_id,
        "broker_code": candle.broker_code,
        "symbol": candle.symbol,
        "timeframe": candle.timeframe,
    }
    for field_name, expected_value in expected.items():
        if actual[field_name] != expected_value:
            raise ValidationError(
                f"bars.csv:{line_number} {field_name}={actual[field_name]!r} does not match manifest value {expected_value!r}."
            )


def validate_manifest_vs_rows(manifest: RunManifest, rows: list[CandleRow], csv_path: Path) -> None:
    if len(rows) != manifest.row_count:
        raise ValidationError(f"{csv_path} contains {len(rows)} rows but manifest row_count is {manifest.row_count}.")

    if not rows:
        if manifest.first_bar_utc is not None or manifest.last_bar_utc is not None:
            raise ValidationError("first_bar_utc and last_bar_utc must be null when row_count is zero.")
        return

    if manifest.first_bar_utc != rows[0].open_time_utc:
        raise ValidationError("first_bar_utc does not match the first row in bars.csv.")
    if manifest.last_bar_utc != rows[-1].open_time_utc:
        raise ValidationError("last_bar_utc does not match the last row in bars.csv.")


class ExportArtifactImporter:
    def __init__(self, dsn: str, quarantine_root: Path | None = None) -> None:
        self._dsn = dsn
        self._quarantine_root = (quarantine_root or settings.data_dir / "quarantine" / "CandleExportBot").resolve()

    def import_pass(
        self,
        *,
        job_id: str,
        pass_id: str,
        run_id: str,
        delete_artifacts: bool,
    ) -> ImportCompletedExportResult:
        artifact_dir = (settings.results_dir / pass_id).resolve()
        quarantine_dir = self._quarantine_root / pass_id

        if self.run_exists(run_id):
            if delete_artifacts and artifact_dir.exists():
                shutil.rmtree(artifact_dir, ignore_errors=True)
            return ImportCompletedExportResult(pass_id=pass_id, status="skipped", detail="already present in VPS DB")

        if not artifact_dir.exists():
            if quarantine_dir.exists():
                return ImportCompletedExportResult(pass_id=pass_id, status="quarantined", detail="artifact already quarantined")
            return ImportCompletedExportResult(pass_id=pass_id, status="failed", detail="artifact directory not found on server")

        try:
            prepared_run = load_prepared_run(artifact_dir)
            if prepared_run.manifest.run_id != run_id:
                raise ValidationError(
                    f"manifest run_id {prepared_run.manifest.run_id!r} does not match pass result run_id {run_id!r}."
                )
            self.ingest_run(prepared_run)
        except ValidationError as exc:
            self.quarantine_artifact(
                quarantine_dir=quarantine_dir,
                artifact_dir=artifact_dir,
                run_id=run_id,
                job_id=job_id,
                pass_id=pass_id,
                error_message=str(exc),
            )
            return ImportCompletedExportResult(pass_id=pass_id, status="quarantined", detail=str(exc))
        except Exception as exc:  # noqa: BLE001
            return ImportCompletedExportResult(pass_id=pass_id, status="failed", detail=str(exc))

        if delete_artifacts and artifact_dir.exists():
            shutil.rmtree(artifact_dir, ignore_errors=True)
        return ImportCompletedExportResult(pass_id=pass_id, status="imported", detail="imported into VPS DB")

    def run_exists(self, run_id: str) -> bool:
        with psycopg.connect(self._dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1 FROM candle_export_runs WHERE run_id = %s LIMIT 1", (run_id,))
                return cursor.fetchone() is not None

    def ingest_run(self, prepared_run: PreparedRun) -> None:
        with psycopg.connect(self._dsn) as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    cursor.execute("SET TIME ZONE 'UTC'")
                    self._upsert_run(cursor, prepared_run)
                    self._load_stage(cursor, prepared_run)
                    self._upsert_candles(cursor)

    def quarantine_artifact(
        self,
        *,
        quarantine_dir: Path,
        artifact_dir: Path,
        run_id: str,
        job_id: str,
        pass_id: str,
        error_message: str,
    ) -> None:
        quarantine_dir.parent.mkdir(parents=True, exist_ok=True)
        if quarantine_dir.exists():
            shutil.rmtree(quarantine_dir, ignore_errors=True)
        shutil.move(str(artifact_dir), str(quarantine_dir))
        metadata = {
            "run_id": run_id,
            "job_id": job_id,
            "pass_id": pass_id,
            "quarantined_at": datetime.now(timezone.utc).isoformat(),
            "error": error_message,
        }
        (quarantine_dir / "quarantine.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")

    def _upsert_run(self, cursor: Any, prepared_run: PreparedRun) -> None:
        manifest = prepared_run.manifest
        cursor.execute(
            """
            INSERT INTO candle_export_runs (
                run_id,
                broker_code,
                symbol,
                timeframe,
                data_mode_label,
                requested_start_utc,
                requested_end_utc,
                first_bar_utc,
                last_bar_utc,
                row_count,
                export_started_utc,
                export_finished_utc,
                manifest_json,
                report_json,
                ctrader_report_json_path,
                ctrader_report_html_path,
                source_path,
                ingested_at,
                updated_at
            ) VALUES (
                %(run_id)s,
                %(broker_code)s,
                %(symbol)s,
                %(timeframe)s,
                %(data_mode_label)s,
                %(requested_start_utc)s,
                %(requested_end_utc)s,
                %(first_bar_utc)s,
                %(last_bar_utc)s,
                %(row_count)s,
                %(export_started_utc)s,
                %(export_finished_utc)s,
                %(manifest_json)s,
                %(report_json)s,
                %(ctrader_report_json_path)s,
                %(ctrader_report_html_path)s,
                %(source_path)s,
                now(),
                now()
            )
            ON CONFLICT (run_id) DO UPDATE SET
                broker_code = EXCLUDED.broker_code,
                symbol = EXCLUDED.symbol,
                timeframe = EXCLUDED.timeframe,
                data_mode_label = EXCLUDED.data_mode_label,
                requested_start_utc = EXCLUDED.requested_start_utc,
                requested_end_utc = EXCLUDED.requested_end_utc,
                first_bar_utc = EXCLUDED.first_bar_utc,
                last_bar_utc = EXCLUDED.last_bar_utc,
                row_count = EXCLUDED.row_count,
                export_started_utc = EXCLUDED.export_started_utc,
                export_finished_utc = EXCLUDED.export_finished_utc,
                manifest_json = EXCLUDED.manifest_json,
                report_json = EXCLUDED.report_json,
                ctrader_report_json_path = EXCLUDED.ctrader_report_json_path,
                ctrader_report_html_path = EXCLUDED.ctrader_report_html_path,
                source_path = EXCLUDED.source_path,
                ingested_at = now(),
                updated_at = now()
            """,
            {
                "run_id": manifest.run_id,
                "broker_code": manifest.broker_code,
                "symbol": manifest.symbol,
                "timeframe": manifest.timeframe,
                "data_mode_label": manifest.data_mode_label,
                "requested_start_utc": manifest.requested_start_utc,
                "requested_end_utc": manifest.requested_end_utc,
                "first_bar_utc": manifest.first_bar_utc,
                "last_bar_utc": manifest.last_bar_utc,
                "row_count": manifest.row_count,
                "export_started_utc": manifest.export_started_utc,
                "export_finished_utc": manifest.export_finished_utc,
                "manifest_json": Json(prepared_run.manifest_raw),
                "report_json": Json(prepared_run.report_json_raw) if prepared_run.report_json_raw is not None else None,
                "ctrader_report_json_path": optional_path(prepared_run.report_json_path),
                "ctrader_report_html_path": optional_path(prepared_run.report_html_path),
                "source_path": str(prepared_run.run_dir),
            },
        )

    def _load_stage(self, cursor: Any, prepared_run: PreparedRun) -> None:
        cursor.execute(
            """
            CREATE TEMP TABLE candle_import_stage (
                run_id text NOT NULL,
                broker_code text NOT NULL,
                symbol text NOT NULL,
                timeframe text NOT NULL,
                sequence_no bigint NOT NULL,
                open_time_utc timestamptz NOT NULL,
                open numeric(20, 10) NOT NULL,
                high numeric(20, 10) NOT NULL,
                low numeric(20, 10) NOT NULL,
                close numeric(20, 10) NOT NULL,
                tick_volume bigint NOT NULL
            ) ON COMMIT DROP
            """
        )

        with cursor.copy(
            """
            COPY candle_import_stage (
                run_id,
                broker_code,
                symbol,
                timeframe,
                sequence_no,
                open_time_utc,
                open,
                high,
                low,
                close,
                tick_volume
            ) FROM STDIN
            """
        ) as copy:
            for row in prepared_run.rows:
                copy.write_row(row.as_stage_tuple())

    def _upsert_candles(self, cursor: Any) -> None:
        cursor.execute(
            """
            INSERT INTO market_candles (
                broker_code,
                symbol,
                timeframe,
                open_time_utc,
                open,
                high,
                low,
                close,
                tick_volume,
                source_run_id,
                sequence_no,
                inserted_at,
                updated_at
            )
            SELECT
                broker_code,
                symbol,
                timeframe,
                open_time_utc,
                open,
                high,
                low,
                close,
                tick_volume,
                run_id,
                sequence_no,
                now(),
                now()
            FROM candle_import_stage
            ON CONFLICT (broker_code, symbol, timeframe, open_time_utc) DO UPDATE SET
                open = EXCLUDED.open,
                high = EXCLUDED.high,
                low = EXCLUDED.low,
                close = EXCLUDED.close,
                tick_volume = EXCLUDED.tick_volume,
                source_run_id = EXCLUDED.source_run_id,
                sequence_no = EXCLUDED.sequence_no,
                updated_at = now()
            """
        )


def optional_path(value: Path | None) -> str | None:
    return str(value) if value else None
