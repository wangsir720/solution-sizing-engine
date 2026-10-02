# -*- coding: utf-8 -*-
"""端到端测试：场景 JSON -> 方案包文件。"""
import os

from src import cli
from src.pricing import list_scenarios

GENERATED = [
    "solution_package.md",
    "tco_breakdown.csv",
    "migration_checklist.csv",
    "result.json",
]


def test_all_scenarios_generate_all_artifacts():
    for sid in list_scenarios():
        cli.run(sid)
        base = os.path.join(cli.OUTPUT_DIR, sid)
        for name in GENERATED:
            path = os.path.join(base, name)
            assert os.path.isfile(path), "缺少产物 %s" % path
            assert os.path.getsize(path) > 0, "产物为空 %s" % path


def test_solution_doc_has_required_sections():
    doc = cli.run("gov_video_surveillance")
    for heading in ("## 1. 场景与规模", "## 2. 算力与资源配置", "## 3. 三年 TCO 明细",
                    "## 4. 统建迁移路径", "## 5. 交付运营指标基线",
                    "## 6. 业务价值测算", "## 7. 本方案未做的事"):
        assert heading in doc, "方案文档缺少章节：%s" % heading


def test_solution_doc_declares_non_client_data():
    doc = cli.run("gov_video_surveillance")
    assert "非真实交付项目" in doc


def test_doc_prices_all_carry_urls():
    doc = cli.run("finance_ai_assistant")
    assert "https://" in doc
    # 不应出现任何真实客户名 / 合同金额类字段
    for banned in ("合同金额", "客户名称", "内部报价"):
        assert banned not in doc, "方案文档出现敏感字段：%s" % banned
