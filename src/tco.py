# -*- coding: utf-8 -*-
"""三年 TCO 测算：配置清单 -> 成本明细表。

设计原则（对应 JD 职责 3「支撑招投标管理」里的成本可解释性要求）：
1. **每一行成本都能点回价目表的具体行**，带 `price_ref` 字段（单价 + 单位 + 来源 URL + 抓取日期）。
2. **推导链与金额同行输出**（`derivation`），评审时不用反推。
3. **缺价格不猜**：官方未提供可回溯公开单价的项，一律进 `unpriced_items`，
   从总额中剔除并显式列示 —— 宁可少算，不可虚报。
4. 不含税、不含商务折扣、不含实施人力（见 `data/assumptions.md` B 节）。
"""
from __future__ import annotations

from .pricing import get_storage_tier, load_network_prices

YEARS = 3
MONTHS = YEARS * 12

# 快照容量占总存储比例 —— 与 data/assumptions.md 保持一致，登记为 A-10
SNAPSHOT_RATIO = 0.10
# 冷数据在公开价目表内没有更低档位，暂按高性能云硬盘计，局限在输出中显式说明
COLD_STORAGE_TIER = "performance_hdd"
HOT_STORAGE_TIER = "performance_hdd"
VECTOR_STORAGE_TIER = "general_ssd"

# 公网包月带宽阶梯（广州，常规 BGP IP）—— 单位元/Mbps/月
BW_TIERS = [
    (2.0, "public_bandwidth_le_2mbps"),
    (5.0, "public_bandwidth_2_5mbps"),
    (float("inf"), "public_bandwidth_gt_5mbps"),
]


class TcoError(Exception):
    pass


def bandwidth_monthly_cny(peak_mbps: float) -> tuple[float, str]:
    """按官方阶梯规则算包月带宽费用。

    阶梯规则（官方示例：广州 15 Mbps 一个月）：

        ≤2Mbps 部分      × 20 元/Mbps/月
        2Mbps<带宽≤5Mbps  × 25 元/Mbps/月
        >5Mbps 部分       × 90 元/Mbps/月

    Returns
    -------
    (月费, 推导说明)
    """
    if peak_mbps < 0:
        raise TcoError("带宽峰值不能为负数：%r" % peak_mbps)
    prices = load_network_prices()
    total = 0.0
    parts = []
    lower = 0.0
    for upper, item in BW_TIERS:
        if peak_mbps <= lower:
            break
        span = min(peak_mbps, upper) - lower
        price = prices[item]
        total += span * price.value
        parts.append("%.0f-%.0f Mbps × %s = %.2f 元/月"
                     % (lower, min(peak_mbps, upper), price.value, span * price.value))
        lower = upper
        if upper == float("inf"):
            break
    return total, "阶梯累加：" + " + ".join(parts) if parts else "带宽峰值为 0，不计公网带宽费"


def _line(category: str, item: str, qty: float, unit: str, unit_price: float,
          price_ref: str, derivation: str) -> dict:
    monthly = qty * unit_price
    return {
        "category": category,
        "item": item,
        "qty": round(qty, 2),
        "unit": unit,
        "unit_price": unit_price,
        "unit_price_source": price_ref,
        "derivation": derivation,
        "monthly_cny": round(monthly, 2),
        "total_cny": round(monthly * MONTHS, 2),
    }


