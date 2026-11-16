"""用户洞察增量刷新、快照唯一性与订阅消费的回归测试。

运行：cd backend && .venv/bin/python -m unittest discover -s tests -v
"""
from __future__ import annotations

import unittest

from app.services import userinsight as insight_module
from app.services.subscription import SubscriptionService
from app.services.userinsight import UserinsightService
from app.store import store


class InsightTestCase(unittest.TestCase):
    def setUp(self) -> None:
        store.reset()
        self.service = UserinsightService()
        self.subscriptions = SubscriptionService()

    def snapshot_rows(self) -> list[dict]:
        return store.rows("_insight_snapshots")

    def status_rows(self) -> list[dict]:
        return store.rows("_insight_status_logs")


class IntervalSelectionTest(InsightTestCase):
    def test_gap_batch_is_excluded_in_favour_of_contiguous_interval(self) -> None:
        snapshot, _meta, message = self.service.refresh(1)
        self.assertIsNotNone(snapshot, message)
        self.assertEqual(snapshot["数据区间"], "2026-09-01 ~ 2026-09-08")
        self.assertNotIn("BATCH-0006", snapshot["批次编号"])
        excluded = {item["批次编号"]: item for item in snapshot["区间判定"]["排除批次"]}
        self.assertIn("进行中", excluded["BATCH-0005"]["原因"])
        self.assertIn("缺口", excluded["BATCH-0006"]["原因"])

    def test_snapshot_always_reads_one_contiguous_interval(self) -> None:
        snapshot, _meta, _message = self.service.refresh(1)
        starts = [batch["数据区间起"] for batch in store.rows("_insight_batches") if int(batch["id"]) in snapshot["批次ids"]]
        self.assertEqual(starts, sorted(starts))
        self.assertTrue(snapshot["区间判定"]["选定区间"])

    def test_non_contiguous_selection_is_rejected(self) -> None:
        snapshot, _meta, message = self.service.refresh(3, batch_ids=[7, 9])
        self.assertIsNone(snapshot)
        self.assertIn("不连续", message)

    def test_incomplete_selection_is_rejected(self) -> None:
        snapshot, _meta, message = self.service.refresh(1, batch_ids=[1, 5])
        self.assertIsNone(snapshot)
        self.assertIn("未完成", message)

    def test_foreign_batch_is_rejected(self) -> None:
        snapshot, _meta, message = self.service.refresh(3, batch_ids=[1])
        self.assertIsNone(snapshot)
        self.assertIn("不属于目标人群", message)


class IncrementalRefreshTest(InsightTestCase):
    def test_refresh_computes_metric_changes_against_previous_snapshot(self) -> None:
        snapshot, _meta, _message = self.service.refresh(3)
        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot["基线快照"], "SNAP-0001")
        self.assertTrue(snapshot["可比"])
        changes = {item["指标"]: item for item in snapshot["关键指标变化"]}
        self.assertEqual(changes["日均活跃用户"]["变化类型"], "上升")
        self.assertAlmostEqual(changes["日均活跃用户"]["本期值"], 4150.0)

    def test_shared_crowd_scans_only_once(self) -> None:
        first, _meta, _message = self.service.refresh(1)
        second, _meta2, _message2 = self.service.refresh(2)
        self.assertEqual(first["扫描"]["复用批次"], [])
        self.assertEqual(second["扫描"]["扫描批次"], [])
        self.assertEqual(set(second["扫描"]["复用批次"]), set(first["批次编号"]))
        caches = store.rows("_insight_scans")
        self.assertEqual(len({item["缓存键"] for item in caches}), len(caches))

    def test_batch_refresh_returns_per_report_results(self) -> None:
        result = self.service.refresh_many([1, 999, 1])
        self.assertEqual(result["total"], 2)
        self.assertEqual(result["succeeded"], 1)
        self.assertEqual(result["failed"], 1)
        self.assertFalse(result["items"][1]["ok"])
        self.assertEqual(result["items"][0]["快照版本"], 1)

    def test_version_and_status_record_are_incremented_together(self) -> None:
        first, meta, _message = self.service.refresh(1)
        second, meta2, _message2 = self.service.refresh(1)
        self.assertEqual((second["版本"], meta2["状态记录id"]), (first["版本"] + 1, 2))
        self.assertEqual(len(self.status_rows()), 2)


