"""定时分析任务业务规则：状态流转、字段校验、执行代次与通知边界都收在这里。

执行代次与通知收在 services/execution.py 和 services/notifications.py，这里只负责把
任务动作与它们串起来：停用任务会取消待发送通知，启用 / 停用都不会清掉最后有效代次。
"""
from __future__ import annotations

from typing import Any

from app.services import execution, notifications
from app.store import store

MODULE = "schedule"
REQUIRED_FIELDS = ["任务编号", "任务名称", "分析类型"]
STATUS_ORDER = ["草稿", "待校验", "已启用", "已停用"]
ACTION_RULES = {"提交校验": "待校验", "启用配置": "已启用", "停用配置": "已停用"}
NEGATIVE_ACTIONS = ["停用配置"]

# 执行类动作：不改变任务自身状态，只操作执行代次和通知。
EXECUTION_ACTIONS = {"触发执行", "完成执行", "重试通知"}


class ScheduleService:
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
            rows = [row for row in rows if keyword in str(row.get("任务编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return [execution.schedule_view(row) for row in rows[start:start + size]], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        row = store.find(MODULE, entry_id)
        if row is None:
            return None
        view = execution.schedule_view(row)
        view["executions"] = [
            execution.execution_view(item) for item in execution.executions_for_task(entry_id)
        ]
        return view

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry.update({
            "status": STATUS_ORDER[0],
            "pending": True,
            "abnormal": False,
            "是否启用": "否",
            "last_valid_generation": 0,
        })
        for optional in ["调度表达式", "输入参数", "输出目标", "通知渠道"]:
            if values.get(optional) is not None:
                entry[optional] = values[optional]
        rows.append(entry)
        return entry, []

    def run_action(
        self,
        entry_id: int,
        action: str,
        values: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"定时任务 {entry_id} 不存在或已归档"
        values = values or {}

        if action in EXECUTION_ACTIONS:
            result, message = self._run_execution_action(entry, action, values)
            return (execution.schedule_view(entry) if result else None), message

        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于定时分析任务可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"

        entry["status"] = target
        entry["是否启用"] = "是" if target == "已启用" else "否"
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS

        if action == "停用配置":
            cancelled = execution.cancel_for_disabled_task(
                entry_id, at=values.get("at") or values.get("停用时间")
            )
            entry["last_valid_generation"] = int(
                (execution.latest_execution(entry_id) or {}).get("generation", 0)
            )
            if cancelled:
                return entry, f"定时任务已停用，已取消 {len(cancelled)} 条待发送通知"
            return entry, "定时任务已停用，没有待发送通知需要取消"
        return entry, f"定时任务已{action}"

    def _run_execution_action(
        self,
        task: dict[str, Any],
        action: str,
        values: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, str]:
        task_id = int(task.get("id", 0))
        if action == "触发执行":
            started_at = values.get("started_at") or values.get("开始时间")
            report_id = values.get("report_id")
            report_id = int(report_id) if str(report_id or "").strip().isdigit() else None
            channels = notifications.split_channels(values.get("通知渠道")) or None
            return execution.start_execution(
                task_id, started_at=started_at, report_id=report_id, channels=channels
            )

        latest = execution.latest_execution(task_id)
        if latest is None:
            return None, "该任务还没有执行代次，请先触发一次执行"

        if action == "完成执行":
            report_status = str(values.get("report_status") or values.get("报告状态") or "已生成")
            if not notifications.is_terminal_report(report_status):
                return None, f"报告状态「{report_status}」不是终态，不能结束执行并发送通知"
            execution.complete_execution(
                latest,
                report_status=report_status,
                finished_at=values.get("finished_at") or values.get("完成时间"),
                channel_outcomes=values.get("channel_outcomes"),
            )
            result = notifications.overall_result(
                store.find("report", int(latest.get("report_id", 0))),
                notifications.rows_for_execution(latest),
            )
            return task, f"报告已进入终态，通知结果：{result}"

        if action == "重试通知":
            report = store.find("report", int(latest.get("report_id", 0)))
            if report is None:
                return None, "执行代次对应的分析报告不存在"
            channel = values.get("channel") or values.get("渠道")
            outcome = values.get("outcome") or values.get("结果") or "success"
            ok, message, _ = notifications.retry(
                report,
                task,
                channel=channel,
                outcome=outcome,
                at=values.get("at"),
                generation=int(latest.get("generation", 0) or 0) or None,
            )
            return (task if ok else None), message

        return None, f"动作「{action}」不属于执行类动作"

    def retry_notification(
        self, entry_id: int, values: dict[str, Any] | None = None
    ) -> tuple[dict[str, Any] | None, str]:
        return self.run_action(entry_id, "重试通知", values)
