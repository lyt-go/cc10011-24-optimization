"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。

以 ``_`` 开头的是模块内部表（人群、批次、快照、扫描缓存、交付记录），
它们不参与运营概览的模块统计，但与业务表共享同一份事务：写入失败时整组回滚，
不会留下"快照写了一半、报告状态已经变了"的半成品。
"""
from __future__ import annotations

import copy
import threading
from contextlib import contextmanager
from typing import Any, Iterator

from app.insight_seed import SEED_INTERNAL
from app.seed import SEED_ROWS


class Store:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.reset()

    def reset(self) -> None:
        """恢复示例数据；测试与本地重放都靠它拿到干净的起点。"""
        with self._lock:
            self._tables: dict[str, list[dict[str, Any]]] = {
                name: [dict(row) for row in rows]
                for name, rows in {**SEED_ROWS, **SEED_INTERNAL}.items()
            }

    def module_names(self) -> list[str]:
        """只暴露业务模块；内部表由所属模块自己维护。"""
        return sorted(name for name in self._tables if not name.startswith("_"))

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def next_id(self, module: str) -> int:
        rows = self.rows(module)
        return max((int(row.get("id", 0)) for row in rows), default=0) + 1

    @contextmanager
    def transaction(self) -> Iterator["Store"]:
        """整组写入的事务：成功一起生效，异常则恢复到进入事务前的状态。

        内存实现用整表深拷贝做保存点。请求量小、表也小，换来的是失败时
        绝不会残留半成品；换成数据库时这层会替换成真正的 begin/commit/rollback。
        """
        with self._lock:
            snapshot = copy.deepcopy(self._tables)
            try:
                yield self
            except BaseException:
                self._tables = snapshot
                raise

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            rows = self.rows(name)
            modules.append({
                "name": name,
                "created": len(rows),
                "pending": sum(1 for row in rows if row.get("pending")),
                "abnormal": sum(1 for row in rows if row.get("abnormal")),
            })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}


store = Store()