class SnapshotUniquenessTest(InsightTestCase):
    def test_only_one_current_snapshot_per_business_key(self) -> None:
        self.service.refresh(1)
        self.service.refresh(1)
        current = [row for row in self.snapshot_rows() if row["业务键"] == "USER-0001" and row["是否当前"]]
        self.assertEqual(len(current), 1)
        historical = [row for row in self.snapshot_rows() if row["业务键"] == "USER-0001" and not row["是否当前"]]
        self.assertEqual(len(historical), 1)


class CrowdModificationTest(InsightTestCase):
    def test_modify_crowd_does_not_recompute_or_overwrite_history(self) -> None:
        self.service.refresh(1)
        before = {row["报告编号"]: dict(row) for row in store.rows("userinsight")}
        crowd, message, affected = self.service.modify_crowd("CROWD-0001", {"人群定义": "新的定义"})
        self.assertEqual(crowd["版本"], 4)
        self.assertEqual(len(affected), 2)
        after = {row["报告编号"]: row for row in store.rows("userinsight")}
        for code in ("USER-0001", "USER-0002"):
            self.assertEqual(after[code]["关键指标"], before[code]["关键指标"])
            self.assertEqual(after[code]["数据区间"], before[code]["数据区间"])
            self.assertEqual(after[code]["生成时间"], before[code]["生成时间"])
            self.assertEqual(after[code]["下次生成"], before[code]["下次生成"])
            self.assertEqual(after[code]["刷新状态"], "待刷新")
        self.assertEqual(after["USER-0003"]["刷新状态"], "已是最新")
        self.assertIn("未被覆盖", message)

    def test_next_generation_advances_from_previous_schedule(self) -> None:
        original = store.find("userinsight", 3)["下次生成"]
        _snapshot, meta, _message = self.service.refresh(3)
        self.assertNotEqual(meta["下次生成"], original)
        _snapshot2, meta2, _message2 = self.service.refresh(3)
        self.assertGreaterEqual(meta2["下次生成"], meta["下次生成"])


