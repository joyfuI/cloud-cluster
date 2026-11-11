"""Memory-only task queue. Retrying a logical mutation requires a new plan."""

import logging
from collections.abc import Callable

# shortcut: Nuitka 4.3 misses Python 3.15's lazy re-export; restore it after compiler upgrade.
from concurrent.futures.thread import ThreadPoolExecutor
from dataclasses import dataclass, field
from threading import Event, RLock
from time import monotonic
from uuid import uuid4

from .diagnostics import error_summary, logger
from .domain import BackendError, ErrorKind, OperationResult

_TASK_ACTIONS = {
    "다운로드",
    "대소문자 이름 변경",
    "복사",
    "복제",
    "삭제",
    "새 폴더",
    "휴지통 비우기",
    "이동",
    "정밀 검증",
    "추천 계정으로 복제",
    "누락된 폴더 생성",
    "파일 업로드",
    "폴더 업로드",
    "폴더 검사",
}


@dataclass
class TaskContext:
    cancelled: Event = field(default_factory=Event)
    progress: Callable[[dict], None] = lambda value: None
    retries: int = 2
    detail: str = ""
    trace_id: str = "-"
    phase: str = "stage=task"
    failure: str = ""
    _phase_started: float | None = None

    def set_phase(self, phase):
        self.finish_phase()
        self.phase = phase
        if logger.isEnabledFor(logging.DEBUG):
            self._phase_started = monotonic()
            logger.debug("phase started trace=%s %s", self.trace_id, phase)

    def finish_phase(self):
        if self._phase_started is not None:
            logger.debug(
                "phase finished trace=%s %s elapsed_ms=%.0f",
                self.trace_id,
                self.phase,
                (monotonic() - self._phase_started) * 1000,
            )
            self._phase_started = None

    def record_failure(self, error):
        self.failure = f"{self.phase} error={error_summary(error)}"

    def check(self):
        if self.cancelled.is_set():
            raise BackendError(ErrorKind.CANCELLED)

    def wait(self, seconds):
        if self.cancelled.wait(seconds):
            self.check()

    def describe(self, text, size=None):
        self.detail = text
        self.progress({"detail": text, "bytes": 0, "totalBytes": size})


@dataclass
class TransferTask:
    id: str
    label: str
    context: TaskContext
    state: str = "대기"
    stats: dict = field(default_factory=dict)
    result: object = None
    error: str = ""
    queued_at: float = field(default_factory=monotonic)

    @property
    def action(self):
        action = self.label.partition(" · ")[0]
        return action if action in _TASK_ACTIONS else "other"


