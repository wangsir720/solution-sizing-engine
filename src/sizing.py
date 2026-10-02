# -*- coding: utf-8 -*-
"""算力配置测算：业务场景参数 -> 算力/存储/网络配置清单。

输入 `data/scenarios/*.json` 里的业务参数，输出可直接进方案文档的**配置清单**。
每一项都带 `trace` 字段，写明推导链与所依据的假设编号（对应 `data/assumptions.md`）。

关键约束（来自真实交付经验，不是风格问题）：
1. **卡数必须向上取整到整机粒度** —— 不能输出 3.5 张卡这种数字。
2. 节点的 vCPU / 内存不是自由参数，必须由所选 SKU 携带，避免算力与计费口径脱节。
3. 任何非法输入（负数、缺失字段、引用不存在的 SKU）必须抛错，**不允许静默降级为 0**。
"""
from __future__ import annotations

import math
from typing import Any

from .pricing import get_gpu_sku

# ---- 来自 data/assumptions.md 的假设取值（代码里只允许出现这里已登记的常量）----
# A-01 单路摄像机码率缺省基线（Mbps）；场景 JSON 未给时用此值
DEFAULT_STREAM_BITRATE_MBPS = 4.0
# A-02 热/冷分层由 retention_days 推导，无额外常量
# A-03 单卡（24GB 显存）支撑的并发会话数 —— 占位待实测替换
SESSIONS_PER_GPU_24G = 18.0
# A-03 平均单条语料 token 数
TOKENS_PER_DOC = 300
# A-03 向量维度（float32）
VECTOR_DIM = 1536
# A-05 无 GPU 通用节点单节点规格
CPU_NODE_VCPU = 8
CPU_NODE_MEM_GIB = 32
# A-05 每 GPU 卡配套 vCPU 上限 / 内存下限
VCPU_PER_GPU_CAP = 16
MEM_PER_GPU_FLOOR_GIB = 64
# A-09 超分比
CPU_OVERSUBSCRIBE = 1.0
GPU_OVERSUBSCRIBE = 1.0
STORAGE_OVERSUBSCRIBE = 1.2
# A-06 SLA 达到该阈值时按 N+1 分布
SLA_N_PLUS_ONE_THRESHOLD = 0.9995
# 单节点可承载的 API QPS 基线（占位待压测替换）
QPS_PER_CPU_NODE = 150.0

GB_PER_MIB_S_DAY = 1000 * 86400 / 8 / 1024 / 1024  # A-01：1 Mbps 持续一天 ≈ 10.29 GiB


class SizingError(Exception):
    """输入不合法。不做兜底 —— 错误必须在测算阶段暴露，不能带着脏数据出方案。"""


def _require(scenario: dict, path: str, kind=None):
    """取嵌套字段，缺失即报错。path 形如 'workload.camera_count'。"""
    cur: Any = scenario
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            raise SizingError("场景参数缺少必填字段：%s" % path)
        cur = cur[part]
    if kind is not None and not isinstance(cur, kind):
        raise SizingError("场景字段 %s 类型应为 %s，实际为 %s"
                          % (path, kind.__name__, type(cur).__name__))
    return cur