class TransactionTest(InsightTestCase):
    def test_failure_leaves_no_partial_snapshot(self) -> None:
        before = (
            len(self.snapshot_rows()),
            len(self.status_rows()),
            len(store.rows("_insight_scans")),
            store.find("userinsight", 3)["快照编号"],
        )
        original = self.service._upsert_scan_cache
        calls = {"count": 0}

        def failing(*args, **kwargs):
            calls["count"] += 1
            if calls["count"] == 2:
                raise RuntimeError("模拟写入失败")
            return original(*args, **kwargs)

        self.service._upsert_scan_cache = failing  # type: ignore[method-assign]
        try:
            snapshot, _meta, message = self.service.refresh(3)
        finally:
            self.service._upsert_scan_cache = original  # type: ignore[method-assign]
        self.assertIsNone(snapshot)
        self.assertIn("事务已回滚", message)
        after = (
            len(self.snapshot_rows()),
            len(self.status_rows()),
            len(store.rows("_insight_scans")),
            store.find("userinsight", 3)["快照编号"],
        )
        self.assertEqual(before, after)

    def test_batch_failure_rolls_back_only_the_failed_report(self) -> None:
        original = self.service._upsert_scan_cache
        calls = {"count": 0}

        def failing(*args, **kwargs):
            calls["count"] += 1
            if calls["count"] == 6:
                raise RuntimeError("模拟第二份报告写入失败")
            return original(*args, **kwargs)

        self.service._upsert_scan_cache = failing  # type: ignore[method-assign]
        try:
            result = self.service.refresh_many([1, 2])
        finally:
            self.service._upsert_scan_cache = original  # type: ignore[method-assign]
        self.assertEqual((result["succeeded"], result["failed"]), (1, 1))
        keys = {row["业务键"] for row in self.snapshot_rows()}
        self.assertIn("USER-0001", keys)
        self.assertNotIn("USER-0002", keys)
        self.assertEqual([row["业务键"] for row in self.status_rows()], ["USER-0001"])

    def test_subscription_consumption_is_atomic_and_idempotent(self) -> None:
        entry, message = self.subscriptions.consume_insight_snapshot(3)
        self.assertIsNotNone(entry, message)
        self.assertEqual(entry["最近消费快照"], "SNAP-0001")
        self.assertEqual(len(store.rows("_insight_deliveries")), 1)
        again, message2 = self.subscriptions.consume_insight_snapshot(3)
        self.assertIsNone(again)
        self.assertIn("不再重复推送", message2)
        self.assertEqual(len(store.rows("_insight_deliveries")), 1)

    def test_subscription_consumption_failure_rolls_back(self) -> None:
        before = dict(store.find("subscription", 3))
        real_rows = store.rows

        def failing_rows(name: str):
            if name == "_insight_deliveries":
                raise RuntimeError("模拟交付记录写入失败")
            return real_rows(name)

        store.rows = failing_rows  # type: ignore[method-assign]
        try:
            entry, message = self.subscriptions.consume_insight_snapshot(3)
        finally:
            store.rows = real_rows  # type: ignore[method-assign]
        self.assertIsNone(entry)
        self.assertIn("事务已回滚", message)
        self.assertEqual(store.find("subscription", 3), before)
        self.assertEqual(len(real_rows("_insight_deliveries")), 0)

    def test_subscription_requires_enabled_status_and_current_snapshot(self) -> None:
        entry, message = self.subscriptions.consume_insight_snapshot(1)
        self.assertIsNone(entry)
        self.assertIn("只有已启用订阅", message)

        report_one = store.find("subscription", 1)
        report_one["status"] = "已启用"
        entry, message = self.subscriptions.consume_insight_snapshot(1)
        self.assertIsNone(entry)
        self.assertIn("还没有当前快照", message)


class TraceabilityTest(InsightTestCase):
    def test_findings_carry_interval_batches_and_baseline(self) -> None:
        snapshot, _meta, _message = self.service.refresh(3)
        findings = snapshot["核心发现"]
        self.assertTrue(findings)
        for finding in findings:
            self.assertTrue(finding["数据区间"])
            self.assertTrue(finding["依据批次"])
            self.assertTrue(set(finding["依据批次"]).issubset(set(snapshot["批次编号"])))
        self.assertIn("排除批次", snapshot["区间判定"])
        self.assertTrue(snapshot["区间判定"]["说明"])

    def test_trace_exposes_status_logs_and_deliveries(self) -> None:
        self.service.refresh(3)
        self.subscriptions.consume_insight_snapshot(3)
        trace = self.service.trace(3)
        self.assertIsNotNone(trace)
        self.assertEqual(trace["当前快照"]["快照编号"], store.find("userinsight", 3)["快照编号"])
        self.assertTrue(trace["状态记录"])
        self.assertEqual(len(trace["订阅交付"]), 1)
        self.assertEqual(trace["扫描明细"][0]["批次编号"], "BATCH-0007")

    def test_historical_snapshot_is_readable_by_version(self) -> None:
        self.service.refresh(3)
        self.service.refresh(3, batch_ids=[9, 10])
        original = self.service.get_snapshot(3, 1)
        self.assertIsNotNone(original)
        self.assertFalse(original["是否当前"])
        current = self.service.get_snapshot(3)
        self.assertEqual(current["数据区间"], "2026-09-05 ~ 2026-09-08")


if __name__ == "__main__":
    unittest.main()
