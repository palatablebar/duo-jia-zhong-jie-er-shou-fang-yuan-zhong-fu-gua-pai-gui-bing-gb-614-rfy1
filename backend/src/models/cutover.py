"""规则切换任务与断点检查点。

CutoverJob：一次「v1 单阈值 → v2 复合门控」整批回填的状态机记录；
CutoverCheckpoint：按挂牌批次切分的断点，重试时跳过 done 批次，从断点继续，
保证同批重试不重复生成房源簇或复核单。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from src.models import Base
from src.utils.timeutil import utcnow


class CutoverJob(Base):
    __tablename__ = "cutover_jobs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    job_key: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    from_version: Mapped[str] = mapped_column(String(24), default="v1_threshold")
    to_version: Mapped[str] = mapped_column(String(24), default="v2_composite")
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    backfill_days: Mapped[int] = mapped_column(Integer, default=30)
    total_listings: Mapped[int] = mapped_column(Integer, default=0)
    total_batches: Mapped[int] = mapped_column(Integer, default=0)
    done_batches: Mapped[int] = mapped_column(Integer, default=0)
    # 对账读数：完成后簇数 / 成员数 / 导出行数必须相互对上
    cluster_count: Mapped[int] = mapped_column(Integer, default=0)
    member_count: Mapped[int] = mapped_column(Integer, default=0)
    export_rows: Mapped[int] = mapped_column(Integer, default=0)
    review_carried: Mapped[int] = mapped_column(Integer, default=0)
    review_invalidated: Mapped[int] = mapped_column(Integer, default=0)
    review_requeued: Mapped[int] = mapped_column(Integer, default=0)
    reconciliation: Mapped[dict] = mapped_column(JSON, default=dict)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class CutoverCheckpoint(Base):
    __tablename__ = "cutover_checkpoints"
    __table_args__ = (
        # 同一任务下批次号唯一：重试按 (job_id, batch_no) 幂等定位，不重复建簇/复核单
        UniqueConstraint("job_id", "batch_no", name="uq_cutover_checkpoint_batch"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("cutover_jobs.id", ondelete="CASCADE"), index=True)
    batch_no: Mapped[int] = mapped_column(Integer, index=True)
    status: Mapped[str] = mapped_column(String(12), default="pending", index=True)
    phase: Mapped[str] = mapped_column(String(16), default="normalize")
    listing_ids: Mapped[list] = mapped_column(JSON, default=list)
    cluster_fingerprints: Mapped[list] = mapped_column(JSON, default=list)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
