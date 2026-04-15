from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

import server.config
import server.db
import server.main


def _set_setting(name: str, value) -> None:
    object.__setattr__(server.config.settings, name, value)
    object.__setattr__(server.main.settings, name, value)
    object.__setattr__(server.db.settings, name, value)


def test_list_jobs_survives_malformed_job_config(tmp_path, monkeypatch) -> None:
    old_db_path = server.db.DB_PATH
    old_data_dir = server.config.settings.data_dir

    async def fake_worker_loop() -> None:
        await asyncio.sleep(3600)

    try:
        server.db.DB_PATH = tmp_path / "opti.db"
        _set_setting("data_dir", tmp_path)
        monkeypatch.setattr(server.main, "worker_loop", fake_worker_loop)

        with TestClient(server.main.app) as client:
            headers = {"X-API-Key": server.main.settings.api_key}
            good_resp = client.post(
                "/jobs",
                headers=headers,
                files={"file": ("bot.algo", b"algo-bytes", "application/octet-stream")},
                data={
                    "config": (
                        '{"name":"good-grid","strategy":"grid","max_passes":1,'
                        '"params":{"FastPeriod":{"min":5,"max":5,"step":1}}}'
                    )
                },
            )
            assert good_resp.status_code == 200

            job_id = "broken-job-id"
            db = asyncio.run(server.db.get_db())
            try:
                asyncio.run(
                    server.db.insert_job(
                        db,
                        job_id=job_id,
                        name="broken-job",
                        algo_path="/tmp/broken.algo",
                        strategy="grid",
                        total_passes=1,
                        created_at="2026-04-13T00:00:00+00:00",
                        config_json="{not-json",
                    )
                )
            finally:
                asyncio.run(db.close())

            jobs_resp = client.get("/jobs", headers=headers)

        assert jobs_resp.status_code == 200
        jobs = jobs_resp.json()
        by_id = {job["id"]: job for job in jobs}
        assert job_id in by_id
        assert "Status summary unavailable" in (by_id[job_id].get("error_detail") or "")
    finally:
        server.db.DB_PATH = old_db_path
        _set_setting("data_dir", old_data_dir)
