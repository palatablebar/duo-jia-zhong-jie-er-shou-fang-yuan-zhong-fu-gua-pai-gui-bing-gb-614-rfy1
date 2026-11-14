"""风险复核单：算法 B 出的风险簇进入人工复核队列。"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from src.models import Base
from src.utils.timeutil import utcnow


class Review(Base):
    __tablename__ = "reviews"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    property_id: Mapped[int] = mapped_column(ForeignKey("properties.id", ondelete="CASCADE"), index=True)
    run_id: Mapped[int | None] = mapped_column(
        ForeignKey("match_runs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    reason: Mapped[str] = mapped_column(String(20), default="manual")
    severity: Mapped[str] = mapped_column(String(10), default="medium", index=True)
    risk_score: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    reviewer: Mapped[str | None] = mapped_column(String(160), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    seed_key: Mapped[str | None] = mapped_column(String(120), unique=True, nullable=True)
    # 规则切换：成员指纹用于判断能否沿用；旧簇拆散/成员变化时结论作废并重新排队
    member_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    rule_version: Mapped[str] = mapped_column(String(24), default="v1_threshold", index=True)
    invalidated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    invalid_reason: Mapped[str | None] = mapped_column(String(24), nullable=True)
    superseded_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("reviews.id", ondelete="SET NULL"), nullable=True
    )
    recheck_of_id: Mapped[int | None] = mapped_column(
        ForeignKey("reviews.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
