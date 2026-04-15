from __future__ import annotations

import asyncio
import json

from fastapi.testclient import TestClient

import server.config
import server.db
import server.export_importer
import server.main
from server.models import ImportCompletedExportsResponse, ImportCompletedExportsResult


def _set_setting(name: str, value) -> None:
    object.__setattr__(server.config.settings, name, value)
    object.__setattr__(server.main.settings, name, value)
    object.__setattr__(server.db.settings, name, value)
    object.__setattr__(server.export_importer.settings, name, value)


async def _fake_worker_loop() -> None:
    await asyncio.sleep(3600)


def _seed_export_job(tmp_path, monkeypatch, *, job_id: str = "job-export-1") -> None:
    old_db_path = getattr(_seed_export_job, "_old_db_path", None)
    if old_db_path is None:
        _seed_export_job._old_db_path = server.db.DB_PATH
        _seed_export_job._old_data_dir = server.config.settings.data_dir
        _seed_export_job._old_fsb_data_dsn = server.config.settings.fsb_data_dsn
    server.db.DB_PATH = tmp_path / "opti.db"
    _set_setting("data_dir", tmp_path)
    _set_setting("fsb_data_dsn", "postgresql://postgres:postgres@localhost:55432/market")
    monkeypatch.setattr(server.main, "worker_loop", _fake_worker_loop)
    asyncio.run(server.db.init_db())

    config_payload = {
        "name": "export-job",
        "strategy": "export",
        "symbol": "EURUSD",
        "period": "m1",
        "chunks": [
            {
                "symbol": "EURUSD",
                "period": "m1",
                "start_utc": "2026-04-01T00:00:00Z",
                "end_utc": "2026-04-01T01:00:00Z",
            }
        ],
    }

    async def _seed() -> None:
        db = await server.db.get_db()
        try:
            await server.db.insert_job(
                db,
                job_id=job_id,
                name="export-job",
                algo_path="/tmp/export.algo",
                strategy="export",
                total_passes=3,
                created_at="2026-04-14T00:00:00+00:00",
                config_json=json.dumps(config_payload),
            )
            await server.db.insert_passes(
                db,
                [
                    ("pass-done-1", job_id, json.dumps({"chunk": 1}), "done"),
                    ("pass-queued-1", job_id, json.dumps({"chunk": 2}), "queued"),
                    ("pass-done-2", job_id, json.dumps({"chunk": 3}), "done"),
                ],
            )
            await db.execute(
                "UPDATE passes SET result_json = ? WHERE id = ?",
                (json.dumps({"job_type": "export", "run_id": "run-1"}), "pass-done-1"),
            )
            await db.execute(
                "UPDATE passes SET result_json = ? WHERE id = ?",
                (json.dumps({"job_type": "export", "run_id": "run-2"}), "pass-done-2"),
            )
            await db.commit()
        finally:
            await db.close()

    asyncio.run(_seed())


def _restore_seed_settings() -> None:
    old_db_path = getattr(_seed_export_job, "_old_db_path", None)
    if old_db_path is None:
        return
    server.db.DB_PATH = _seed_export_job._old_db_path
    _set_setting("data_dir", _seed_export_job._old_data_dir)
    _set_setting("fsb_data_dsn", _seed_export_job._old_fsb_data_dsn)


