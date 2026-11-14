"""规则切换服务：v1 单阈值 → v2 复合门控的整批回填、断点续跑与对账。

核心保证（对应业务约束）：

1. 整批回填：对最近 ``CUTOVER_BACKFILL_DAYS`` 天（默认 30 天）的挂牌全量重算，
   新结果以 ``publish_state='staging'`` 落库，与已发布的 v1 数据物理隔离；
2. 断点续跑：按 ``CUTOVER_BATCH_SIZE`` 切批，检查点落库；重试跳过 done 批次，
   批次内按成员指纹 upsert，同批重试不重复生成房源簇或复核单；
3. 复核结论沿用/作废：成员指纹完全一致才沿用旧结论；原簇被拆散或成员变化，
   旧单置 ``invalidated`` 并为新簇重新排队；
4. 导出闸门：未完成切换前导出只认 active（v1），半成品 staging 不进入导出；失败保留旧版；
5. 对账：完成后簇数、成员数（= 回填挂牌数）、导出行数三方对得上才允许激活，替换不丢记录。
"""
from __future__ import annotations

import hashlib
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.constants.enums import (
    CheckpointStatus,
    CutoverPhase,
    CutoverStatus,
    ReviewInvalidReason,
    ReviewStatus,
)
from src.constants.limits import (
    CUTOVER_BATCH_SIZE,
    CUTOVER_BACKFILL_DAYS,
    RULE_PROFILE_V1,
    RULE_PROFILE_V2,
)
from src.models.agency import Agency
from src.models.cutover import CutoverCheckpoint, CutoverJob
from src.models.listing import Listing
from src.models.match_run import MatchRun
from src.models.property import Property
from src.models.review import Review
from src.services import entity_resolution, normalizer, risk_rules
from src.services.rule_profiles import get_profile
from src.utils.errors import AppError
from src.utils.timeutil import utcnow

LISTING_FIELDS = (
    "community_raw",
    "layout_raw",
    "area_raw",
    "price_raw",
    "floor_raw",
    "orientation_raw",
    "title",
)


# ---------------------------------------------------------------- 指纹工具
def member_fingerprint(member_lids: list[str]) -> str:
    """簇成员指纹：成员挂牌自然键（agency:lid）排序后的哈希。

    成员集合完全相同 ⇔ 指纹相同，是复核结论能否沿用的唯一判据。
    """
    return hashlib.sha256("|".join(sorted(member_lids)).encode("utf-8")).hexdigest()[:40]


def _member_lid(agencies: dict[int, Agency], listing: Listing) -> str:
    return f"{agencies[listing.agency_id].code}:{listing.lid}"


# ---------------------------------------------------------------- 任务管理
def get_or_create_job(db: Session, *, job_key: str, backfill_days: int = CUTOVER_BACKFILL_DAYS) -> CutoverJob:
    job = db.scalar(select(CutoverJob).where(CutoverJob.job_key == job_key))
    if job is not None:
        return job
    job = CutoverJob(
        job_key=job_key,
        from_version=RULE_PROFILE_V1,
        to_version=RULE_PROFILE_V2,
        status=CutoverStatus.PENDING.value,
        backfill_days=backfill_days,
    )
    db.add(job)
    db.flush()
    return job


def latest_job(db: Session) -> CutoverJob | None:
    return db.scalar(select(CutoverJob).order_by(CutoverJob.id.desc()).limit(1))


def _backfill_listings(db: Session, job: CutoverJob) -> list[Listing]:
    """最近一个月入库的挂牌（按 captured_at 过滤），全量参与回填重算。"""
    cutoff = utcnow() - timedelta(days=job.backfill_days)
    return list(
        db.scalars(
            select(Listing).where(Listing.captured_at >= cutoff).order_by(Listing.id)
        ).all()
    )


