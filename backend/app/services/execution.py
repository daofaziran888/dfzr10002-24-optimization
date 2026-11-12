"""定时任务执行代次：把每次调度、对应报告与业务日期绑定在一起。

业务日期只按执行开始时间归属，因此 23:55 开始、次日 00:10 完成的报告仍然算前一天。
执行代次一旦生成就保留，任务停用后列表和报告详情仍能看到最后一次有效代次。
"""
from __future__ import annotations

from typing import Any

from app.services import notifications
from app.store import store

EXECUTION_MODULE = notifications.EXECUTION_MODULE
SCHEDULE_MODULE = "schedule"
REPORT_MODULE = "report"

EXECUTING = "执行中"
COMPLETED = "已完成"


def _report(task_id: int, report_id: int | None = None) -> dict[str, Any] | None:
    if report_id:
        return store.find(REPORT_MODULE, int(report_id))
    for row in store.rows(REPORT_MODULE):
        if int(row.get("schedule_task_id", 0) or 0) == int(task_id):
            return row
    return None


def _task(task_id: int) -> dict[str, Any] | None:
    return store.find(SCHEDULE_MODULE, int(task_id))


def executions_for_task(task_id: int) -> list[dict[str, Any]]:
    return [
        row for row in store.rows(EXECUTION_MODULE)
        if int(row.get("task_id", 0)) == int(task_id)
    ]


def execution_for_report(report_id: int) -> dict[str, Any] | None:
    rows = [
        row for row in store.rows(EXECUTION_MODULE)
        if int(row.get("report_id", 0)) == int(report_id)
    ]
    if not rows:
        return None
    return max(rows, key=lambda row: (int(row.get("generation", 0)), int(row.get("id", 0))))


def latest_execution(task_id: int) -> dict[str, Any] | None:
    rows = executions_for_task(task_id)
    if not rows:
        return None
    return max(rows, key=lambda row: (int(row.get("generation", 0)), int(row.get("id", 0))))


def start_execution(
    task_id: int,
    *,
    started_at: Any = None,
    report_id: int | None = None,
    channels: list[str] | None = None,
) -> tuple[dict[str, Any] | None, str]:
    """开始一次执行；同一任务同一业务日期重复触发时复用已有代次，不会重复发通知。"""
    task = _task(task_id)
    if task is None:
        return None, f"定时任务 {task_id} 不存在或已归档"
    if str(task.get("status")) == "已停用":
        return None, "任务已停用，不能开始新的执行"

    started_at_text = notifications.normalize_time(started_at)
    business_date = notifications.business_date_of(started_at_text)
    existing = next(
        (row for row in executions_for_task(task_id) if row.get("business_date") == business_date),
        None,
    )
    report = _report(task_id, report_id)
    if report is None:
        return None, "没有找到本次执行对应的分析报告，无法建立执行记录"

    if existing is not None:
        notifications.ensure_queued(task, existing, report, channels, at=started_at_text)
        existing["updated_at"] = started_at_text
        return existing, "同一业务日期已有执行代次，本次重复触发已合并"

    generations = [int(row.get("generation", 0)) for row in executions_for_task(task_id)]
    execution = {
        "id": max((int(row.get("id", 0)) for row in store.rows(EXECUTION_MODULE)), default=0) + 1,
        "task_id": int(task.get("id", 0)),
        "task_no": str(task.get("任务编号") or ""),
        "generation": max(generations, default=0) + 1,
        "report_id": int(report.get("id", 0)),
        "report_no": str(report.get("报告编号") or ""),
        "business_date": business_date,
        "started_at": started_at_text,
        "finished_at": None,
        "terminal_at": None,
        "report_status": str(report.get("status") or ""),
        "status": EXECUTING,
        "created_at": started_at_text,
        "updated_at": started_at_text,
    }
    store.rows(EXECUTION_MODULE).append(execution)
    report["schedule_task_id"] = int(task.get("id", 0))
    report["schedule_task_no"] = str(task.get("任务编号") or "")
    report["execution_generation"] = execution["generation"]
    report["business_date"] = business_date
    report["started_at"] = started_at_text
    # 新一代次开始：报告回到非终态，等本次报告真正完成后再由终态触发通知。
    report["status"] = "生成中"
    report["报告状态"] = "生成中"
    report["pending"] = True
    report["finished_at"] = None
    report["terminal_at"] = None

    notifications.ensure_queued(task, execution, report, channels, at=started_at_text)
    return execution, f"执行代次 {execution['generation']} 已建立，业务日期 {business_date}"


