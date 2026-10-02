# -*- coding: utf-8 -*-
"""命令行入口：输入场景 -> 输出一整套方案包。

    python -m src.cli --list
    python -m src.cli --scenario gov_video_surveillance
    python -m src.cli --all

输出落在 `output/<scenario_id>/`，包含方案文档（Markdown）、TCO 明细（CSV）
与迁移检查清单（CSV），可直接作为方案包草稿使用。
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys

from .migration import build_migration_plan
from .ops_metrics import build_ops_metrics
from .pricing import PricingError, list_scenarios, load_scenario
from .sizing import size
from .tco import compute_tco
from .value_model import build_value_model

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT_DIR = os.path.join(ROOT, "output")


def _w(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)


def _md_table(headers: list[str], rows: list[list]) -> str:
    out = ["| " + " | ".join(headers) + " |",
           "|" + "|".join(["---"] * len(headers)) + "|"]
    for r in rows:
        out.append("| " + " | ".join("" if c is None else str(c) for c in r) + " |")
    return "\n".join(out)


def render_solution_doc(scenario: dict, sizing: dict, tco: dict,
                        migration: dict, ops: dict, value: dict) -> str:
    gpu = sizing["gpu"]
    cpu = sizing["cpu"]
    sku = gpu.get("sku")
    L: list[str] = []
    L.append("# %s · 方案包（自动生成）" % sizing["scenario_name"])
    L.append("")
    L.append("> 本文由 `src/cli.py` 从 `data/scenarios/%s.json` 自动生成，"
             "所有单价均可回溯至 `data/pricing/` 下的公开价目表。"
             "**数据为公开信息与行业典型值，非真实交付项目。**" % scenario["scenario_id"])
    L.append("")
    L.append("## 1. 场景与规模")
    L.append(_md_table(
        ["项", "值"],
        [["场景编号", sizing["scenario_id"]],
         ["行业", scenario.get("industry", "-")],
         ["并发用户数", sizing["network"]["users"]],
         ["摄像机路数", scenario.get("workload", {}).get("camera_count", 0)],
         ["接口 QPS", scenario.get("workload", {}).get("api_qps", 0)],
         ["可用性目标", scenario.get("service_level", {}).get("availability_target")],
         ["RTO / RPO", "%s 分钟 / %s 分钟" % (scenario.get("service_level", {}).get("rto_minutes"),
                                            scenario.get("service_level", {}).get("rpo_minutes"))]]))
    L.append("")
    L.append("## 2. 算力与资源配置")
    L.append(_md_table(
        ["资源", "数量", "推导依据"],
        [["GPU 节点", "%d 台（%s）" % (gpu.get("gpu_nodes_with_ha", gpu.get("gpu_nodes", 0)),
                                       sizing.get("redundancy", {}).get("mode", "-")),
          gpu.get("trace", "-")],
         ["GPU 规格", sku.sku if sku else "-", sku.cite() if sku else "-"],
         ["通用计算节点", "%d 台" % cpu.get("cpu_nodes_with_ha", cpu.get("cpu_nodes", 0)),
          cpu.get("trace", "-")],
         ["热存储", "%.2f GiB" % sizing["video"]["hot_gib"], sizing["video"].get("trace", "-")],
         ["冷存储", "%.2f GiB" % sizing["video"]["cold_gib"], sizing["video"].get("trace", "-")],
         ["存储合计（含超分）", "%.2f GiB" % sizing["storage_total_gib_with_oversubscribe"],
          "超分比见 data/assumptions.md A-09"],
         ["公网出向带宽", "%.2f Mbps" % sizing["network"]["peak_mbps"],
          sizing["network"].get("trace", "-")]]))
    L.append("")
    if gpu.get("demands"):
        L.append("### 2.1 GPU 需求拆解")
        L.append(_md_table(
            ["用途", "需求卡数（小数）", "推导链"],
            [[d["purpose"], d["cards_raw"], d["derivation"]] for d in gpu["demands"]]))
        L.append("")
        L.append("> 卡数向上取整到整机粒度后的上浮量已在配置清单中显式标注，不做静默吸收。")
        L.append("")

    L.append("## 3. 三年 TCO 明细")
    L.append(_md_table(
        ["科目", "项", "数量", "单位", "单价(元)", "月费(元)", "三年合计(元)"],
        [[l["category"], l["item"], l["qty"], l["unit"], l["unit_price"],
          l["monthly_cny"], l["total_cny"]] for l in tco["lines"]]))
    L.append("")
    L.append("**月费合计 %.2f 元 ｜ 三年合计 %.2f 元**" % (tco["monthly_total_cny"], tco["total_cny"]))
    L.append("")
    L.append("### 3.1 每行成本的价格来源")
    for l in tco["lines"]:
        L.append("- **%s** —— %s" % (l["item"], l["unit_price_source"]))
    L.append("")
    L.append("### 3.2 未计入项（不猜数）")
    L.append(_md_table(
        ["未计入项", "原因", "补齐方式"],
        [[u["item"], u["reason"], u["action"]] for u in tco["unpriced_items"]]))
    L.append("")

    L.append("## 4. 统建迁移路径")
    L.append("关键路径：%s" % migration["critical_path"])
    L.append("")
    L.append("> %s" % migration["key_constraint"])
    L.append("")
    for w in migration["waves"]:
        L.append("### 波次 %d · %s（风险：%s）" % (w["wave"], w["name"], w["risk"]))
        L.append("")
        L.append(_md_table(
            ["要素", "内容"],
            [["迁移对象", w["target"]],
             ["割接窗口", w["cutover_window"]],
             ["双写安排", w["dual_write"]],
             ["灰度策略", w["gray"]],
             ["业务影响面", "%s；预计中断 %s" % (w["impact"]["scope"], w["impact"]["downtime"])],
             ["回退方案", w["rollback"]],
             ["工时估算", "%.0f 小时" % w["duration_hours"]]]))
        L.append("")
        L.append("割接检查清单：")
        L.append("")
        for c in w["checklist"]:
            L.append("- [ ] %s" % c)
        L.append("")

    L.append("## 5. 交付运营指标基线")
    L.append(_md_table(
        ["指标", "定义", "目标值", "测量方式", "口径依据", "是否引用公开标准"],
        [[m["name"], m["definition"], m["target"], m["measure"], m["basis"],
          "是" if m["basis_is_public_standard"] else "否"] for m in ops["metrics"]]))
    L.append("")
    L.append("> %s" % ops["scope_note"])
    L.append("")
    L.append("合规参照：%s —— %s" % (ops["compliance_reference"]["name"],
                                    ops["compliance_reference"]["note"]))
    L.append("")

    L.append("## 6. 业务价值测算")
    L.append(_md_table(
        ["价值项", "状态", "计算口径", "所需输入 / 说明"],
        [[q["item"], q["status"], q["formula"],
          (q.get("what_needed") or q.get("note") or "-")] for q in value["quantified"]]))
    L.append("")
    L.append("> %s" % value["discipline_note"])
    L.append("")
    L.append("## 7. 本方案未做的事（边界声明）")
    for line in [
        "不含实施与迁移人力成本：本文只测算云资源侧支出（不含税、不含商务折扣）。",
        "不含 AI 训练算力规划：只覆盖推理与转码的训练后算力。",
        "不含跨云与多云比价：仅测算单一云、单一地域的方案包。",
        "不含应用级代码依赖扫描：迁移波次按数据层/应用层/流量层划分，"
        "真实波次须以实际依赖分析结果为准。",
        "不含 SLA 违约赔付测算：可用性目标达成后的赔付条件需逐条核对服务协议。",
    ]:
        L.append("- %s" % line)
    L.append("")
    L.append("---")
    L.append("生成命令：`python -m src.cli --scenario %s`" % scenario["scenario_id"])
    L.append("")
    return "\n".join(L)


def run(scenario_id: str) -> str:
    scenario = load_scenario(scenario_id)
    sizing = size(scenario)
    tco = compute_tco(scenario, sizing)
    migration = build_migration_plan(scenario, sizing)
    ops = build_ops_metrics(scenario, sizing)
    value = build_value_model(scenario, sizing, tco)

    base = os.path.join(OUTPUT_DIR, scenario_id)
    doc = render_solution_doc(scenario, sizing, tco, migration, ops, value)
    _w(os.path.join(base, "solution_package.md"), doc)

    # TCO 明细 CSV
    with open(os.path.join(base, "tco_breakdown.csv"), "w", encoding="utf-8-sig",
              newline="") as f:
        wtr = csv.writer(f)
        wtr.writerow(["科目", "项", "数量", "单位", "单价(元)", "单价来源", "推导链",
                      "月费(元)", "三年合计(元)"])
        for l in tco["lines"]:
            wtr.writerow([l["category"], l["item"], l["qty"], l["unit"], l["unit_price"],
                          l["unit_price_source"], l["derivation"],
                          l["monthly_cny"], l["total_cny"]])
        wtr.writerow([])
        wtr.writerow(["月费合计", "", "", "", "", "", "", tco["monthly_total_cny"],
                      tco["total_cny"]])

    # 迁移检查清单 CSV
    with open(os.path.join(base, "migration_checklist.csv"), "w", encoding="utf-8-sig",
              newline="") as f:
        wtr = csv.writer(f)
        wtr.writerow(["波次", "波次名", "风险", "检查项"])
        for w in migration["waves"]:
            for c in w["checklist"]:
                wtr.writerow([w["wave"], w["name"], w["risk"], c])

    # 机器可读的全量结果
    _w(os.path.join(base, "result.json"), json.dumps(
        {"scenario": scenario, "sizing": _jsonable(sizing), "tco": tco,
         "migration": migration, "ops": ops, "value": value},
        ensure_ascii=False, indent=2, default=str))

    return doc


def _jsonable(obj):
    """把 dataclass 等非 JSON 类型转成可序列化结构。"""
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items() if k != "sku"}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if hasattr(obj, "__dataclass_fields__"):
        return {k: _jsonable(v) for k, v in obj.__dict__.items()}
    return obj


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="src.cli", description="行业解决方案测算引擎")
    ap.add_argument("--scenario", help="场景编号（见 data/scenarios/）")
    ap.add_argument("--all", action="store_true", help="跑全部场景")
    ap.add_argument("--list", action="store_true", help="列出可用场景")
    args = ap.parse_args(argv)

    if args.list or not (args.scenario or args.all):
        print("可用场景：")
        for s in list_scenarios():
            print("  - %s" % s)
        return 0

    targets = list_scenarios() if args.all else [args.scenario]
    for sid in targets:
        try:
            run(sid)
        except (PricingError, KeyError, ValueError) as e:
            print("场景 %s 测算失败：%s: %s" % (sid, type(e).__name__, e), file=sys.stderr)
            return 1
        print("已生成：output/%s/{solution_package.md, tco_breakdown.csv, "
              "migration_checklist.csv, result.json}" % sid)
    return 0


if __name__ == "__main__":
    sys.exit(main())