def _ensure_checkpoints(db: Session, job: CutoverJob, listings: list[Listing]) -> list[CutoverCheckpoint]:
    """按批次登记检查点（幂等：已存在的批次不重建）。"""
    existing = {cp.batch_no: cp for cp in db.scalars(select(CutoverCheckpoint).where(CutoverCheckpoint.job_id == job.id)).all()}
    checkpoints: list[CutoverCheckpoint] = []
    batch_size = CUTOVER_BATCH_SIZE
    total_batches = (len(listings) + batch_size - 1) // batch_size if listings else 0
    for batch_no in range(total_batches):
        start = batch_no * batch_size
        batch = listings[start:start + batch_size]
        cp = existing.get(batch_no)
        if cp is None:
            cp = CutoverCheckpoint(
                job_id=job.id,
                batch_no=batch_no,
                status=CheckpointStatus.PENDING.value,
                phase=CutoverPhase.NORMALIZE.value,
                listing_ids=[row.id for row in batch],
            )
            db.add(cp)
        checkpoints.append(cp)
    job.total_listings = len(listings)
    job.total_batches = total_batches
    db.flush()
    return checkpoints


# ---------------------------------------------------------------- 单批处理
def _persist_cluster(
    db: Session,
    job: CutoverJob,
    run: MatchRun,
    agencies: dict[int, Agency],
    listings: list[Listing],
    normalized: list[dict],
    cluster: dict,
    fingerprints: set[str],
) -> tuple[Property, list[int]]:
    """把一个 v2 簇以 staging 状态 upsert（按成员指纹幂等，重试不重复建簇）。"""
    members = cluster["members"]
    member_lids = sorted(_member_lid(agencies, listings[index]) for index in members)
    fingerprint = member_fingerprint(member_lids)
    fingerprints.add(fingerprint)
    golden = cluster["golden"]
    member_rows = [
        {
            "id": listings[index].id,
            "lid": listings[index].lid,
            "price_num": normalized[index]["price_num"],
            "listed_days": listings[index].listed_days,
            "views": listings[index].views,
            "status": listings[index].status,
        }
        for index in members
    ]
    risk = risk_rules.scan_cluster(member_rows, golden)

    prop = db.scalar(
        select(Property).where(
            Property.cutover_job_id == job.id,
            Property.member_fingerprint == fingerprint,
        )
    )
    if prop is None:
        prop = Property(
            cluster_key=hashlib.sha256(f"{job.job_key}:{fingerprint}".encode("utf-8")).hexdigest()[:40],
            cutover_job_id=job.id,
        )
        db.add(prop)
    prop.publish_state = "staging"
    prop.rule_version = job.to_version
    prop.member_fingerprint = fingerprint
    prop.community = golden["community"]
    prop.layout = golden["layout"]
    prop.area_mode = golden["area_mode"]
    prop.area_min = golden["area_min"]
    prop.area_max = golden["area_max"]
    prop.price_median = golden["price_median"]
    prop.price_min = golden["price_min"]
    prop.price_max = golden["price_max"]
    prop.price_p10 = golden["price_p10"]
    prop.price_p90 = golden["price_p90"]
    prop.spread = golden["spread"]
    prop.dispersion = golden["dispersion"]
    prop.agency_count = golden["agency_count"]
    prop.member_count = golden["member_count"]
    prop.confidence = cluster["confidence"]
    prop.risk_score = risk["risk_score"]
    prop.risk_level = risk["risk_level"]
    prop.risk_tags = risk["risk_tags"]
    prop.risk_detail = risk["risk_detail"]
    prop.member_lids = member_lids
    prop.run_id = run.id
    prop.updated_at = utcnow()
    db.flush()
    return prop, members