class TransferManager:
    def __init__(self, workers=2, retries=2, on_change=None):
        self._pools = {
            name: ThreadPoolExecutor(max_workers=count, thread_name_prefix=name)
            for name, count in (("transfer", workers), ("scan", 2), ("other", 2))
        }
        self.tasks = {}
        self._waiting = {}
        self._lock = RLock()
        self._accepting = True
        self.retries = retries
        self.on_change = on_change or (lambda task: None)

    @property
    def busy(self):
        with self._lock:
            return any(
                task.state in {"대기", "진행 중", "취소 요청"} for task in self.tasks.values()
            )

    def submit(self, label, operation, *, after=(), wait_for=(), queue="transfer"):
        with self._lock:
            if not self._accepting:
                raise RuntimeError("프로그램을 종료하는 중입니다.")
            pool = self._pools[queue]
            dependencies = tuple(self.tasks[task_id] for task_id in after)
            predecessors = dependencies + tuple(self.tasks[task_id] for task_id in wait_for)
            task_id = uuid4().hex
            task = TransferTask(task_id, label, TaskContext(retries=self.retries, trace_id=task_id))
            task.context.progress = lambda value: self._progress(task, value)
            self.tasks[task.id] = task
            logger.info(
                "task queued task=%s action=%s queue=%s dependencies=%s",
                task.id,
                task.action,
                queue,
                ",".join(item.id for item in predecessors) or "-",
            )
            self.on_change(task)
            if any(item.state in {"대기", "진행 중", "취소 요청"} for item in predecessors):
                self._waiting[task.id] = (pool, operation, dependencies, predecessors)
            else:
                try:
                    pool.submit(self._run, task, operation, dependencies)
                except Exception as error:
                    task.state, task.error = "실패", str(error)
                    logger.error(
                        "task submit failed task=%s action=%s error=%s",
                        task.id,
                        task.action,
                        error_summary(error),
                    )
                    self.on_change(task)
                    raise
            return task

    def _start_ready(self):
        for task_id, (pool, operation, dependencies, predecessors) in tuple(self._waiting.items()):
            if any(item.state in {"대기", "진행 중", "취소 요청"} for item in predecessors):
                continue
            del self._waiting[task_id]
            task = self.tasks[task_id]
            try:
                pool.submit(self._run, task, operation, dependencies)
            except Exception as error:
                task.state, task.error = "실패", str(error)
                logger.error(
                    "task submit failed task=%s action=%s error=%s",
                    task.id,
                    task.action,
                    error_summary(error),
                )
                self.on_change(task)

    def _progress(self, task, value):
        with self._lock:
            task.stats = dict(value)
            self.on_change(task)

    def _run(self, task, operation, dependencies=()):
        started = monotonic()
        try:
            with self._lock:
                task.context.check()
                if unmet := [item.id for item in dependencies if item.state != "완료"]:
                    logger.warning(
                        "task dependency failed task=%s dependencies=%s", task.id, ",".join(unmet)
                    )
                    raise ValueError("선행 작업이 완료되지 않아 실행하지 않았습니다.")
                task.state = "진행 중"
            logger.info(
                "task started task=%s queue_ms=%.0f", task.id, (started - task.queued_at) * 1000
            )
            self.on_change(task)
            task.result = operation(task.context)
            task.state = getattr(task.result, "outcome", "완료")
        except Exception as error:
            task.context.record_failure(error)
            task.error = str(error)
            task.state = (
                "취소됨"
                if isinstance(error, BackendError) and error.kind == ErrorKind.CANCELLED
                else "실패"
            )
        finally:
            task.context.finish_phase()
            steps = task.result.steps if isinstance(task.result, OperationResult) else ()
            log = logger.info if task.state in {"완료", "취소됨"} else logger.error
            log(
                "task finished task=%s state=%s action=%s elapsed_ms=%.0f "
                "steps_ok=%d steps_failed=%d steps_total=%d %s",
                task.id,
                task.state,
                task.action,
                (monotonic() - started) * 1000,
                sum(step.state == "ok" for step in steps),
                sum(step.state in {"error", "rejected"} for step in steps),
                len(steps),
                task.context.failure or "error=-",
            )
            self.on_change(task)
            with self._lock:
                self._start_ready()

    def cancel(self, task_id):
        with self._lock:
            task = self.tasks[task_id]
            if task.state in {"대기", "진행 중", "취소 요청"}:
                if task.state != "취소 요청":
                    logger.info("task cancel requested task=%s state=%s", task.id, task.state)
                task.state = "취소 요청"
                task.context.cancelled.set()
                if task_id in self._waiting:
                    del self._waiting[task_id]
                    task.state = "취소됨"
                    logger.info("task cancelled before start task=%s", task.id)
                self.on_change(task)
                self._start_ready()

    def cancel_all(self):
        with self._lock:
            self._accepting = False
            for task_id in self.tasks:
                self.cancel(task_id)

    def remove_finished(self, task_ids):
        with self._lock:
            removed = set()
            for task_id in task_ids:
                task = self.tasks.get(task_id)
                if task is not None and task.state not in {"대기", "진행 중", "취소 요청"}:
                    del self.tasks[task_id]
                    removed.add(task_id)
            return removed

    def resize_idle_transfer_pool(self, workers):
        with self._lock:
            if self.busy or not self._accepting:
                raise RuntimeError("작업이 끝난 뒤 동시 전송 수를 변경할 수 있습니다.")
            old_pool = self._pools["transfer"]
            self._pools["transfer"] = ThreadPoolExecutor(
                max_workers=workers, thread_name_prefix="transfer"
            )
        # A finished worker can still need the lock to leave _run.
        old_pool.shutdown(wait=True)

    def close(self):
        self.cancel_all()
        for pool in self._pools.values():
            pool.shutdown(wait=True, cancel_futures=False)
