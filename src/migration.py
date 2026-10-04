# -*- coding: utf-8 -*-
"""统建迁移路径生成：现网规模 + 目标态 -> 分波次迁移方案。

这是统建迁移与运行管理的技术内核，也是方案评审里最容易被专家问住的
部分。因此每一条波次都必须给出**四要素**，缺一不可：

    ① 业务影响面  —— 这一波会碰到哪些在运业务、影响多少用户
    ② 双写/灰度   —— 怎么在不影响老系统的前提下验证新链路
    ③ 回退方案    —— 什么条件下回退、回退要多久、回退后数据怎么办
    ④ 割接检查清单 —— 开工前逐条打勾的硬性检查项

只写「分 3 波次」而不给这四要素，等于没做迁移方案。
"""
from __future__ import annotations

import math

# ---- 来自 data/assumptions.md 的假设取值 ----
# A-07 数据层与应用层双写天数
DUAL_WRITE_DAYS = 7
# A-07 单波次回滚预留时长（小时）
ROLLBACK_WINDOW_HOURS = 4
# A-07 流量灰度阶梯
GRAYSCALE_STEPS = [1, 10, 50, 100]

# 波次定义：按「风险从低到高」排序。风险高的后做，因为回退成本更高。
WAVES = [
    {
        "wave": 1,
        "name": "基础设施与网络就绪",
        "target": "VPC / 子网 / 专线 / 安全组 / 负载均衡",
        "risk": "低",
        "cutover_window": "工作时间，无需割接窗口",
        "dual_write": "不涉及（无业务数据）",
        "gray": "不涉及",
    },
    {
        "wave": 2,
        "name": "存储与分布式存储迁移",
        "target": "块存储 / 对象存储 / 文件存储",
        "risk": "低",
        "cutover_window": "工作时间",
        "dual_write": "新写入双落到新旧两套存储，保留 %d 天" % DUAL_WRITE_DAYS,
        "gray": "按桶/按目录灰度，先迁冷数据与归档数据",
    },
    {
        "wave": 3,
        "name": "数据库与中间件迁移",
        "target": "关系型数据库 / 缓存 / 消息队列",
        "risk": "高",
        "cutover_window": "割接窗口（低峰期，需业务方确认）",
        "dual_write": "应用层双写新旧库，持续 %d 天；每日增量追平并做一致性校验" % DUAL_WRITE_DAYS,
        "gray": "先只读流量切新库，双写期结束后再切写流量",
    },
    {
        "wave": 4,
        "name": "应用与依赖服务迁移",
        "target": "微服务 / 定时任务 / 批处理",
        "risk": "中",
        "cutover_window": "割接窗口",
        "dual_write": "依赖第 3 波的数据双写，本波不再新增双写对象",
        "gray": "按服务实例分批摘流，逐批放量",
    },
    {
        "wave": 5,
        "name": "业务流量切换与老系统下线",
        "target": "公网入口 / 专线入口 / 流量调度 / 老机房设备",
        "risk": "中",
        "cutover_window": "割接窗口 + 黄金观察期 %d 小时" % ROLLBACK_WINDOW_HOURS,
        "dual_write": "不涉及（流量层不承载数据写入）",
        "gray": " -> ".join("%d%%" % s for s in GRAYSCALE_STEPS),
    },
]


class MigrationError(Exception):
    pass


def _rollback_clause(wave: dict) -> str:
    risk = wave["risk"]
    if risk == "低":
        return ("回退触发：验证指标不达标（如吞吐或时延超出目标）立即回退；"
                "回退动作：把流量/挂载切回原资源；因未切写流量，无需数据回滚；"
                "预计回退耗时 ≤ 1 小时")
    if risk == "中":
        return ("回退触发：黄金观察期内错误率超过基线 1.5 倍，或关键接口成功率低于 99.9%%；"
                "回退动作：DNS/流量调度切回老入口 %s；"
                "新写入已通过双写或变更捕获保留在老系统，无需反向数据同步；"
                "预计回退耗时 ≤ %d 小时" % (" -> ".join("%d%%" % s for s in reversed(GRAYSCALE_STEPS[:-1])),
                                        ROLLBACK_WINDOW_HOURS))
    return ("回退触发：数据一致性校验失败、核心事务失败率 > 0.1%%，或业务方主动叫停；"
            "回退动作：应用层停止写新库，切换回老库连接串；"
            "双写期内老库始终为权威数据源，无数据丢失；需回滚新库已写入的增量"
            "（依据双写日志做反向补偿，耗时按增量量级估算）；"
            "预计回退耗时 ≤ %d 小时，且**必须在双写窗口内完成**，超窗后回退成本显著上升"
            % ROLLBACK_WINDOW_HOURS)


