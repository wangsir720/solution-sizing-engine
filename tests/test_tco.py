# -*- coding: utf-8 -*-
"""tco 模块测试 —— 重点验证「每行成本可追溯」与「缺价格不静默填 0」。"""
import copy

import pytest

from src.pricing import load_scenario
from src.sizing import size
from src.tco import MONTHS, TcoError, bandwidth_monthly_cny, compute_tco


@pytest.fixture
def gov_tco():
    s = load_scenario("gov_video_surveillance")
    return s, size(s), compute_tco(s, size(s))


def test_every_line_carries_a_traceable_price_source(gov_tco):
    _, _, tco = gov_tco
    assert tco["lines"], "TCO 不应为空行"
    for line in tco["lines"]:
        assert line["unit_price_source"], "成本行缺少价格来源：%s" % line["item"]
        assert "https://" in line["unit_price_source"], \
            "价格来源必须含可回溯 URL：%s" % line["item"]
        assert line["derivation"], "成本行缺少推导链：%s" % line["item"]


def test_line_total_is_qty_times_price_times_months(gov_tco):
    _, _, tco = gov_tco
    for line in tco["lines"]:
        assert line["monthly_cny"] == pytest.approx(line["qty"] * line["unit_price"], rel=1e-6)
        assert line["total_cny"] == pytest.approx(line["monthly_cny"] * MONTHS, rel=1e-6)


def test_bandwidth_tier_matches_official_example(gov_tco):
    """对齐官方计费示例：广州 15 Mbps 包月
    = 20×2 + 25×3 + 90×10 = 1015 元/月；官方原文「2 个月共 2030 元」可反推月费 1015。
    """
    fee, chain = bandwidth_monthly_cny(15.0)
    assert fee == pytest.approx(1015.0, rel=1e-9)
    assert fee * 2 == pytest.approx(2030.0, rel=1e-9)  # 官方示例值
    assert "阶梯累加" in chain


def test_bandwidth_tiers_below_threshold():
    assert bandwidth_monthly_cny(2.0)[0] == pytest.approx(40.0)      # 2 × 20
    assert bandwidth_monthly_cny(5.0)[0] == pytest.approx(40.0 + 75.0)  # 2×20 + 3×25
    assert bandwidth_monthly_cny(0.0)[0] == pytest.approx(0.0)


def test_negative_bandwidth_raises():
    with pytest.raises(TcoError):
        bandwidth_monthly_cny(-1.0)


def test_cpu_node_cost_is_listed_as_unpriced_not_zero_filled(gov_tco):
    """核心纪律：官方未提供可回溯单价时，进 unpriced 清单，绝不用 0 元凑总额。"""
    s, sizing, tco = gov_tco
    assert sizing["cpu"]["cpu_nodes"] > 0
    unpriced_items = [u["item"] for u in tco["unpriced_items"]]
    assert any("通用计算节点" in i for i in unpriced_items)
    # 总额里不能出现一条 0 元的通用计算节点行
    for line in tco["lines"]:
        assert "通用计算节点" not in line["item"]


def test_totals_are_internally_consistent(gov_tco):
    _, _, tco = gov_tco
    assert tco["monthly_total_cny"] == pytest.approx(
        sum(l["monthly_cny"] for l in tco["lines"]), abs=0.05)
    assert tco["total_cny"] == pytest.approx(tco["monthly_total_cny"] * MONTHS, abs=1.0)
    assert tco["years"] == 3 and tco["months"] == 36


def test_growing_scenario_costs_more(gov_tco):
    s, sizing, base = gov_tco
    bigger = copy.deepcopy(s)
    bigger["workload"]["camera_count"] *= 2
    bigger_sizing = size(bigger)
    bigger_tco = compute_tco(bigger, bigger_sizing)
    assert bigger_tco["total_cny"] > base["total_cny"]
