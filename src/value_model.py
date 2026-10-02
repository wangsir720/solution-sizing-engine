# -*- coding: utf-8 -*-
"""业务价值测算：场景参数 + 成本结果 -> 业务价值表。

对应 JD 职责 2 里那句「发现转型业务价值」。这一模块的**纪律**比算法重要：

1. **每一项收益必须写清计算口径**，不写「提升 X%」这种无来源结论。
2. **所有需要外部输入才能算出来的项（人力单价、客户工时、系统自建成本），
   一律进 `pending_items` 标为待补，绝不用一个看起来合理的数字填进去。**
   数字一旦编造，被追问「这个数怎么来的」时就会对不上，而对不上的数字在评审现场是负资产。
3. 口径写成**可计算式**，客户拿到后只需替换自己的真实参数即可算出结果。

输出分两块：
    quantified  —— 本工具已能算的（基于价目表与场景参数，有来源）
    pending     —— 必须由客户侧提供参数才能算的（给公式与所需输入，不给数）
"""
from __future__ import annotations

from .tco import MONTHS


class ValueError_(Exception):
    pass


def _annual(cny: float) -> float:
    return cny * 12


def build_value_model(scenario: dict, sizing: dict, tco: dict) -> dict:
    """构建业务价值模型。"""
    w = scenario.get("workload")
    if not isinstance(w, dict):
        raise ValueError_("场景缺少 workload，无法测算业务价值")

    users = int(w.get("concurrent_users", 0) or 0)
    camera = int(w.get("camera_count", 0) or 0)
    three_year_cloud = tco.get("total_cny", 0.0)

    quantified = [
        {
            "item": "自建机房对比云资源的三年支出差",
            "formula": "三年云资源 TCO − 自建三年总拥有成本（服务器 + 存储 + 网络 + 机房 + 运维分摊）",
            "value_cny": None,
            "status": "待补",
            "reason": "自建侧成本无公开可比价目，且机房与运维分摊口径因客户而异",
            "what_needed": "客户侧提供：现有机房机柜数与单价、服务器折旧政策、运维人力编制与成本",
            "note": "本工具只给云资源侧 TCO %.2f 元（三年，不含税与折扣），"
                    "不替客户估自建成本" % three_year_cloud,
        },
        {
            "item": "方案测算环节人工节省",
            "formula": "（人工测算工时 − 工具测算工时） × 频次 × 三年",
            "value_cny": None,
            "status": "待补",
            "reason": "工时与频次是客户侧数据",
            "what_needed": "客户侧提供：一份方案的人工测算工时、每月方案数量、"
                           "以及实际使用本工具后的工时（建议现场计时 3 个真实场景取均值）",
            "note": "把工时记录下来才算数：现场计时 3 个真实场景取均值，"
                    "比引用任何行业平均值都有说服力",
        },
        {
            "item": "迁移回退风险的成本敞口",
            "formula": "预计回退次数 × 每次回退人工时 × 人工时成本 + 回退导致的业务中断损失",
            "value_cny": None,
            "status": "待补",
            "reason": "回退概率与业务中断损失需按客户实际业务等级评估",
            "what_needed": "客户侧提供：人工时成本、业务中断的小时损失口径",
            "note": "本工具的价值不在于算出这个数，而在于把回退触发条件、回退动作、"
                    "回退耗时明确写进方案（见 output 的迁移章节），让风险可被管理",
        },
    ]

    # 本工具能直接算的：把逐年增长显式摊出来，说明为什么不能只按首年算
    growth = scenario.get("growth", {})
    cam_g = float(growth.get("camera_growth_annual", 0) or 0)
    user_g = float(growth.get("user_growth_annual", 0) or 0)
    if cam_g or user_g:
        # 按成本科目拆首年月费：算力随节点增长、存储随摄像机增长、网络随用户增长。
        # 科目划分直接取自 tco 明细，不引入任何额外权重。
        base_by_category: dict[str, float] = {}
        for line in tco.get("lines", []):
            base_by_category[line["category"]] = base_by_category.get(line["category"], 0.0) \
                + line["monthly_cny"]

        years = []
        for y in range(1, 4):
            f_cam = (1 + cam_g) ** (y - 1)
            f_user = (1 + user_g) ** (y - 1)
            year_monthly = 0.0
            for cat, monthly in base_by_category.items():
                if cat == "存储":
                    factor = f_cam
                elif cat == "网络":
                    factor = f_user
                else:  # 计算类：取算力与用户增长的较大者
                    factor = max(f_cam, f_user)
                year_monthly += monthly * factor
            years.append({
                "year": y,
                "camera_factor": round(f_cam, 4),
                "user_factor": round(f_user, 4),
                "projected_storage_gib": round(sizing.get("storage_total_gib", 0.0) * f_cam, 2),
                "projected_gpu_nodes": int(round(sizing.get("gpu", {}).get("gpu_nodes", 0)
                                                  * max(f_cam, f_user))),
                "projected_cpu_nodes": int(round(sizing.get("cpu", {}).get("cpu_nodes", 0)
                                                  * f_user)),
                "projected_monthly_cny": round(year_monthly, 2),
                "projected_year_cny": round(year_monthly * 12, 2),
            })

        growth_total = sum(y["projected_year_cny"] for y in years)
        quantified.append({
            "item": "三年容量增长轨迹下的逐年预算（容量摊销口径）",
            "formula": "第 N 年月费 = Σ 各成本科目首年月费 × 该科目对应的增长因子；"
                       "存储科目按摄像机增长率、计算科目按 max(摄像机, 用户) 增长率、"
                       "网络科目按用户增长率",
            "value_cny": round(growth_total, 2),
            "status": "已计算",
            "reason": None,
            "what_needed": None,
            "note": "对照首年 ×3 的静态口径 %.2f 元，增长口径为 %.2f 元，"
                    "差额 %.2f 元即「按首年做三年预算」的低估额。"
                    "本项为**已计算**而非待补，因为全部输入（价目表 + 场景增长率）均在手。"
                    % (three_year_cloud, growth_total, growth_total - three_year_cloud),
            "static_three_year_cny": three_year_cloud,
            "yearly": years,
        })

    return {
        "scenario_id": scenario.get("scenario_id"),
        "scale_context": {
            "concurrent_users": users,
            "camera_count": camera,
            "three_year_cloud_tco_cny": three_year_cloud,
            "months": MONTHS,
        },
        "quantified": quantified,
        "pending_items": [q for q in quantified if q["status"] == "待补"],
        "discipline_note": "本表不给出「效率提升 X%」「成本降低 Y%」这类无口径结论。"
                           "所有收益项要么给出可复算的公式并标明所需输入，"
                           "要么标记为待补 —— 价值可以算，口径不能编。",
    }
