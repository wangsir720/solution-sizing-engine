"""包标记文件。

结构说明：`src` 下是纯计算逻辑与 I/O，不含任何第三方依赖；
CLI 通过 `python -m src.cli` 调用。
"""

__all__ = ["pricing", "sizing", "tco", "migration", "ops_metrics", "value_model", "cli"]
