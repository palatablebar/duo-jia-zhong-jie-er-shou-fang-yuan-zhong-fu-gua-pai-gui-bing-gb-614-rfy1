from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from src.config.database import get_db
from src.constants.roles import RUN_CREATE_ROLES
from src.controllers import cutover_controller
from src.middlewares.rbac import READ_ROLES, require_roles
from src.schemas.cutover import CutoverStartIn

router = APIRouter(prefix="/cutover", tags=["cutover"])


@router.get("")
def latest_cutover(
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*READ_ROLES)),
) -> dict:
    return {"job": cutover_controller.get_latest(db)}


@router.get("/{job_id}")
def get_cutover(
    job_id: int,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*READ_ROLES)),
) -> dict:
    return cutover_controller.get_job(db, job_id)


@router.post("/run", status_code=201)
def run_cutover(
    payload: CutoverStartIn,
    request: Request,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*RUN_CREATE_ROLES)),
) -> dict:
    return cutover_controller.start_or_resume(db, payload.model_dump(), principal, request)
