from __future__ import annotations

import asyncio
import json
from pathlib import Path

from fastapi.testclient import TestClient

import server.config
import server.db
import server.main


def _set_setting(name: str, value) -> None:
    object.__setattr__(server.config.settings, name, value)
    object.__setattr__(server.main.settings, name, value)
    object.__setattr__(server.db.settings, name, value)


def test_health_reports_fsb_not_ready_when_dsn_missing(tmp_path, monkeypatch) -> None:
    old_db_path = server.db.DB_PATH
    old_values = {
        "data_dir": server.config.settings.data_dir,
        "fsb_data_dsn": server.config.settings.fsb_data_dsn,
        "fsb_repo_root": server.config.settings.fsb_repo_root,
        "fsb_python_bin": server.config.settings.fsb_python_bin,
    }

    async def fake_worker_loop() -> None:
        await asyncio.sleep(3600)

    try:
        server.db.DB_PATH = tmp_path / "opti.db"
        _set_setting("data_dir", tmp_path)
        _set_setting("fsb_data_dsn", "")
        _set_setting("fsb_repo_root", tmp_path / "missing-fsb")
        _set_setting("fsb_python_bin", str(tmp_path / "missing-python"))
        monkeypatch.setattr(server.main, "worker_loop", fake_worker_loop)
        monkeypatch.setattr(server.main, "check_docker", lambda: False)

        with TestClient(server.main.app) as client:
            resp = client.get("/health")

        assert resp.status_code == 200
        assert resp.json()["fsb_ready"] is False
    finally:
        server.db.DB_PATH = old_db_path
        for key, value in old_values.items():
            _set_setting(key, value)


def test_fsb_job_creation_rejected_when_server_missing_fsb_dsn(tmp_path, monkeypatch) -> None:
    old_db_path = server.db.DB_PATH
    old_values = {
        "data_dir": server.config.settings.data_dir,
        "fsb_data_dsn": server.config.settings.fsb_data_dsn,
        "fsb_repo_root": server.config.settings.fsb_repo_root,
        "fsb_python_bin": server.config.settings.fsb_python_bin,
    }

    async def fake_worker_loop() -> None:
        await asyncio.sleep(3600)

    try:
        server.db.DB_PATH = tmp_path / "opti.db"
        _set_setting("data_dir", tmp_path)
        _set_setting("fsb_data_dsn", "")
        _set_setting("fsb_repo_root", tmp_path / "missing-fsb")
        _set_setting("fsb_python_bin", str(tmp_path / "missing-python"))
        monkeypatch.setattr(server.main, "worker_loop", fake_worker_loop)

        with TestClient(server.main.app) as client:
            resp = client.post(
                "/jobs",
                headers={"X-API-Key": server.main.settings.api_key},
                json={
                    "job_type": "fsb_search",
                    "name": "fsb-smoke",
                    "planned_total_candidates": 8,
                    "symbols": ["EURUSD"],
                    "profile": "vps",
                    "config": {},
                },
            )

        assert resp.status_code == 503
        assert "FSB_DATA_DSN" in resp.text
    finally:
        server.db.DB_PATH = old_db_path
        for key, value in old_values.items():
            _set_setting(key, value)


def test_legacy_multipart_job_creation_still_works(tmp_path, monkeypatch) -> None:
    old_db_path = server.db.DB_PATH
    old_data_dir = server.config.settings.data_dir

    async def fake_worker_loop() -> None:
        await asyncio.sleep(3600)

    try:
        server.db.DB_PATH = tmp_path / "opti.db"
        _set_setting("data_dir", tmp_path)
        monkeypatch.setattr(server.main, "worker_loop", fake_worker_loop)

        with TestClient(server.main.app) as client:
            resp = client.post(
                "/jobs",
                headers={"X-API-Key": server.main.settings.api_key},
                files={"file": ("bot.algo", b"algo-bytes", "application/octet-stream")},
                data={
                    "config": json.dumps(
                        {
                            "name": "legacy-grid",
                            "strategy": "grid",
                            "max_passes": 1,
                            "params": {"FastPeriod": {"min": 5, "max": 5, "step": 1}},
                        }
                    )
                },
            )

        assert resp.status_code == 200
        assert "job_id" in resp.json()
        assert resp.json()["total_passes"] == 1
        assert any(Path(tmp_path / "algos").iterdir())
    finally:
        server.db.DB_PATH = old_db_path
        _set_setting("data_dir", old_data_dir)