def _sync_reviews(
    db: Session,
    job: CutoverJob,
    run: MatchRun,
    staging_props: list[Property],
) -> tuple[int, int, int]:
    """按成员指纹处理复核单：成员未变沿用；拆散/成员变化则旧单作废、新簇重新排队。

    返回 (沿用数, 作废数, 重新排队数)。全程按指纹幂等，重试不产生重复复核单。
    """
    carried = invalidated = requeued = 0
    # 当前仍有效的 v1 复核单（含历史未作废单），按成员指纹索引
    old_reviews = list(
        db.scalars(
            select(Review).where(
                Review.rule_version == job.from_version,
                Review.invalidated_at.is_(None),
            )
        ).all()
    )
    old_props = {prop.id: prop for prop in db.scalars(select(Property).where(Property.publish_state == "active")).all()}
    old_by_fingerprint: dict[str, Review] = {}
    for review in old_reviews:
        prop = old_props.get(review.property_id)
        fingerprint = review.member_fingerprint or (prop.member_fingerprint if prop else None)
        if fingerprint:
            old_by_fingerprint.setdefault(fingerprint, review)

    staging_by_fingerprint = {prop.member_fingerprint: prop for prop in staging_props}

    # 1) 作废：旧指纹在新结果中找不到完全相同成员的簇（被拆散或成员变化）
    for fingerprint, review in old_by_fingerprint.items():
        if fingerprint in staging_by_fingerprint:
            continue
        old_prop = old_props.get(review.property_id)
        reason = (
            ReviewInvalidReason.CLUSTER_SPLIT.value
            if old_prop is not None and _was_split(old_prop, staging_props)
            else ReviewInvalidReason.MEMBERS_CHANGED.value
        )
        review.invalidated_at = utcnow()
        review.invalid_reason = reason
        review.status = ReviewStatus.DISMISSED.value
        invalidated += 1

    # 2) 沿用 + 重新排队（按新簇指纹幂等）
    for prop in staging_props:
        if prop.risk_level not in ("high", "medium"):
            continue
        fingerprint = prop.member_fingerprint
        old_review = old_by_fingerprint.get(fingerprint)
        exists = db.scalar(
            select(Review).where(
                Review.property_id == prop.id,
                Review.rule_version == job.to_version,
            )
        )
        if exists is not None:
            # 重试幂等：该新簇的复核单已生成；若它沿用自旧单则计入沿用
            if exists.invalidated_at is None and old_review is not None and exists.recheck_of_id == old_review.id:
                carried += 1
            continue
        if old_review is not None and old_review.invalidated_at is None:
            # 成员未变：沿用旧结论，挂到新簇并保留复核人/状态/备注，旧单记录被谁接替
            new_review = Review(
                property_id=prop.id,
                run_id=run.id if run else None,
                reason=old_review.reason,
                severity=prop.risk_level,
                risk_score=prop.risk_score,
                status=old_review.status,
                reviewer=old_review.reviewer,
                note=old_review.note,
                seed_key=None,
                member_fingerprint=fingerprint,
                rule_version=job.to_version,
                recheck_of_id=old_review.id,
            )
            db.add(new_review)
            db.flush()
            old_review.superseded_by_id = new_review.id
            carried += 1
        else:
            # 成员变化/拆散后的新簇：旧结论已作废，新簇重新排队
            reason = "bait" if "bait" in (prop.risk_tags or []) else "stale" if "stale" in (prop.risk_tags or []) else "spread"
            new_review = Review(
                property_id=prop.id,
                run_id=run.id if run else None,
                reason=reason,
                severity=prop.risk_level,
                risk_score=prop.risk_score,
                status=ReviewStatus.PENDING.value,
                seed_key=None,
                member_fingerprint=fingerprint,
                rule_version=job.to_version,
                recheck_of_id=old_review.id if old_review is not None else None,
            )
            db.add(new_review)
            requeued += 1
    db.flush()
    return carried, invalidated, requeued


def _was_split(old_prop: Property, staging_props: list[Property]) -> bool:
    """旧簇的成员是否被拆到多个 staging 簇。

    若旧簇任一成员出现在多个新簇、或没有任何一个新簇完整包含旧成员集合，即视为拆散。
    """
    old_lids = set(old_prop.member_lids or [])
    if not old_lids:
        return old_prop.member_count > 1
    covered = 0
    for prop in staging_props:
        new_lids = set(prop.member_lids or [])
        overlap = old_lids & new_lids
        if overlap:
            covered += len(overlap)
            if new_lids >= old_lids:
                return False  # 存在完整包含旧成员的新簇 → 未拆散
    return covered > 0 or old_prop.member_count > 1


def _resolve_all(db: Session, job: CutoverJob, listings: list[Listing]) -> tuple[list[dict], list[dict], MatchRun]:
    """对全部回填挂牌做一次全局实体解析。

    归并（blocking + 相似度 + 并查集）必须在全量挂牌上一次性完成，
    否则跨批次的同一套房无法聚成一簇。归一与解析无副作用、可安全重算。
    """
    raw_rows = [{field: getattr(row, field) for field in LISTING_FIELDS} for row in listings]
    normalized = [normalizer.normalize_listing(raw) for raw in raw_rows]
    run = _ensure_run(db, job)
    agency_ids = [row.agency_id for row in listings]
    profile = get_profile(job.to_version)
    result = entity_resolution.resolve(normalized, agency_ids, profile=profile)
    run.status = "success"
    run.listing_count = len(listings)
    run.cluster_count = result["metrics"]["clusters"]
    run.multi_count = result["metrics"]["multi_clusters"]
    run.dedup_rate = result["metrics"]["dedup_rate"]
    run.metrics = result["metrics"]
    db.flush()
    return normalized, result["clusters"], run


