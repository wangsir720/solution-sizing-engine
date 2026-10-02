# -*- coding: utf-8 -*-
"""交付运营指标基线：配置清单 -> 运营指标表 + 达标口径。

对应 JD 职责 4「交付运营管理」。这一模块的要点不是算出多少数字，而是：
**每个指标必须写清定义、目标值、测量方式、以及这个口径的依据来自哪里**。

指标口径优先引用公开标准（国标 / 行业规范），引用不到的写明「口径为本项目自定义」，
不假装是国标要求 —— 专家一问就知道哪个是真有依据、哪个是拍脑袋。
"""
from __future__ import annotations

# ---- 引用口径来源（公开可查） ----
SL_STANDARD = {
    "name": "GB/T 22239-2019 信息安全技术 网络安全等级保护基本要求",
    "scope": "第三级安全保护对象",
    "url": "https://openstd.samr.gov.cn/",
    "note": "等级保护对信息系统提出可用性、完整性的定级要求；"
            "本表用它作为**合规底线口径**，不把自定 SLA 冒充国标指标",
}

# 公有云服务可用性的常见承诺档位（各云厂商 SLA 条款普遍口径，用作行业参照而非强制标准）
AVAILABILITY_TIERS = [
    {"tier": "单可用区", "target": 0.99900, "label": "99.90%"},
    {"tier": "多可用区", "target": 0.99950, "label": "99.95%"},
    {"tier": "单地域多可用区 + 核心链路冗余", "target": 0.99990, "label": "99.99%"},
]

# 时延与可用性的换算关系（Little's Law），用于从 SLA 倒推容量水位
LATENCY_BUDGET_RULE = (
    "P95 时延目标 × 可用性目标共同决定容量水位："
    "在 %s 目标下，平均故障恢复时间（MTTR）内损失的可用时间占比不能挤占时延预算，"
    "否则扩容无法同时满足可用性与时延两个指标。"
)


class OpsError(Exception):
    pass


def _availability_tier(target: float) -> dict:
    """返回覆盖该目标的**最低**档位。命中即停 —— 不能继续往上匹配到更严的档。"""
    for t in AVAILABILITY_TIERS:
        if target <= t["target"]:
            return t
    return AVAILABILITY_TIERS[-1]


