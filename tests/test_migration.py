# -*- coding: utf-8 -*-
"""migration 模块测试 —— 重点验证四要素齐全，这是最容易做漏的地方。"""
import copy

import pytest

from src.migration import DUAL_WRITE_DAYS, ROLLBACK_WINDOW_HOURS, MigrationError, build_migration_plan
from src.pricing import load_scenario
from src.sizing import size

REQUIRED_KEYS = ("impact", "rollback", "checklist", "dual_write", "gray", "cutover_window")


@pytest.fixture
def plan():
    s = load_scenario("gov_video_surveillance")
    return s, size(s), build_migration_plan(s, size(s))


def test_every_wave_has_all_four_elements(plan):
    _, _, p = plan
    for w in p["waves"]:
        for key in REQUIRED_KEYS:
            assert w.get(key), "波次 %d 缺少 %s" % (w["wave"], key)
        assert len(w["checklist"]) >= 6, "波次 %d 检查项过少" % w["wave"]


def test_high_risk_wave_has_dual_write_and_hard_rollback_deadline(plan):
    _, _, p = plan
    db_wave = next(w for w in p["waves"] if w["wave"] == 3)
    assert db_wave["risk"] == "高"
    assert str(DUAL_WRITE_DAYS) in db_wave["dual_write"]
    assert "双写窗口内完成" in db_wave["rollback"]


def test_waves_are_ordered_by_risk_and_cover_full_stack(plan):
    _, _, p = plan
    assert [w["wave"] for w in p["waves"]] == [1, 2, 3, 4, 5]
    risks = [w["risk"] for w in p["waves"]]
    assert risks.count("高") == 1, "只有一个波次允许是最高风险"
    targets = " ".join(w["target"] for w in p["waves"])
    for kw in ("存储", "数据库", "微服务", "流量"):
        assert kw in targets, "波次未覆盖 %s 层" % kw


def test_traffic_wave_uses_gray_scale_ladder(plan):
    _, _, p = plan
    traffic = next(w for w in p["waves"] if w["wave"] == 5)
    for step in ("1%", "10%", "50%", "100%"):
        assert step in traffic["gray"]


def test_larger_data_extends_migration_duration(plan):
    s, small_sizing, small_plan = plan
    big = copy.deepcopy(s)
    big["workload"]["camera_count"] = s["workload"]["camera_count"] * 20
    big_sizing = size(big)
    big_plan = build_migration_plan(big, big_sizing)
    assert big_plan["total_duration_hours"] > small_plan["total_duration_hours"]


def test_missing_scenario_id_raises():
    with pytest.raises(MigrationError):
        build_migration_plan({}, {})


def test_rollback_window_is_consistent_across_waves(plan):
    _, _, p = plan
    assert ROLLBACK_WINDOW_HOURS > 0
    high = next(w for w in p["waves"] if w["risk"] == "高")
    assert str(ROLLBACK_WINDOW_HOURS) in high["rollback"]