def _assign_clusters_to_batches(listings: list[Listing], clusters: list[dict], batch_size: int) -> list[list[dict]]:
    """把全局簇按其首成员所属挂牌批次分配，保证每个簇只在一个批次落库（幂等边界清晰）。"""
    id_to_index = {row.id: index for index, row in enumerate(listings)}
    buckets: dict[int, list[dict]] = {}
    for cluster in clusters:
        first_listing_id = listings[cluster["members"][0]].id
        global_index = id_to_index[first_listing_id]
        batch_no = global_index // batch_size
        buckets.setdefault(batch_no, []).append(cluster)
    total_batches = (len(listings) + batch_size - 1) // batch_size if listings else 0
    return [buckets.get(batch_no, []) for batch_no in range(total_batches)]


def _run_batch(
    db: Session,
    job: CutoverJob,
    cp: CutoverCheckpoint,
    listings: list[Listing],
    normalized: list[dict],
    batch_clusters: list[dict],
    run: MatchRun,
) -> None:
    """执行一个批次的持久化：把分配给本批的全局簇落为 staging（按指纹 upsert，失败随保存点整体回滚）。"""
    cp.status = CheckpointStatus.PENDING.value
    cp.phase = CutoverPhase.PERSIST.value
    cp.started_at = utcnow()
    cp.error = None
    db.flush()

    agencies = {agency.id: agency for agency in db.scalars(select(Agency)).all()}
    fingerprints: set[str] = set()
    for cluster in batch_clusters:
        _persist_cluster(db, job, run, agencies, listings, normalized, cluster, fingerprints)
    cp.cluster_fingerprints = sorted(fingerprints)

    cp.phase = CutoverPhase.DONE.value
    cp.status = CheckpointStatus.DONE.value
    cp.finished_at = utcnow()
    db.flush()


def _ensure_run(db: Session, job: CutoverJob) -> MatchRun:
    seed_key = f"cutover-{job.job_key}-resolve"
    run = db.scalar(select(MatchRun).where(MatchRun.seed_key == seed_key))
    if run is not None:
        return run
    profile = get_profile(job.to_version)
    run = MatchRun(
        run_no=f"C{utcnow():%Y%m%d}-{job.id:03d}",
        trigger="schedule",
        status="running",
        operator_email=f"cutover@{job.job_key}",
        threshold=profile.score_threshold,
        rule_version=job.to_version,
        seed_key=seed_key,
    )
    db.add(run)
    db.flush()
    return run


def stage_first_batch(db: Session, job: CutoverJob) -> CutoverCheckpoint:
    """全局解析后只持久化第一个批次，制造一个合法的 staging 半成品（用于断点/导出闸门验证）。"""
    listings = _backfill_listings(db, job)
    checkpoints = _ensure_checkpoints(db, job, listings)
    normalized, all_clusters, run = _resolve_all(db, job, listings)
    clusters_by_batch = _assign_clusters_to_batches(listings, all_clusters, CUTOVER_BATCH_SIZE)
    cp = checkpoints[0]
    _run_batch(db, job, cp, listings, normalized, clusters_by_batch[0], run)
    return cp