def complete_execution(
    execution: dict[str, Any],
    *,
    report_status: str,
    finished_at: Any = None,
    channel_outcomes: dict[str, str] | None = None,
) -> dict[str, Any]:
    """报告进入终态：先落报告终态，再按终态发送一次通知。"""
    finished_at_text = notifications.normalize_time(finished_at)
    execution["status"] = COMPLETED
    execution["finished_at"] = finished_at_text
    execution["terminal_at"] = finished_at_text
    execution["report_status"] = report_status
    execution["updated_at"] = finished_at_text

    report = store.find(REPORT_MODULE, int(execution.get("report_id", 0)))
    if report is None:
        return execution
    report["status"] = report_status
    report["报告状态"] = report_status
    report["pending"] = report_status not in {"已生成", "生成失败", "已归档"}
    report["abnormal"] = report_status in {"生成失败"}
    report["finished_at"] = finished_at_text
    report["terminal_at"] = finished_at_text
    report["schedule_task_id"] = int(execution.get("task_id", 0))
    report["schedule_task_no"] = str(execution.get("task_no") or "")
    report["execution_generation"] = int(execution.get("generation", 0))
    report["business_date"] = str(execution.get("business_date") or "")

    task = _task(int(execution.get("task_id", 0)))
    notifications.dispatch(
        report,
        task,
        at=finished_at_text,
        channel_outcomes=channel_outcomes,
        generation=int(execution.get("generation", 0)),
    )
    return execution


def cancel_for_disabled_task(task_id: int, *, at: Any = None) -> list[dict[str, Any]]:
    """任务停用：取消所有待发送/待重试消息，并保留执行代次与已发送记录。"""
    task = _task(task_id)
    reason = "任务已停用，取消待发送通知"
    cancelled = notifications.cancel_pending(task_id, reason, at=at)
    latest = latest_execution(task_id)
    if task is not None:
        task["last_valid_generation"] = int((latest or {}).get("generation", 0))
        task["notification_cancelled_at"] = notifications.normalize_time(at)
        task["notification_cancel_reason"] = reason
    return cancelled


def reconcile_report(report: dict[str, Any]) -> dict[str, Any]:
    """通知状态跟随报告终态：报告未到终态时，任何已发送记录回退为待发送。

    只回退当前执行代次的记录，历史代次的通知结果保持可查。
    """
    execution = execution_for_report(int(report.get("id", 0)))
    if execution is not None:
        report.setdefault("schedule_task_id", execution.get("task_id"))
        report.setdefault("schedule_task_no", execution.get("task_no"))
        report.setdefault("execution_generation", execution.get("generation"))
        report.setdefault("business_date", execution.get("business_date"))
        report.setdefault("started_at", execution.get("started_at"))
        report.setdefault("finished_at", execution.get("terminal_at"))
        report.setdefault("terminal_at", execution.get("terminal_at"))
    if notifications.is_terminal_report(report.get("status")):
        return report

    generation = int((execution or {}).get("generation", 0) or 0)
    for row in notifications.rows_for_report(int(report.get("id", 0))):
        if generation and int(row.get("generation", 0) or 0) != generation:
            continue
        if row.get("status") in {notifications.SENT, notifications.FAILED, notifications.RETRYABLE}:
            row["status"] = notifications.PENDING
            row["sent_at"] = None
            row["error"] = None
            row["updated_at"] = notifications.now_text()
    return report


def execution_view(execution: dict[str, Any] | None) -> dict[str, Any] | None:
    if execution is None:
        return None
    report = store.find(REPORT_MODULE, int(execution.get("report_id", 0)))
    # 只展示当前代次的渠道结果，旧代次不会混进来让通知结果看起来像重复发送。
    rows = notifications.rows_for_execution(execution)
    return {
        "id": execution.get("id"),
        "generation": execution.get("generation"),
        "执行代次": execution.get("generation"),
        "business_date": execution.get("business_date"),
        "业务日期": execution.get("business_date"),
        "started_at": execution.get("started_at"),
        "开始时间": execution.get("started_at"),
        "finished_at": execution.get("finished_at"),
        "完成时间": execution.get("finished_at"),
        "terminal_at": execution.get("terminal_at"),
        "终态时间": execution.get("terminal_at"),
        "status": execution.get("status"),
        "执行状态": execution.get("status"),
        "report_id": execution.get("report_id"),
        "report_no": execution.get("report_no"),
        "报告编号": execution.get("report_no"),
        "report_status": execution.get("report_status"),
        "报告状态": execution.get("report_status"),
        "notification_result": notifications.overall_result(report, rows),
        "通知结果": notifications.overall_result(report, rows),
        "notifications": [notifications.notification_view(row) for row in rows],
        "notification_cancelled_at": execution.get("notification_cancelled_at"),
        "notification_cancel_reason": execution.get("notification_cancel_reason"),
    }