def _checklist(wave: dict) -> list[str]:
    common = [
        "确认回退方案已验证（在预发环境完整演练过至少 1 次）",
        "确认监控告警已接入，且告警接收人已确认在岗",
        "确认割接窗口已与业务方书面确认（涉及停机的必须提前通知）",
        "确认数据备份已完成且可恢复（迁移前做一次全量快照）",
    ]
    wave_specific = {
        1: ["确认 VPC 与现有 IDC 路由可达，MTU 已核对", "确认安全组策略最小化放通"],
        2: ["确认新旧存储数据一致性校验脚本已就绪",
            "确认对象存储的跨区复制规则已关闭或已评估流量费用"],
        3: ["确认双写开关可一键关闭（开关本身要有回滚预案）",
            "确认一致性校验连续 72 小时无差异",
            "确认慢查询与锁等待指标在双写期未劣化"],
        4: ["确认所有定时任务/批处理已在新环境完成一轮完整跑通",
            "确认服务注册发现已切到新注册中心"],
        5: ["确认证书与域名切换方案已备案（提前 7 天）",
            "确认老系统进入只读观察期而非立即停机",
            "确认下线前完成一次完整的数据归档与核对"],
    }
    return common + wave_specific.get(wave["wave"], [])


def _impact(wave: dict, scenario: dict) -> dict:
    """业务影响面：按场景规模折算，本质是把「多少业务被打到」量化出来。"""
    w = scenario.get("workload", {})
    users = w.get("concurrent_users", 0)
    if wave["wave"] == 5:
        return {
            "scope": "全量用户（%d 并发）" % users,
            "downtime": "割接窗口内预计中断 ≤ 30 分钟（DNS/入口切换）",
            "note": "本波影响面最大，必须安排业务方值班并预留回退窗口",
        }
    if wave["wave"] == 3:
        return {
            "scope": "依赖该数据库的全部在运业务（约 %d 并发用户的写链路）" % users,
            "downtime": "双写与只读灰度阶段无中断；切写窗口预计中断 ≤ 5 分钟",
            "note": "切写前必须完成 %d 天双写与一致性校验" % DUAL_WRITE_DAYS,
        }
    if wave["wave"] == 2:
        return {
            "scope": "历史数据与归档数据（不影响在线读写）",
            "downtime": "无中断",
            "note": "先迁冷数据，避免与在线业务争抢带宽",
        }
    if wave["wave"] == 4:
        return {
            "scope": "分批摘流的单个服务实例（按 %s 灰度）" % " -> ".join(
                "%d%%" % s for s in GRAYSCALE_STEPS),
            "downtime": "单批摘流期间该实例请求由其余实例承接，不中断",
            "note": "需确认剩余实例有余量承接摘流流量",
        }
    return {"scope": "无在运业务受影响", "downtime": "无中断", "note": "基础设施就绪是前置条件"}


def _wave_duration_hours(wave: dict, sizing: dict) -> float:
    """波次工时估算：按数据量与节点数给量级估算，不承诺精确工期。"""
    data_gib = sizing.get("storage_total_gib", 0.0)
    base = {1: 8, 2: 24, 3: 40, 4: 32, 5: 8}[wave["wave"]]
    # 数据量越大，迁移窗口越长；按每 10 TiB 增加一个班次（8 小时）粗估
    extra_shifts = math.floor(data_gib / (10 * 1024))
    return float(base + extra_shifts * 8)


def build_migration_plan(scenario: dict, sizing: dict) -> dict:
    """生成完整迁移方案。"""
    sid = scenario.get("scenario_id", "")
    if not sid:
        raise MigrationError("场景缺少 scenario_id，无法生成迁移方案")

    waves_out = []
    for wave in WAVES:
        waves_out.append({
            "wave": wave["wave"],
            "name": wave["name"],
            "target": wave["target"],
            "risk": wave["risk"],
            "cutover_window": wave["cutover_window"],
            "dual_write": wave["dual_write"],
            "gray": wave["gray"],
            "impact": _impact(wave, scenario),
            "rollback": _rollback_clause(wave),
            "checklist": _checklist(wave),
            "duration_hours": _wave_duration_hours(wave, sizing),
        })

    total_hours = sum(w["duration_hours"] for w in waves_out)
    return {
        "scenario_id": sid,
        "waves": waves_out,
        "total_waves": len(waves_out),
        "total_duration_hours": total_hours,
        "critical_path": "波次 3（数据库）-> 波次 4（应用）-> 波次 5（流量）",
        "key_constraint": "波次 3 的双写窗口（%d 天）是全程最硬的时间约束："
                          "回退动作必须在双写窗口内完成，超窗后回退成本显著上升。"
                          % DUAL_WRITE_DAYS,
        "assumption_refs": ["A-07"],
        "scope_note": "本方案按数据层/应用层/流量层划分波次，不含应用代码依赖扫描；"
                      "实际波次划分须以真实系统的依赖分析结果为准。",
    }