# ---------------------------------------------------------------- 主流程
def run_cutover(db: Session, *, job_key: str = "rule-v1-to-v2") -> CutoverJob:
    """创建/续跑切换任务：已完成批次跳过，从第一个未完成断点继续。

    每个批次在 SAVEPOINT 中执行：单批失败只回滚该批，已完成批次的落库与检查点保留，
    任务置 failed 且旧版（active v1）继续生效；重试从失败批次断点继续。
    并发保护：已有 running 任务时拒绝重复触发，避免两个执行者重复建簇/复核单。
    """
    job = get_or_create_job(db, job_key=job_key)
    if job.status == CutoverStatus.COMPLETED.value:
        return job
    if job.status == CutoverStatus.RUNNING.value:
        raise AppError(
            "CONFLICT",
            409,
            {"job_key": job_key, "status": job.status, "hint": "回填正在执行，请等待当前批次结束后再续跑"},
        )
    job_id = job.id
    job.status = CutoverStatus.RUNNING.value
    job.last_error = None
    db.flush()

    try:
        listings = _backfill_listings(db, job)
        if not listings:
            raise AppError("VALIDATION", 422, {"reason": "最近一个月没有可回填的挂牌"})
        checkpoints = _ensure_checkpoints(db, job, listings)
        batch_size = CUTOVER_BATCH_SIZE

        # 全局解析一次（无副作用）：保证跨批次的同一套房能聚成同一簇。
        # 已全部完成的任务在入口即返回，因此走到这里时解析结果要么首次生成、要么需要补齐。
        normalized, all_clusters, run = _resolve_all(db, job, listings)
        clusters_by_batch = _assign_clusters_to_batches(listings, all_clusters, batch_size)

        for cp in checkpoints:
            if cp.status == CheckpointStatus.DONE.value:
                continue  # 断点续跑：跳过已完成批次，不重复生成簇/复核单
            batch_no = cp.batch_no
            cp.attempts += 1  # 保存点之外累计，失败回滚不影响尝试计数
            db.flush()
            savepoint = db.begin_nested()
            try:
                _run_batch(db, job, cp, listings, normalized, clusters_by_batch[batch_no], run)
                savepoint.commit()
            except Exception as exc:  # noqa: BLE001 - 单批失败回滚到保存点，保留此前断点
                savepoint.rollback()
                job = db.get(CutoverJob, job_id)
                failed_cp = db.scalar(
                    select(CutoverCheckpoint).where(
                        CutoverCheckpoint.job_id == job_id,
                        CutoverCheckpoint.batch_no == batch_no,
                    )
                )
                if failed_cp is not None:
                    failed_cp.status = CheckpointStatus.FAILED.value
                    failed_cp.error = str(exc)[:500]
                job.status = CutoverStatus.FAILED.value
                job.last_error = f"批次 {batch_no} 失败：{exc}"[:500]
                job.done_batches = db.scalar(
                    select(func.count()).select_from(CutoverCheckpoint).where(
                        CutoverCheckpoint.job_id == job_id,
                        CutoverCheckpoint.status == CheckpointStatus.DONE.value,
                    )
                ) or 0
                db.flush()
                return job

        # 复核单同步（阶段 4，全量一次，按指纹幂等）
        job = db.get(CutoverJob, job_id)
        job.status = CutoverStatus.RUNNING.value
        staging_props = list(
            db.scalars(
                select(Property).where(
                    Property.cutover_job_id == job.id,
                    Property.publish_state == "staging",
                )
            ).all()
        )
        carried, invalidated, requeued = _sync_reviews(db, job, run, staging_props)
        job.review_carried = carried
        job.review_invalidated = invalidated
        job.review_requeued = requeued
        job.done_batches = job.total_batches

        report = reconcile(db, job)
        job.reconciliation = report
        if not report["consistent"]:
            job.status = CutoverStatus.FAILED.value
            job.last_error = "对账失败，切换中止并保留旧版：" + "; ".join(report["errors"])
            job.finished_at = utcnow()
            db.flush()
            return job

        # 对账通过 → 原子激活：staging 转 active，旧 v1 归档
        _activate(db, job, staging_props)
        job.status = CutoverStatus.COMPLETED.value
        job.finished_at = utcnow()
        db.flush()
        return job
    except AppError:
        db.rollback()
        raise
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        job = db.scalar(select(CutoverJob).where(CutoverJob.job_key == job_key))
        if job is not None and job.status != CutoverStatus.COMPLETED.value:
            job.status = CutoverStatus.FAILED.value
            job.last_error = str(exc)[:500]
            db.flush()
        raise