def schedule_view(task: dict[str, Any]) -> dict[str, Any]:
    """定时任务列表行：把执行代次、业务日期、报告与通知结果合并进来。"""
    row = dict(task)
    latest = latest_execution(int(task.get("id", 0)))
    report = store.find(REPORT_MODULE, int((latest or {}).get("report_id", 0))) if latest else None
    rows = notifications.rows_for_execution(latest) if latest else []
    generation = int((latest or {}).get("generation", 0)) or int(task.get("last_valid_generation", 0) or 0)
    row.update({
        "执行代次": generation or "—",
        "generation": generation,
        "最后有效执行代次": generation,
        "last_valid_generation": generation,
        "业务日期": (latest or {}).get("business_date") or "—",
        "business_date": (latest or {}).get("business_date"),
        "执行状态": (latest or {}).get("status") or "未执行",
        "execution_status": (latest or {}).get("status"),
        "开始时间": (latest or {}).get("started_at"),
        "完成时间": (latest or {}).get("finished_at"),
        "终态时间": (latest or {}).get("terminal_at"),
        "报告编号": (report or {}).get("报告编号") or (latest or {}).get("report_no") or "—",
        "报告状态": (report or {}).get("status") or (latest or {}).get("report_status") or "—",
        "报告终态": notifications.is_terminal_report((report or {}).get("status")),
        "通知结果": notifications.overall_result(report, rows) if latest else notifications.RESULT_NONE,
        "notification_result": notifications.overall_result(report, rows) if latest else notifications.RESULT_NONE,
        "待发送通知数": (
            notifications.pending_count(
                int((latest or {}).get("report_id", 0)),
                int((latest or {}).get("generation", 0) or 0) or None,
            )
            if latest else 0
        ),
        "report_id": (latest or {}).get("report_id"),
        "execution_id": (latest or {}).get("id"),
        "execution": execution_view(latest),
    })
    return row


def report_view(report: dict[str, Any]) -> dict[str, Any]:
    """分析报告详情：业务日期、终态时间与各渠道通知结果都从这里统一返回。

    通知结果只取当前执行代次的记录，与定时任务列表口径一致；历史代次放在
    notification_history 里，仍可回查但不参与本次结果判定。
    """
    reconcile_report(report)
    row = dict(report)
    execution = execution_for_report(int(report.get("id", 0)))
    rows = notifications.rows_for_execution(execution) if execution else notifications.rows_for_report(
        int(report.get("id", 0))
    )
    history_rows = [
        item for item in notifications.rows_for_report(int(report.get("id", 0)))
        if int(item.get("id", -1)) not in {int(row.get("id", -2)) for row in rows}
    ]
    status = str(report.get("status") or "")
    row.update({
        "报告状态": status,
        "status": status,
        "status_is_terminal": notifications.is_terminal_report(status),
        "报告终态": notifications.is_terminal_report(status),
        "business_date": report.get("business_date") or (execution or {}).get("business_date"),
        "业务日期": report.get("business_date") or (execution or {}).get("business_date") or "—",
        "execution_generation": report.get("execution_generation") or (execution or {}).get("generation"),
        "执行代次": report.get("execution_generation") or (execution or {}).get("generation") or "—",
        "schedule_task_id": report.get("schedule_task_id") or (execution or {}).get("task_id"),
        "schedule_task_no": report.get("schedule_task_no") or (execution or {}).get("task_no"),
        "started_at": report.get("started_at") or (execution or {}).get("started_at"),
        "finished_at": report.get("finished_at") or (execution or {}).get("terminal_at"),
        "terminal_at": report.get("terminal_at") or (execution or {}).get("terminal_at"),
        "终态时间": report.get("terminal_at") or (execution or {}).get("terminal_at") or "—",
        "notification_result": notifications.overall_result(report, rows),
        "通知结果": notifications.overall_result(report, rows),
        "pending_notification_count": notifications.pending_count(
            int(report.get("id", 0) or 0),
            int((execution or {}).get("generation", 0) or 0) or None,
        ),
        "notifications": [notifications.notification_view(item) for item in rows],
        "notification_history": [notifications.notification_view(item) for item in history_rows],
        "execution": execution_view(execution),
    })
    return row