def _nonneg(value: float, name: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise SizingError("%s 必须是数值，实际为 %r" % (name, value))
    if value < 0:
        raise SizingError("%s 不能为负数，实际为 %r" % (name, value))
    return float(value)


def gb_from_bitrate_mbps(mbps: float, days: float) -> float:
    """A-01 换算：码率(Mbps) × 天数 -> GiB。"""
    return mbps * 1000 * 86400 / 8 / 1024 / 1024 * days


# ---------------------------------------------------------------- 视频与存储

def compute_video_storage(scenario: dict) -> dict:
    """按码率 × 时长推算热/冷存储容量（GiB）。"""
    w = _require(scenario, "workload", dict)
    bitrate = _nonneg(w.get("stream_bitrate_mbps", DEFAULT_STREAM_BITRATE_MBPS),
                      "workload.stream_bitrate_mbps")
    hot_days = _nonneg(_require(scenario, "workload.retention_days_hot"), "retention_days_hot")
    cold_days = _nonneg(_require(scenario, "workload.retention_days_cold"), "retention_days_cold")

    # 视联网类场景：摄像机路数 × 码率；直播类场景：并发流数 × 单流码率
    cameras = _nonneg(w.get("camera_count", 0), "workload.camera_count")
    live_streams = _nonneg(w.get("concurrent_live_streams", 0), "concurrent_live_streams")
    live_bitrate = _nonneg(w.get("live_stream_bitrate_mbps", 0), "concurrent_live_streams.bitrate")

    if cameras > 0:
        streams = cameras
        stream_bitrate = bitrate
        basis = "camera_count(%d) × stream_bitrate_mbps(%.1f)" % (cameras, stream_bitrate)
    elif live_streams > 0:
        streams = live_streams
        stream_bitrate = live_bitrate
        basis = "concurrent_live_streams(%d) × live_stream_bitrate_mbps(%.1f)" % (
            live_streams, live_bitrate)
    else:
        return {
            "hot_gib": 0.0, "cold_gib": 0.0, "streams": 0,
            "trace": "场景无摄像机与直播流，媒体存储按 0 计入（仍参与总容量汇总）",
            "assumption_refs": ["A-01"],
        }

    daily_gib = gb_from_bitrate_mbps(stream_bitrate, 1.0) * streams
    hot_gib = daily_gib * hot_days
    cold_gib = daily_gib * cold_days
    return {
        "hot_gib": round(hot_gib, 2),
        "cold_gib": round(cold_gib, 2),
        "streams": int(streams),
        "daily_growth_gib": round(daily_gib, 2),
        "trace": "日增量 = %s = %.2f GiB/天；热存储 = 日增量 × %d 天 = %.2f GiB；"
                 "冷存储 = 日增量 × %d 天 = %.2f GiB"
                 % (basis, daily_gib, int(hot_days), hot_gib, int(cold_days), cold_gib),
        "assumption_refs": ["A-01", "A-02"],
    }


def compute_knowledge_storage(scenario: dict) -> dict:
    """知识库 / 向量库存储（GiB）。无知识库字段时返回 0。"""
    w = _require(scenario, "workload", dict)
    kb = _nonneg(w.get("knowledge_base_gb", 0), "workload.knowledge_base_gb")
    if kb == 0:
        return {"raw_gib": 0.0, "vector_gib": 0.0,
                "trace": "场景未定义 knowledge_base_gb，知识库存储按 0 计入",
                "assumption_refs": ["A-04"]}

    # A-04：向量总量 = 语料 token 数 / 每条 token 数 × 单条向量字节数
    doc_count = kb * 1024 * 1024 * 1024 / 1024 / TOKENS_PER_DOC  # 粗略：1KB 语料≈1 token 量级
    # 更保守的可复核口径：1 GB 中文语料约 50 万 token
    tokens = kb * 1_000_000_000 / 1.5 * 2  # 1 汉字≈1.5 字节，1 token≈1.5 汉字
    doc_count = tokens / TOKENS_PER_DOC
    vector_bytes = doc_count * VECTOR_DIM * 4
    vector_gib = vector_bytes / 1024 ** 3
    return {
        "raw_gib": round(kb, 2),
        "vector_gib": round(vector_gib, 2),
        "trace": "原始语料 %.0f GB；按中文语料密度约 1.5 字节/token 折算 tokens=%.0f，"
                 "每条 %d token -> 条数 %.0f；单条向量 %d 维 ×4 Byte = %d Byte -> 向量总量 %.2f GiB"
                 % (kb, tokens, TOKENS_PER_DOC, doc_count, VECTOR_DIM,
                    VECTOR_DIM * 4, vector_gib),
        "assumption_refs": ["A-04"],
    }


# ---------------------------------------------------------------- 算力测算

def compute_gpu_nodes(scenario: dict) -> dict:
    """测算 GPU 节点数。卡数与节点数都向上取整到整机粒度。"""
    w = _require(scenario, "workload", dict)
    sku_id = w.get("ai_inference_gpu_type")
    if not sku_id:
        raise SizingError("场景缺少 workload.ai_inference_gpu_type，无法测算 GPU 规格")
    sku = get_gpu_sku(sku_id)  # 引用不存在的 SKU 会抛 PricingError，这里不吞

    per_node = _require(scenario, "workload.ai_inference_gpu_per_node", int)
    if per_node <= 0:
        raise SizingError("workload.ai_inference_gpu_per_node 必须为正整数")
    if per_node > sku.gpu_count:
        raise SizingError("ai_inference_gpu_per_node=%d 超过 SKU %s 实际卡数 %d"
                          % (per_node, sku.sku, sku.gpu_count))

    demands: list[tuple[str, float, str]] = []  # (用途, 需求卡数, 推导链)

    # 1) 实时流 AI 解析：吞吐型需求（路/秒）
    inf_fps = _nonneg(w.get("ai_inference_fps", 0), "workload.ai_inference_fps")
    if inf_fps > 0:
        gpu = sku.gpu_model
        # A-08/T4 与 A10 吞吐差异：这里用「每卡每秒可解析路数」的量级估算
        # TODO(占位待替换)：必须用真实模型压测数据替换
        if gpu in ("NVIDIA T4", "NVIDIA V100"):
            fps_per_gpu = 30.0
        else:
            fps_per_gpu = 60.0
        demands.append((
            "实时流 AI 解析",
            inf_fps / fps_per_gpu,
            "ai_inference_fps(%.0f 路/秒) ÷ %s 单卡吞吐(%.0f 路/秒) = %.2f 卡"
            % (inf_fps, gpu, fps_per_gpu, inf_fps / fps_per_gpu),
        ))

    # 2) 大模型会话式推理：并发型需求
    sessions = _nonneg(w.get("ai_concurrent_sessions", 0), "workload.ai_concurrent_sessions")
    if sessions > 0:
        per_gpu = SESSIONS_PER_GPU_24G * (sku.gpu_memory_gib / 24.0)
        demands.append((
            "大模型会话推理",
            sessions / per_gpu,
            "ai_concurrent_sessions(%.0f) ÷ 单卡可承载并发(%.1f，按 A-03 的 %s 按显存 %.0fGB 折算) = %.2f 卡"
            % (sessions, per_gpu, SESSIONS_PER_GPU_24G, sku.gpu_memory_gib, sessions / per_gpu),
        ))

    # 3) 视频转码：并发路数型需求
    transcode = _nonneg(w.get("transcode_streams_concurrent", 0),
                        "workload.transcode_streams_concurrent")
    if transcode > 0:
        streams_per_gpu = 4.0  # A-08，占位待实测替换
        demands.append((
            "视频转码",
            transcode / streams_per_gpu,
            "transcode_streams_concurrent(%.0f 路) ÷ 单卡并行转码(%.0f 路，A-08 占位值) = %.2f 卡"
            % (transcode, streams_per_gpu, transcode / streams_per_gpu),
        ))

    if not demands:
        return {"gpu_cards": 0, "gpu_nodes": 0, "sku": None, "demands": [],
                "trace": "场景未定义任何 GPU 负载（ai_inference_fps / ai_concurrent_sessions / "
                         "transcode_streams_concurrent 均为 0）",
                "assumption_refs": ["A-08"]}

    raw_cards = sum(d[1] for d in demands)
    # 真实约束：卡不能是小数，必须向上取整
    gpu_cards = int(math.ceil(raw_cards))
    gpu_nodes = int(math.ceil(gpu_cards / per_node))
    # 节点数向上取整后，实际分配卡数可能大于需求卡数，必须显式说明，避免读者误读
    allocated_cards = gpu_nodes * per_node

    trace = " ; ".join("%s: %s" % (name, chain) for name, _, chain in demands)
    trace += (" ; 合计需求 %.2f 卡 -> 向上取整到整机粒度 = %d 卡" % (raw_cards, gpu_cards))
    trace += (" ; 卡数 / 每节点 %d 卡 = %.2f -> 向上取整 = %d 节点" % (
        per_node, gpu_cards / per_node, gpu_nodes))
    if allocated_cards != gpu_cards:
        trace += (" ; 实际分配 %d 卡（整机粒度导致上浮 %d 卡，计入冗余）"
                  % (allocated_cards, allocated_cards - gpu_cards))

    return {
        "gpu_cards": gpu_cards,
        "allocated_cards": allocated_cards,
        "gpu_nodes": gpu_nodes,
        "sku": sku,
        "gpu_per_node": per_node,
        "demands": [{"purpose": n, "cards_raw": round(c, 3), "derivation": ch}
                    for n, c, ch in demands],
        "trace": trace,
        "assumption_refs": ["A-03", "A-05", "A-08"],
    }


def compute_cpu_nodes(scenario: dict) -> dict:
    """按 API QPS 测算通用计算节点数。"""
    qps = _nonneg(_require(scenario, "workload.api_qps"), "workload.api_qps")
    if qps == 0:
        return {"cpu_nodes": 0, "vcpu_total": 0, "mem_gib_total": 0,
                "trace": "api_qps = 0，通用计算节点按 0 计入",
                "assumption_refs": ["A-05"]}
    raw = qps / QPS_PER_CPU_NODE / CPU_OVERSUBSCRIBE
    nodes = int(math.ceil(raw))
    vcpu = nodes * CPU_NODE_VCPU
    mem = nodes * CPU_NODE_MEM_GIB
    return {
        "cpu_nodes": nodes,
        "vcpu_total": vcpu,
        "mem_gib_total": mem,
        "trace": "api_qps(%.0f) ÷ 单节点 %.0f QPS(占位待压测) ÷ 超分比 %.1f = %.2f 节点 -> 向上取整 %d 节点"
                 "（单节点 %d vCPU / %d GB）"
                 % (qps, QPS_PER_CPU_NODE, CPU_OVERSUBSCRIBE, raw, nodes,
                    CPU_NODE_VCPU, CPU_NODE_MEM_GIB),
        "assumption_refs": ["A-05", "A-09"],
    }


def compute_network(scenario: dict) -> dict:
    """公网出向带宽测算（阶梯计费）。"""
    w = _require(scenario, "workload", dict)
    users = _nonneg(_require(scenario, "workload.concurrent_users"), "workload.concurrent_users")
    per_user = _nonneg(w.get("avg_bandwidth_mbps_per_user", 0),
                       "workload.avg_bandwidth_mbps_per_user")
    peak = users * per_user
    return {
        "peak_mbps": round(peak, 2),
        "users": int(users),
        "per_user_mbps": per_user,
        "trace": "并发用户(%d) × 人均带宽(%.2f Mbps) = 峰值 %.2f Mbps"
                 % (int(users), per_user, peak),
        "assumption_refs": ["A-01"],
    }


def apply_redundancy(scenario: dict, sizing: dict) -> dict:
    """A-06：SLA 目标达到 0.9995 时按 N+1 分布（副本 +1）。"""
    sla = float(_require(scenario, "service_level.availability_target"))
    if sla < SLA_N_PLUS_ONE_THRESHOLD:
        sizing["redundancy"] = {
            "mode": "无额外冗余",
            "trace": "可用性目标 %.4f < %.4f，不叠加 N+1" % (sla, SLA_N_PLUS_ONE_THRESHOLD),
        }
        return sizing
    for key in ("gpu_nodes", "cpu_nodes"):
        base = sizing.get(key, 0)
        sizing[key + "_with_ha"] = base + 1
    sizing["redundancy"] = {
        "mode": "N+1",
        "trace": "可用性目标 %.4f ≥ %.4f -> 各节点类型副本 +1"
                 % (sla, SLA_N_PLUS_ONE_THRESHOLD),
    }
    return sizing


def size(scenario: dict) -> dict:
    """主入口：场景参数 -> 完整算力配置清单。"""
    _require(scenario, "scenario_id")
    _require(scenario, "workload", dict)
    _require(scenario, "service_level", dict)

    video = compute_video_storage(scenario)
    knowledge = compute_knowledge_storage(scenario)
    gpu = compute_gpu_nodes(scenario)
    cpu = compute_cpu_nodes(scenario)
    net = compute_network(scenario)

    sizing = {
        "scenario_id": scenario["scenario_id"],
        "scenario_name": scenario.get("name", scenario["scenario_id"]),
        "video": video,
        "knowledge": knowledge,
        "gpu": gpu,
        "cpu": cpu,
        "network": net,
    }
    apply_redundancy(scenario, sizing)

    sizing["storage_total_gib"] = round(
        video["hot_gib"] + video["cold_gib"]
        + knowledge["raw_gib"] + knowledge["vector_gib"], 2)
    sizing["storage_total_gib_with_oversubscribe"] = round(
        sizing["storage_total_gib"] * STORAGE_OVERSUBSCRIBE, 2)
    return sizing