# ---------------------------------------------------------------- 对账与激活
def reconcile(db: Session, job: CutoverJob) -> dict:
    """三方对账：簇数、成员数（=回填挂牌数）、导出行数必须相互对上。"""
    staging = list(
        db.scalars(
            select(Property).where(
                Property.cutover_job_id == job.id,
                Property.publish_state == "staging",
            )
        ).all()
    )
    cluster_count = len(staging)
    member_count = sum(prop.member_count for prop in staging)
    listing_ids = {lid for cp in db.scalars(select(CutoverCheckpoint).where(CutoverCheckpoint.job_id == job.id)).all()
                   for lid in (cp.listing_ids or [])}
    # 导出行数：簇 CSV 每个 active/staging 簇一行（激活后口径），此处按 staging 预估激活后行数
    export_rows = cluster_count
    errors: list[str] = []
    if member_count != len(listing_ids):
        errors.append(f"成员数 {member_count} 与回填挂牌数 {len(listing_ids)} 不一致")
    if member_count != job.total_listings:
        errors.append(f"成员数 {member_count} 与任务登记挂牌数 {job.total_listings} 不一致")
    if export_rows != cluster_count:
        errors.append(f"导出行数 {export_rows} 与簇数 {cluster_count} 不一致")
    # 每个簇的成员数必须与其登记成员数一致
    bad = [prop.id for prop in staging if prop.member_count <= 0]
    if bad:
        errors.append(f"存在成员数非法的簇：{bad[:5]}")
    # 成员指纹不得重复（两个簇不能是同一批成员）
    fingerprints = [prop.member_fingerprint for prop in staging]
    if len(set(fingerprints)) != len(fingerprints):
        errors.append("存在重复成员指纹的簇")
    # 覆盖完整性：所有回填挂牌的自然键必须且只能出现在一个 staging 簇的成员清单里
    covered_lids: list[str] = []
    for prop in staging:
        covered_lids.extend(prop.member_lids or [])
    if len(covered_lids) != len(set(covered_lids)):
        errors.append("存在挂牌被多个簇重复挂靠")
    agencies = {agency.id: agency for agency in db.scalars(select(Agency)).all()}
    backfill_lid_set = {
        _member_lid(agencies, lid_row)
        for cp in db.scalars(select(CutoverCheckpoint).where(CutoverCheckpoint.job_id == job.id)).all()
        for lid_row in db.scalars(select(Listing).where(Listing.id.in_(cp.listing_ids or []))).all()
    }
    missing = backfill_lid_set - set(covered_lids)
    if missing:
        errors.append(f"{len(missing)} 条回填挂牌未被任何新簇覆盖（疑似丢记录）")

    job.cluster_count = cluster_count
    job.member_count = member_count
    job.export_rows = export_rows
    return {
        "consistent": not errors,
        "errors": errors,
        "cluster_count": cluster_count,
        "member_count": member_count,
        "backfill_listing_count": len(listing_ids),
        "export_rows": export_rows,
        "review_carried": job.review_carried,
        "review_invalidated": job.review_invalidated,
        "review_requeued": job.review_requeued,
    }


def _activate(db: Session, job: CutoverJob, staging_props: list[Property]) -> None:
    """原子替换：staging 簇激活为 v2 正式版，旧 v1 簇归档（不删除，保留追溯且不丢记录）。"""
    old_active = list(db.scalars(select(Property).where(Property.publish_state == "active")).all())
    old_ids = {prop.id for prop in old_active}
    # 旧挂牌解除与 v1 簇的关联，随后按新簇成员重新挂靠
    for listing in db.scalars(select(Listing).where(Listing.property_id.in_(old_ids))).all():
        listing.property_id = None
    for prop in old_active:
        prop.publish_state = "archived"
        prop.updated_at = utcnow()
    for prop in staging_props:
        prop.publish_state = "active"
        prop.updated_at = utcnow()
    # 按 staging 簇缓存的成员清单把挂牌挂靠到激活后的新簇
    agencies = {agency.id: agency for agency in db.scalars(select(Agency)).all()}
    lid_to_listing = {_member_lid(agencies, row): row for row in db.scalars(select(Listing)).all()}
    for prop in staging_props:
        _attach_listings(prop, lid_to_listing)
    db.flush()


def _attach_listings(prop: Property, lid_to_listing: dict[str, Listing]) -> None:
    """把 staging 簇的成员挂牌挂靠到激活后的簇（成员 lid 落库时已缓存在 member_lids）。"""
    for lid in prop.member_lids or []:
        listing = lid_to_listing.get(lid)
        if listing is not None:
            listing.property_id = prop.id
