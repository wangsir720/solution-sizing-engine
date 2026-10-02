# -*- coding: utf-8 -*-
"""pricing 模块测试：价目表的完整性校验与错误处理。"""
import pytest

from src import pricing
from src.pricing import PricingError


def test_gpu_price_table_loads_and_is_traceable():
    skus = pricing.load_gpu_skus()
    assert len(skus) >= 10, "GPU 价目表条目过少"
    for sku in skus.values():
        assert sku.monthly_cny > 0
        # 真实性红线：每条价格必须能点回公开来源
        assert sku.source_url.startswith("https://"), sku.sku
        assert sku.fetched_at, sku.sku


def test_gpu_count_parsed_from_sku():
    skus = pricing.load_gpu_skus()
    assert skus["GNV4.44CU.4GPU"].gpu_count == 4
    assert skus["GN7.8CU.1GPU"].gpu_count == 1
    assert skus["GN10XP.80CU.8GPU"].gpu_count == 8


def test_unknown_sku_raises_not_silent_default():
    with pytest.raises(PricingError) as e:
        pricing.get_gpu_sku("NOT-A-REAL-SKU")
    assert "不存在" in str(e.value)


def test_unknown_storage_tier_raises():
    with pytest.raises(PricingError):
        pricing.get_storage_tier("tier-from-thin-air")


def test_unknown_network_item_raises():
    with pytest.raises(PricingError):
        pricing.get_network_price("no_such_item")


def test_unknown_scenario_raises_with_available_list():
    with pytest.raises(PricingError) as e:
        pricing.load_scenario("no-such-scenario")
    assert "可用场景" in str(e.value)


def test_three_scenarios_present():
    scenarios = pricing.list_scenarios()
    for expected in ("gov_video_surveillance", "finance_ai_assistant", "edu_hybrid_meeting"):
        assert expected in scenarios
