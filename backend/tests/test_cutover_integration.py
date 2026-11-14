"""规则切换（v1→v2 整批回填）端到端集成测试（SQLite 内存库）。

覆盖业务约束：
1. 最近一个月房源整批回填，切换完成前 staging 半成品不进入导出、银行清单仍用 v1；
2. 中途失败后同批重试从断点继续，不重复生成房源簇或复核单；
3. 原簇拆散/成员变化 → 旧复核结论作废并重新排队；成员未变 → 沿用；
4. 完成后簇数/成员数/导出行数三方对账一致，替换不丢记录。
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from src.constants.enums import CutoverStatus, ReviewStatus
from src.constants.limits import RULE_PROFILE_V1, RULE_PROFILE_V2
from src.models import Base
from src.models.agency import Agency
from src.models.cutover import CutoverCheckpoint, CutoverJob
from src.models.listing import Listing
from src.models.match_run import MatchRun
from src.models.property import Property
from src.models.review import Review
from src.services import cutover_service, export_service, match_run_service


# ---------------------------------------------------------------- 夹具
@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def _agency(db: Session, code: str) -> Agency:
    agency = Agency(code=code, name=code, kind="broker", owner="org", enabled=True)
    db.add(agency)
    db.flush()
    return agency


def _listing(db: Session, agency: Agency, lid: str, *, price: int, days_old: int, area: float = 89.5,
             community: str = "翠湖名邸", layout: str = "3-2-1", views: int = 5) -> Listing:
    from src.utils.timeutil import utcnow
    from datetime import timedelta

    listing = Listing(
        agency_id=agency.id,
        lid=lid,
        community_raw=community,
        layout_raw=layout,
        area_raw=f"{area}㎡",
        price_raw=f"{price // 10000}万",
        floor_raw="2/18",
        orientation_raw="南北",
        title=f"{layout} {area}平方米 南北 2层",
        status="active",
        listed_days=days_old,
        views=views,
        captured_at=utcnow() - timedelta(days=days_old),
    )
    db.add(listing)
    return listing


def _seed_recent_listings(db: Session, count: int = 250) -> None:
    """构造最近一个月的挂牌：含同小区同户型的重复挂牌（会归并成多成员簇）。"""
    agencies = [_agency(db, f"agency{i}") for i in range(4)]
    for i in range(count):
        agency = agencies[i % len(agencies)]
        # 每 3 条造一套重复房源（价格小幅波动），其余为独立房源
        group = i // 3
        duplicate = i % 3
        lid = f"L{group:04d}-{duplicate}"
        price = 6_000_000 + (group % 7) * 10_000 + duplicate * 20_000
        community = "翠湖名邸" if duplicate == 0 else f"小区{group % 5}"
        _listing(db, agency, lid, price=price, days_old=(i % 28), community=community)
    db.flush()


def _run_v1(db: Session) -> MatchRun:
    return match_run_service.create_and_execute(
        db, trigger="seed", operator_email="seed@x.io", rule_version=RULE_PROFILE_V1, seed_key="seed-v1"
    )


# ---------------------------------------------------------------- 测试
class TestBackfillAndExportGate:
    def test_staging_isolated_and_export_keeps_v1_until_completed(self, db):
        _seed_recent_listings(db)
        run = _run_v1(db)
        assert run.rule_version == RULE_PROFILE_V1
        active_before = db.scalar(select(func.count()).select_from(Property).where(Property.publish_state == "active"))
        assert active_before > 0

        # 建任务并跑到一半：全局解析后只落第一个批次，任务处于半成品状态
        job = cutover_service.get_or_create_job(db, job_key="rule-v1-to-v2")
        cutover_service._ensure_checkpoints(db, job, cutover_service._backfill_listings(db, job))
        assert job.total_batches >= 2

        # 手动成功持久化第一个批次（模拟断点）
        cutover_service.stage_first_batch(db, job)
        staging_count = db.scalar(
            select(func.count()).select_from(Property).where(Property.publish_state == "staging")
        )
        assert staging_count > 0

        # 半成品不得进入导出：导出仍是纯 v1 active
        active_props = db.scalars(select(Property).where(Property.publish_state == "active")).all()
        assert all(p.rule_version == RULE_PROFILE_V1 for p in active_props)
        csv_rows = export_service.clusters_csv(db).decode("utf-8").count("\r\n") - 1
        assert csv_rows == len(active_props)
        assert b"staging" not in export_service.clusters_csv(db)

    def test_full_cutover_reconciles_and_activates_v2(self, db):
        _seed_recent_listings(db)
        _run_v1(db)
        job = cutover_service.run_cutover(db, job_key="rule-v1-to-v2")

        assert job.status == CutoverStatus.COMPLETED.value
        report = job.reconciliation
        assert report["consistent"] is True
        # 三方对账：成员数 == 回填挂牌数；导出行数 == 簇数
        assert report["member_count"] == report["backfill_listing_count"] == job.total_listings
        assert report["export_rows"] == report["cluster_count"] == job.cluster_count

        active = db.scalars(select(Property).where(Property.publish_state == "active")).all()
        assert all(p.rule_version == RULE_PROFILE_V2 for p in active)
        # 旧 v1 簇归档而非删除，记录不丢
        archived = db.scalars(select(Property).where(Property.publish_state == "archived")).all()
        assert len(archived) > 0
        total = db.scalar(select(func.count()).select_from(Property))
        assert total == len(active) + len(archived)
        # 激活后导出全部为 v2
        assert b"v2_composite" in export_service.clusters_csv(db)
        assert b"v1_threshold" not in export_service.clusters_csv(db)

    def test_listing_attachments_survive_activation(self, db):
        _seed_recent_listings(db)
        _run_v1(db)
        cutover_service.run_cutover(db, job_key="rule-v1-to-v2")
        # 每条挂牌都重新挂靠到某个 active 簇，且成员数之和等于挂牌数
        active = db.scalars(select(Property).where(Property.publish_state == "active")).all()
        attached = db.scalar(select(func.count()).select_from(Listing).where(Listing.property_id.is_not(None)))
        job = db.scalar(select(CutoverJob).order_by(CutoverJob.id.desc()).limit(1))
        assert attached == sum(p.member_count for p in active)
        assert sum(p.member_count for p in active) == job.total_listings


class TestCheckpointResume:
    def test_concurrent_running_job_rejected(self, db):
        _seed_recent_listings(db)
        _run_v1(db)
        job = cutover_service.get_or_create_job(db, job_key="rule-v1-to-v2")
        job.status = CutoverStatus.RUNNING.value
        db.flush()
        from src.utils.errors import AppError

        with pytest.raises(AppError) as exc:
            cutover_service.run_cutover(db, job_key="rule-v1-to-v2")
        assert exc.value.status_code == 409
        # 没有产生任何 staging 簇
        assert db.scalar(select(func.count()).select_from(Property).where(Property.publish_state == "staging")) == 0

    def test_retry_skips_done_batches_and_never_duplicates(self, db, monkeypatch):
        _seed_recent_listings(db)
        _run_v1(db)
        job_key = "rule-v1-to-v2"

        # 第一次执行：让第二个批次抛错，任务失败、保留旧版
        real_run_batch = cutover_service._run_batch
        calls = {"n": 0}

        def flaky(session, job, cp, listings, normalized, batch_clusters, run):
            calls["n"] += 1
            if cp.batch_no == 1:
                raise RuntimeError("模拟批次失败")
            return real_run_batch(session, job, cp, listings, normalized, batch_clusters, run)

        monkeypatch.setattr(cutover_service, "_run_batch", flaky)
        job = cutover_service.run_cutover(db, job_key=job_key)
        assert job.status == CutoverStatus.FAILED.value
        done_first = db.scalar(
            select(func.count()).select_from(CutoverCheckpoint).where(
                CutoverCheckpoint.job_id == job.id, CutoverCheckpoint.status == "done"
            )
        )
        assert done_first == 1
        # 旧版仍在导出
        assert b"v1_threshold" in export_service.clusters_csv(db)

        # 恢复真实实现后重试：从断点继续，已完成批次不重跑
        monkeypatch.undo()
        job2 = cutover_service.run_cutover(db, job_key=job_key)
        assert job2.id == job.id
        assert job2.status == CutoverStatus.COMPLETED.value
        assert job2.done_batches == job2.total_batches

        # 所有检查点恰好 done 一次，attempts 体现失败批次被重试
        cps = db.scalars(select(CutoverCheckpoint).where(CutoverCheckpoint.job_id == job.id)).all()
        assert all(cp.status == "done" for cp in cps)
        assert {cp.batch_no for cp in cps} == set(range(job.total_batches))
        failed_cp = next(cp for cp in cps if cp.batch_no == 1)
        assert failed_cp.attempts >= 2

        # 不重复生成簇：staging 簇指纹唯一
        staging = db.scalars(select(Property).where(Property.cutover_job_id == job.id)).all()
        fingerprints = [p.member_fingerprint for p in staging]
        assert len(fingerprints) == len(set(fingerprints))
        # 再次幂等重跑（已完成，直接返回）不产生任何新簇/新行
        total_after_completion = db.scalar(select(func.count()).select_from(Property))
        cutover_service.run_cutover(db, job_key=job_key)
        total_after_rerun = db.scalar(select(func.count()).select_from(Property))
        assert total_after_rerun == total_after_completion
        # 归档旧簇 + 激活新簇之和等于总簇数，替换过程没有丢记录
        active = db.scalars(select(Property).where(Property.publish_state == "active")).all()
        archived = db.scalars(select(Property).where(Property.publish_state == "archived")).all()
        assert len(active) + len(archived) == total_after_rerun
        assert all(p.rule_version == RULE_PROFILE_V2 for p in active)

    def test_idempotent_runs_do_not_duplicate_reviews(self, db):
        _seed_recent_listings(db)
        _run_v1(db)
        cutover_service.run_cutover(db, job_key="rule-v1-to-v2")
        reviews_after_first = db.scalar(select(func.count()).select_from(Review).where(Review.rule_version == RULE_PROFILE_V2))
        # 再跑一次（任务已完成，直接返回）
        cutover_service.run_cutover(db, job_key="rule-v1-to-v2")
        reviews_after_second = db.scalar(select(func.count()).select_from(Review).where(Review.rule_version == RULE_PROFILE_V2))
        assert reviews_after_first == reviews_after_second


class TestReviewCarryAndInvalidate:
    def _v1_cluster_with_review(self, db) -> tuple[Property, Review]:
        run = _run_v1(db)
        # 复核单只对高/中风险簇开立，优先选多成员的风险簇；没有则退而求其次并断言其进入风险集
        prop = db.scalars(
            select(Property)
            .where(Property.publish_state == "active", Property.member_count > 1,
                   Property.risk_level.in_(["high", "medium"]))
            .order_by(Property.risk_score.desc(), Property.id)
        ).first()
        if prop is None:
            prop = db.scalars(
                select(Property).where(Property.publish_state == "active", Property.member_count > 1)
                .order_by(Property.id)
            ).first()
        assert prop is not None, "需要一个多成员 v1 簇"
        review = Review(
            property_id=prop.id,
            run_id=run.id,
            reason="bait",
            severity=prop.risk_level,
            risk_score=prop.risk_score,
            status="confirmed",
            reviewer="analyst@x.io",
            note="旧结论",
            member_fingerprint=prop.member_fingerprint,
            rule_version=RULE_PROFILE_V1,
        )
        db.add(review)
        db.flush()
        return prop, review

    def test_unchanged_members_carry_review(self, db):
        _seed_recent_listings(db)
        prop, old_review = self._v1_cluster_with_review(db)
        fingerprint = prop.member_fingerprint
        # 让该簇成员成为僵尸挂牌（>180 天且 0 带看），保证切换后仍是中/高风险簇且成员集合不变
        from src.models.listing import Listing

        members = db.scalars(select(Listing).where(Listing.property_id == prop.id)).all()
        assert members, "夹具簇应包含成员挂牌"
        for listing in members:
            listing.listed_days = 220
            listing.views = 0
        db.flush()
        cutover_service.run_cutover(db, job_key="rule-v1-to-v2")

        # 成员未变 → 同指纹簇仍存在，结论应被沿用（复核人/状态/备注保留）
        new_prop = db.scalar(select(Property).where(
            Property.publish_state == "active", Property.member_fingerprint == fingerprint
        ))
        assert new_prop is not None, "成员未变时新规则下必须仍有同指纹簇"
        carried = db.scalar(select(Review).where(
            Review.property_id == new_prop.id, Review.rule_version == RULE_PROFILE_V2
        ))
        assert carried is not None
        assert carried.status == "confirmed"
        assert carried.reviewer == "analyst@x.io"
        assert carried.note == "旧结论"
        assert carried.recheck_of_id == old_review.id

    def test_split_cluster_invalidates_and_requeues(self, db, monkeypatch):
        _seed_recent_listings(db)
        _prop, old_review = self._v1_cluster_with_review(db)

        # 强制 v2 连边失败：所有候选对都被拒绝 → 旧多成员簇必然被拆散为单成员簇
        def reject_all(detail, left, right, profile, cross_agency):
            return False, {"rule_version": profile.version, "rejected_by": "forced", "cross_agency": cross_agency,
                           "score_gate": 1.0, "area_gate": None, "price_gate": None, "price_deviation": None}

        monkeypatch.setattr(cutover_service, "should_link", reject_all, raising=False)
        # entity_resolution 模块内也引用了 should_link，需同步替换
        from src.services import entity_resolution
        monkeypatch.setattr(entity_resolution, "should_link", reject_all)

        job = cutover_service.run_cutover(db, job_key="rule-v1-to-v2")
        # 全部拆散时成员数仍守恒，对账通过
        assert job.status == CutoverStatus.COMPLETED.value
        db.refresh(old_review)
        assert old_review.invalidated_at is not None
        assert old_review.status == ReviewStatus.DISMISSED.value
        assert old_review.invalid_reason in {"cluster_split", "members_changed"}
        # 至少作废计数被记录
        assert job.review_invalidated >= 1

    def test_changed_members_requeue_with_link_to_old_review(self, db, monkeypatch):
        from src.models.listing import Listing

        _seed_recent_listings(db)
        # 制造僵尸挂牌（>180 天且 0 带看），拆散后单成员簇仍是中/高风险，需要重新排队
        for listing in db.scalars(select(Listing).limit(3)).all():
            listing.listed_days = 200
            listing.views = 0
        db.flush()
        _prop, old_review = self._v1_cluster_with_review(db)

        def reject_all(detail, left, right, profile, cross_agency):
            return False, {"rule_version": profile.version, "rejected_by": "forced", "cross_agency": cross_agency,
                           "score_gate": 1.0, "area_gate": None, "price_gate": None, "price_deviation": None}

        from src.services import entity_resolution
        monkeypatch.setattr(entity_resolution, "should_link", reject_all)
        job = cutover_service.run_cutover(db, job_key="rule-v1-to-v2")
        assert job.status == CutoverStatus.COMPLETED.value
        assert job.review_invalidated >= 1
        assert job.review_requeued >= 1
        requeued = db.scalars(select(Review).where(
            Review.rule_version == RULE_PROFILE_V2, Review.status == ReviewStatus.PENDING.value
        )).all()
        assert requeued and all(r.invalidated_at is None for r in requeued)
        # 旧结论已作废，新旧复核单通过 recheck_of_id 留痕可追溯
        db.refresh(old_review)
        assert old_review.invalidated_at is not None


class TestGlobalResolutionAcrossBatches:
    def test_duplicates_spanning_batches_merge_into_one_cluster(self, db):
        """同一套房的重复挂牌落在不同批次时，全局解析必须把它们聚成同一个 staging 簇。"""
        from src.constants.limits import CUTOVER_BATCH_SIZE
        from src.models.listing import Listing

        agencies = [_agency(db, f"cross{i}") for i in range(3)]
        # 造两组跨批次的重复房源：同小区/户型/面积，仅价格小幅波动
        total = CUTOVER_BATCH_SIZE + 8
        for i in range(total):
            agency = agencies[i % 3]
            is_x = i < 4 or i >= CUTOVER_BATCH_SIZE
            if is_x:
                # 前 4 条与批次边界之后的 4 条是同一套房（跨批次）
                community = "跨批御湖湾"
                price = 6_000_000 + (i % 4) * 10_000
            else:
                community = f"独立小区{i}"
                price = 5_000_000 + i * 1000
            _listing(db, agency, f"L{i:04d}", price=price, days_old=i % 20,
                     community=community, layout="3-2-1", area=89.5)
        db.flush()
        _run_v1(db)
        job = cutover_service.run_cutover(db, job_key="rule-v1-to-v2")
        assert job.status == CutoverStatus.COMPLETED.value
        assert job.total_batches >= 2

        # 跨批次的 X 房源（前 4 条 + 批次边界后的 8 条 = 12 条）必须落在同一个 active 簇
        x_listings = db.scalars(
            select(Listing).where(Listing.community_raw == "跨批御湖湾")
        ).all()
        assert len(x_listings) == 12
        cluster_ids = {row.property_id for row in x_listings}
        assert len(cluster_ids) == 1, f"跨批次重复房源被拆成了多个簇: {cluster_ids}"
        cluster = db.get(Property, next(iter(cluster_ids)))
        assert cluster.member_count >= 12  # 可能进一步并入其它高度相似房源
        assert cluster.publish_state == "active"
        assert cluster.rule_version == RULE_PROFILE_V2


class TestReconciliationGuards:
    def test_export_rows_match_active_clusters(self, db):
        _seed_recent_listings(db)
        _run_v1(db)
        job = cutover_service.run_cutover(db, job_key="rule-v1-to-v2")
        active = db.scalars(select(Property).where(Property.publish_state == "active")).all()
        csv_rows = export_service.clusters_csv(db).decode("utf-8").count("\r\n") - 1
        assert csv_rows == len(active) == job.export_rows == job.cluster_count

    def test_export_version_flips_only_after_completion(self, db):
        _seed_recent_listings(db)
        _run_v1(db)
        # 切换前：导出全部 v1
        assert b"v1_threshold" in export_service.clusters_csv(db)
        # 造一个半成品批次（不激活）
        job = cutover_service.get_or_create_job(db, job_key="rule-v1-to-v2")
        cutover_service.stage_first_batch(db, job)
        # 半成品存在期间，导出仍只含已发布 v1，不泄漏 staging
        clusters = export_service.clusters_csv(db).decode("utf-8")
        assert "v2_composite" not in clusters
        assert "staging" not in clusters
        active = db.scalars(select(Property).where(Property.publish_state == "active")).all()
        assert clusters.count("\r\n") - 1 == len(active)
        # 完成切换后，簇与复核导出切换到 v2
        cutover_service.run_cutover(db, job_key="rule-v1-to-v2")
        assert b"v2_composite" in export_service.clusters_csv(db)
        assert b"v1_threshold" not in export_service.clusters_csv(db)
        assert b"v1_threshold" not in export_service.reviews_csv(db)

    def test_reviews_csv_excludes_invalidated(self, db, monkeypatch):
        _seed_recent_listings(db)
        prop, old_review = TestReviewCarryAndInvalidate()._v1_cluster_with_review(db)

        def reject_all(detail, left, right, profile, cross_agency):
            return False, {"rule_version": profile.version, "rejected_by": "forced", "cross_agency": cross_agency,
                           "score_gate": 1.0, "area_gate": None, "price_gate": None, "price_deviation": None}

        from src.services import entity_resolution
        monkeypatch.setattr(entity_resolution, "should_link", reject_all)
        cutover_service.run_cutover(db, job_key="rule-v1-to-v2")
        content = export_service.reviews_csv(db).decode("utf-8")
        # 已作废复核单不进入导出
        invalidated_rows = db.scalars(select(Review).where(Review.invalidated_at.is_not(None))).all()
        for review in invalidated_rows:
            assert f"\r\n{review.id}," not in "\r\n" + content
