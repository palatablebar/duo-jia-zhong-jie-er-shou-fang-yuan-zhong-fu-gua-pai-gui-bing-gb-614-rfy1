from __future__ import annotations

from pydantic import BaseModel, Field


class CutoverStartIn(BaseModel):
    job_key: str = Field(default="rule-v1-to-v2", max_length=80)
    backfill_days: int = Field(default=30, ge=1, le=365)
