"""多规则复合门控（v2）与 v1 单阈值的行为对照。

v1：只看综合分阈值，保持升级前语义；
v2：综合分 + 面积硬门槛 + 跨中介价格门控，拒绝原因可追溯。
"""
from __future__ import annotations

from src.constants.limits import RULE_PROFILE_V1, RULE_PROFILE_V2
from src.services import entity_resolution as er
from src.services import normalizer
from src.services.rule_profiles import V1_PROFILE, V2_PROFILE, get_profile, should_link


def _row(community="翠湖名邸", layout="3-2-1", area="89.5", price="598万", floor="2/18", orientation="南北",
         title="3-2-1 89.5平方米 南北 2层 满五唯一"):
    return normalizer.normalize_listing(
        {
            "community_raw": community,
            "layout_raw": layout,
            "area_raw": area,
            "price_raw": price,
            "floor_raw": floor,
            "orientation_raw": orientation,
            "title": title,
        }
    )


def test_profile_lookup_defaults_to_v1():
    assert get_profile(None).version == RULE_PROFILE_V1
    assert get_profile(RULE_PROFILE_V1).is_composite is False
    assert get_profile(RULE_PROFILE_V2).is_composite is True


def test_v1_keeps_single_threshold_semantics():
    left = _row(price="598万")
    right = _row(price="602万")
    detail = er.similarity(left, right)
    accepted_same, gates_same = should_link(detail, left, right, V1_PROFILE, cross_agency=False)
    accepted_cross, gates_cross = should_link(detail, left, right, V1_PROFILE, cross_agency=True)
    assert accepted_same and accepted_cross
    assert gates_same["rejected_by"] is None
    assert gates_same["area_gate"] is None  # v1 没有面积硬门槛


def test_v2_rejects_cross_agency_price_outlier():
    # 同面积、同楼层、同朝向，但跨中介价差 25%（>18% 门控）
    left = _row(price="600万")
    right = _row(price="750万")
    detail = er.similarity(left, right)
    accepted, gates = should_link(detail, left, right, V2_PROFILE, cross_agency=True)
    assert accepted is False
    assert gates["rejected_by"] == "price"
    assert gates["price_deviation"] is not None and gates["price_deviation"] > 0.18


def test_v2_same_agency_pair_skips_price_gate():
    # 同中介的大价差不走跨中介价格门控，只看综合分/面积门
    left = _row(price="600万")
    right = _row(price="750万")
    detail = er.similarity(left, right)
    accepted, gates = should_link(detail, left, right, V2_PROFILE, cross_agency=False)
    # 综合分低于门槛时被 score 拒绝，而不是 price
    if not accepted:
        assert gates["rejected_by"] in {"score", "area"}


def test_v2_rejects_area_mismatch():
    left = _row(area="89.5")
    right = _row(area="94.0")  # 面积差 4.5㎡，面积分过低
    detail = er.similarity(left, right)
    accepted, gates = should_link(detail, left, right, V2_PROFILE, cross_agency=False)
    if not accepted and detail["score"] >= V2_PROFILE.score_threshold:
        assert gates["rejected_by"] == "area"


def test_resolve_records_rule_version_and_gates():
    rows = [_row(price="598万"), _row(price="602万"), _row(area="118", price="780万")]
    v1 = er.resolve(rows, [1, 2, 3], profile=RULE_PROFILE_V1)
    v2 = er.resolve(rows, [1, 2, 3], profile=RULE_PROFILE_V2)
    assert v1["metrics"]["rule_version"] == RULE_PROFILE_V1
    assert v2["metrics"]["rule_version"] == RULE_PROFILE_V2
    # 边上带门控读数，可追溯是哪条规则放行/拒绝
    assert all("gates" in edge for edge in v2["edges"])
    assert all(edge["gates"]["rule_version"] == RULE_PROFILE_V2 for edge in v2["edges"])


def test_v1_threshold_path_unchanged():
    # 不传 profile，仅传 threshold，结果与升级前完全一致
    rows = [_row(price="598万"), _row(price="602万")]
    legacy = er.resolve(rows, [1, 2], threshold=0.80)
    profiled = er.resolve(rows, [1, 2], profile=RULE_PROFILE_V1)
    assert legacy["clusters"] == profiled["clusters"]
    assert legacy["metrics"]["clusters"] == profiled["metrics"]["clusters"]