def build_ops_metrics(scenario: dict, sizing: dict) -> dict:
    """生成交付运营指标基线。"""
    sl = scenario.get("service_level")
    if not isinstance(sl, dict):
        raise OpsError("场景缺少 service_level，无法生成运营指标")
    for key in ("availability_target", "rto_minutes", "rpo_minutes"):
        if key not in sl:
            raise OpsError("service_level 缺少字段 %s" % key)

    avail = float(sl["availability_target"])
    if not 0 < avail < 1:
        raise OpsError("availability_target 必须为 0 到 1 之间的小数，实际为 %r" % avail)
    p95_target = float(scenario.get("workload", {}).get("api_p95_latency_ms", 0) or 0)
    if p95_target <= 0:
        raise OpsError("workload.api_p95_latency_ms 必须为正数（运营指标需要时延目标）")

    tier = _availability_tier(avail)
    gpu_nodes = sizing.get("gpu", {}).get("gpu_nodes", 0)
    cpu_nodes = sizing.get("cpu", {}).get("cpu_nodes", 0)
    total_nodes = gpu_nodes + cpu_nodes

    # 允许的年度非计划停机时长 = (1 - 可用性) × 全年分钟数
    downtime_budget_min = round((1 - avail) * 365 * 24 * 60, 1)

    metrics = [
        {
            "name": "服务可用性",
            "definition": "统计周期内，服务端点返回非 5xx 且响应体完整的请求数 / 总请求数",
            "target": tier["label"],
            "measure": "按 1 分钟粒度打点，月度出报表，扣除运维窗口内的计划内变更",
            "basis": "参照公有云 SLA 通用分档（%s 档）；等级保修为合规底线，见 GB/T 22239-2019"
                     % tier["tier"],
            "basis_is_public_standard": False,
        },
        {
            "name": "允许的年度非计划停机",
            "definition": "由可用性目标反推的年度停机时间预算（运维须控制在其内）",
            "target": "≤ %.1f 分钟/年" % downtime_budget_min,
            "measure": "故障台账累加",
            "basis": "计算式：(1 − %.5f) × 365 × 24 × 60 = %.1f 分钟"
                     % (avail, downtime_budget_min),
            "basis_is_public_standard": False,
        },
        {
            "name": "接口 P95 时延",
            "definition": "网关侧统计，5xx 计入、超时计入，剔除客户端网络抖动段",
            "target": "≤ %d ms" % int(p95_target),
            "measure": "网关 access log 逐分钟聚合，月度出 P50/P95/P99",
            "basis": "场景 JSON 显式给定（workload.api_p95_latency_ms），"
                     "属项目目标值而非行业强制标准",
            "basis_is_public_standard": False,
        },
        {
            "name": "RTO（恢复时间目标）",
            "definition": "从故障发生到业务恢复可用的时长",
            "target": "≤ %d 分钟" % int(sl["rto_minutes"]),
            "measure": "故障台账逐次记录实际恢复时间，季度复盘",
            "basis": "场景 JSON 给定；RTO 与可用性预算必须自洽 —— "
                     "单次故障恢复耗时超过 RTO 时须核减年度停机预算余量",
            "basis_is_public_standard": False,
        },
        {
            "name": "RPO（恢复点目标）",
            "definition": "从故障发生到可接受数据丢失窗口的时长",
            "target": "≤ %d 分钟" % int(sl["rpo_minutes"]),
            "measure": "核对最近一次备份/日志重放的实际可恢复时间点",
            "basis": "RPO 直接决定存储快照频率与日志保留策略："
                     "RPO=%d 分钟 -> 快照间隔须短于该值，或启用增量日志持续归档"
                     % int(sl["rpo_minutes"]),
            "basis_is_public_standard": False,
        },
        {
            "name": "GPU 算力利用率",
            "definition": "推理/转码任务占用 GPU 时间的比例，按实例 5 分钟粒度取平均",
            "target": "稳态 ≥ 60%，峰值 ≤ 85%",
            "measure": "GPU 监控面板，按 15 分钟窗口出周报",
            "basis": "口径为本项目自定义；下限低于 60% 意味着卡数测算偏保守，"
                     "上限超过 85% 意味着排队时延将击穿 P95 目标",
            "basis_is_public_standard": False,
        },
        {
            "name": "节点资源利用率",
            "definition": "CPU 节点 vCPU 使用率 / 内存使用率的稳态均值",
            "target": "CPU 稳态 40%–65%，内存稳态 55%–75%",
            "measure": "云监控 5 分钟粒度，按月统计 P50",
            "basis": "口径为本项目自定义；CPU 超分比在 data/assumptions.md A-09 中固定为 1.0"
                     "（不超分），故利用率上限不设更高",
            "basis_is_public_standard": False,
        },
        {
            "name": "存储水位",
            "definition": "已用容量 / 已分配容量",
            "target": "≤ 70%（热层），≤ 85%（冷层）",
            "measure": "存储监控按日采集，月度出水位报表",
            "basis": "口径为本项目自定义；70% 上限用于给扩容预留 72 小时的操作窗口",
            "basis_is_public_standard": False,
        },
        {
            "name": "备份成功率与可恢复性",
            "definition": "计划备份任务成功率；且必须定期做**恢复演练**（不只验证备份存在）",
            "target": "备份成功率 ≥ 99.9%；季度恢复演练 1 次，RPO 实测达标",
            "measure": "备份任务台账 + 演练记录",
            "basis": "口径参考 GB/T 22239-2019 第三级对数据备份恢复的要求，"
                     "但具体百分比为项目自定义",
            "basis_is_public_standard": False,
        },
    ]

    return {
        "scenario_id": scenario.get("scenario_id"),
        "metrics": metrics,
        "capacity_context": {
            "gpu_nodes": gpu_nodes,
            "cpu_nodes": cpu_nodes,
            "total_nodes": total_nodes,
            "redundancy": sizing.get("redundancy", {}).get("mode", "-"),
        },
        "latency_rule": LATENCY_BUDGET_RULE % tier["label"],
        "compliance_reference": SL_STANDARD,
        "scope_note": "本表除等级保护合规底线外，其余指标口径均为本项目自定义，"
                      "已在 basis 字段逐条标注是否引用公开标准。",
    }
