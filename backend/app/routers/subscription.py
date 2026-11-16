"""订阅推送接口：维护订阅配置，覆盖提交订阅评审、启用订阅、暂停订阅等动作。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.subscription import SubscriptionService

router = APIRouter(prefix="/api/subscription", tags=["订阅推送"])

service = SubscriptionService()

LIST_FIELDS = ["订阅编号", "订阅内容", "接收人", "推送渠道", "推送频率", "数据粒度", "静默时段", "下次推送时间", "关联报告", "快照编号"]
STATUSES = ["草稿", "订阅评审中", "已启用", "已暂停"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按订阅编号检索"),
    status: str | None = Query(default=None, description="草稿、订阅评审中、已启用、已暂停"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按订阅编号与状态过滤订阅推送列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


# 静态路径必须写在 /{entry_id} 之前，否则会被动态路由当成 id 匹配走。
@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出订阅推送清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "subscription", "total": total, "items": items}


@router.get("/summary", response_model=dict)
def summary() -> dict[str, int]:
    """订阅看板：总数、启用数、评审中数量与已消费洞察快照的订阅数。"""
    return service.summary()


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条订阅配置明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"订阅配置 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条订阅配置，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="订阅配置已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条订阅配置执行提交订阅评审、启用订阅、暂停订阅；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/consume-insight", response_model=ActionResult)
def consume_insight(entry_id: int, payload: EntryPayload) -> ActionResult:
    """订阅消费用户洞察的当前快照：落交付记录并推进下次推送时间，重复消费会被幂等拦下。"""
    snapshot_code = str(payload.values.get("snapshot_code") or "").strip() or None
    entry, message = service.consume_insight_snapshot(entry_id, snapshot_code=snapshot_code)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.get("/{entry_id}/deliveries", response_model=dict)
def list_deliveries(entry_id: int) -> dict[str, Any]:
    """订阅的快照交付流水：每次推送消费了哪个快照、覆盖哪个数据区间。"""
    entry, items = service.deliveries(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"订阅配置 {entry_id} 不存在或已归档")
    return {"entry": entry, "items": items, "total": len(items)}
