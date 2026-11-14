"""HTTP 层：规则切换接口的鉴权、断点续跑与导出闸门（FastAPI TestClient + SQLite）。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from src.config.database import get_db
from src.main import app
from src.models import Base
from src.utils.security import create_access_token

from tests.test_cutover_integration import _run_v1, _seed_recent_listings  # noqa: F401


@pytest.fixture()
def client_db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session, engine


@pytest.fixture()
def client(client_db):
    _session, engine = client_db

    def _override():
        db = Session(engine)
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override
    # 不使用 with：TestClient 仅在上下文管理器模式下才跑 lifespan（其中会 wait_for_db 连真实 MySQL）
    test_client = TestClient(app, raise_server_exceptions=True)
    yield test_client
    app.dependency_overrides.clear()


def _token(role: str) -> str:
    token, _ = create_access_token(f"{role}@x.io", role)
    return token


def _auth(role: str) -> dict:
    return {"Authorization": f"Bearer {_token(role)}"}


class TestCutoverApi:
    def test_viewer_cannot_start_cutover(self, client, client_db):
        db, _ = client_db
        _seed_recent_listings(db)
        _run_v1(db)
        resp = client.post("/api/cutover/run", json={"job_key": "rule-v1-to-v2"}, headers=_auth("viewer"))
        assert resp.status_code == 403

    def test_anonymous_rejected(self, client):
        resp = client.get("/api/cutover")
        assert resp.status_code == 401

    def test_analyst_runs_cutover_and_sees_checkpoints(self, client, client_db):
        db, _ = client_db
        _seed_recent_listings(db)
        _run_v1(db)
        resp = client.post(
            "/api/cutover/run",
            json={"job_key": "rule-v1-to-v2", "backfill_days": 30},
            headers=_auth("analyst"),
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["status"] == "completed"
        assert body["done_batches"] == body["total_batches"]
        assert body["reconciliation"]["consistent"] is True
        assert len(body["checkpoints"]) == body["total_batches"]
        assert all(cp["status"] == "done" for cp in body["checkpoints"])
        # 三种角色都能读状态
        got = client.get("/api/cutover", headers=_auth("viewer"))
        assert got.status_code == 200
        assert got.json()["job"]["status"] == "completed"

    def test_export_gate_returns_only_active_before_completion(self, client, client_db):
        db, _ = client_db
        _seed_recent_listings(db)
        _run_v1(db)
        # 造半成品
        from src.services import cutover_service

        job = cutover_service.get_or_create_job(db, job_key="rule-v1-to-v2")
        cutover_service.stage_first_batch(db, job)
        db.commit()

        resp = client.get("/api/exports/clusters.csv", headers=_auth("analyst"))
        assert resp.status_code == 200
        text = resp.text
        assert "v2_composite" not in text
        assert "staging" not in text

    def test_run_accepts_rule_version(self, client, client_db):
        db, _ = client_db
        _seed_recent_listings(db)
        resp = client.post(
            "/api/runs",
            json={"trigger": "manual", "threshold": 0.82, "rule_version": "v2_composite"},
            headers=_auth("admin"),
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["rule_version"] == "v2_composite"

    def test_run_rejects_unknown_rule_version(self, client, client_db):
        db, _ = client_db
        _seed_recent_listings(db)
        resp = client.post(
            "/api/runs",
            json={"rule_version": "v9_unknown"},
            headers=_auth("admin"),
        )
        assert resp.status_code == 422
