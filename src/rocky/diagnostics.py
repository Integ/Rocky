"""Owner-facing status derived from projections and live admission leases."""

import time
from datetime import UTC, datetime

from rocky.model_queue import ModelQueue
from rocky.monitoring import _worker
from rocky.store import TERMINAL_STATUSES

_TOOLS = {
    "run_command": "正在执行命令",
    "read_file": "正在读取文件",
    "list_files": "正在查看目录",
    "write_file": "正在写入文件",
    "append_file": "正在追加内容",
    "edit_file": "正在修改文件",
    "fetch_url": "正在读取网页",
    "http_request": "正在发送网络请求",
    "browse": "正在操作浏览器",
}


def goal_diagnostics(goal, queue: ModelQueue, settings):
    state, now = goal.state, datetime.now(UTC)
    step = state.execution_step
    since = step.started_at if step and step.status == "started" else state.updated_at or None
    stage, title, detail = state.status, "正在工作", "正在推进任务。"
    request = queue.latest(goal.goal_id)
    retry_at, attempt, priority, queue_seconds = None, None, None, None
    if state.status == "queued":
        title, detail = "等待执行", "目标已保存，等待执行进程接收。"
    elif state.status == "blocked":
        title, detail = (
            ("等待操作批准", "批准具体操作后继续；等待期间暂停任务计时。")
            if state.pending_approval
            else ("等待你的回复", "补充必要资料后继续；等待期间暂停任务计时。")
        )
    elif state.status == "waiting":
        title, detail = "定时等待", "等待约定时间或外部输入。"
        retry_at = state.wake_at
    elif state.status in TERMINAL_STATUSES:
        title = {
            "succeeded": "任务已完成",
            "cancelled": "任务已停止",
            "failed": "执行需要恢复",
            "budget_exhausted": "已达到执行预算",
            "deadline_exceeded": "已达到任务时限",
        }[state.status]
        detail, since = "可在执行记录中查看最后结果和操作。", state.updated_at or None
    elif state.phase == "act" and step and step.status == "started":
        stage = "tool"
        decision = step.input.get("decision") or {}
        actions = decision.get("actions") or [decision.get("action") or {}]
        title = (
            _TOOLS.get(actions[0].get("tool"), "正在执行操作")
            if len(actions) == 1
            else "正在并行读取资料"
        )
        detail = "操作结束后会记录实际结果。"
    else:
        reviewing = state.phase == "complete"
        stage, title = "model_dispatch", "等待分配验收请求" if reviewing else "等待分配规划请求"
        detail = "等待模型工作进程接收；你的追加消息使用独立接收队列。"
        expected = "reviewer" if reviewing else "planner"
        if (
            request
            and request["purpose"] == expected
            and (not step or request["created_at"] >= step.started_at)
        ):
            priority, attempt = request["priority"], request["attempt"]
            if request["expired"]:
                stage, title, detail = (
                    "recovering",
                    "等待执行进程恢复",
                    "上一次模型请求的运行记录已过期，等待工作流重试或恢复。",
                )
            elif request["state"] in {"waiting", "running", "backoff"}:
                since = request["created_at"]
                if request["state"] == "waiting":
                    stage, title = "model_queued", "等待模型名额"
                    detail = "人工追加消息优先；普通请求按提交顺序等待，并保留人工处理名额。"
                elif request["state"] == "running":
                    stage, title, since = (
                        "model",
                        "模型正在独立验收" if reviewing else "模型正在规划",
                        request["started_at"],
                    )
                    detail = "已获得调用名额，正在请求模型。"
                    queue_seconds = max(
                        0,
                        (
                            datetime.fromisoformat(request["started_at"])
                            - datetime.fromisoformat(request["created_at"])
                        ).total_seconds(),
                    )
                else:
                    stage, title, since = "retry", "模型请求等待重试", request["updated_at"]
                    detail, retry_at = (
                        "服务暂时不可用或触发限流，执行引擎将按重试策略处理。",
                        request["retry_at"],
                    )
    worker = _worker(settings, time.time())
    if state.status == "running" and worker["state"] in {"down", "stale"}:
        stage, title, detail = (
            "recovering",
            "执行进程暂时不可用",
            "任务进度已保存，等待后台服务恢复连接。",
        )
    limits = goal.contract.limits

    def remaining(limit, used):
        return None if limit is None else max(0, limit - used)

    paused = state.status == "blocked"
    wall = None
    if state.deadline_at and not paused and state.status not in TERMINAL_STATUSES:
        wall = max(0, (datetime.fromisoformat(state.deadline_at) - now).total_seconds())
    return {
        "goal_id": goal.goal_id,
        "revision": state.revision,
        "observed_at": now.isoformat(),
        "status": state.status,
        "stage": stage,
        "title": title,
        "detail": detail,
        "since": since,
        "retry_at": retry_at,
        "attempt": attempt,
        "priority": priority,
        "queue_wait_seconds": queue_seconds,
        "model_queue": queue.summary(),
        "budget": {
            "steps_remaining": remaining(limits.max_steps, state.steps),
            "model_calls_remaining": remaining(limits.max_model_calls, state.model_calls),
            "tokens_remaining": remaining(
                limits.max_tokens, state.input_tokens + state.output_tokens
            ),
            "wall_seconds_remaining": wall,
            "timer_paused": paused,
        },
    }
