"""规则切换控制器：发起/续跑整批回填、查询断点与对账结果。"""
from __future__ import annotations

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.constants.enums import CUTOVER_STATUS_LABELS, RULE_PROFILE_LABELS
from src.models.cutover import CutoverCheckpoint, CutoverJob
from src.services import audit_service, cutover_service
from src.utils.errors import not_found
from src.utils.timeutil import iso_utc


def _serialize_job(job: CutoverJob, checkpoints: list[CutoverCheckpoint] | None = None) -> dict:
    return {
        "id": job.id,
        "job_key": job.job_key,
        "from_version": job.from_version,
        "to_version": job.to_version,
        "from_version_label": RULE_PROFILE_LABELS.get(job.from_version, job.from_version),
        "to_version_label": RULE_PROFILE_LABELS.get(job.to_version, job.to_version),
        "status": job.status,
        "status_label": CUTOVER_STATUS_LABELS.get(job.status, job.status),
        "backfill_days": job.backfill_days,
        "total_listings": job.total_listings,
        "total_batches": job.total_batches,
        "done_batches": job.done_batches,
        "cluster_count": job.cluster_count,
        "member_count": job.member_count,
        "export_rows": job.export_rows,
        "review_carried": job.review_carried,
        "review_invalidated": job.review_invalidated,
        "review_requeued": job.review_requeued,
        "reconciliation": job.reconciliation or {},
        "last_error": job.last_error,
        "started_at": iso_utc(job.started_at),
        "finished_at": iso_utc(job.finished_at) if job.finished_at else None,
        "checkpoints": [
            {
                "batch_no": cp.batch_no,
                "status": cp.status,
                "phase": cp.phase,
                "attempts": cp.attempts,
                "listing_count": len(cp.listing_ids or []),
                "cluster_count": len(cp.cluster_fingerprints or []),
                "error": cp.error,
                "finished_at": iso_utc(cp.finished_at) if cp.finished_at else None,
            }
            for cp in (checkpoints or [])
        ],
    }


def start_or_resume(db: Session, payload: dict, principal: dict, request: Request) -> dict:
    # backfill_days 仅在首次创建时生效；已存在任务沿用原回填窗口，重试从断点继续
    job_key = payload.get("job_key") or "rule-v1-to-v2"
    job = cutover_service.get_or_create_job(db, job_key=job_key, backfill_days=payload.get("backfill_days", 30))
    db.flush()
    job = cutover_service.run_cutover(db, job_key=job_key)
    audit_service.record(
        db,
        actor_email=principal["email"],
        actor_role=principal["role"],
        action="cutover.run",
        entity="cutover_job",
        entity_id=job.id,
        detail={"status": job.status, "done_batches": job.done_batches, "total_batches": job.total_batches},
        ip=request.client.host if request.client else None,
    )
    db.commit()
    checkpoints = list(
        db.scalars(
            select(CutoverCheckpoint).where(CutoverCheckpoint.job_id == job.id).order_by(CutoverCheckpoint.batch_no)
        ).all()
    )
    return _serialize_job(db.get(CutoverJob, job.id), checkpoints)


def get_latest(db: Session) -> dict | None:
    job = cutover_service.latest_job(db)
    if job is None:
        return None
    checkpoints = list(
        db.scalars(
            select(CutoverCheckpoint).where(CutoverCheckpoint.job_id == job.id).order_by(CutoverCheckpoint.batch_no)
        ).all()
    )
    return _serialize_job(job, checkpoints)


def get_job(db: Session, job_id: int) -> dict:
    job = db.get(CutoverJob, job_id)
    if job is None:
        raise not_found("cutover_job", job_id)
    checkpoints = list(
        db.scalars(
            select(CutoverCheckpoint).where(CutoverCheckpoint.job_id == job.id).order_by(CutoverCheckpoint.batch_no)
        ).all()
    )
    return _serialize_job(job, checkpoints)
