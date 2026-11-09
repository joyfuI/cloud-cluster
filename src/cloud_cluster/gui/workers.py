from time import monotonic
from uuid import uuid4

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot

from ..diagnostics import error_summary, logger
from ..transfers import TaskContext


class WorkerSignals(QObject):
    done = Signal(object)
    failed = Signal(str)
    progress = Signal(object)
    finished = Signal()


class Worker(QRunnable):
    def __init__(self, function, *, blocks_configuration):
        super().__init__()
        self.signals = WorkerSignals()
        self.context = TaskContext(progress=self.signals.progress.emit, trace_id=uuid4().hex)
        self.function = function
        self.blocks_configuration = blocks_configuration
        self.action = getattr(function, "__qualname__", type(function).__qualname__)

    @Slot()
    def run(self):
        started = monotonic()
        trace = self.context.trace_id
        logger.debug("worker started trace=%s action=%s", trace, self.action)
        try:
            self.signals.done.emit(self.function(self.context))
        except Exception as error:
            log = logger.info if self.context.cancelled.is_set() else logger.error
            log(
                "worker failed trace=%s action=%s elapsed_ms=%.0f %s error=%s",
                trace,
                self.action,
                (monotonic() - started) * 1000,
                self.context.phase,
                error_summary(error),
            )
            self.signals.failed.emit(str(error))
        else:
            logger.debug(
                "worker finished trace=%s action=%s elapsed_ms=%.0f",
                trace,
                self.action,
                (monotonic() - started) * 1000,
            )
        finally:
            self.context.finish_phase()
            self.signals.finished.emit()


class WorkerPool(QObject):
    busy_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(4)
        self.pending = set()

    @property
    def configuration_pending(self):
        return any(worker.blocks_configuration for worker in self.pending)

    def submit(self, function, done=None, failed=None, progress=None, *, blocks_configuration=True):
        worker = Worker(function, blocks_configuration=blocks_configuration)
        self.pending.add(worker)
        self.busy_changed.emit()
        if done:
            worker.signals.done.connect(done)
        if failed:
            worker.signals.failed.connect(failed)
        if progress:
            worker.signals.progress.connect(progress)

        def finished():
            self.pending.discard(worker)
            self.busy_changed.emit()

        worker.signals.finished.connect(finished)
        try:
            logger.debug("worker queued trace=%s action=%s", worker.context.trace_id, worker.action)
            self.pool.start(worker)
        except Exception as error:
            logger.error(
                "worker submit failed trace=%s action=%s error=%s",
                worker.context.trace_id,
                worker.action,
                error_summary(error),
            )
            finished()
            raise
        return worker.context

    def cancel_all(self):
        for worker in self.pending:
            worker.context.cancelled.set()
