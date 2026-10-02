# -*- coding: utf-8 -*-
"""ops_metrics 模块测试 —— 重点验证「口径必须写清依据，不许把自定值冒充国标」。"""
import copy

import pytest

from src.ops_metrics import AVAILABILITY_TIERS, OpsError, build_ops_metrics
from src.pricing import load_scenario
from src.sizing import size


@pytest.fixture
def ops():
    s = load_scenario("gov_video_surveillance")
    return s, size(s), build_ops_metrics(s, size(s))


def test_every_metric_has_definition_target_measure_and_basis(ops):
    _, _, o = ops
    assert len(o["metrics"]) >= 8
    for m in o["metrics"]:
        assert m["definition"], m["name"]
        assert m["target"], m["name"]
        assert m["measure"], m["name"]
        assert m["basis"], m["name"]


def test_custom_metrics_are_not_labelled_as_national_standard(ops):
    """核心纪律：自定口径必须标 basis_is_public_standard=False，不能冒充 GB/T 要求。"""
    _, _, o = ops
    for m in o["metrics"]:
        if m["basis_is_public_standard"]:
            raise AssertionError("指标 %s 被标为公开标准，需人工复核" % m["name"])
    assert "GB/T 22239" in o["compliance_reference"]["name"]


def test_downtime_budget_is_derived_from_availability():
    s = load_scenario("gov_video_surveillance")
    s["service_level"]["availability_target"] = 0.999
    o = build_ops_metrics(s, size(s))
    downtime = next(m for m in o["metrics"] if m["name"] == "允许的年度非计划停机")
    expected = round((1 - 0.999) * 365 * 24 * 60, 1)
    assert str(expected) in downtime["target"]


def test_stricter_sla_gives_tighter_tier():
    s = load_scenario("gov_video_surveillance")
    s["service_level"]["availability_target"] = 0.9995
    o = build_ops_metrics(s, size(s))
    assert "99.95%" in o["metrics"][0]["target"]
    s["service_level"]["availability_target"] = 0.9999
    o2 = build_ops_metrics(s, size(s))
    assert o2["metrics"][0]["target"] == AVAILABILITY_TIERS[-1]["label"]


def test_missing_service_level_raises():
    s = load_scenario("gov_video_surveillance")
    del s["service_level"]["rto_minutes"]
    with pytest.raises(OpsError) as e:
        build_ops_metrics(s, size(s))
    assert "rto_minutes" in str(e.value)


def test_invalid_availability_raises():
    s = load_scenario("gov_video_surveillance")
    s["service_level"]["availability_target"] = 1.5
    with pytest.raises(OpsError):
        build_ops_metrics(s, size(s))


def test_zero_latency_target_raises():
    s = load_scenario("gov_video_surveillance")
    s["workload"]["api_p95_latency_ms"] = 0
    with pytest.raises(OpsError) as e:
        build_ops_metrics(s, size(s))
    assert "api_p95_latency_ms" in str(e.value)


def test_rpo_drives_snapshot_requirement(ops):
    _, _, o = ops
    rpo = next(m for m in o["metrics"] if m["name"].startswith("RPO"))
    assert "快照间隔" in rpo["basis"] or "日志" in rpo["basis"]
