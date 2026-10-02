# -*- coding: utf-8 -*-
"""价目表加载器 —— 所有成本计算的唯一价格来源。

设计要点（借鉴 Cyclenerd/google-cloud-pricing-cost-calculator 的「价格表外置」结构）：
价格与计算逻辑彻底分离。`data/pricing/*.csv` 是纯数据，本模块只负责读、
校验、索引。任何成本函数都不允许内置价格字面量。

价格表文件头必须自带 `source_url` 与 `fetched_at` 两列 —— 每一笔成本在后续
输出中都要能回溯到「哪张公开价格表的哪一行、什么时候抓的」。
"""
from __future__ import annotations

import csv
import os
from dataclasses import dataclass

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
PRICING_DIR = os.path.join(DATA_DIR, "pricing")
SCENARIO_DIR = os.path.join(DATA_DIR, "scenarios")


class PricingError(Exception):
    """价目表缺失或格式错误。不做静默降级 —— 缺价格必须报错，不能用 0 顶替。"""


@dataclass(frozen=True)
class PriceRef:
    """一条价格的完整回溯信息。"""
    item: str
    value: float
    unit: str
    source_url: str
    fetched_at: str
    note: str

    def cite(self) -> str:
        return "%s = %s %s（来源 %s，抓取 %s）" % (
            self.item, self.value, self.unit, self.source_url, self.fetched_at)


@dataclass(frozen=True)
class GpuSku:
    """GPU 云服务器规格 + 月价。"""
    sku: str
    gpu_model: str
    gpu_memory_gib: float
    vcpu_cores: int
    mem_gib: int
    monthly_cny: float
    source_url: str
    fetched_at: str
    note: str

    @property
    def gpu_count(self) -> int:
        """从 SKU 名解析 GPU 卡数，例如 GNV4.44CU.4GPU -> 4。"""
        return int(self.sku.split(".")[-1].replace("GPU", ""))

    @property
    def vcpu_per_gpu(self) -> float:
        return self.vcpu_cores / self.gpu_count

    @property
    def mem_per_gpu(self) -> float:
        return self.mem_gib / self.gpu_count

    def cite(self) -> str:
        return "%s（%s，%d vCPU / %d GB，%d 卡）月价 %s 元 —— %s" % (
            self.sku, self.gpu_model, self.vcpu_cores, self.mem_gib,
            self.gpu_count, self.monthly_cny, self.source_url)


def _read_csv(path: str) -> list[dict]:
    if not os.path.isfile(path):
        raise PricingError("价目表文件不存在：%s" % path)
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        raise PricingError("价目表为空：%s" % path)
    return rows


def load_gpu_skus() -> dict[str, GpuSku]:
    """加载 GPU 规格价目表，按 sku 索引。"""
    path = os.path.join(PRICING_DIR, "gpu_instance.csv")
    out: dict[str, GpuSku] = {}
    for r in _read_csv(path):
        sku = r["sku"].strip()
        if sku in out:
            raise PricingError("gpu_instance.csv 中 sku 重复：%s" % sku)
        for required in ("source_url", "fetched_at", "monthly_cny"):
            if not r.get(required, "").strip():
                raise PricingError("gpu_instance.csv 缺少字段 %s（sku=%s）" % (required, sku))
        out[sku] = GpuSku(
            sku=sku,
            gpu_model=r["gpu_model"].strip(),
            gpu_memory_gib=float(r["gpu_memory_gib"]),
            vcpu_cores=int(r["vcpu_cores"]),
            mem_gib=int(r["mem_gib"]),
            monthly_cny=float(r["monthly_cny"]),
            source_url=r["source_url"].strip(),
            fetched_at=r["fetched_at"].strip(),
            note=r.get("note", "").strip(),
        )
    return out


def load_storage_tiers() -> dict[str, PriceRef]:
    """加载存储分层价目表，按 tier 索引（单位：元/GiB/月）。"""
    path = os.path.join(PRICING_DIR, "storage_tier.csv")
    out: dict[str, PriceRef] = {}
    for r in _read_csv(path):
        out[r["tier"].strip()] = PriceRef(
            item=r["medium"].strip(),
            value=float(r["monthly_cny_per_gib"]),
            unit="元/GiB/月",
            source_url=r["source_url"].strip(),
            fetched_at=r["fetched_at"].strip(),
            note=r.get("note", "").strip(),
        )
    return out


def load_network_prices() -> dict[str, PriceRef]:
    """加载网络价目表，按 item 索引。"""
    path = os.path.join(PRICING_DIR, "network_bandwidth.csv")
    out: dict[str, PriceRef] = {}
    for r in _read_csv(path):
        out[r["item"].strip()] = PriceRef(
            item=r["item"].strip(),
            value=float(r["monthly_cny"]),
            unit=r["unit"].strip(),
            source_url=r["source_url"].strip(),
            fetched_at=r["fetched_at"].strip(),
            note=r.get("note", "").strip(),
        )
    return out


def get_gpu_sku(sku_id: str) -> GpuSku:
    skus = load_gpu_skus()
    if sku_id not in skus:
        raise PricingError(
            "场景引用了不存在的 GPU 规格 %r；可用规格：%s"
            % (sku_id, ", ".join(sorted(skus))))
    return skus[sku_id]


def get_storage_tier(tier: str) -> PriceRef:
    tiers = load_storage_tiers()
    if tier not in tiers:
        raise PricingError(
            "未知存储层 %r；可用层：%s" % (tier, ", ".join(sorted(tiers))))
    return tiers[tier]


def get_network_price(item: str) -> PriceRef:
    prices = load_network_prices()
    if item not in prices:
        raise PricingError(
            "未知网络价格项 %r；可用项：%s" % (item, ", ".join(sorted(prices))))
    return prices[item]


def list_scenarios() -> list[str]:
    if not os.path.isdir(SCENARIO_DIR):
        return []
    return sorted(f[:-5] for f in os.listdir(SCENARIO_DIR) if f.endswith(".json"))


def load_scenario(scenario_id: str) -> dict:
    """读取场景参数 JSON。缺字段直接 KeyError/ValueError 抛出，不做默认值兜底。"""
    import json
    path = os.path.join(SCENARIO_DIR, "%s.json" % scenario_id)
    if not os.path.isfile(path):
        raise PricingError(
            "场景 %r 不存在；可用场景：%s" % (scenario_id, ", ".join(list_scenarios())))
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)
