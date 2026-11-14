"""归并规则档案：v1 单一相似度阈值 → v2 多规则复合门控。

- v1（``v1_threshold``）：综合相似度 ≥ 单一阈值即连边，行为与升级前逐字节一致；
- v2（``v2_composite``）：在加权相似度之外叠加三个硬门控——
  1. 面积分硬门槛（面积对不上直接不连，防止不同面积房源被其它字段带偏）；
  2. 跨中介价格合理性门控（成对价格偏离超过 18% 不连，拦截钓鱼价制造的假同簇）；
  3. 综合分门槛（同中介 0.82 / 跨中介 0.80，显式区分信息来源）。

纯数据 + 纯函数，不访问数据库与网络；同一输入 + 同一档案两次执行结果完全相同。
"""
from __future__ import annotations

from dataclasses import dataclass

from src.constants.limits import (
    RULE_PROFILE_V1,
    RULE_PROFILE_V2,
    SIMILARITY_THRESHOLD,
    V2_AREA_SCORE_MIN,
    V2_CROSS_AGENCY_SCORE_MIN,
    V2_PRICE_DEVIATION_LIMIT,
    V2_SAME_AGENCY_SCORE_MIN,
)


@dataclass(frozen=True)
class RuleProfile:
    """一套可执行的连边规则。"""

    version: str
    score_threshold: float
    area_score_min: float | None = None
    price_deviation_limit: float | None = None
    cross_agency_score_min: float | None = None

    @property
    def is_composite(self) -> bool:
        return self.version == RULE_PROFILE_V2


V1_PROFILE = RuleProfile(
    version=RULE_PROFILE_V1,
    score_threshold=SIMILARITY_THRESHOLD,
)

V2_PROFILE = RuleProfile(
    version=RULE_PROFILE_V2,
    score_threshold=V2_SAME_AGENCY_SCORE_MIN,
    area_score_min=V2_AREA_SCORE_MIN,
    price_deviation_limit=V2_PRICE_DEVIATION_LIMIT,
    cross_agency_score_min=V2_CROSS_AGENCY_SCORE_MIN,
)

PROFILES: dict[str, RuleProfile] = {
    RULE_PROFILE_V1: V1_PROFILE,
    RULE_PROFILE_V2: V2_PROFILE,
}


def get_profile(version: str | None) -> RuleProfile:
    """按版本号取档案；缺省/未知版本回退 v1，保证旧调用方行为不变。"""
    if not version:
        return V1_PROFILE
    profile = PROFILES.get(version)
    if profile is None:
        raise ValueError(f"未知归并规则版本: {version}")
    return profile


def _pair_price_deviation(left: dict, right: dict) -> float | None:
    left_price = left.get("price_num")
    right_price = right.get("price_num")
    if not left_price or not right_price:
        return None
    base = (abs(left_price) + abs(right_price)) / 2.0
    if base <= 0:
        return None
    return abs(left_price - right_price) / base


def should_link(detail: dict, left: dict, right: dict, profile: RuleProfile, cross_agency: bool) -> tuple[bool, dict]:
    """按规则档案判定一对候选是否连边。

    返回 ``(是否连边, 门控读数)``；门控读数原样挂到边上，供页面与导出追溯。
    v1 只看综合分阈值，保持升级前语义。
    """
    gates: dict = {
        "rule_version": profile.version,
        "cross_agency": cross_agency,
        "score_gate": round(profile.cross_agency_score_min if cross_agency and profile.cross_agency_score_min
                            else profile.score_threshold, 4),
        "area_gate": profile.area_score_min,
        "price_gate": profile.price_deviation_limit,
        "price_deviation": None,
        "rejected_by": None,
    }

    score_floor = gates["score_gate"]
    if detail["score"] < score_floor:
        gates["rejected_by"] = "score"
        return False, gates

    if not profile.is_composite:
        return True, gates

    # ---- v2 多规则硬门控 ----
    if profile.area_score_min is not None and detail["area"] < profile.area_score_min:
        gates["rejected_by"] = "area"
        return False, gates

    if profile.price_deviation_limit is not None and cross_agency:
        deviation = _pair_price_deviation(left, right)
        gates["price_deviation"] = round(deviation, 4) if deviation is not None else None
        if deviation is not None and deviation > profile.price_deviation_limit:
            gates["rejected_by"] = "price"
            return False, gates

    return True, gates
