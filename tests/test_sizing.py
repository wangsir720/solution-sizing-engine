# -*- coding: utf-8 -*-
"""sizing 模块测试 —— 重点验证「卡数必须向上取整到整机粒度」这条真实约束。"""
import copy

import pytest

from src.pricing import load_scenario
from src.sizing import SizingError, gb_from_bitrate_mbps, size


@pytest.fixture
def gov():
    return load_scenario("gov_video_surveillance")


def test_gpu_cards_are_rounded_up_to_whole_nodes(gov):
    r = size(gov)
    gpu = r["gpu"]
    assert gpu["gpu_cards"] == int(gpu["gpu_cards"]), "卡数必须为整数"
    assert gpu["gpu_nodes"] == int(gpu["gpu_nodes"]), "节点数必须为整数"
    # 分配的卡数必须是「每节点卡数 × 节点数」，不能出现小数卡
    assert gpu["allocated_cards"] == gpu["gpu_nodes"] * gpu["gpu_per_node"]
    assert gpu["allocated_cards"] >= gpu["gpu_cards"]


def test_scaling_input_scales_output(gov):
    """推理吞吐只影响算力，不影响存储 —— 两个维度必须解耦，否则无法定位成本动因。"""
    base = size(gov)
    more_cameras = copy.deepcopy(gov)
    more_cameras["workload"]["camera_count"] = gov["workload"]["camera_count"] * 2
    scaled = size(more_cameras)
    assert scaled["storage_total_gib"] > base["storage_total_gib"]
    # 摄像机翻倍，GPU 需求不变（推理路数由 ai_inference_fps 决定）
    assert scaled["gpu"]["gpu_cards"] == base["gpu"]["gpu_cards"]


def test_inference_fps_only_drives_gpu(gov):
    base = size(gov)
    more_fps = copy.deepcopy(gov)
    more_fps["workload"]["ai_inference_fps"] = gov["workload"]["ai_inference_fps"] * 3
    scaled = size(more_fps)
    assert scaled["gpu"]["gpu_cards"] > base["gpu"]["gpu_cards"]
    assert scaled["storage_total_gib"] == base["storage_total_gib"]


def test_missing_required_field_raises_not_defaulted():
    bad = load_scenario("gov_video_surveillance")
    del bad["workload"]["retention_days_hot"]
    with pytest.raises(SizingError) as e:
        size(bad)
    assert "retention_days_hot" in str(e.value)


def test_negative_value_raises():
    bad = load_scenario("gov_video_surveillance")
    bad["workload"]["camera_count"] = -1
    with pytest.raises(SizingError) as e:
        size(bad)
    assert "不能为负数" in str(e.value)


def test_zero_workload_is_valid_and_returns_zero(gov):
    zero = copy.deepcopy(gov)
    zero["workload"]["camera_count"] = 0
    zero["workload"]["ai_inference_fps"] = 0
    r = size(zero)
    assert r["gpu"]["gpu_nodes"] == 0
    assert r["video"]["hot_gib"] == 0


def test_huge_config_does_not_overflow_int(gov):
    huge = copy.deepcopy(gov)
    huge["workload"]["camera_count"] = 2_000_000
    r = size(huge)
    assert r["gpu"]["gpu_nodes"] > 0
    assert r["storage_total_gib"] > 0


def test_gpu_per_node_exceeding_sku_raises():
    bad = load_scenario("gov_video_surveillance")
    bad["workload"]["ai_inference_gpu_per_node"] = 8  # 场景选的 SKU 只有 1 卡
    with pytest.raises(SizingError) as e:
        size(bad)
    assert "超过 SKU" in str(e.value)


def test_sla_threshold_controls_redundancy(gov):
    strict = copy.deepcopy(gov)
    strict["service_level"]["availability_target"] = 0.9995
    assert size(strict)["redundancy"]["mode"] == "N+1"

    loose = copy.deepcopy(gov)
    loose["service_level"]["availability_target"] = 0.99
    r = size(loose)
    assert r["redundancy"]["mode"] == "无额外冗余"
    assert "gpu_nodes_with_ha" not in r


def test_bitrate_conversion_matches_hand_calculation():
    # 1 Mbps 持续一天 = 1000000*86400/8/1024/1024 ≈ 10.29 GiB
    got = gb_from_bitrate_mbps(1.0, 1.0)
    assert got == pytest.approx(10.29, abs=0.02)
    # 4 Mbps × 30 天
    assert gb_from_bitrate_mbps(4.0, 30.0) == pytest.approx(got * 120, rel=1e-9)
