"""用户洞察接口：增量刷新、报告快照、数据区间与核心发现追溯。

路由只做参数转接：所有状态流转、区间取舍与事务边界都在 ``app.services.userinsight``。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import (
    ActionResult,
    EntryPayload,
    InsightBatchRefreshPayload,
    InsightCrowdPayload,
    InsightRefreshPayload,
    PageResult,
)
from app.services.userinsight import CROWD_EDITABLE_FIELDS, UserinsightService

router = APIRouter(prefix="/api/userinsight", tags=["用户洞察"])

service = UserinsightService()

LIST_FIELDS = [
    "报告编号", "分析主题", "目标人群", "关键指标", "核心发现",
    "数据区间", "生成时间", "下次生成", "当前快照编号", "人群最新版本", "刷新状态",
]
STATUSES = ["草稿", "生成中", "已发布", "已归档"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按报告编号或分析主题检索"),
    status: str | None = Query(default=None, description="草稿、生成中、已发布、已归档"),
    refresh: str | None = Query(default=None, description="传 stale 只看人群变更后待刷新的报告"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按编号、状态与刷新状态过滤洞察报告；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(
        keyword=keyword, status=status, page=page, size=size, stale_only=refresh == "stale"
    )
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/summary", response_model=dict)
def summary() -> dict[str, Any]:
    """洞察看板：报告数、待刷新数、当前快照数与最近的下次生成时间。"""
    return service.summary()


@router.get("/crowds", response_model=list[dict])
def list_crowds() -> list[dict]:
    """目标人群及其批次全景：哪些批次已完成、哪些断档，一眼看清。"""
    return service.list_crowds()


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出用户洞察清单：返回当前全量数据（含当前快照编号）。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "userinsight", "total": total, "items": items}


@router.post("/batch-refresh", response_model=dict)
def batch_refresh(payload: InsightBatchRefreshPayload) -> dict[str, Any]:
    """一次提交多个报告生成，逐条返回每个批次结果；单条失败不回滚其他报告。"""
    report_ids = [int(item) for item in payload.report_ids]
    if not report_ids:
        raise HTTPException(status_code=400, detail="请至少选择一个洞察报告")
    if len(set(report_ids)) > 50:
        raise HTTPException(status_code=400, detail="单次最多提交 50 个报告，请分批操作")
    result = service.refresh_many(
        report_ids,
        batch_ids=[int(item) for item in payload.batch_ids] or None,
        trigger=str(payload.trigger or "批量生成"),
    )
    return result


@router.post("/crowds/{reference}/modify", response_model=ActionResult)
def modify_crowd(reference: str, payload: InsightCrowdPayload) -> ActionResult:
    """修改目标人群：升版本并标记待刷新，历史指标与下次生成时间保持原样。"""
    crowd, message, affected = service.modify_crowd(reference, payload.values)
    if crowd is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(
        ok=True,
        message=message,
        entry={"人群": crowd, "受影响报告": affected, "可编辑字段": CROWD_EDITABLE_FIELDS},
    )


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条洞察报告明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"洞察报告 {entry_id} 不存在或已归档")
    return entry


@router.get("/{entry_id}/snapshots", response_model=dict)
def list_snapshots(entry_id: int) -> dict[str, Any]:
    """报告的数据快照版本列表：当前版本唯一，历史版本只读保留。"""
    report, snapshots = service.list_snapshots(entry_id)
    if report is None:
        raise HTTPException(status_code=404, detail=f"洞察报告 {entry_id} 不存在或已归档")
    return {"report": report, "items": snapshots, "total": len(snapshots)}


@router.get("/{entry_id}/snapshots/{version}", response_model=dict)
def get_snapshot(entry_id: int, version: int) -> dict:
    """按版本读取快照原文，用于历史指标追溯。"""
    snapshot = service.get_snapshot(entry_id, version)
    if snapshot is None:
        raise HTTPException(status_code=404, detail=f"洞察报告 {entry_id} 不存在版本 v{version} 的快照")
    return snapshot


@router.get("/{entry_id}/trace", response_model=dict)
def trace(entry_id: int) -> dict[str, Any]:
    """追溯链路：当前快照、历史版本、区间判定、批次扫描明细、状态记录与订阅交付。"""
    result = service.trace(entry_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"洞察报告 {entry_id} 不存在或已归档")
    return result


@router.post("/{entry_id}/refresh", response_model=dict)
def refresh(entry_id: int, payload: InsightRefreshPayload) -> dict[str, Any]:
    """增量刷新单份报告：复用未受影响批次的扫描结果，生成新的当前快照。"""
    snapshot, meta, message = service.refresh(
        entry_id,
        batch_ids=[int(item) for item in payload.batch_ids] or None,
        trigger=str(payload.trigger or "增量刷新"),
    )
    if snapshot is None:
        raise HTTPException(status_code=400, detail=message)
    return {"ok": True, "message": message, "snapshot": snapshot, **meta}


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条洞察报告，缺字段或人群不存在时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少或无效字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="洞察报告已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """执行提交生成、发布报告、归档报告；提交生成会真正产出当前快照。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
