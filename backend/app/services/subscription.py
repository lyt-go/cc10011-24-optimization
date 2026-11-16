"""订阅推送业务规则：状态流转、字段校验、筛选口径，以及洞察报告快照的消费。

订阅只消费用户洞察的"当前"快照：读到之后先写入本模块的交付记录（推送流水），
再更新自身的下次推送时间。写入步骤在 ``consume_insight_snapshot`` 里，整个动作
和洞察侧共享同一个 ``store.transaction()``，失败时不会留下只有一半的交付记录。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from app.services.userinsight import InsightError, current_snapshot
from app.store import store

MODULE = "subscription"
DELIVERY_TABLE = "_insight_deliveries"
REQUIRED_FIELDS = ["订阅编号", "订阅内容", "接收人"]
STATUS_ORDER = ["草稿", "订阅评审中", "已启用", "已暂停"]
ACTION_RULES = {"提交订阅评审": "订阅评审中", "启用订阅": "已启用", "暂停订阅": "已暂停"}
NEGATIVE_ACTIONS = []
PUSHABLE_STATUS = "已启用"


class SubscriptionService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("订阅编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        enriched = [self._enrich(row) for row in rows]
        total = len(enriched)
        start = max(page - 1, 0) * size
        return enriched[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        row = store.find(MODULE, entry_id)
        return self._enrich(row) if row is not None else None

    def _enrich(self, row: dict[str, Any]) -> dict[str, Any]:
        enriched = dict(row)
        business_key = str(row.get("关联报告") or "")
        snapshot = current_snapshot(business_key) if business_key else None
        enriched["关联报告"] = business_key or None
        enriched["快照编号"] = snapshot.get("快照编号") if snapshot else None
        enriched["快照版本"] = snapshot.get("版本") if snapshot else None
        enriched["快照区间"] = snapshot.get("数据区间") if snapshot else None
        return enriched

    def summary(self) -> dict[str, int]:
        rows = store.rows(MODULE)
        return {
            "订阅配置总数": len(rows),
            "已启用订阅": sum(1 for row in rows if row.get("status") == "已启用"),
            "评审中订阅": sum(1 for row in rows if row.get("status") == "订阅评审中"),
            "已消费快照": sum(1 for row in rows if row.get("最近消费快照")),
        }

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"订阅配置 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于订阅推送可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"订阅配置已{action}"

    # ------------------------------------------------------------------ 消费洞察快照
    def consume_insight_snapshot(
        self, entry_id: int, *, snapshot_code: str | None = None
    ) -> tuple[dict[str, Any] | None, str]:
        """沿真实数据流消费洞察快照：读取当前快照 → 落交付记录 → 推进下次推送时间。

        只允许已启用的订阅消费；重复消费同一快照会被幂等拦下，不会重复推送。
        """
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"订阅配置 {entry_id} 不存在或已归档"
        if entry.get("status") != PUSHABLE_STATUS:
            return None, f"订阅配置 {entry_id} 当前状态为「{entry.get('status')}」，只有已启用订阅可以消费洞察快照"
        business_key = str(entry.get("关联报告") or "").strip()
        if not business_key:
            return None, f"订阅配置 {entry_id} 未关联洞察报告，无法消费快照"
        try:
            with store.transaction():
                snapshot = current_snapshot(business_key)
                if snapshot is None:
                    raise InsightError(f"关联报告 {business_key} 还没有当前快照，请先生成洞察报告")
                code = str(snapshot.get("快照编号"))
                if snapshot_code and str(snapshot_code) != code:
                    raise InsightError(
                        f"指定快照 {snapshot_code} 不是报告 {business_key} 的当前快照（当前为 {code}）"
                    )
                for record in store.rows(DELIVERY_TABLE):
                    if record.get("订阅编号") == entry.get("订阅编号") and record.get("快照编号") == code:
                        raise InsightError(f"订阅 {entry.get('订阅编号')} 已消费过快照 {code}，本次不再重复推送")

                delivered_at = datetime.now()
                delivery_id = store.next_id(DELIVERY_TABLE)
                store.rows(DELIVERY_TABLE).append({
                    "id": delivery_id,
                    "交付编号": f"DLVR-{delivery_id:04d}",
                    "订阅id": entry_id,
                    "订阅编号": entry.get("订阅编号"),
                    "业务键": business_key,
                    "快照编号": code,
                    "快照版本": snapshot.get("版本"),
                    "数据区间": snapshot.get("数据区间"),
                    "核心发现": [item.get("结论") for item in snapshot.get("核心发现") or []],
                    "接收人": entry.get("接收人"),
                    "推送渠道": entry.get("推送渠道"),
                    "状态": "已推送",
                    "交付时间": delivered_at.strftime("%Y-%m-%d %H:%M"),
                })
                entry["最近消费快照"] = code
                entry["最近推送时间"] = delivered_at.strftime("%Y-%m-%d %H:%M")
                entry["下次推送时间"] = _next_push_time(
                    str(entry.get("下次推送时间") or ""), delivered_at
                )
                entry["pending"] = False
                entry["abnormal"] = False
        except InsightError as exc:
            return None, str(exc)
        except Exception as exc:  # 事务已回滚，交付记录与下次推送时间都保持原样
            return None, f"消费洞察快照失败，事务已回滚：{exc}"
        return self.get_entry(entry_id), (
            f"订阅 {entry.get('订阅编号')} 已消费快照 {code}（{snapshot.get('数据区间')}），"
            f"下次推送时间更新为 {entry.get('下次推送时间')}"
        )

    def deliveries(self, entry_id: int) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, []
        rows = [
            dict(record) for record in store.rows(DELIVERY_TABLE)
            if record.get("订阅id") == entry_id
        ]
        rows.sort(key=lambda item: int(item.get("id", 0)), reverse=True)
        return dict(entry), rows


def _next_push_time(previous: str, delivered_at: datetime) -> str:
    try:
        value = datetime.strptime(previous, "%Y-%m-%d %H:%M")
    except ValueError:
        return (delivered_at + timedelta(days=1)).strftime("%Y-%m-%d %H:%M")
    while value <= delivered_at:
        value += timedelta(days=1)
    return value.strftime("%Y-%m-%d %H:%M")