def test_import_completed_exports_filters_to_done_selected_passes(tmp_path, monkeypatch) -> None:
    _seed_export_job(tmp_path, monkeypatch)
    captured = {}

    def fake_import(*, job_id: str, pass_rows: list[dict], delete_artifacts: bool) -> ImportCompletedExportsResponse:
        captured["job_id"] = job_id
        captured["pass_ids"] = [row["id"] for row in pass_rows]
        captured["delete_artifacts"] = delete_artifacts
        return ImportCompletedExportsResponse(
            job_id=job_id,
            discovered=len(pass_rows),
            imported=1,
            skipped=0,
            quarantined=0,
            failed=0,
            results=[ImportCompletedExportsResult(pass_id="pass-done-2", status="imported", detail="imported into VPS DB")],
        )

    monkeypatch.setattr(server.main, "_import_completed_export_passes", fake_import)

    try:
        with TestClient(server.main.app) as client:
            response = client.post(
                "/jobs/job-export-1/import-completed-exports",
                headers={"X-API-Key": server.main.settings.api_key},
                json={"delete_artifacts": True, "pass_ids": ["pass-done-2", "pass-queued-1", "missing-pass"]},
            )
        assert response.status_code == 200
        assert captured == {
            "job_id": "job-export-1",
            "pass_ids": ["pass-done-2"],
            "delete_artifacts": True,
        }
        body = response.json()
        assert body["discovered"] == 1
        assert body["imported"] == 1
        assert body["results"][0]["status"] == "imported"
    finally:
        _restore_seed_settings()


def test_import_completed_exports_supports_idempotent_rerun(tmp_path, monkeypatch) -> None:
    _seed_export_job(tmp_path, monkeypatch)
    call_count = {"value": 0}

    def fake_import(*, job_id: str, pass_rows: list[dict], delete_artifacts: bool) -> ImportCompletedExportsResponse:
        call_count["value"] += 1
        if call_count["value"] == 1:
            return ImportCompletedExportsResponse(
                job_id=job_id,
                discovered=1,
                imported=1,
                skipped=0,
                quarantined=0,
                failed=0,
                results=[ImportCompletedExportsResult(pass_id="pass-done-1", status="imported", detail="imported into VPS DB")],
            )
        return ImportCompletedExportsResponse(
            job_id=job_id,
            discovered=1,
            imported=0,
            skipped=1,
            quarantined=0,
            failed=0,
            results=[ImportCompletedExportsResult(pass_id="pass-done-1", status="skipped", detail="already present in VPS DB")],
        )

    monkeypatch.setattr(server.main, "_import_completed_export_passes", fake_import)

    try:
        with TestClient(server.main.app) as client:
            headers = {"X-API-Key": server.main.settings.api_key}
            first = client.post(
                "/jobs/job-export-1/import-completed-exports",
                headers=headers,
                json={"pass_ids": ["pass-done-1"]},
            )
            second = client.post(
                "/jobs/job-export-1/import-completed-exports",
                headers=headers,
                json={"pass_ids": ["pass-done-1"]},
            )
        assert first.status_code == 200
        assert second.status_code == 200
        assert first.json()["imported"] == 1
        assert second.json()["skipped"] == 1
    finally:
        _restore_seed_settings()


def test_import_completed_exports_reports_mixed_outcomes_without_aborting(tmp_path, monkeypatch) -> None:
    _seed_export_job(tmp_path, monkeypatch)

    def fake_import(*, job_id: str, pass_rows: list[dict], delete_artifacts: bool) -> ImportCompletedExportsResponse:
        return ImportCompletedExportsResponse(
            job_id=job_id,
            discovered=2,
            imported=0,
            skipped=0,
            quarantined=1,
            failed=1,
            results=[
                ImportCompletedExportsResult(pass_id="pass-done-1", status="quarantined", detail="CSV row 7 has 20 columns; expected 11."),
                ImportCompletedExportsResult(pass_id="pass-done-2", status="failed", detail="artifact directory not found on server"),
            ],
        )

    monkeypatch.setattr(server.main, "_import_completed_export_passes", fake_import)

    try:
        with TestClient(server.main.app) as client:
            response = client.post(
                "/jobs/job-export-1/import-completed-exports",
                headers={"X-API-Key": server.main.settings.api_key},
                json={},
            )
        assert response.status_code == 200
        body = response.json()
        assert body["discovered"] == 2
        assert body["quarantined"] == 1
        assert body["failed"] == 1
        assert [item["status"] for item in body["results"]] == ["quarantined", "failed"]
    finally:
        _restore_seed_settings()
