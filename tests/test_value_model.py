# -*- coding: utf-8 -*-
"""value_model 模块测试 —— 重点验证「不编数字」这条纪律。"""
import copy

import pytest

from src.pricing import load_scenario
from src.sizing import size
from src.tco import compute_tco
from src.value_model import build_value_model


@pytest.fixture
def value():
    s = load_scenario("gov_video_surveillance")
    sizing = size(s)
    return s, sizing, build_value_model(s, sizing, compute_tco(s, sizing))


def test_items_requiring_external_input_are_marked_pending(value):
    _, _, v = value
    assert v["pending_items"], "应至少有一项因缺客户输入而标为待补"
    for item in v["pending_items"]:
        assert item["value_cny"] is None, \
            "待补项不得带数字：%s" % item["item"]
        assert item["what_needed"], "待补项必须写明需要客户提供什么"


def test_no_item_claims_a_bare_percentage(value):
    """核心纪律：不得出现「提升 X%」这类无口径结论。"""
    _, _, v = value
    for item in v["quantified"]:
        text = " ".join(str(x) for x in (item["item"], item["formula"],
                                         item.get("note") or ""))
        assert "提升" not in text and "降低了" not in text and "节约了" not in text, \
            "出现无口径的收益结论：%s" % item["item"]


def test_growth_trajectory_is_computed_from_traceable_categories(value):
    _, _, v = value
    growth = next(i for i in v["quantified"] if i["status"] == "已计算")
    assert growth["value_cny"] is not None
    assert len(growth["yearly"]) == 3
    # 逐年预算应随增长上升
    monthly = [y["projected_monthly_cny"] for y in growth["yearly"]]
    assert monthly == sorted(monthly), "逐年预算未随增长上升"
    assert growth["static_three_year_cny"] > 0


def _growth_value(model: dict) -> float:
    return next(i for i in model["quantified"] if i["status"] == "已计算")["value_cny"]


def test_higher_growth_yields_higher_three_year_budget():
    s = load_scenario("gov_video_surveillance")
    base_sizing = size(s)
    low = _growth_value(build_value_model(s, base_sizing, compute_tco(s, base_sizing)))

    fast = copy.deepcopy(s)
    fast["growth"]["camera_growth_annual"] = 0.6
    fast_sizing = size(fast)
    high = _growth_value(build_value_model(fast, fast_sizing, compute_tco(fast, fast_sizing)))
    assert high > low


def test_zero_growth_scenario_has_no_growth_item():
    s = load_scenario("gov_video_surveillance")
    s["growth"] = {"camera_growth_annual": 0, "user_growth_annual": 0, "forecast_years": 3}
    sizing = size(s)
    v = build_value_model(s, sizing, compute_tco(s, sizing))
    assert all(i["status"] != "已计算" for i in v["quantified"])


def test_scale_context_is_carried_through(value):
    _, _, v = value
    ctx = v["scale_context"]
    assert ctx["concurrent_users"] == 300
    assert ctx["camera_count"] == 2000
    assert ctx["three_year_cloud_tco_cny"] > 0
