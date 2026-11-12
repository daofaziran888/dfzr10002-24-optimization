"""通知边界规则：排队、终态触发、去重、停用取消与失败重试。

这个模块只处理通知，不改报告本身的终态。所有通知动作都先回到报告终态再执行：
报告没有进入终态时通知保持排队；报告终态与通知结果冲突时，以报告终态为准。
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Iterable

from app.store import store

NOTIFICATION_MODULE = "notification"
EXECUTION_MODULE = "schedule_execution"

# 通知状态
PENDING = "待发送"
RETRYABLE = "待重试"
SENT = "已发送"
FAILED = "发送失败"
CANCELLED = "已取消"

# 报告终态：只有这些状态才允许发送通知
TERMINAL_REPORT_STATUSES = {"已生成", "生成失败", "已归档"}
FAILED_REPORT_STATUSES = {"生成失败"}

# 总体通知结果的展示顺序
RESULT_SENT = "已发送"
RESULT_PARTIAL = "部分失败"
RESULT_FAILED = "发送失败"
RESULT_CANCELLED = "已取消"
RESULT_PARTIAL_CANCELLED = "部分取消"
RESULT_PENDING = "待发送"
RESULT_RETRY = "待重试"
RESULT_WAITING_TERMINAL = "待报告终态"
RESULT_NONE = "未发送"


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def normalize_time(value: Any, default: str | None = None) -> str:
    """把 ISO 或空格分隔的时间统一成 YYYY-MM-DD HH:MM:SS。"""
    text = str(value or "").strip()
    if not text:
        return default or now_text()
    text = text.replace("T", " ")
    if len(text) < 19:
        text = text[:19] if len(text) >= 16 else text
    return text[:19]


def business_date_of(started_at: Any) -> str:
    """业务日期只取执行开始时间所在的日期，跨午夜不跟着自然日切走。"""
    text = str(started_at or "").strip().replace("T", " ")
    match = re.search(r"(\d{4}-\d{2}-\d{2})", text)
    if match:
        return match.group(1)
    return now_text()[:10]


def split_channels(value: Any) -> list[str]:
    """「短信、邮件」这类渠道配置拆成有序列表。"""
    if isinstance(value, (list, tuple, set)):
        items = [str(item).strip() for item in value]
    else:
        items = re.split(r"[、,，/|;；\s]+", str(value or ""))
    return [item for item in items if item]


def is_terminal_report(status: Any) -> bool:
    return str(status or "") in TERMINAL_REPORT_STATUSES


def idempotency_key(task_id: int, generation: int, channel: str) -> str:
    return f"{task_id}:{generation}:{channel}"


def rows_for_report(report_id: int) -> list[dict[str, Any]]:
    return [
        row for row in store.rows(NOTIFICATION_MODULE)
        if int(row.get("report_id", 0)) == int(report_id)
    ]


def rows_for_task(task_id: int) -> list[dict[str, Any]]:
    return [
        row for row in store.rows(NOTIFICATION_MODULE)
        if int(row.get("task_id", 0)) == int(task_id)
    ]


def rows_for_execution(execution: dict[str, Any] | None) -> list[dict[str, Any]]:
    """按「报告 + 执行代次」取通知记录，旧代次的记录不会混进当前代次的结果。"""
    if not execution:
        return []
    report_id = int(execution.get("report_id", 0) or 0)
    generation = int(execution.get("generation", 0) or 0)
    return [
        row for row in rows_for_report(report_id)
        if int(row.get("generation", 0) or 0) == generation
    ]


def overall_result(report: dict[str, Any] | None, rows: Iterable[dict[str, Any]] | None = None) -> str:
    """计算整份报告的通知结果，未进入终态时一律停留在「待报告终态」。"""
    report = report or {}
    status = str(report.get("status") or report.get("报告状态") or "")
    rows = list(rows if rows is not None else rows_for_report(int(report.get("id", 0) or 0)))
    if not is_terminal_report(status):
        # 还没有排队记录时说明这份报告不涉及通知；有排队记录才叫「待报告终态」。
        return RESULT_WAITING_TERMINAL if rows else RESULT_NONE
    if not rows:
        return RESULT_NONE

    statuses = [str(row.get("status") or "") for row in rows]
    if any(item in {PENDING, RETRYABLE} for item in statuses):
        # 还有渠道没拿到最终结果时，整体不能显示成成功或失败。
        return RESULT_RETRY if RETRYABLE in statuses and PENDING not in statuses else RESULT_PENDING
    cancelled = statuses.count(CANCELLED)
    sent = statuses.count(SENT)
    failed = statuses.count(FAILED)
    if cancelled and cancelled == len(statuses):
        return RESULT_CANCELLED
    if cancelled:
        # 只剩下一部分渠道真的发出去 / 失败，其余随停用取消。
        return RESULT_PARTIAL_CANCELLED
    if failed and sent:
        return RESULT_PARTIAL
    if failed:
        return RESULT_FAILED
    if sent == len(statuses):
        return RESULT_SENT
    return RESULT_NONE


def pending_count(report_id: int, generation: int | None = None) -> int:
    rows = rows_for_report(report_id)
    if generation is not None:
        rows = [row for row in rows if int(row.get("generation", 0) or 0) == int(generation)]
    return sum(1 for row in rows if row.get("status") in {PENDING, RETRYABLE})


def _base_notification(
    *,
    task: dict[str, Any],
    execution: dict[str, Any],
    report: dict[str, Any],
    channel: str,
    at: str,
) -> dict[str, Any]:
    rows = store.rows(NOTIFICATION_MODULE)
    row_id = max((int(row.get("id", 0)) for row in rows), default=0) + 1
    task_id = int(task.get("id", 0))
    generation = int(execution.get("generation", 0))
    return {
        "id": row_id,
        "task_id": task_id,
        "task_no": str(task.get("任务编号") or ""),
        "report_id": int(report.get("id", 0)),
        "report_no": str(report.get("报告编号") or ""),
        "execution_id": int(execution.get("id", 0)),
        "generation": generation,
        "channel": channel,
        "status": PENDING,
        "attempts": 0,
        "attempt_history": [],
        "last_attempt_at": None,
        "sent_at": None,
        "cancelled_at": None,
        "error": None,
        "dedupe_skipped": 0,
        "idempotency_key": idempotency_key(task_id, generation, channel),
        "created_at": at,
        "updated_at": at,
    }


def ensure_queued(
    task: dict[str, Any],
    execution: dict[str, Any],
    report: dict[str, Any],
    channels: list[str] | None = None,
    *,
    at: str | None = None,
) -> list[dict[str, Any]]:
    """为一次执行排队通知；同一个「任务 + 代次 + 渠道」只保留一条有效记录。"""
    at = normalize_time(at)
    channels = channels or split_channels(task.get("通知渠道"))
    queued: list[dict[str, Any]] = []
    for channel in channels:
        key = idempotency_key(int(task.get("id", 0)), int(execution.get("generation", 0)), channel)
        existing = next(
            (row for row in store.rows(NOTIFICATION_MODULE) if row.get("idempotency_key") == key),
            None,
        )
        if existing is not None:
            existing["dedupe_skipped"] = int(existing.get("dedupe_skipped", 0)) + 1
            existing["updated_at"] = at
            queued.append(existing)
            continue
        row = _base_notification(task=task, execution=execution, report=report, channel=channel, at=at)
        store.rows(NOTIFICATION_MODULE).append(row)
        queued.append(row)
    return queued


def cancel_pending(task_id: int, reason: str, *, at: str | None = None) -> list[dict[str, Any]]:
    """任务停用后取消所有仍在排队（含待重试）的消息，已发送 / 已失败记录保持可查。"""
    at = normalize_time(at)
    cancelled: list[dict[str, Any]] = []
    for row in rows_for_task(task_id):
        if row.get("status") not in {PENDING, RETRYABLE}:
            continue
        row["status"] = CANCELLED
        row["cancelled_at"] = at
        row["cancelled_reason"] = reason
        row["error"] = reason
        row["updated_at"] = at
        cancelled.append(row)

    for execution in store.rows(EXECUTION_MODULE):
        if int(execution.get("task_id", 0)) != int(task_id):
            continue
        # 在最近一次执行上留下取消说明，报告详情能直接看到「为什么没发」；
        # 执行代次和报告终态字段本身保持不动。
        execution["notification_cancelled_at"] = at
        execution["notification_cancel_reason"] = reason
        execution["updated_at"] = at
    return cancelled


def dispatch(
    report: dict[str, Any],
    task: dict[str, Any] | None,
    *,
    at: str | None = None,
    channel_outcomes: dict[str, str] | None = None,
    generation: int | None = None,
) -> list[dict[str, Any]]:
    """报告进入终态后统一发送一次通知；重复调用会被幂等键挡住。"""
    at = normalize_time(at)
    channel_outcomes = channel_outcomes or {}
    report_status = str(report.get("status") or "")
    if not is_terminal_report(report_status):
        return []

    task = task or {}
    task_disabled = str(task.get("status") or "") == "已停用" or str(task.get("是否启用") or "") == "否"
    task_id = int(task.get("id", 0) or report.get("schedule_task_id", 0) or 0)
    if task_disabled:
        return cancel_pending(task_id, "任务已停用，取消待发送通知", at=at)

    dispatched: list[dict[str, Any]] = []
    for row in rows_for_report(int(report.get("id", 0))):
        if generation is not None and int(row.get("generation", 0) or 0) != int(generation):
            continue
        if row.get("status") not in {PENDING, RETRYABLE}:
            continue
        outcome = str(channel_outcomes.get(str(row.get("channel")), "success")).lower()
        row["attempts"] = int(row.get("attempts", 0)) + 1
        row["last_attempt_at"] = at
        row["updated_at"] = at
        history = list(row.get("attempt_history") or [])
        if outcome == "timeout":
            row["status"] = RETRYABLE
            row["error"] = "通知发送超时，等待重试"
            history.append({"attempt": row["attempts"], "result": "超时", "at": at})
        elif outcome in {"fail", "failure", "failed", "error"}:
            row["status"] = FAILED
            row["error"] = str(row.get("error") or "通知渠道返回失败")
            history.append({"attempt": row["attempts"], "result": "失败", "at": at})
        else:
            row["status"] = SENT
            row["sent_at"] = at
            row["error"] = None
            history.append({"attempt": row["attempts"], "result": "成功", "at": at})
        row["attempt_history"] = history
        dispatched.append(row)
    return dispatched


def retry(
    report: dict[str, Any],
    task: dict[str, Any] | None,
    *,
    channel: str | None = None,
    outcome: str = "success",
    at: str | None = None,
    generation: int | None = None,
) -> tuple[bool, str, list[dict[str, Any]]]:
    """重试通知。必须仍挂在同一份报告的终态上，不会新建执行代次或覆盖报告状态。

    可重试的范围是「已超时 / 失败但还没有有效结果」的记录，因此超时后重试成功只会有
    一次有效通知；已经发送成功的记录不会被重复发送，停用取消的记录也不会再重发。
    """
    at = normalize_time(at)
    if not is_terminal_report(report.get("status")):
        return False, "报告尚未进入终态，通知暂不发送", []
    task = task or {}
    if str(task.get("status") or "") == "已停用" or str(task.get("是否启用") or "") == "否":
        return False, "任务已停用，待发送通知已取消，不能重试", []

    candidates = [
        row for row in rows_for_report(int(report.get("id", 0)))
        if (generation is None or int(row.get("generation", 0) or 0) == int(generation))
        and (channel is None or str(row.get("channel")) == str(channel))
    ]
    if not candidates:
        return False, "没有找到对应的通知记录", []

    retryable = [row for row in candidates if row.get("status") in {RETRYABLE, FAILED}]
    already_valid = [row for row in candidates if row.get("status") == SENT]
    cancelled = [row for row in candidates if row.get("status") == CANCELLED]
    if not retryable:
        if already_valid:
            return False, "该渠道已有一次有效通知，本次重复请求已忽略", already_valid
        if cancelled:
            return False, "通知已随任务停用取消，不再重发", cancelled
        return False, "当前没有可重试的通知", candidates

    result_rows: list[dict[str, Any]] = []
    for row in retryable:
        row["attempts"] = int(row.get("attempts", 0)) + 1
        row["last_attempt_at"] = at
        row["updated_at"] = at
        history = list(row.get("attempt_history") or [])
        normalized = str(outcome).lower()
        if normalized == "timeout":
            row["status"] = RETRYABLE
            row["error"] = "通知发送再次超时，等待重试"
            history.append({"attempt": row["attempts"], "result": "超时", "at": at})
        elif normalized in {"fail", "failure", "failed", "error"}:
            row["status"] = FAILED
            row["error"] = str(row.get("error") or "通知渠道返回失败")
            history.append({"attempt": row["attempts"], "result": "失败", "at": at})
        else:
            row["status"] = SENT
            row["sent_at"] = at
            row["error"] = None
            history.append({"attempt": row["attempts"], "result": "成功", "at": at})
        row["attempt_history"] = history
        result_rows.append(row)
    return True, "通知已重试，报告终态保持不变", result_rows


def notification_view(row: dict[str, Any]) -> dict[str, Any]:
    """通知记录的统一返回结构，中英文键并存，页面和接口都读同一份。"""
    return {
        "id": row.get("id"),
        "渠道": row.get("channel"),
        "channel": row.get("channel"),
        "状态": row.get("status"),
        "status": row.get("status"),
        "尝试次数": row.get("attempts", 0),
        "attempts": row.get("attempts", 0),
        "最近尝试时间": row.get("last_attempt_at"),
        "last_attempt_at": row.get("last_attempt_at"),
        "发送时间": row.get("sent_at"),
        "sent_at": row.get("sent_at"),
        "取消时间": row.get("cancelled_at"),
        "cancelled_at": row.get("cancelled_at"),
        "失败原因": row.get("error"),
        "error": row.get("error"),
        "重试记录": row.get("attempt_history", []),
        "attempt_history": row.get("attempt_history", []),
        "重复请求已忽略": row.get("dedupe_skipped", 0),
        "dedupe_skipped": row.get("dedupe_skipped", 0),
        "通知幂等键": row.get("idempotency_key"),
        "idempotency_key": row.get("idempotency_key"),
        "报告编号": row.get("report_no"),
        "执行代次": row.get("generation"),
        "generation": row.get("generation"),
    }
