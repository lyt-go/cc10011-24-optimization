"""用户洞察业务规则：增量刷新、报告快照、指标变化与数据区间追溯都收在这里。

三条硬约束：

1. 修改目标人群只登记版本、标记受影响报告，不重算、不动历史指标与下次生成时间；
2. 同一业务键（报告编号）同时只有一个当前快照，历史版本保留为只读追溯；
3. 快照、报告状态、扫描缓存与状态记录写在同一个事务里，失败整组回滚。

数据区间取舍：一个报告只读取一个"连续且完整"的批次区间。当更晚完成的增量批次
与已有区间之间断档时，连续性优先于新鲜度；同长度时取更晚的区间。被排除的批次
和取舍原因会写进快照的 ``区间判定``，保证事后可追溯。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Callable, Iterable

from app.store import store

MODULE = "userinsight"
CROWD_TABLE = "_insight_crowds"
BATCH_TABLE = "_insight_batches"
SNAPSHOT_TABLE = "_insight_snapshots"
SCAN_TABLE = "_insight_scans"
STATUS_TABLE = "_insight_status_logs"
DELIVERY_TABLE = "_insight_deliveries"

REQUIRED_FIELDS = ["报告编号", "分析主题", "目标人群"]
STATUS_ORDER = ["草稿", "生成中", "已发布", "已归档"]
ACTION_RULES = {"提交生成": "生成中", "发布报告": "已发布", "归档报告": "已归档"}
NEGATIVE_ACTIONS = []

DATE_FORMAT = "%Y-%m-%d"
DATETIME_FORMAT = "%Y-%m-%d %H:%M"
COUNT_KEYS = ["活跃用户", "新增用户", "留存用户", "总时长分钟", "付费用户"]
CROWD_EDITABLE_FIELDS = ["人群名称", "人群定义"]


class InsightError(ValueError):
    """业务规则不允许时的可读异常，路由层转成 400。"""


def _parse_date(value: str) -> date:
    return datetime.strptime(str(value)[:10], DATE_FORMAT).date()


def _parse_datetime(value: str | None) -> datetime | None:
    text = str(value or "").strip()
    for fmt in (DATETIME_FORMAT, DATE_FORMAT):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _now_text() -> str:
    return datetime.now().strftime(DATETIME_FORMAT)


def _interval_text(batches: Iterable[dict[str, Any]]) -> str:
    rows = sorted(batches, key=lambda item: str(item["数据区间起"]))
    return f"{rows[0]['数据区间起']} ~ {rows[-1]['数据区间止']}"


def _sum_counts(batches: Iterable[dict[str, Any]]) -> dict[str, int]:
    totals = {key: 0 for key in COUNT_KEYS}
    for batch in batches:
        counts = batch.get("计数") or {}
        for key in COUNT_KEYS:
            totals[key] += int(counts.get(key, 0) or 0)
    return totals


def _days(batches: Iterable[dict[str, Any]]) -> int:
    return sum(
        (_parse_date(batch["数据区间止"]) - _parse_date(batch["数据区间起"])).days + 1
        for batch in batches
    )


def _metric_specs() -> list[tuple[str, str, Callable[[dict[str, int], int], float], str]]:
    def ratio(numerator: str) -> Callable[[dict[str, int], int], float]:
        return lambda totals, _days_: (
            totals[numerator] / totals["活跃用户"] if totals["活跃用户"] else 0.0
        )

    return [
        ("日均活跃用户", "人/日", lambda totals, days: totals["活跃用户"] / days, "number"),
        ("日均新增用户", "人/日", lambda totals, days: totals["新增用户"] / days, "number"),
        ("留存率", "%", ratio("留存用户"), "percent"),
        ("人均时长分钟", "分钟", ratio("总时长分钟"), "number"),
        ("付费率", "%", ratio("付费用户"), "percent"),
    ]


def _derive_metrics(totals: dict[str, int], days: int) -> dict[str, float]:
    values: dict[str, float] = {}
    for name, _unit, compute, kind in _metric_specs():
        value = compute(totals, days)
        values[name] = round(value, 4 if kind == "percent" else 1)
    return values


def _format_metric(name: str, value: float | None) -> str:
    if value is None:
        return "—"
    kind = next((kind for metric, _u, _c, kind in _metric_specs() if metric == name), "number")
    return f"{value:.1%}" if kind == "percent" else f"{value:,.1f}"


def _diff_metrics(
    current: dict[str, float], baseline: dict[str, float] | None
) -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []
    for name, unit, _compute, kind in _metric_specs():
        current_value = current.get(name)
        base_value = (baseline or {}).get(name)
        if base_value is None or current_value is None:
            changes.append({
                "指标": name,
                "单位": unit,
                "基期值": None,
                "本期值": current_value,
                "变化值": None,
                "变化率": None,
                "变化类型": "首次生成",
                "展示": f"本期 {_format_metric(name, current_value)}",
            })
            continue
        delta = current_value - base_value
        rate = (delta / base_value) if base_value else None
        if abs(delta) < (0.00005 if kind == "percent" else 0.05):
            change_type = "持平"
        elif delta > 0:
            change_type = "上升"
        else:
            change_type = "下降"
        sign = "+" if delta > 0 else ""
        if kind == "percent":
            display = f"{base_value:.1%} → {current_value:.1%}（{sign}{delta * 100:.1f} 个百分点）"
        else:
            display = f"{base_value:,.1f} → {current_value:,.1f}（{sign}{delta:,.1f}）"
        changes.append({
            "指标": name,
            "单位": unit,
            "基期值": base_value,
            "本期值": current_value,
            "变化值": round(delta, 4 if kind == "percent" else 2),
            "变化率": round(rate, 4) if rate is not None else None,
            "变化类型": change_type,
            "展示": display,
        })
    return changes


def _advance_next_generation(previous: str | None, generated_at: datetime) -> str:
    """按原有排期往后推，不让一次补算把下次生成时间冲掉或越推越偏。"""
    base = _parse_datetime(previous)
    if base is None:
        return (generated_at + timedelta(days=1)).strftime(DATETIME_FORMAT)
    while base <= generated_at:
        base += timedelta(days=1)
    return base.strftime(DATETIME_FORMAT)


def _contiguous_runs(batches: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    runs: list[list[dict[str, Any]]] = []
    for batch in sorted(batches, key=lambda item: str(item["数据区间起"])):
        if runs:
            previous_end = _parse_date(runs[-1][-1]["数据区间止"])
            if _parse_date(batch["数据区间起"]) == previous_end + timedelta(days=1):
                runs[-1].append(batch)
                continue
        runs.append([batch])
    return runs


def _is_contiguous(batches: list[dict[str, Any]]) -> bool:
    return len(_contiguous_runs(batches)) == 1


def _current_snapshots() -> list[dict[str, Any]]:
    """当前快照读取入口：订阅推送等其他模块也走这里，不各自拼查询。"""
    return [row for row in store.rows(SNAPSHOT_TABLE) if row.get("是否当前")]


def _current_snapshot_for(business_key: str) -> dict[str, Any] | None:
    for row in _current_snapshots():
        if row.get("业务键") == business_key:
            return row
    return None


def current_snapshot(business_key: str) -> dict[str, Any] | None:
    """跨模块读取入口：订阅推送等下游只消费"当前"快照，不碰历史版本。"""
    return _current_snapshot_for(str(business_key or "").strip())


def _resolve_crowd(reference: str) -> dict[str, Any] | None:
    text = str(reference or "").strip()
    for crowd in store.rows(CROWD_TABLE):
        if text in (crowd.get("人群编号"), crowd.get("人群名称")):
            return crowd
    return None


def _current_scan(crowd_code: str, crowd_version: int, batch_id: int) -> dict[str, Any] | None:
    for row in store.rows(SCAN_TABLE):
        if (
            row.get("人群编号") == crowd_code
            and int(row.get("人群版本", 0)) == crowd_version
            and int(row.get("批次id", 0)) == batch_id
        ):
            return row
    return None


class UserinsightService:
    # ------------------------------------------------------------------ 列表与明细
    def list_entries(        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
        stale_only: bool = False,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [
                row for row in rows
                if keyword in str(row.get("报告编号", "")) or keyword in str(row.get("分析主题", ""))
            ]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        enriched = [self._enrich_report(row) for row in rows]
        if stale_only:
            enriched = [row for row in enriched if row.get("待刷新")]
        total = len(enriched)
        start = max(page - 1, 0) * size
        return enriched[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        row = store.find(MODULE, entry_id)
        return self._enrich_report(row) if row is not None else None

    def summary(self) -> dict[str, Any]:
        report_rows = store.rows(MODULE)
        snapshots = store.rows(SNAPSHOT_TABLE)
        return {
            "报告总数": len(report_rows),
            "已发布报告": sum(1 for row in report_rows if row.get("status") == "已发布"),
            "生成中报告": sum(1 for row in report_rows if row.get("status") == "生成中"),
            "待刷新报告": sum(1 for row in report_rows if row.get("刷新状态") == "待刷新"),
            "当前快照数": len(_current_snapshots()),
            "快照版本总数": len(snapshots),
            "下次生成时间": min(
                (str(row.get("下次生成")) for row in report_rows if row.get("下次生成")),
                default="—",
            ),
        }

    def _enrich_report(self, row: dict[str, Any]) -> dict[str, Any]:
        enriched = dict(row)
        snapshot = _current_snapshot_for(str(row.get("报告编号", "")))
        crowd = _resolve_crowd(str(row.get("目标人群", "")))
        latest_version = int(crowd.get("版本", 0)) if crowd else None
        generated_version = row.get("人群版本")
        enriched.update({
            "当前快照编号": snapshot.get("快照编号") if snapshot else None,
            "当前快照版本": snapshot.get("版本") if snapshot else None,
            "快照数": sum(
                1 for item in store.rows(SNAPSHOT_TABLE)
                if item.get("业务键") == row.get("报告编号")
            ),
            "人群编号": crowd.get("人群编号") if crowd else None,
            "人群最新版本": latest_version,
            "待刷新": row.get("刷新状态") == "待刷新",
            "人群已变更": bool(
                latest_version is not None
                and generated_version is not None
                and int(generated_version) < latest_version
            ),
        })
        return enriched

    def list_crowds(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for crowd in store.rows(CROWD_TABLE):
            batches = [
                dict(batch) for batch in store.rows(BATCH_TABLE)
                if batch.get("人群编号") == crowd.get("人群编号")
            ]
            batches.sort(key=lambda item: str(item["数据区间起"]))
            result.append({**crowd, "批次数": len(batches), "批次": batches})
        return result

    def get_crowd(self, reference: str) -> dict[str, Any] | None:
        crowd = _resolve_crowd(reference)
        return dict(crowd) if crowd else None

    def modify_crowd(self, reference: str, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str, list[dict[str, Any]]]:
        """修改人群定义：只升版本并标记受影响报告，绝不整份重算历史报告。"""
        updates = {
            field: values.get(field)
            for field in CROWD_EDITABLE_FIELDS
            if str(values.get(field) or "").strip()
        }
        if not updates:
            return None, f"请至少提供 {'、'.join(CROWD_EDITABLE_FIELDS)} 中的一项", []

        with store.transaction():
            crowd = _resolve_crowd(reference)
            if crowd is None:
                raise InsightError(f"目标人群「{reference}」不存在")
            old_version = int(crowd.get("版本", 1))
            crowd.update(updates)
            crowd["版本"] = old_version + 1
            crowd["更新时间"] = _now_text()

            affected: list[dict[str, Any]] = []
            for report in store.rows(MODULE):
                if _resolve_crowd(str(report.get("目标人群", ""))) is not crowd:
                    continue
                # 历史指标、数据区间、生成时间与下次生成保持原样，只登记待刷新。
                report["刷新状态"] = "待刷新"
                report["待刷新原因"] = (
                    f"目标人群版本 v{old_version}→v{crowd['版本']}，刷新时仅重算受影响批次"
                )
                affected.append({
                    "id": report.get("id"),
                    "报告编号": report.get("报告编号"),
                    "刷新状态": report.get("刷新状态"),
                    "当前快照版本": ( _current_snapshot_for(str(report.get("报告编号"))) or {}).get("版本"),
                    "下次生成": report.get("下次生成"),
                })
            return dict(crowd), (
                f"人群版本已升级到 v{crowd['版本']}，{len(affected)} 份报告标记为待刷新；"
                "历史指标与下次生成时间未被覆盖"
            ), affected

    # ------------------------------------------------------------------ 快照与追溯
    def list_snapshots(self, report_id: int) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        report = store.find(MODULE, report_id)
        if report is None:
            return None, []
        rows = [
            dict(item) for item in store.rows(SNAPSHOT_TABLE)
            if item.get("业务键") == report.get("报告编号")
        ]
        rows.sort(key=lambda item: int(item.get("版本", 0)), reverse=True)
        return dict(report), rows

    def get_snapshot(self, report_id: int, version: int | None = None) -> dict[str, Any] | None:
        report = store.find(MODULE, report_id)
        if report is None:
            return None
        rows = [
            item for item in store.rows(SNAPSHOT_TABLE)
            if item.get("业务键") == report.get("报告编号")
        ]
        if version is not None:
            return next((dict(item) for item in rows if int(item.get("版本", 0)) == version), None)
        current = next((item for item in rows if item.get("是否当前")), None)
        return dict(current) if current else None

    def trace(self, report_id: int) -> dict[str, Any] | None:
        report = store.find(MODULE, report_id)
        if report is None:
            return None
        business_key = str(report.get("报告编号"))
        snapshots = [
            dict(item) for item in store.rows(SNAPSHOT_TABLE)
            if item.get("业务键") == business_key
        ]
        snapshots.sort(key=lambda item: int(item.get("版本", 0)), reverse=True)
        current = next((item for item in snapshots if item.get("是否当前")), None)
        status_logs = [
            dict(item) for item in store.rows(STATUS_TABLE)
            if item.get("业务键") == business_key
        ]
        status_logs.sort(key=lambda item: int(item.get("id", 0)), reverse=True)
        deliveries = [
            dict(item) for item in store.rows(DELIVERY_TABLE)
            if current is not None and item.get("快照编号") == current.get("快照编号")
        ]
        return {
            "报告": self._enrich_report(report),
            "当前快照": current,
            "快照版本": [
                {
                    "快照编号": item.get("快照编号"),
                    "版本": item.get("版本"),
                    "是否当前": item.get("是否当前"),
                    "数据区间": item.get("数据区间"),
                    "生成时间": item.get("生成时间"),
                    "生成方式": item.get("生成方式"),
                    "基线快照": item.get("基线快照"),
                }
                for item in snapshots
            ],
            "状态记录": status_logs,
            "区间判定": current.get("区间判定") if current else None,
            "扫描明细": current.get("扫描", {}).get("明细") if current else [],
            "订阅交付": deliveries,
        }

    # ------------------------------------------------------------------ 刷新
    def refresh(
        self,
        report_id: int,
        *,
        batch_ids: list[int] | None = None,
        trigger: str = "增量刷新",
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None, str]:
        try:
            with store.transaction():
                snapshot, report, meta = self._refresh_locked(
                    report_id, batch_ids=batch_ids, trigger=trigger
                )
        except InsightError as exc:
            return None, None, str(exc)
        except Exception as exc:  # 事务已回滚，明确告诉调用方没有留下半成品
            return None, None, f"刷新失败，事务已回滚，未写入任何快照：{exc}"
        return snapshot, {"报告": self._enrich_report(report), **meta}, "洞察报告已刷新"

    def refresh_many(
        self,
        report_ids: list[int],
        *,
        batch_ids: list[int] | None = None,
        trigger: str = "批量生成",
    ) -> dict[str, Any]:
        """一次提交整组报告，逐条返回每个批次的结果。

        每份报告各自一个事务：某一份失败只回滚它自己，不会污染同组其他报告，
        也不会留下"快照已写、状态没落"的半成品。
        """
        unique_ids: list[int] = []
        for report_id in report_ids:
            if report_id not in unique_ids:
                unique_ids.append(report_id)
        items: list[dict[str, Any]] = []
        for report_id in unique_ids:
            report = store.find(MODULE, report_id)
            report_code = report.get("报告编号") if report else None
            try:
                with store.transaction():
                    snapshot, _report, meta = self._refresh_locked(
                        report_id, batch_ids=batch_ids, trigger=trigger
                    )
                items.append({
                    "ok": True,
                    "报告id": report_id,
                    "报告编号": report_code,
                    "message": "已生成新的当前快照",
                    "快照编号": snapshot.get("快照编号"),
                    "快照版本": snapshot.get("版本"),
                    "数据区间": snapshot.get("数据区间"),
                    "生成方式": snapshot.get("生成方式"),
                    "扫描批次": meta["扫描"]["扫描批次"],
                    "复用批次": meta["扫描"]["复用批次"],
                    "关键指标变化": snapshot.get("关键指标变化"),
                })
            except InsightError as exc:
                items.append({
                    "ok": False, "报告id": report_id, "报告编号": report_code,
                    "message": str(exc),
                })
            except Exception as exc:  # pragma: no cover - 兜底，事务已回滚
                items.append({
                    "ok": False, "报告id": report_id, "报告编号": report_code,
                    "message": f"生成失败，事务已回滚：{exc}",
                })
        succeeded = sum(1 for item in items if item["ok"])
        return {
            "ok": succeeded == len(items) and bool(items),
            "total": len(items),
            "succeeded": succeeded,
            "failed": len(items) - succeeded,
            "items": items,
        }

    def _refresh_locked(
        self,
        report_id: int,
        *,
        batch_ids: list[int] | None,
        trigger: str,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        report = store.find(MODULE, report_id)
        if report is None:
            raise InsightError(f"洞察报告 {report_id} 不存在或已归档")
        crowd = _resolve_crowd(str(report.get("目标人群", "")))
        if crowd is None:
            raise InsightError(f"报告 {report.get('报告编号')} 的目标人群「{report.get('目标人群')}」不存在")

        plan = self._plan_interval(crowd, batch_ids)
        business_key = str(report.get("报告编号"))
        previous = _current_snapshot_for(business_key)

        details: list[dict[str, Any]] = []
        scanned: list[str] = []
        reused: list[str] = []
        batch_counts: dict[str, dict[str, int]] = {}
        scanned_rows = 0
        reused_rows = 0
        for batch in plan["批次"]:
            counts, source, row_count, cached = self._obtain_counts(crowd, batch, previous)
            batch_counts[str(batch["id"])] = counts
            details.append({
                "批次编号": batch["批次编号"],
                "数据区间": f"{batch['数据区间起']} ~ {batch['数据区间止']}",
                "来源": source,
                "本次扫描行数": row_count,
                "缓存命中": cached,
            })
            if source == "扫描":
                scanned.append(str(batch["批次编号"]))
                scanned_rows += row_count
            else:
                reused.append(str(batch["批次编号"]))
                reused_rows += row_count

        totals = {key: 0 for key in COUNT_KEYS}
        for counts in batch_counts.values():
            for key in COUNT_KEYS:
                totals[key] += int(counts.get(key, 0) or 0)
        days = _days(plan["批次"])
        metrics = _derive_metrics(totals, days)
        baseline_metrics = previous.get("指标") if previous else None
        changes = _diff_metrics(metrics, baseline_metrics)

        generated_at = datetime.now()
        generated_text = generated_at.strftime(DATETIME_FORMAT)
        relation = self._baseline_relation(previous, plan["批次"])
        findings = self._build_findings(
            plan=plan,
            changes=changes,
            metrics=metrics,
            previous=previous,
            relation=relation,
            scanned=scanned,
            reused=reused,
        )

        versions = [
            int(item.get("版本", 0)) for item in store.rows(SNAPSHOT_TABLE)
            if item.get("业务键") == business_key
        ]
        version = max(versions, default=0) + 1
        snapshot_id = store.next_id(SNAPSHOT_TABLE)
        snapshot_code = f"SNAP-{snapshot_id:04d}"
        generation_mode = "增量" if reused else "全量"
        scan_mode = "增量扫描" if reused else "全量扫描"

        # 同一业务键只留一个当前版本：先把旧版本降为历史，再写入新版本。
        for item in store.rows(SNAPSHOT_TABLE):
            if item.get("业务键") == business_key and item.get("是否当前"):
                item["是否当前"] = False
                item["状态"] = "历史"

        snapshot = {
            "id": snapshot_id,
            "快照编号": snapshot_code,
            "业务键": business_key,
            "报告id": report_id,
            "报告编号": business_key,
            "人群编号": crowd.get("人群编号"),
            "人群名称": crowd.get("人群名称"),
            "人群版本": int(crowd.get("版本", 1)),
            "版本": version,
            "是否当前": True,
            "状态": "当前",
            "数据区间起": plan["批次"][0]["数据区间起"],
            "数据区间止": plan["批次"][-1]["数据区间止"],
            "数据区间": _interval_text(plan["批次"]),
            "批次ids": [int(batch["id"]) for batch in plan["批次"]],
            "批次编号": [str(batch["批次编号"]) for batch in plan["批次"]],
            "计数": totals,
            "批次计数": batch_counts,
            "天数": days,
            "指标": metrics,
            "关键指标变化": changes,
            "核心发现": findings,
            "生成时间": generated_text,
            "生成方式": generation_mode,
            "扫描方式": scan_mode,
            "基线快照": previous.get("快照编号") if previous else None,
            "基线版本": previous.get("版本") if previous else None,
            "基线区间": previous.get("数据区间") if previous else None,
            "可比": bool(previous) and relation == "区间扩展",
            "对比说明": relation,
            "区间判定": plan["区间判定"],
            "扫描": {
                "扫描批次": scanned,
                "复用批次": reused,
                "扫描行数": scanned_rows,
                "复用行数": reused_rows,
                "缓存命中": len(reused),
                "明细": details,
            },
            "触发方式": trigger,
        }
        store.rows(SNAPSHOT_TABLE).append(snapshot)

        # 扫描缓存按 (人群编号, 人群版本, 批次) 共享，多份报告共用同一人群时不再重复扫描。
        for batch in plan["批次"]:
            self._upsert_scan_cache(
                crowd, batch, batch_counts[str(batch["id"])],
                source=next(item["来源"] for item in details if item["批次编号"] == batch["批次编号"]),
            )

        previous_next = report.get("下次生成")
        next_generation = _advance_next_generation(
            str(previous_next) if previous_next else None, generated_at
        )
        report.update({
            "关键指标": self._metrics_summary(metrics),
            "核心发现": "；".join(item["结论"] for item in findings[:2]),
            "数据区间": snapshot["数据区间"],
            "生成时间": generated_text,
            "下次生成": next_generation,
            "快照编号": snapshot_code,
            "快照版本": version,
            "人群版本": int(crowd.get("版本", 1)),
            "刷新状态": "已是最新",
            "待刷新原因": "",
            "生成次数": int(report.get("生成次数", 0) or 0) + 1,
            "最近生成方式": generation_mode,
            "最近扫描方式": scan_mode,
        })
        if trigger == "提交生成":
            report["status"] = "生成中"
            report["pending"] = True
            report["abnormal"] = False
        else:
            report["pending"] = report.get("status") != "已归档"

        status_id = store.next_id(STATUS_TABLE)
        store.rows(STATUS_TABLE).append({
            "id": status_id,
            "业务键": business_key,
            "报告id": report_id,
            "动作": trigger,
            "结果": "成功",
            "快照编号": snapshot_code,
            "快照版本": version,
            "人群编号": crowd.get("人群编号"),
            "人群版本": int(crowd.get("版本", 1)),
            "数据区间": snapshot["数据区间"],
            "扫描批次": scanned,
            "复用批次": reused,
            "下次生成": next_generation,
            "时间": generated_text,
            "说明": "快照、报告状态、扫描缓存与状态记录在同一事务内提交",
        })

        meta = {
            "区间判定": plan["区间判定"],
            "扫描": snapshot["扫描"],
            "生成方式": generation_mode,
            "扫描方式": scan_mode,
            "对比说明": relation,
            "关键指标变化": changes,
            "核心发现": findings,
            "状态记录id": status_id,
            "下次生成": next_generation,
        }
        return snapshot, report, meta

    def _baseline_relation(
        self, previous: dict[str, Any] | None, chosen: list[dict[str, Any]]
    ) -> str:
        """本期区间与上一当前快照的关系：决定能不能拿来做同比基期。

        - 区间扩展：本期完整包含上一区间且更晚，同一口径可以直接比较；
        - 同区间重算：区间完全一致，属于人群口径变化后的重算；
        - 区间变化（缩短/错位/断档跳段）：两者不在同一口径上，指标变化只做展示，
          不冒充环比，核心发现里会明确写出口径不可比。
        """
        if previous is None:
            return "首次生成"
        previous_start = str(previous.get("数据区间起") or previous.get("数据区间", "").split(" ~ ")[0])
        previous_end = str(previous.get("数据区间止") or previous.get("数据区间", " ~ ")[-1])
        current_start = str(chosen[0]["数据区间起"])
        current_end = str(chosen[-1]["数据区间止"])
        if (current_start, current_end) == (previous_start, previous_end):
            return "同区间重算"
        if current_start <= previous_start and current_end >= previous_end:
            return "区间扩展"
        return "区间变化"

    def _plan_interval(
        self, crowd: dict[str, Any], batch_ids: list[int] | None
    ) -> dict[str, Any]:
        crowd_batches = [
            batch for batch in store.rows(BATCH_TABLE)
            if batch.get("人群编号") == crowd.get("人群编号")
        ]
        completed = [batch for batch in crowd_batches if batch.get("状态") == "已完成"]
        if not completed:
            raise InsightError(f"目标人群 {crowd.get('人群编号')} 没有已完成的批次，无法生成报告")

        if batch_ids:
            requested = list(dict.fromkeys(int(item) for item in batch_ids))
            by_id = {int(batch["id"]): batch for batch in crowd_batches}
            unknown = [item for item in requested if item not in by_id]
            if unknown:
                raise InsightError(f"所选批次 {unknown} 不属于目标人群 {crowd.get('人群编号')}")
            incomplete = [by_id[item] for item in requested if by_id[item].get("状态") != "已完成"]
            if incomplete:
                labels = "、".join(str(item["批次编号"]) for item in incomplete)
                raise InsightError(f"所选批次 {labels} 未完成，不能进入报告数据区间")
            chosen = sorted(
                (by_id[item] for item in requested),
                key=lambda item: str(item["数据区间起"]),
            )
            if not chosen:
                raise InsightError("所选批次为空，未生成报告")
            if not _is_contiguous(chosen):
                raise InsightError("所选批次不连续：报告只读取一个连续完整数据区间，请补齐缺口后重试")
            decision = self._decision(crowd, chosen, user_selected=True)
            return {"批次": chosen, "区间判定": decision}

        runs = _contiguous_runs(completed)
        chosen_run = max(
            runs,
            key=lambda run: (
                _days(run),
                _parse_date(run[-1]["数据区间止"]),
            ),
        )
        decision = self._decision(crowd, chosen_run, user_selected=False)
        return {"批次": chosen_run, "区间判定": decision}

    def _decision(
        self,
        crowd: dict[str, Any],
        chosen: list[dict[str, Any]],
        *,
        user_selected: bool,
    ) -> dict[str, Any]:
        chosen_ids = {int(item["id"]) for item in chosen}
        excluded: list[dict[str, Any]] = []
        for batch in store.rows(BATCH_TABLE):
            if batch.get("人群编号") != crowd.get("人群编号") or int(batch["id"]) in chosen_ids:
                continue
            if batch.get("状态") != "已完成":
                reason = f"批次未完成（{batch.get('状态')}），不进入任何报告区间"
            elif _parse_date(batch["数据区间起"]) > _parse_date(chosen[-1]["数据区间止"]):
                gap = (_parse_date(batch["数据区间起"]) - _parse_date(chosen[-1]["数据区间止"])).days - 1
                reason = f"与选定区间之间存在 {gap} 天缺口，按连续性优先暂不纳入"
            else:
                reason = "属于更早的较短连续区间，未被选中"
            excluded.append({
                "批次编号": batch.get("批次编号"),
                "状态": batch.get("状态"),
                "数据区间": f"{batch.get('数据区间起')} ~ {batch.get('数据区间止')}",
                "完成时间": batch.get("完成时间"),
                "原因": reason,
            })

        chosen_interval = _interval_text(chosen)
        excluded_sorted = sorted(excluded, key=lambda item: str(item["数据区间"]))
        completed_newer = [
            item for item in excluded_sorted
            if item["状态"] == "已完成" and item["原因"].startswith("与选定区间")
        ]
        notes = [f"{chosen_interval} 为选定区间，共 {len(chosen)} 个批次、{_days(chosen)} 天，全部已完成且首尾相接"]
        if completed_newer:
            labels = "、".join(str(item["批次编号"]) for item in completed_newer)
            notes.append(
                f"{labels} 已完成得更晚，但与选定区间断档；本次取舍为“连续完整优先于新鲜度”，"
                "待缺口批次补齐后再随增量一并纳入"
            )
        if any(item["状态"] != "已完成" for item in excluded_sorted):
            labels = "、".join(
                str(item["批次编号"]) for item in excluded_sorted if item["状态"] != "已完成"
            )
            notes.append(f"{labels} 尚未完成，不会进入快照")
        if user_selected:
            notes.append("本次区间由调用方指定，已校验连续性与完成状态")

        return {
            "规则": "一个报告只读取一个连续完整数据区间；同长度时取更晚的区间",
            "取舍": "连续性优先于新鲜度：断档或未完成的批次不进入快照",
            "选择方式": "调用方指定" if user_selected else "系统自动选取",
            "选定区间": chosen_interval,
            "选定批次": [str(item["批次编号"]) for item in chosen],
            "覆盖天数": _days(chosen),
            "排除批次": excluded_sorted,
            "说明": notes,
        }

    def _obtain_counts(
        self,
        crowd: dict[str, Any],
        batch: dict[str, Any],
        previous: dict[str, Any] | None,
    ) -> tuple[dict[str, int], str, int, bool]:
        crowd_code = str(crowd.get("人群编号"))
        crowd_version = int(crowd.get("版本", 1))
        batch_id = int(batch["id"])

        cached = _current_scan(crowd_code, crowd_version, batch_id)
        if cached is not None:
            cached["复用次数"] = int(cached.get("复用次数", 0) or 0) + 1
            return dict(cached.get("计数") or {}), "扫描缓存", int(cached.get("扫描行数", 0) or 0), True

        if (
            previous is not None
            and int(previous.get("人群版本", 0)) == crowd_version
            and str(batch_id) in (previous.get("批次计数") or {})
        ):
            counts = {key: int((previous["批次计数"][str(batch_id)] or {}).get(key, 0)) for key in COUNT_KEYS}
            return counts, "上一快照", int(batch.get("扫描行数", 0) or 0), True

        return self._scan_one(batch), "扫描", int(batch.get("扫描行数", 0) or 0), False

    def _scan_one(self, batch: dict[str, Any]) -> dict[str, int]:
        if batch.get("状态") != "已完成":
            raise InsightError(f"批次 {batch.get('批次编号')} 未完成，不能扫描")
        counts = batch.get("计数") or {}
        return {key: int(counts.get(key, 0) or 0) for key in COUNT_KEYS}

    def _upsert_scan_cache(
        self,
        crowd: dict[str, Any],
        batch: dict[str, Any],
        counts: dict[str, int],
        *,
        source: str,
    ) -> None:
        crowd_code = str(crowd.get("人群编号"))
        crowd_version = int(crowd.get("版本", 1))
        batch_id = int(batch["id"])
        cached = _current_scan(crowd_code, crowd_version, batch_id)
        if cached is None:
            cached = {
                "id": store.next_id(SCAN_TABLE),
                "缓存键": f"{crowd_code}@{crowd_version}#{batch_id}",
                "人群编号": crowd_code,
                "人群版本": crowd_version,
                "批次id": batch_id,
                "批次编号": batch.get("批次编号"),
                "复用次数": 0,
            }
            store.rows(SCAN_TABLE).append(cached)
        cached.update({
            "计数": dict(counts),
            "扫描行数": int(batch.get("扫描行数", 0) or 0),
            "本次来源": source,
            "更新时间": _now_text(),
        })

    def _build_findings(
        self,
        *,
        plan: dict[str, Any],
        changes: list[dict[str, Any]],
        metrics: dict[str, float],
        previous: dict[str, Any] | None,
        relation: str,
        scanned: list[str],
        reused: list[str],
    ) -> list[dict[str, Any]]:
        batch_codes = [str(item["批次编号"]) for item in plan["批次"]]
        interval = _interval_text(plan["批次"])
        findings: list[dict[str, Any]] = []
        comparable = relation == "区间扩展"

        if comparable:
            moved = [
                item for item in changes
                if item["变化率"] is not None and item["变化类型"] != "持平"
            ]
            moved.sort(key=lambda item: abs(float(item["变化率"])), reverse=True)
            for item in moved[:2]:
                direction = "上升" if float(item["变化率"]) > 0 else "下降"
                findings.append({
                    "结论": (
                        f"{item['指标']} {item['展示']}，环比{direction} "
                        f"{abs(float(item['变化率'])):.1%}，是本次区间内变化最显著的指标"
                    ),
                    "指标": item["指标"],
                    "基期值": item["基期值"],
                    "本期值": item["本期值"],
                    "变化率": item["变化率"],
                    "依据批次": batch_codes,
                    "基线快照": previous.get("快照编号") if previous else None,
                    "数据区间": interval,
                    "计算口径": "本期区间日均/比率对比包含它的上一当前快照的同一口径",
                })
            if not moved:
                findings.append({
                    "结论": f"本期在上一快照基础上扩展后，关键指标整体平稳："
                    + "、".join(f"{name} {_format_metric(name, value)}" for name, value in list(metrics.items())[:3]),
                    "指标": "整体",
                    "基期值": None,
                    "本期值": None,
                    "变化率": None,
                    "依据批次": batch_codes,
                    "基线快照": previous.get("快照编号") if previous else None,
                    "数据区间": interval,
                    "计算口径": "本期区间日均/比率对比包含它的上一当前快照的同一口径",
                })
        else:
            top = "、".join(
                f"{name} {_format_metric(name, value)}" for name, value in list(metrics.items())[:3]
            )
            if previous is None:
                reason = f"首次生成，暂无历史基线；本期关键指标为 {top}"
            elif relation == "同区间重算":
                reason = (
                    f"本次按相同区间 {interval} 重算（人群口径变化），"
                    f"关键指标为 {top}；指标变化以同一区间的上一版本为参照"
                )
            else:
                reason = (
                    f"本次区间 {interval} 与上一快照 {previous.get('数据区间')} 不在同一口径上，"
                    f"指标变化仅做展示、不作为环比结论；本期关键指标为 {top}"
                )
            findings.append({
                "结论": reason,
                "指标": "整体",
                "基期值": None,
                "本期值": None,
                "变化率": None,
                "依据批次": batch_codes,
                "基线快照": previous.get("快照编号") if previous else None,
                "数据区间": interval,
                "计算口径": "不同区间不可直接比较，只保留展示值",
            })

        findings.append({
            "结论": plan["区间判定"]["说明"][0] + "；断档与未完成批次已排除在快照之外",
            "指标": "数据区间",
            "基期值": None,
            "本期值": None,
            "变化率": None,
            "依据批次": batch_codes,
            "基线快照": previous.get("快照编号") if previous else None,
            "数据区间": interval,
            "计算口径": plan["区间判定"]["规则"],
            "排除批次": plan["区间判定"]["排除批次"],
        })

        if reused:
            findings.append({
                "结论": (
                    f"增量刷新复用 {len(reused)} 个批次的历史扫描结果，仅新扫描 {len(scanned)} 个批次，"
                    "未对整份人群重算"
                ),
                "指标": "扫描成本",
                "基期值": None,
                "本期值": None,
                "变化率": None,
                "依据批次": reused,
                "基线快照": previous.get("快照编号") if previous else None,
                "数据区间": interval,
                "计算口径": "扫描缓存与上一快照的批次计数按人群版本复用",
                "新扫描批次": scanned,
            })
        return findings

    def _metrics_summary(self, metrics: dict[str, float]) -> str:
        parts = []
        for name in ["日均活跃用户", "留存率", "付费率"]:
            if name in metrics:
                parts.append(f"{name} {_format_metric(name, metrics[name])}")
        return "；".join(parts)

    # ------------------------------------------------------------------ 动作
    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        crowd = _resolve_crowd(str(values.get("目标人群")))
        if crowd is None:
            return None, [f"目标人群「{values.get('目标人群')}」不存在（可选 CROWD-0001 / CROWD-0002）"]
        with store.transaction():
            report = {"id": store.next_id(MODULE)}
            report.update({field: values.get(field) for field in REQUIRED_FIELDS})
            report.update({
                "目标人群": crowd.get("人群编号"),
                "关键指标": "尚未生成",
                "核心发现": "尚未生成",
                "数据区间": "—",
                "生成时间": "",
                "下次生成": str(values.get("下次生成") or "").strip()
                or (datetime.now() + timedelta(days=1)).strftime(DATETIME_FORMAT),
                "status": STATUS_ORDER[0],
                "pending": True,
                "abnormal": False,
                "人群版本": None,
                "快照编号": None,
                "快照版本": None,
                "刷新状态": "待生成",
                "待刷新原因": "",
                "生成次数": 0,
                "最近生成方式": "",
            })
            store.rows(MODULE).append(report)
        return report, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"洞察报告 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于用户洞察可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        if action == "提交生成":
            snapshot, _meta, message = self.refresh(entry_id, trigger="提交生成")
            if snapshot is None:
                return None, message
            return store.find(MODULE, entry_id), "洞察报告已提交生成并产出当前快照"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"洞察报告已{action}"