def compute_tco(scenario: dict, sizing: dict) -> dict:
    """由配置清单算出三年 TCO 明细。"""
    lines: list[dict] = []
    unpriced: list[dict] = []

    # ---------- 1. GPU 节点 ----------
    gpu = sizing.get("gpu") or {}
    if gpu.get("sku") is not None and gpu.get("gpu_nodes", 0) > 0:
        nodes = gpu.get("gpu_nodes_with_ha", gpu["gpu_nodes"])
        sku = gpu["sku"]  # size() 已校验过 SKU 存在性
        lines.append(_line(
            "计算", "GPU 云服务器（%s）" % sku.sku, nodes, "节点·月", sku.monthly_cny,
            sku.cite(),
            "需求 %d 卡 / 每节点 %d 卡 -> %d 节点；叠加 %s 后按 %d 节点计价"
            % (gpu["gpu_cards"], gpu["gpu_per_node"], gpu["gpu_nodes"],
               sizing.get("redundancy", {}).get("mode", "-"), nodes)))
        # 需求拆解（实时流解析 / 会话推理 / 转码）不单独计价，已包含在节点数里，
        # 拆解过程保留在 sizing["gpu"]["demands"] 与 output 文档中。
    else:
        unpriced.append({
            "item": "GPU 算力",
            "reason": "场景未定义 GPU 负载（ai_inference_fps / ai_concurrent_sessions / "
                      "transcode_streams_concurrent 均为 0）",
            "action": "如需含推理/转码算力，在场景 JSON 中补齐对应字段后重跑",
        })

    # ---------- 2. 存储 ----------
    hot_tier = get_storage_tier(HOT_STORAGE_TIER)
    cold_tier = get_storage_tier(COLD_STORAGE_TIER)

    video = sizing.get("video") or {}
    if video.get("hot_gib", 0) > 0:
        lines.append(_line(
            "存储", "热存储（%s）" % hot_tier.item, video["hot_gib"], "GiB·月",
            hot_tier.value, hot_tier.cite(),
            "热容量 %.2f GiB（%s）" % (video["hot_gib"], video.get("trace", "-"))))
    if video.get("cold_gib", 0) > 0:
        lines.append(_line(
            "存储", "冷存储（按 %s 计，见局限）" % cold_tier.item, video["cold_gib"],
            "GiB·月", cold_tier.value, cold_tier.cite(),
            "冷容量 %.2f GiB；公开价目表无冷/归档档位，暂按高性能云硬盘单价计，"
            "实际冷存储应走归档类存储，金额会偏高" % video["cold_gib"]))

    know = sizing.get("knowledge") or {}
    if know.get("raw_gib", 0) > 0:
        t = get_storage_tier(VECTOR_STORAGE_TIER)
        lines.append(_line(
            "存储", "知识库原始语料（%s）" % t.item, know["raw_gib"], "GiB·月",
            t.value, t.cite(), "语料 %.0f GB" % know["raw_gib"]))
    if know.get("vector_gib", 0) > 0:
        t = get_storage_tier(VECTOR_STORAGE_TIER)
        lines.append(_line(
            "存储", "向量索引（%s）" % t.item, know["vector_gib"], "GiB·月",
            t.value, t.cite(), know.get("trace", "-")))

    # 快照：按存储总量的固定比例
    total_storage = sizing.get("storage_total_gib", 0.0)
    if total_storage > 0:
        snap_tier = get_storage_tier("snapshot")
        snap_gib = total_storage * SNAPSHOT_RATIO
        lines.append(_line(
            "存储", "快照容量（%s）" % snap_tier.item, snap_gib, "GiB·月",
            snap_tier.value, snap_tier.cite(),
            "存储总量 %.2f GiB × 快照比例 %.0f%%（假设 A-10）= %.2f GiB"
            % (total_storage, SNAPSHOT_RATIO * 100, snap_gib)))

    # ---------- 3. 网络 ----------
    net = sizing.get("network") or {}
    peak = net.get("peak_mbps", 0.0)
    if peak > 0:
        fee, chain = bandwidth_monthly_cny(peak)
        ref = " + ".join(
            "%s = %s %s（来源 %s）" % (i, load_network_prices()[i].value,
                                       load_network_prices()[i].unit,
                                       load_network_prices()[i].source_url)
            for i in ("public_bandwidth_le_2mbps", "public_bandwidth_2_5mbps",
                      "public_bandwidth_gt_5mbps"))
        lines.append({
            "category": "网络",
            "item": "公网出向带宽（包月阶梯）",
            "qty": peak,
            "unit": "Mbps",
            "unit_price": round(fee / peak, 4) if peak else 0.0,
            "unit_price_source": ref,
            "derivation": "%s -> %.2f 元/月" % (chain, fee),
            "monthly_cny": round(fee, 2),
            "total_cny": round(fee * MONTHS, 2),
        })

    # ---------- 4. 显式剔除项（不猜数） ----------
    cpu_nodes = (sizing.get("cpu") or {}).get("cpu_nodes", 0)
    if cpu_nodes > 0:
        unpriced.append({
            "item": "通用计算节点（%d 节点）" % cpu_nodes,
            "reason": "云厂商官方文档只提供在线价格计算器，未给出可回溯的公开单价表；"
                      "本工具不猜测单价",
            "action": "按实际规格与当日官方报价，在 compute_tco() 的「计算」科目下补一行，"
                      "并把单价与来源 URL 一并写入 data/pricing/",
        })
    unpriced.extend([
        {"item": "实施与迁移人力", "reason": "属项目交付成本，非云资源标价",
         "action": "按项目单独核算，不并入云资源 TCO"},
        {"item": "税费与商务折扣", "reason": "本工具取公开标价，不含税费与任何商务折扣",
         "action": "以采购当日官方报价与合同为准"},
    ])

    monthly_total = round(sum(x["monthly_cny"] for x in lines), 2)
    return {
        "scenario_id": sizing.get("scenario_id"),
        "years": YEARS,
        "months": MONTHS,
        "lines": lines,
        "monthly_total_cny": monthly_total,
        "total_cny": round(monthly_total * MONTHS, 2),
        "unpriced_items": unpriced,
        "pricing_note": "全部单价取自公开标价页，含来源 URL 与抓取日期；"
                        "不含税、不含商务折扣、不含实施人力。",
    }
