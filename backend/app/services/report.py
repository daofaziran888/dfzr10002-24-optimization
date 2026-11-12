"""分析报告管理业务规则：状态流转、终态判定与通知结果回写。

报告只有进入终态（已生成 / 生成失败 / 已归档）时才会触发一次通知；报告被回退到
非终态时，已经回写的通知结果会回退成待发送，保证持久化的通知状态与报告终态一致。
"""
from __future__ import annotations

from typing import Any

from app.services import execution, notifications
from app.store import store

MODULE = "report"
REQUIRED_FIELDS = ["报告编号", "报告标题", "关联数据集"]
STATUS_ORDER = ["草稿", "生成中", "已生成", "已归档"]
TERMINAL_STATUSES = notifications.TERMINAL_REPORT_STATUSES
ACTION_RULES = {
    "开始生成": "生成中",
    "生成报告": "已生成",
    "标记生成失败": "生成失败",
    "归档报告": "已归档",
    "删除报告": "已归档",
}
NEGATIVE_ACTIONS = ["标记生成失败"]
NOTIFICATION_ACTIONS = {"重试通知"}


class ReportService:
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
            rows = [row for row in rows if keyword in str(row.get("报告编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return [execution.report_view(row) for row in rows[start:start + size]], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None
        return execution.report_view(entry)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry.update({
            "status": STATUS_ORDER[0],
            "报告状态": STATUS_ORDER[0],
            "pending": True,
            "abnormal": False,
        })
        for optional in ["分析周期", "报告模板", "生成时间", "生成人", "schedule_task_id"]:
            if values.get(optional) is not None:
                entry[optional] = values.get(optional)
        rows.append(entry)
        return entry, []

    def run_action(
        self,
        entry_id: int,
        action: str,
        values: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        values = values or {}
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"分析报告 {entry_id} 不存在或已归档"

        if action in NOTIFICATION_ACTIONS:
            return self._retry_notification(entry, values)

        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于分析报告管理可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"

        previous = str(entry.get("status") or "")
        at = notifications.normalize_time(values.get("at") or values.get("完成时间"))
        entry["status"] = target
        entry["报告状态"] = target
        # 生成失败同样是终态：终态记录在通知侧统一判定，避免两处口径不一致。
        entry["pending"] = not notifications.is_terminal_report(target)
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        entry["updated_at"] = at

        execution_row = execution.execution_for_report(entry_id)
        if execution_row is not None:
            entry["schedule_task_id"] = execution_row.get("task_id")
            entry["schedule_task_no"] = execution_row.get("task_no")
            entry["execution_generation"] = execution_row.get("generation")
            entry["business_date"] = execution_row.get("business_date")
            entry["started_at"] = execution_row.get("started_at")
            execution_row["updated_at"] = at

        if notifications.is_terminal_report(target):
            entry["finished_at"] = at
            entry["terminal_at"] = at
            if execution_row is not None:
                execution_row["report_status"] = target
                execution_row["finished_at"] = at
                execution_row["terminal_at"] = at
                execution_row["status"] = execution.COMPLETED
            task = store.find("schedule", int(entry.get("schedule_task_id", 0) or 0))
            dispatched = notifications.dispatch(
                entry,
                task,
                at=at,
                channel_outcomes=values.get("channel_outcomes"),
                generation=int((execution_row or {}).get("generation", 0) or 0) or None,
            )
            if not notifications.is_terminal_report(previous) and dispatched:
                return entry, f"报告已进入终态（{target}），已发送 {len(dispatched)} 条通知"
            return entry, f"分析报告已{action}，通知结果：{notifications.overall_result(entry)}"

        # 回退到非终态：通知状态跟随报告回退为待发送，终态时间也一并清掉，
        # 不会留下「仍在生成中、却显示上一次终态时间」的冲突展示。
        entry["finished_at"] = None
        entry["terminal_at"] = None
        if execution_row is not None:
            execution_row["terminal_at"] = None
            execution_row["finished_at"] = None
            if execution_row.get("status") == execution.COMPLETED:
                execution_row["status"] = execution.EXECUTING
        execution.reconcile_report(entry)
        return entry, f"分析报告已{action}，等待报告进入终态后再发送通知"

    def _retry_notification(
        self, entry: dict[str, Any], values: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, str]:
        task = store.find("schedule", int(entry.get("schedule_task_id", 0) or 0))
        execution_row = execution.execution_for_report(int(entry.get("id", 0)))
        if task is None and execution_row is not None:
            task = store.find("schedule", int(execution_row.get("task_id", 0)))
        # 重试只挂在当前报告的终态上：报告状态、终态时间、执行代次都不变。
        ok, message, _ = notifications.retry(
            entry,
            task,
            channel=values.get("channel") or values.get("渠道"),
            outcome=values.get("outcome") or values.get("结果") or "success",
            at=values.get("at"),
            generation=int((execution_row or {}).get("generation", 0) or 0) or None,
        )
        return (entry if ok else None), message
