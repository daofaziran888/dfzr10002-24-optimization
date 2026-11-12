"""定时任务执行代次与通知边界的行为测试。

用标准库 unittest 运行：backend 目录下 `python -m unittest discover -s tests`。
每个用例都从种子数据重建内存仓库，互不影响。
"""
from __future__ import annotations

import unittest

from app.seed import SEED_ROWS
from app.services import notifications
from app.services.report import ReportService
from app.services.schedule import ScheduleService
from app.store import store


def reset_store() -> None:
    store._tables = {name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()}


class NotificationBoundaryTest(unittest.TestCase):
    def setUp(self) -> None:
        reset_store()
        self.schedule = ScheduleService()
        self.report = ReportService()

    def _new_task(self, task_no: str, channels: str = "短信、邮件") -> int:
        task, _ = self.schedule.create_entry({
            "任务编号": task_no,
            "任务名称": f"{task_no} 测试任务",
            "分析类型": "销售分析",
            "通知渠道": channels,
        })
        self.schedule.run_action(task["id"], "启用配置", {})
        return task["id"]

    def _new_report(self, report_no: str, task_id: int) -> int:
        report, _ = self.report.create_entry({
            "报告编号": report_no,
            "报告标题": f"{report_no} 测试报告",
            "关联数据集": "订单明细",
            "schedule_task_id": task_id,
        })
        return report["id"]

    def _channel_state(self, report_id: int) -> dict[str, str]:
        return {
            row["channel"]: row["status"]
            for row in notifications.rows_for_report(report_id)
        }

    def test_cross_midnight_keeps_business_date_and_sends_once(self) -> None:
        task_id = self._new_task("SCH-T01")
        report_id = self._new_report("REP-T01", task_id)
        self.schedule.run_action(task_id, "触发执行", {
            "started_at": "2026-11-10T23:55:00",
            "report_id": report_id,
        })
        self.assertEqual(self._channel_state(report_id), {"短信": "待发送", "邮件": "待发送"})

        _, message = self.schedule.run_action(task_id, "完成执行", {
            "finished_at": "2026-11-11T00:10:00",
            "report_status": "已生成",
        })
        self.assertIn("已发送", message)

        detail = self.report.get_entry(report_id)
        self.assertEqual(detail["业务日期"], "2026-11-10")
        self.assertEqual(detail["终态时间"], "2026-11-11 00:10:00")
        self.assertEqual(detail["通知结果"], "已发送")
        self.assertEqual(self._channel_state(report_id), {"短信": "已发送", "邮件": "已发送"})

        # 重复完成 / 重复触发同一业务日期只保留一次有效通知。
        self.schedule.run_action(task_id, "完成执行", {
            "finished_at": "2026-11-11T00:20:00",
            "report_status": "已生成",
        })
        self.schedule.run_action(task_id, "触发执行", {
            "started_at": "2026-11-10T23:56:00",
            "report_id": report_id,
        })
        rows = notifications.rows_for_report(report_id)
        self.assertEqual(len(rows), 2)
        self.assertEqual([row["attempts"] for row in rows], [1, 1])

    def test_no_notification_before_terminal(self) -> None:
        task_id = self._new_task("SCH-T02", channels="短信")
        report_id = self._new_report("REP-T02", task_id)
        self.schedule.run_action(task_id, "触发执行", {
            "started_at": "2026-11-10T23:55:00",
            "report_id": report_id,
        })
        detail = self.report.get_entry(report_id)
        self.assertEqual(detail["通知结果"], "待报告终态")
        self.assertEqual(self._channel_state(report_id), {"短信": "待发送"})

        ok, message, _ = notifications.retry(
            store.find("report", report_id), store.find("schedule", task_id)
        )
        self.assertFalse(ok)
        self.assertIn("尚未进入终态", message)

    def test_partial_failure_when_sms_fails_and_mail_succeeds(self) -> None:
        task_id = self._new_task("SCH-T03")
        report_id = self._new_report("REP-T03", task_id)
        self.schedule.run_action(task_id, "触发执行", {
            "started_at": "2026-11-11T08:30:00",
            "report_id": report_id,
        })
        self.schedule.run_action(task_id, "完成执行", {
            "finished_at": "2026-11-11T09:10:00",
            "report_status": "已生成",
            "channel_outcomes": {"短信": "fail", "邮件": "success"},
        })
        detail = self.report.get_entry(report_id)
        self.assertEqual(detail["通知结果"], "部分失败")
        self.assertEqual(self._channel_state(report_id), {"短信": "发送失败", "邮件": "已发送"})

        schedule_row = self.schedule.get_entry(task_id)
        self.assertEqual(schedule_row["通知结果"], detail["通知结果"])
        self.assertEqual(schedule_row["业务日期"], detail["业务日期"])

    def test_timeout_retry_returns_to_same_terminal(self) -> None:
        task_id = self._new_task("SCH-T04", channels="短信")
        report_id = self._new_report("REP-T04", task_id)
        self.schedule.run_action(task_id, "触发执行", {
            "started_at": "2026-11-11T08:30:00",
            "report_id": report_id,
        })
        self.schedule.run_action(task_id, "完成执行", {
            "finished_at": "2026-11-11T09:10:00",
            "report_status": "已生成",
            "channel_outcomes": {"短信": "timeout"},
        })
        before = self.report.get_entry(report_id)
        self.assertEqual(self._channel_state(report_id), {"短信": "待重试"})
        self.assertEqual(before["通知结果"], "待重试")

        _, message = self.report.run_action(report_id, "重试通知", {
            "channel": "短信",
            "outcome": "success",
            "at": "2026-11-11 09:12:00",
        })
        self.assertIn("报告终态保持不变", message)
        after = self.report.get_entry(report_id)
        self.assertEqual(after["报告状态"], "已生成")
        self.assertEqual(after["终态时间"], before["终态时间"])
        self.assertEqual(after["通知结果"], "已发送")
        self.assertEqual(notifications.rows_for_report(report_id)[0]["attempts"], 2)

        ok, message, _ = notifications.retry(
            store.find("report", report_id), store.find("schedule", task_id), channel="短信"
        )
        self.assertFalse(ok)
        self.assertIn("已有一次有效通知", message)

    def test_disable_cancels_queued_and_keeps_last_generation(self) -> None:
        task_id = self._new_task("SCH-T05")
        report_id = self._new_report("REP-T05", task_id)
        self.schedule.run_action(task_id, "触发执行", {
            "started_at": "2026-11-10T23:55:00",
            "report_id": report_id,
        })
        _, message = self.schedule.run_action(task_id, "停用配置", {
            "at": "2026-11-11 00:02:00",
        })
        self.assertIn("2 条待发送通知", message)
        self.assertEqual(notifications.pending_count(report_id), 0)
        self.assertEqual(self._channel_state(report_id), {"短信": "已取消", "邮件": "已取消"})

        # 任务停用后重新读取，最后有效代次和业务日期仍然保留。
        row = self.schedule.get_entry(task_id)
        self.assertEqual(row["执行代次"], 1)
        self.assertEqual(row["last_valid_generation"], 1)
        self.assertEqual(row["业务日期"], "2026-11-10")
        self.assertEqual(row["status"], "已停用")

        # 报告随后进入终态也不会补发通知。
        self.report.run_action(report_id, "生成报告", {"at": "2026-11-11 00:10:00"})
        detail = self.report.get_entry(report_id)
        self.assertEqual(detail["业务日期"], "2026-11-10")
        self.assertEqual(detail["通知结果"], "已取消")
        self.assertEqual(detail["报告状态"], "已生成")

        # 停用后重试也被挡住。
        ok, message, _ = notifications.retry(
            store.find("report", report_id), store.find("schedule", task_id)
        )
        self.assertFalse(ok)
        self.assertIn("任务已停用", message)

    def test_disable_keeps_sent_records_visible(self) -> None:
        task_id = self._new_task("SCH-T06")
        report_id = self._new_report("REP-T06", task_id)
        self.schedule.run_action(task_id, "触发执行", {
            "started_at": "2026-11-10T23:55:00",
            "report_id": report_id,
        })
        self.schedule.run_action(task_id, "完成执行", {
            "finished_at": "2026-11-11T00:10:00",
            "report_status": "已生成",
            "channel_outcomes": {"短信": "success", "邮件": "timeout"},
        })
        self.schedule.run_action(task_id, "停用配置", {"at": "2026-11-11 00:15:00"})
        detail = self.report.get_entry(report_id)
        self.assertEqual(self._channel_state(report_id), {"短信": "已发送", "邮件": "已取消"})
        self.assertEqual(detail["通知结果"], "部分取消")
        self.assertEqual(detail["notifications"][0]["发送时间"], "2026-11-11 00:10:00")

    def test_report_terminal_wins_over_notification_state(self) -> None:
        task_id = self._new_task("SCH-T07", channels="短信")
        report_id = self._new_report("REP-T07", task_id)
        self.schedule.run_action(task_id, "触发执行", {
            "started_at": "2026-11-10T23:55:00",
            "report_id": report_id,
        })
        self.schedule.run_action(task_id, "完成执行", {
            "finished_at": "2026-11-11T00:10:00",
            "report_status": "已生成",
        })
        self.assertEqual(self.report.get_entry(report_id)["通知结果"], "已发送")

        # 报告被回退到非终态：已回写的通知结果跟随回退，不会提前显示成功。
        self.report.run_action(report_id, "开始生成", {"at": "2026-11-11 00:15:00"})
        detail = self.report.get_entry(report_id)
        self.assertEqual(detail["报告状态"], "生成中")
        self.assertEqual(detail["通知结果"], "待报告终态")
        self.assertEqual(self._channel_state(report_id), {"短信": "待发送"})

    def test_new_business_date_is_a_new_generation(self) -> None:
        task_id = self._new_task("SCH-T08", channels="短信")
        report_id = self._new_report("REP-T08", task_id)
        self.schedule.run_action(task_id, "触发执行", {
            "started_at": "2026-11-10T23:55:00",
            "report_id": report_id,
        })
        self.schedule.run_action(task_id, "完成执行", {
            "finished_at": "2026-11-11T00:10:00",
            "report_status": "已生成",
        })
        view, message = self.schedule.run_action(task_id, "触发执行", {
            "started_at": "2026-11-12T23:55:00",
            "report_id": report_id,
        })
        self.assertEqual(view["执行代次"], 2)
        self.assertEqual(message, "执行代次 2 已建立，业务日期 2026-11-12")
        self.assertEqual(self.report.get_entry(report_id)["通知结果"], "待报告终态")
        # 旧代次的已发送记录保留在历史里。
        self.assertEqual(len(self.report.get_entry(report_id)["notification_history"]), 1)


if __name__ == "__main__":
    unittest.main()
