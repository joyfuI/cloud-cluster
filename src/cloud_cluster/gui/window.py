import json
from dataclasses import dataclass, field, replace
from functools import partial
from pathlib import Path, PurePosixPath
from time import monotonic

from PySide6.QtCore import (
    QEventLoop,
    QItemSelectionModel,
    QMimeData,
    QPersistentModelIndex,
    Qt,
    QTimer,
    QUrl,
    Signal,
)
from PySide6.QtGui import QAction, QDesktopServices, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QDialog,
    QDockWidget,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QStyle,
    QTableWidget,
    QTableWidgetItem,
    QToolBar,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..diagnostics import diagnostic_ref, error_summary, logger
from ..domain import (
    BackendError,
    Endpoint,
    ErrorKind,
    OperationResult,
    QuotaSnapshot,
    Status,
    StepResult,
)
from ..operations import CopyIntent, ExpectedItem, MutationPlan, OperationEngine, local_endpoint
from ..replicas import choose_accounts, is_excluded, path_key, path_keys_overlap, validate_path
from ..transfers import TransferManager
from ..trash import GoogleTrashAdapter
from ..trash import capabilities as trash_capabilities
from ..verification import VerificationStore
from ..vfs import VirtualFileSystem
from .dialogs import (
    AccountDialog,
    IssueDialog,
    LicensesDialog,
    ReplicaDialog,
    SettingsDialog,
    confirm,
    confirm_upload_overwrite,
    message,
    show_text,
)
from .file_table import INTERNAL_MIME, FileTableView
from .folder_tree import FolderTreeView
from .models import EntryProxy, FileTableModel, FolderTreeModel, size_text
from .workers import WorkerPool

COMPLETED_TASK_STATES = {"완료"}
FAILED_TASK_STATES = {"실패", "실패 / 결과 확인 필요", "일부 성공"}


@dataclass
class FolderScan:
    path: str
    pending: set = field(default_factory=set)
    seen: set = field(default_factory=set)
    issues: dict = field(default_factory=dict)
    checked: int = 0
    complete: bool = True
    has_results: bool = False
    interrupted: bool = False


class MainWindow(QMainWindow):
    task_changed = Signal(object)
    partial_listing = Signal(int, object)

    def __init__(self, backend, settings, store, account_manager=None, startup_error=""):
        super().__init__()
        self.backend, self.settings, self.store = backend, settings, store
        self.account_manager = account_manager
        self.workers = WorkerPool(self)
        self.vfs = (
            VirtualFileSystem(
                backend,
                settings.accounts,
                ttl=settings.cache_ttl,
                verification_store=VerificationStore(store.directory / "content_hashes.db"),
            )
            if backend
            else None
        )
        self.engine = (
            OperationEngine(
                backend, settings.accounts, quotas=account_manager.quotas if account_manager else None
            )
            if backend
            else None
        )
        self.transfers = TransferManager(
            settings.transfers, settings.retries, self.task_changed.emit
        )
        self.path, self.scope, self.mode = "", "", "browse"
        self.history, self.history_index = [], -1
        self.generation, self.sort_generation = 0, 0
        self.snapshot, self.scan_results = None, ()
        self.scan_path = None
        self.scan_complete = True
        self.issue_dialog = None
        self._issue_blockers = ()
        self._repair_busy, self._repair_total = False, 0
        self._repair_tasks = set()
        self._repair_progress = ""
        self._preparing, self._replica_busy, self._scan_busy = False, False, False
        self._scan_run = None
        self._upload_context = None
        self._explorer_drag = False
        self._replica_task_id = None
        self._delete_targets = {}
        self._verification_targets = ()
        self._verification_reads = {}
        self._pending_plans = {}
        self._pending_repairs = {}
        self._trash_loading, self._restore_pending, self._restore_tasks = set(), set(), {}
        self._refreshing = None
        self._task_rechecks = []
        self._task_browse_request = None
        self._task_refresh_running = False
        self._releasing_task_rechecks = False
        self._task_refresh_timer = QTimer(self)
        self._task_refresh_timer.setSingleShot(True)
        self._task_refresh_timer.timeout.connect(self.flush_task_refreshes)
        self.replica_dialog = None
        self._account_dialog = None
        self._settings_dialog = None
        self._reported_tasks, self._task_rows, self._task_retries = set(), {}, {}
        self._finished_tasks = set()
        self._active_task_rows = {}
        self._task_scroll_anchor, self._task_scroll_pending = None, False
        self._capacity_accounts, self._capacity_quotas = (), None
        self._capacity_loading, self._capacity_dirty = False, False
        self._capacity_force = False
        self._closing, self._shutdown_started, self._shutdown_done = False, False, False
        self.setWindowTitle("Cloud Cluster")
        self.resize(1360, 840)
        self.setMinimumSize(900, 560)
        self._build_ui()
        self.accounts_action.setEnabled(account_manager is not None)
        self.task_changed.connect(self.update_task)
        self.partial_listing.connect(self.apply_partial)
        self.workers.busy_changed.connect(self.update_configuration_buttons)
        self.update_command_buttons()
        if startup_error:
            self.banner.setText(
                startup_error + "\n연결 작업을 사용할 수 없습니다. DLL 빌드와 앱 설정을 확인하세요."
            )
        elif not settings.accounts:
            self.banner.setText(
                "클라우드 계정을 연결하세요. 서로 다른 두 계정부터 이중화 업로드를 사용할 수 있습니다."
            )
        else:
            QTimer.singleShot(0, lambda: self.navigate(""))
        self.refresh_capacity()

    def _action(self, label, slot, shortcut=None, icon=None):
        action = QAction(label, self)
        if icon is not None:
            action.setIcon(self.style().standardIcon(icon))
        action.triggered.connect(slot)
        if shortcut:
            action.setShortcut(QKeySequence(shortcut))
            self.addAction(action)
        return action

    def _build_ui(self):
        toolbar = QToolBar("탐색")
        toolbar.setMovable(False)
        self.addToolBar(toolbar)
        sp = QStyle.StandardPixmap
        self.back_action = self._action(
            "뒤로", lambda: self.history_step(-1), "Alt+Left", sp.SP_ArrowBack
        )
        self.forward_action = self._action(
            "앞으로", lambda: self.history_step(1), "Alt+Right", sp.SP_ArrowForward
        )
        self.up_action = self._action("상위 폴더", self.up, "Alt+Up", sp.SP_ArrowUp)
        self.refresh_action = self._action(
            "새로고침",
            lambda: (self.refresh(force=True), self.refresh_capacity()),
            "F5",
            sp.SP_BrowserReload,
        )
        for action in (self.back_action, self.forward_action, self.up_action, self.refresh_action):
            toolbar.addAction(action)
        self.address = QLineEdit("/")
        self.address.setPlaceholderText("통합 클라우드 경로")
        self.address.setAccessibleName("현재 경로")
        self.address.returnPressed.connect(
            lambda: self.navigate(self.address.text().strip("/").replace("\\", "/"))
        )
        toolbar.addWidget(self.address)
        self.search = QLineEdit()
        self.search.setMaximumWidth(260)
        self.search.setPlaceholderText("현재 폴더에서 이름 검색")
        toolbar.addWidget(self.search)
        self._action(
            "주소 표시줄", lambda: (self.address.setFocus(), self.address.selectAll()), "Ctrl+L"
        )
        commands = QToolBar("파일 작업")
        commands.setMovable(False)
        self.addToolBarBreak()
        self.addToolBar(commands)
        self.upload_action = self._action("파일 업로드...", self.upload, icon=sp.SP_ArrowUp)
        self.folder_upload_action = self._action("폴더 업로드...", self.upload_folder)
        self.download_action = self._action("다운로드...", self.download, icon=sp.SP_ArrowDown)
        self.mkdir_action = self._action(
            "새 폴더...", self.mkdir, "Ctrl+Shift+N", sp.SP_FileDialogNewFolder
        )
        self.rename_action = self._action("이름 변경...", self.rename, "F2")
        self.delete_action = self._action("삭제...", self.delete, "Delete", sp.SP_TrashIcon)
        self.copy_action = self._action("복사", lambda: self.copy_paths(False), "Ctrl+C")
        self.cut_action = self._action("잘라내기", lambda: self.copy_paths(True), "Ctrl+X")
        self.paste_action = self._action("붙여넣기", self.paste, "Ctrl+V")
        self.properties_action = self._action(
            "복제본 관리", self.properties, "Alt+Return", sp.SP_FileDialogDetailedView
        )
        for action in (
            self.upload_action,
            self.download_action,
            self.mkdir_action,
            self.copy_action,
            self.cut_action,
            self.paste_action,
            self.rename_action,
            self.delete_action,
            self.properties_action,
        ):
            commands.addAction(action)
        commands.addSeparator()
        self.status_filter = QComboBox()
        self.status_filter.addItem("모든 상태", "")
        for status in Status:
            self.status_filter.addItem(status.value, status.value)
        commands.addWidget(self.status_filter)
        commands.addSeparator()
        self.accounts_action = self._action("계정 관리", self.accounts_dialog)
        self.scan_action = self._action("현재 폴더 검사...", self.scan)
        self.resolve_action = self._action("문제 해결", self.show_issues)
        self.resolve_action.setEnabled(False)
        self.settings_action = self._action("설정", self.settings_dialog)
        for action in (self.accounts_action, self.scan_action, self.settings_action):
            commands.addAction(action)
        file_menu = self.menuBar().addMenu("파일")
        for action in (
            self.upload_action,
            self.folder_upload_action,
            self.download_action,
            self.mkdir_action,
            self.rename_action,
            self.delete_action,
        ):
            file_menu.addAction(action)
        self.menuBar().addMenu("관리").addActions(
            [self.accounts_action, self.scan_action, self.resolve_action, self.settings_action]
        )
        help_menu = self.menuBar().addMenu("도움말")
        help_menu.addAction(self._action("오픈소스 라이선스", self.show_licenses))
        splitter = QSplitter()
        self.tree = FolderTreeView()
        self.tree.setHeaderHidden(True)
        self.tree.setMinimumWidth(190)
        self.tree.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.tree_model = FolderTreeModel(self.settings.accounts, self)
        self.tree.setModel(self.tree_model)
        self.tree_model.load_requested.connect(self.load_tree)
        self.tree.clicked.connect(self.tree_clicked)
        self.tree.files_dropped.connect(self.dropped)
        splitter.addWidget(self.tree)
        center = QWidget()
        center_layout = QVBoxLayout(center)
        center_layout.setContentsMargins(8, 4, 8, 4)
        self.banner = QLabel("")
        self.banner.setWordWrap(True)
        self.banner.setTextFormat(Qt.TextFormat.PlainText)
        center_layout.addWidget(self.banner)
        self.file_model = FileTableModel(self.settings.accounts, self)
        self.proxy = EntryProxy(self)
        self.proxy.setSourceModel(self.file_model)
        self.proxy.sort_requested.connect(self.sort_entries)
        self.selection = QItemSelectionModel(self.proxy, self)
        self.selection.selectionChanged.connect(self.update_details)
        self.selection.selectionChanged.connect(self.update_command_buttons)
        self.table = FileTableView(self.start_download_drag)
        self.table.setWordWrap(False)
        self.table.setModel(self.proxy)
        self.table.setSelectionModel(self.selection)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setDragEnabled(True)
        self.table.setAcceptDrops(True)
        self.table.setDropIndicatorShown(True)
        self.table.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(
            lambda point: self.context_menu(self.table, point)
        )
        self.table.doubleClicked.connect(self.activate_index)
        self.table.files_dropped.connect(self.dropped)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.verticalHeader().hide()
        self.table.setShowGrid(False)
        self.table.horizontalHeader().setSortIndicator(0, Qt.SortOrder.AscendingOrder)
        self.table.setSortingEnabled(True)
        for index, width in enumerate((230, 85, 140, 75, 145, 150)):
            self.table.setColumnWidth(index, width)
        self.table.horizontalHeader().setStretchLastSection(True)
        center_layout.addWidget(self.table)
        splitter.addWidget(center)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setPlaceholderText("항목을 선택하면 복제본과 검증 상태가 표시됩니다.")
        self.details.setMinimumWidth(200)
        splitter.addWidget(self.details)
        splitter.setSizes([220, 900, 240])
        self.setCentralWidget(splitter)
        self.search.textChanged.connect(lambda value: self.proxy.update_filter(query=value))
        self.status_filter.currentIndexChanged.connect(
            lambda: self.proxy.update_filter(status=self.status_filter.currentData())
        )
        dock = QDockWidget("작업", self)
        dock.setAllowedAreas(Qt.DockWidgetArea.BottomDockWidgetArea)
        panel = QWidget()
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(4, 4, 4, 4)
        self.task_tree = QTreeWidget()
        self.task_tree.setHeaderLabels(["작업", "상태", "진행 내용", "속도"])
        self.task_tree.setRootIsDecorated(True)
        self.task_tree.setColumnWidth(0, 270)
        self.task_tree.setColumnWidth(1, 130)
        self.task_tree.setColumnWidth(2, 700)
        self.task_tree.itemChanged.connect(self.task_state_changed)
        self.task_tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.task_tree.customContextMenuRequested.connect(self.task_context_menu)
        task_menu = QAction("작업 메뉴 열기", self.task_tree)
        task_menu.setShortcut(QKeySequence("Shift+F10"))
        task_menu.setShortcutContext(Qt.ShortcutContext.WidgetShortcut)
        task_menu.triggered.connect(lambda: self.task_context_menu())
        self.task_tree.addAction(task_menu)
        panel_layout.addWidget(self.task_tree)
        task_buttons = QHBoxLayout()
        self.clear_completed_button = QPushButton("완료한 작업 지우기")
        self.clear_failed_button = QPushButton("실패한 작업 지우기")
        self.clear_completed_button.clicked.connect(
            lambda: self.remove_finished_tasks(self.finished_tasks_with_states(COMPLETED_TASK_STATES))
        )
        self.clear_failed_button.clicked.connect(
            lambda: self.remove_finished_tasks(self.finished_tasks_with_states(FAILED_TASK_STATES))
        )
        task_buttons.addWidget(self.clear_completed_button)
        task_buttons.addWidget(self.clear_failed_button)
        task_buttons.addStretch()
        panel_layout.addLayout(task_buttons)
        dock.setWidget(panel)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, dock)
        dock.setMinimumHeight(160)
        self.upload_progress = QLabel()
        self.upload_progress.setTextFormat(Qt.TextFormat.PlainText)
        self.upload_progress.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.cancel_upload_button = QPushButton("준비 취소")
        self.cancel_upload_button.clicked.connect(self.cancel_upload_preparation)
        self.statusBar().addPermanentWidget(self.upload_progress, 1)
        self.statusBar().addPermanentWidget(self.cancel_upload_button)
        self.upload_progress.hide()
        self.cancel_upload_button.hide()
        self.capacity_label = QLabel()
        self.capacity_label.setTextFormat(Qt.TextFormat.PlainText)
        self.statusBar().addPermanentWidget(self.capacity_label)
        self.statusBar().showMessage("준비")

    def update_capacity_label(self):
        if self._capacity_quotas is None:
            text = "통합 용량: 조회 중…"
        else:
            if self.engine:
                self._capacity_quotas = self.engine.quotas.snapshot(self._capacity_accounts)
                quota = self.engine.capacity(self._capacity_quotas)
            elif len(self._capacity_accounts) < 2:
                quota = QuotaSnapshot(0, 0)
            else:
                quota = QuotaSnapshot()
            free = size_text(quota.free) if quota.free is not None else "확인 불가"
            total = size_text(quota.total) if quota.total is not None else "확인 불가"
            state = "추정치 갱신 중" if self._capacity_loading else "추정"
            text = f"통합 용량({state}): 남은 {free} / 전체 {total}"
        self.capacity_label.setText(text)

    def refresh_capacity(self, *, force=True):
        if self._closing:
            return
        accounts = tuple(
            account
            for account in self.settings.accounts
            if account.enabled and not account.readonly and not is_excluded(account, "")
        )
        if accounts != self._capacity_accounts:
            self._capacity_accounts, self._capacity_quotas = accounts, None
        if self.engine is None or len(accounts) < 2:
            self._capacity_quotas = {}
            self.update_capacity_label()
            return
        if self._capacity_loading:
            self._capacity_dirty = True
            self._capacity_force |= force
            self.update_capacity_label()
            return
        self._capacity_loading, self._capacity_dirty = True, False
        self._capacity_force = False
        self.update_capacity_label()

        def read(context):
            quotas = {}
            for account in accounts:
                context.check()
                try:
                    quota = self.engine.quotas.get(account, context, force=force) or QuotaSnapshot()
                except BackendError as error:
                    if error.kind == ErrorKind.CANCELLED:
                        raise
                    quota = QuotaSnapshot()
                quotas[account.id] = quota
            context.check()
            return quotas

        def done(result):
            self._capacity_loading = False
            if self._closing:
                return
            if accounts == self._capacity_accounts:
                self._capacity_quotas = result
            else:
                self._capacity_dirty = True
            self.update_capacity_label()
            if self._capacity_dirty:
                self._capacity_dirty = False
                self.refresh_capacity(force=self._capacity_force)

        def failed(_error):
            done({account.id: QuotaSnapshot() for account in accounts})

        try:
            self.workers.submit(read, done, failed)
        except Exception as error:
            failed(error)

    def available(self):
        if self.backend is None or self._closing:
            message(self, "백엔드를 사용할 수 없습니다. DLL 빌드와 설정 상태를 확인하세요.")
            return False
        return True

    def busy_endpoints(self):
        return (
            *(endpoint for plan in self._pending_plans.values() for endpoint in plan.endpoints),
            *self.entry_endpoints(self._pending_repairs.values()),
            *(endpoint for targets in self._delete_targets.values() for endpoint in targets),
            *self._verification_targets,
        )

    def entry_endpoints(self, entries):
        return tuple(
            Endpoint(account.fs, entry.path, account.id)
            for entry in entries
            for account in self.settings.accounts
            if account.enabled and not is_excluded(account, entry.path)
        )

    def endpoints_busy(self, endpoints):
        requested = {(e.fs.casefold(), path_key(e.path)) for e in endpoints}
        active = {(e.fs.casefold(), path_key(e.path)) for e in self.busy_endpoints()}
        return any(
            fs == other_fs and path_keys_overlap(path, other)
            for fs, path in requested
            for other_fs, other in active
        )

    def busy_path_keys(self):
        ids = {account.id for account in self.settings.accounts}
        return {path_key(e.path) for e in self.busy_endpoints() if e.account_id in ids}

    @staticmethod
    def paths_overlap(selected, busy_paths):
        return any(
            path_keys_overlap(path, other)
            for path in map(path_key, selected)
            for other in busy_paths
        )

    def update_command_buttons(self, *args):
        if self._releasing_task_rechecks:
            return
        self.update_configuration_buttons()
        busy_paths = self.busy_path_keys()
        selected_busy = self.paths_overlap((entry.path for entry in self.selected()), busy_paths)
        for action in (
            self.upload_action,
            self.folder_upload_action,
            self.mkdir_action,
            self.paste_action,
        ):
            action.setEnabled(not self._preparing and not self._closing)
        for action in (self.download_action, self.delete_action, self.rename_action):
            action.setEnabled(not self._preparing and not selected_busy and not self._closing)
        self.scan_action.setEnabled(not self._scan_busy and not self._closing)
        if self.issue_dialog is not None:
            blockers = tuple(
                reason
                for reason, active in (
                    ("repair_batch", self._repair_busy),
                    ("preparing", self._preparing),
                    ("verification", self._verification_targets),
                    ("closing", self._closing),
                )
                if active
            )
            selected = None
            if not blockers:
                selected = self.issue_dialog.selected_entries()
                if self.paths_overlap((entry.path for entry in selected), busy_paths):
                    blockers = ("path_overlap",)
            if blockers != self._issue_blockers:
                self._issue_blockers = blockers
                if selected is None:
                    selected = self.issue_dialog.selected_entries()
                logger.info(
                    "issue action blockers=%s scan_path=%s selected=%d selected_path=%s",
                    ",".join(blockers) or "-",
                    diagnostic_ref(path_key(self.scan_path or "")),
                    len(selected),
                    diagnostic_ref(selected[0].key) if len(selected) == 1 else "-",
                )
            self.issue_dialog.set_repair_busy(bool(blockers))
            self.issue_dialog.set_scan_busy(self._scan_busy)
            self.issue_dialog.set_progress(self._repair_progress)
        if self.replica_dialog is not None:
            self.replica_dialog.set_busy(
                self._replica_busy
                or self._preparing
                or self._closing
                or self.paths_overlap((self.replica_dialog.entry.path,), busy_paths),
                verification_busy=bool(self._verification_targets),
            )
        can_remove_tasks = self.task_removal_available()
        self.clear_completed_button.setEnabled(
            can_remove_tasks and any(self.finished_tasks_with_states(COMPLETED_TASK_STATES))
        )
        self.clear_failed_button.setEnabled(
            can_remove_tasks and any(self.finished_tasks_with_states(FAILED_TASK_STATES))
        )

    def set_replica_busy(self, busy):
        self._replica_busy = busy
        if not busy:
            self._replica_task_id = None
        self.update_command_buttons()

    def navigate(self, path, *, scope=None, history=True):
        try:
            validate_path(path, allow_root=True, names=False)
        except ValueError as error:
            message(self, error)
            return
        if scope is not None:
            self.scope = scope
        self.path, self.mode = path, "browse"
        self.address.setText("/" + path)
        self.proxy.update_filter(scope=self.scope, mode="browse")
        if history:
            del self.history[self.history_index + 1 :]
            self.history.append((path, self.scope))
            self.history_index = len(self.history) - 1
        self.back_action.setEnabled(self.history_index > 0)
        self.forward_action.setEnabled(self.history_index + 1 < len(self.history))
        previous = self.snapshot
        self.sync_tree(previous if previous and path_key(previous.path) != path_key(path) else None)
        self.refresh(force=False)

    def history_step(self, delta):
        index = self.history_index + delta
        if 0 <= index < len(self.history):
            self.history_index = index
            path, scope = self.history[index]
            self.navigate(path, scope=scope, history=False)

    def up(self):
        parent = str(PurePosixPath(self.path).parent)
        self.navigate("" if parent == "." else parent)

    def refresh(self, force=True):
        if self.vfs is None or self._closing:
            return
        if self._refreshing == (self.generation, self.path):
            return
        self.generation += 1
        generation, path = self.generation, self.path
        cached = self.vfs.cached(path)
        if cached and not cached.stale and not force:
            self._refreshing = None
            self.refresh_action.setEnabled(True)
            self.show_snapshot(cached)
            self.sync_tree(cached)
            self.sort_entries(
                self.table.horizontalHeader().sortIndicatorSection(),
                self.table.horizontalHeader().sortIndicatorOrder(),
            )
            return
        self.begin_refresh(generation, path, cached)

        def failed(error):
            if self._refreshing == (generation, path):
                self._refreshing = None
                self.refresh_action.setEnabled(True)
            if generation == self.generation and not self._closing:
                self.banner.setText(str(error))

        try:
            self.workers.submit(
                lambda context: self.vfs.refresh(
                    path, context, lambda snapshot: self.partial_listing.emit(generation, snapshot)
                ),
                lambda snapshot: self.finish_refresh(generation, snapshot),
                failed,
            )
        except Exception as error:
            failed(error)

    def begin_refresh(self, generation, path, cached):
        if cached:
            self.show_snapshot(replace(cached, stale=True))
        elif self.mode == "browse":
            self.snapshot = None
            self.sort_generation += 1
            self.file_model.set_entries(())
            self.details.clear()
            self.statusBar().showMessage(f"/{path} · 목록 조회 중…")
        self.banner.setText(
            "목록 갱신 중… 이전 목록을 표시합니다." if cached else "클라우드 목록 조회 중…"
        )
        self._refreshing = (generation, path)
        self.refresh_action.setEnabled(False)

    def apply_partial(self, generation, snapshot):
        if generation == self.generation and not self._closing and self.mode == "browse":
            self.show_snapshot(snapshot, partial=True)

    def finish_refresh(self, generation, snapshot):
        if self._refreshing == (generation, snapshot.path):
            self._refreshing = None
            self.refresh_action.setEnabled(True)
        if self._closing or self.mode != "browse":
            return
        if generation != self.generation:
            current, parent = path_key(self.path), path_key(snapshot.path)
            if current != parent and (not parent or current.startswith(parent + "/")):
                selected = self.tree_model.node(self.tree.currentIndex())
                if path_key(selected.path) != current or selected.scope != self.scope:
                    self.sync_tree(snapshot)
            return
        self.show_snapshot(snapshot)
        self.sort_entries(
            self.table.horizontalHeader().sortIndicatorSection(),
            self.table.horizontalHeader().sortIndicatorOrder(),
        )
        self.sync_tree(snapshot)

    def sync_tree(self, snapshot=None):
        if self.vfs is None or self._closing or self.mode != "browse":
            return
        roots = self.tree_model.root.children
        node = (
            next((n for n in roots[1].children if n.scope == self.scope), None)
            if self.scope
            else roots[0]
        )
        if node is None:
            return

        def find_child(parent, key):
            return next((child for child in parent.children if path_key(child.label) == key), None)

        self.tree.expand(self.tree_model.index_for(node.parent))
        for part in (*PurePosixPath(self.path).parts, None):
            index = self.tree_model.index_for(node)
            key = path_key(part) if part is not None else None
            child = find_child(node, key) if key is not None else None
            if child is None:
                cached = snapshot
                if cached is None or path_key(cached.path) != path_key(node.path):
                    cached = (
                        self.vfs.cached(node.path) if part is not None or not node.loaded else None
                    )
                if cached and not cached.stale:
                    self.tree_model.set_children(
                        node, cached.entries, required_child=key if node.loaded else None
                    )
                    if key is not None:
                        child = find_child(node, key)
            if node.loaded:
                self.tree.expand(index)
            if part is None:
                self.tree.setCurrentIndex(index)
                self.tree.scrollTo(index)
                return
            if not node.loaded:
                self.tree_model.fetchMore(index)
                return
            if child is None:
                return
            node = child

    def show_snapshot(self, snapshot, partial=False):
        self.snapshot = snapshot
        self.sort_generation += 1
        self.file_model.set_entries(snapshot.entries)
        errors = [
            f"{self.account_label(item.account_id)}: {item.error}"
            for item in snapshot.listings
            if item.state in {"error", "excluded"}
        ]
        label = (
            "이전 조회 결과"
            if snapshot.stale
            else ("조회 중" if partial else "최신 조회 결과")
        )
        self.banner.setText(label + (" · " + " / ".join(errors) if errors else ""))
        self.statusBar().showMessage(f"/{snapshot.path} · {len(snapshot.entries):,}개 논리 항목")

    def sort_entries(self, column, order):
        if not hasattr(self, "file_model"):
            return
        entries = self.file_model.entries
        self.sort_generation += 1
        generation = self.sort_generation

        def sort(context):
            def key(entry):
                if column == 1:
                    return max((r.size or 0 for r in entry.replicas), default=0)
                if column == 2:
                    return max(
                        (r.modified.timestamp() for r in entry.replicas if r.modified), default=0
                    )
                if column == 4:
                    return entry.assessment.status.value
                return entry.name.casefold()

            ordered = sorted(entries, key=key, reverse=order == Qt.SortOrder.DescendingOrder)
            return sorted(ordered, key=lambda e: not e.is_dir)

        self.workers.submit(
            sort,
            lambda rows: (
                self.file_model.set_entries(rows)
                if not self._closing and generation == self.sort_generation
                else None
            ),
            blocks_configuration=False,
        )

    def tree_clicked(self, index):
        node = self.tree_model.node(index)
        if node.kind in {"unified", "account", "dir"}:
            self.navigate(node.path, scope=node.scope)
        elif node.kind == "issues":
            self.generation += 1
            self.mode = node.kind
            self.proxy.update_filter(scope="", mode=node.kind)
            self.file_model.set_entries(
                self.scan_results
                if self.scan_path is not None
                else (self.snapshot.entries if self.snapshot else ())
            )
            self.banner.setText(
                f"폴더 검사 결과 · /{self.scan_path}"
                if self.scan_path is not None
                else "현재 폴더의 문제 항목입니다. 하위 폴더까지 보려면 현재 폴더 검사를 실행하세요."
            )
        elif node.kind == "trash":
            self.open_trash(node.scope)

    def load_tree(self, node):
        if self.vfs is None:
            node.loading = False
            return
        index = QPersistentModelIndex(self.tree_model.index_for(node))

        def loaded(snapshot):
            if index.isValid() and node.loading and not self._closing:
                self.tree_model.set_children(node, snapshot.entries)
                self.sync_tree()

        self.workers.submit(
            lambda context: self.vfs.refresh(node.path, context),
            loaded,
            lambda error: setattr(node, "loading", False),
        )

    def selected(self):
        indexes = {
            index.row(): index.siblingAtColumn(0) for index in self.selection.selectedIndexes()
        }
        return [index.data(Qt.ItemDataRole.UserRole) for _, index in sorted(indexes.items())]

    def selected_replicas(self):
        return [
            replica
            for entry in self.selected()
            for replica in entry.replicas
            if not self.scope or replica.account_id == self.scope
        ]

    def account_label(self, account_id):
        return next((a.label for a in self.settings.accounts if a.id == account_id), account_id)

    def update_details(self, *args):
        entries = self.selected()
        if len(entries) != 1:
            self.details.setPlainText(f"{len(entries)}개 항목 선택" if entries else "")
            return
        entry = entries[0]
        lines = [
            entry.path,
            entry.assessment.status.value,
            entry.assessment.verification,
            *entry.assessment.issues,
            "",
        ]
        for replica in entry.replicas:
            lines += [
                self.account_label(replica.account_id),
                size_text(replica.size),
                str(replica.modified or "시각 정보 없음"),
                *[f"{key}: {value}" for key, value in replica.hashes.items()],
                *(
                    [f"정밀 검증 SHA-256: {replica.verified_sha256}"]
                    if replica.verified_sha256
                    else []
                ),
                "",
            ]
        self.details.setPlainText("\n".join(lines))

    def context_menu(self, view, point):
        menu = QMenu(self)
        menu.addActions(
            [
                self.download_action,
                self.properties_action,
                self.copy_action,
                self.cut_action,
                self.paste_action,
                self.rename_action,
                self.delete_action,
            ]
        )
        menu.addSeparator()
        menu.addActions(
            [self.upload_action, self.folder_upload_action, self.mkdir_action, self.refresh_action]
        )
        menu.exec(view.viewport().mapToGlobal(point))

    def enqueue_plan(
        self, plan, builder, *, require_confirmation=True, upload=False, after=(), execute=None
    ):
        if execute is None:
            def execute(context):
                return self.engine.execute(plan, context, approved=True)
        task = self.transfers.submit(
            plan.label,
            execute,
            after=after,
            queue="transfer" if plan.copies or plan.renames else "other",
        )
        account_ids = {a.id for a in self.settings.accounts}
        endpoints = (
            *plan.mkdirs,
            *(copy.target for copy in (*plan.copies, *plan.alternates)),
            *(item.target for item in plan.renames),
            *(item.endpoint for item in (*plan.deletions, *plan.rmdirs)),
        )
        issue_keys = {path_key(e.path) for e in endpoints if e.account_id in account_ids}
        retry = None
        if builder is not None:
            def retry():
                self.prepare(builder, require_confirmation=require_confirmation, upload=upload)

        self._task_retries[task.id] = (retry, False, issue_keys)
        self._pending_plans[task.id] = plan
        return task

    @staticmethod
    def parent_paths(path):
        while parent := path.rpartition("/")[0]:
            yield parent
            path = parent

    def enqueue_directories(self, endpoints, label, *, upload=False, after=()):
        directories, tasks = {}, {}
        for endpoint in endpoints:
            directories.setdefault(path_key(endpoint.path), []).append(endpoint)
        for key in sorted(directories, key=lambda path: path.count("/")):
            path = directories[key][0].path
            parents = tuple(parent for parent in self.parent_paths(path) if path_key(parent) in tasks)
            dependencies = (tasks[path_key(parents[0])].id,) if parents else after
            builder = None
            if upload:
                def builder(context, paths=(*reversed(parents), path)):
                    return self.engine.plan_upload_directories(paths, context)

            tasks[key] = self.enqueue_plan(
                MutationPlan(f"{label} · /{path}", mkdirs=tuple(directories[key])),
                builder,
                upload=upload,
                after=dependencies,
            )
        return tasks

    def enqueue_uploads(self, plan):
        folders = self.enqueue_directories(plan.mkdirs, "폴더 업로드", upload=True)
        yield from folders.values()

        files, alternates = {}, {}
        for copy in plan.copies:
            files.setdefault(copy.target.path, []).append(copy)
        for copy in plan.alternates:
            alternates.setdefault(copy.target.path, []).append(copy)
        for target, copies in files.items():
            endpoint = copies[0].source.endpoint
            source = Path(endpoint.fs) / endpoint.path
            parents = tuple(parent for parent in self.parent_paths(target) if path_key(parent) in folders)

            def rebuild(context, source=source, target=target, parents=tuple(reversed(parents))):
                file_plan = self.engine.plan_upload(source, target, context)
                if parents:
                    directories = self.engine.plan_upload_directories(parents, context)
                    return replace(file_plan, mkdirs=directories.mkdirs)
                return file_plan

            yield self.enqueue_plan(
                MutationPlan(
                    f"파일 업로드 · /{target}",
                    copies=tuple(copies),
                    local_sources=(str(source),),
                    alternates=tuple(alternates.get(target, ())),
                ),
                rebuild,
                upload=True,
                after=(folders[path_key(parents[0])].id,) if parents else (),
            )

    def prepare(
        self, builder, *, replica_request=False, require_confirmation=True, upload=False, enqueue=None
    ):
        if self._preparing:
            if replica_request:
                self.set_replica_busy(False)
            return
        if not self.available():
            if replica_request:
                self.set_replica_busy(False)
            return
        self._preparing = True
        self.update_command_buttons()
        context = None
        started = monotonic()
        if upload:
            self.upload_progress.setText("업로드 준비 중…")
            self.upload_progress.show()
            self.cancel_upload_button.setEnabled(True)
            self.cancel_upload_button.show()
        else:
            self.statusBar().showMessage("작업 범위와 최신 상태를 확인하는 중…")

        def progress(value):
            if (
                not self._closing
                and context is not None
                and context is self._upload_context
                and not context.cancelled.is_set()
            ):
                self.upload_progress.setText(f"업로드 준비 중 · {value['detail']}")

        def finish_upload():
            if not upload:
                return False
            self._upload_context = None
            self.upload_progress.hide()
            self.cancel_upload_button.hide()
            cancelled = context is not None and context.cancelled.is_set()
            if cancelled and not self._closing:
                self.statusBar().showMessage("업로드 준비를 취소했습니다.", 4000)
            return cancelled

        def ready(plan):
            task = None
            elapsed_ms = (monotonic() - started) * 1000
            try:
                if finish_upload() or self._closing:
                    return
                if upload:
                    plan = self._confirm_upload_overwrites(plan)
                    if plan is None:
                        return
                    if not (plan.copies or plan.mkdirs):
                        self.statusBar().showMessage("업로드할 항목이 없습니다.", 4000)
                        return
                if self.endpoints_busy(plan.endpoints):
                    self.statusBar().showMessage("해당 경로의 작업이 끝난 뒤 다시 요청하세요.")
                    return
                if upload:
                    for queued in self.enqueue_uploads(plan):
                        if context is not None:
                            logger.info(
                                "upload prepared task=%s trace=%s elapsed_ms=%.0f",
                                queued.id,
                                context.trace_id,
                                elapsed_ms,
                            )
                    return
                needs_confirmation = (
                    require_confirmation(plan)
                    if callable(require_confirmation)
                    else require_confirmation
                )
                approved = not needs_confirmation or confirm(self, plan.label, plan.summary)
                if approved and not self._closing:
                    if enqueue is not None:
                        enqueue(plan)
                        return
                    task = self.enqueue_plan(
                        plan,
                        builder,
                        require_confirmation=require_confirmation,
                    )
                    if replica_request:
                        self._replica_task_id = task.id
            except Exception as error:
                if not self._closing:
                    message(self, error)
            finally:
                self._preparing = False
                if replica_request and task is None:
                    self.set_replica_busy(False)
                self.update_command_buttons()

        def failed(error):
            try:
                if not finish_upload() and not self._closing:
                    message(self, error)
            finally:
                self._preparing = False
                if replica_request:
                    self.set_replica_busy(False)
                self.update_command_buttons()

        try:
            context = self.workers.submit(builder, ready, failed, progress if upload else None)
            if upload:
                self._upload_context = context
        except Exception as error:
            failed(error)

    def cancel_upload_preparation(self):
        if self._upload_context is not None:
            self._upload_context.cancelled.set()
            self.cancel_upload_button.setEnabled(False)
            self.upload_progress.setText("업로드 준비 취소 중…")

    def upload(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "파일 업로드")
        if paths:
            self.upload_paths(paths, self.path)

    def upload_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "폴더 업로드")
        if folder:
            self.upload_paths([folder], self.path)

    def upload_paths(self, paths, folder):
        paths = tuple(paths)
        self.prepare(
            lambda context: self.engine.plan_uploads(paths, folder, context),
            upload=True,
        )

    def _confirm_upload_overwrites(self, plan):
        conflicts = {}
        for copy in plan.copies:
            if copy.previous is not None:
                conflicts.setdefault(path_key(copy.target.path), []).append(copy.target)
        keys = list(conflicts)
        details = [
            "\n".join(
                f"{self.account_label(target.account_id)} · /{target.path}" for target in targets
            )
            for targets in conflicts.values()
        ]
        skipped = set()
        for index, key in enumerate(keys):
            approved, apply_all = confirm_upload_overwrite(self, details, index)
            if self._closing:
                return None
            if not approved:
                skipped.add(key)
                if apply_all:
                    skipped.update(keys[index + 1 :])
            if apply_all:
                break
        if not skipped:
            return plan
        copies = tuple(copy for copy in plan.copies if path_key(copy.target.path) not in skipped)
        local_sources = dict.fromkeys(
            str(Path(copy.source.endpoint.fs) / copy.source.endpoint.path) for copy in copies
        )
        return replace(
            plan,
            copies=copies,
            alternates=tuple(
                copy for copy in plan.alternates if path_key(copy.target.path) not in skipped
            ),
            local_sources=tuple(local_sources),
        )

    def choose_replica(self, entry, *, scope=None):
        scope = self.scope if scope is None else scope
        if scope:
            scoped = tuple(r for r in entry.replicas if r.account_id == scope)
            if len(scoped) == 1:
                return scoped[0]
        if len(entry.replicas) == 1:
            return entry.replicas[0]
        if entry.assessment.status == Status.HEALTHY:
            return entry.replicas[0]
        labels = [
            f"{self.account_label(r.account_id)} · {r.path} · {size_text(r.size)}"
            for r in entry.replicas
        ]
        value, ok = QInputDialog.getItem(
            self,
            "다운로드",
            "다운로드할 복제본을 선택하세요.",
            labels,
            editable=False,
        )
        return entry.replicas[labels.index(value)] if ok else None

    def start_download_drag(self, entries):
        if (
            QApplication.platformName() != "windows"
            or self.engine is None
            or self._closing
            or self._explorer_drag
            or any(entry.is_dir for entry in entries)
        ):
            return False
        names = [entry.name for entry in entries]
        try:
            for entry, name in zip(entries, names, strict=True):
                validate_path(entry.path)
                validate_path(name)
        except ValueError:
            return False
        if len({path_key(name) for name in names}) != len(names):
            return False

        from .windows_drag import FileDrag

        scope = self.scope
        files = []
        for entry, name in zip(entries, names, strict=True):
            sizes = {replica.size for replica in entry.replicas}
            size = sizes.pop() if len(sizes) == 1 else None
            files.append((name, size if type(size) is int and 0 <= size < 2**64 else None))

        tasks = {}

        def stage(paths, hold):
            try:
                for index, (entry, target) in enumerate(zip(entries, paths, strict=True)):
                    if task := self.enqueue_dragged_file(entry, scope, target):
                        tasks[index] = task
            finally:
                if tasks:
                    release = hold()
                    pending = {task.id for task in tasks.values()}

                    def completed(task):
                        if task.id in pending and task.state not in {"대기", "진행 중", "취소 요청"}:
                            pending.remove(task.id)
                            if not pending:
                                self.task_changed.disconnect(completed)
                                release()

                    self.task_changed.connect(completed, Qt.ConnectionType.QueuedConnection)
                    for task in tasks.values():
                        completed(task)
            return lambda index: self.wait_for_dragged_file(tasks.get(index))

        def finished():
            for task in tasks.values():
                self.cancel_task(task)
            self._explorer_drag = False
            if not self._closing:
                self.update_configuration_buttons()

        drag = FileDrag(
            INTERNAL_MIME,
            json.dumps([entry.path for entry in entries]).encode(),
            files,
            stage,
            finished,
        )
        self._explorer_drag = True
        self.update_configuration_buttons()
        try:
            drag.run()
        except OSError as error:
            message(self, error)
        return True

    def enqueue_dragged_file(self, entry, scope, target):
        if self._closing or self._preparing or not self._explorer_drag:
            return None
        replica = self.choose_replica(entry, scope=scope)
        if replica is None or replica.is_dir or self._closing or not self._explorer_drag:
            return None
        try:
            endpoint = self.engine.endpoint(replica)
            if self.endpoints_busy((endpoint,)):
                raise ValueError("해당 경로의 작업이 끝난 뒤 다시 요청하세요.")
            plan = MutationPlan(
                f"다운로드 · /{replica.path}",
                (CopyIntent(ExpectedItem(endpoint, replica), local_endpoint(target), None),),
            )
            return self.enqueue_plan(plan, None)
        except (ValueError, BackendError, OSError, RuntimeError, StopIteration) as error:
            message(self, str(error) or "계정 목록이 바뀌었습니다. 목록을 새로고침하세요.")
            return None

    def wait_for_dragged_file(self, task):
        if self._closing:
            raise BackendError(ErrorKind.CANCELLED)
        if task is None:
            return False
        active = {"대기", "진행 중", "취소 요청"}
        if task.state in active:
            loop = QEventLoop(self)

            def finished(changed):
                if changed is task and task.state not in active:
                    loop.quit()

            self.task_changed.connect(finished)
            try:
                if task.state in active:
                    loop.exec()
            finally:
                self.task_changed.disconnect(finished)
                loop.deleteLater()
        if self._closing or task.state == "취소됨" or task.context.cancelled.is_set():
            raise BackendError(ErrorKind.CANCELLED)
        return task.state == "완료"

    def download(self):
        entries = self.selected()
        if len(entries) != 1 or entries[0].is_dir:
            message(
                self,
                "다운로드할 파일 하나를 선택하세요. 여러 버전은 복제본 관리에서 개별 다운로드할 수 있습니다.",
            )
            return
        replica = self.choose_replica(entries[0])
        if replica:
            self.download_replica(replica)

    def download_replica(self, replica, *, destination=None, replica_request=False):
        path = destination
        try:
            if path is None:
                path, _ = QFileDialog.getSaveFileName(
                    self,
                    "다운로드",
                    str(Path(self.settings.download_dir) / replica.name),
                    options=QFileDialog.Option.DontConfirmOverwrite,
                )
            if path:
                def build(context):
                    plan = self.engine.plan_download(replica, path, context)
                    if destination is not None and plan.copies[0].previous is None:
                        context.check()
                        destination.parent.mkdir(parents=True, exist_ok=True)
                    return plan

                self.prepare(
                    build,
                    replica_request=replica_request,
                    require_confirmation=lambda plan: plan.copies[0].previous is not None,
                )
        finally:
            if replica_request and not path:
                self.set_replica_busy(False)

    def activate_index(self, index):
        entry = index.siblingAtColumn(0).data(Qt.ItemDataRole.UserRole)
        if entry is None:
            return
        if entry.is_dir:
            self.navigate(entry.path)
            return
        if self.engine is None or self._preparing or self._closing:
            return
        try:
            endpoints = tuple(self.engine.endpoint(replica) for replica in entry.replicas)
        except (ValueError, StopIteration) as error:
            message(self, str(error) or "계정 목록이 바뀌었습니다. 목록을 새로고침하세요.")
            return
        if self.endpoints_busy(endpoints):
            return
        replica = self.choose_replica(entry)
        if replica:
            destination = Path(self.settings.download_dir) / replica.name
            self.download_replica(replica, destination=destination)

    def mkdir(self):
        name, ok = QInputDialog.getText(self, "새 폴더", "폴더 이름")
        if ok and name:
            path = "/".join(filter(None, [self.path, name]))
            self.prepare(
                lambda context: self.engine.plan_mkdir(path, context), require_confirmation=False
            )

    def delete(self):
        self.request_delete(tuple(self.selected_replicas()))

    def rename(self):
        entries = self.selected()
        if len(entries) != 1:
            return
        name, ok = QInputDialog.getText(self, "이름 변경", "새 이름", text=entries[0].name)
        if ok and name:
            parent = str(PurePosixPath(entries[0].path).parent)
            path = name if parent == "." else parent + "/" + name
            replicas = tuple(self.selected_replicas())
            self.prepare(
                lambda context: replace(
                    self.engine.plan_move(replicas, path, context), label="이름 변경"
                )
            )

    def properties(self):
        entries = self.selected()
        if len(entries) == 1:
            self.manage_replicas(entries)

    def manage_replicas(self, entries):
        if self._closing or not entries:
            return
        if self.replica_dialog is not None:
            self.replica_dialog.close()
        dialog = ReplicaDialog(entries, self.settings.accounts, self)
        self.replica_dialog = dialog
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.finished.connect(lambda: setattr(self, "replica_dialog", None))
        dialog.action_requested.connect(self.replica_action)
        dialog.entry_selector.currentIndexChanged.connect(self.update_command_buttons)
        self.update_command_buttons()
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def show_issues(self):
        if self._closing or self.scan_path is None:
            return
        if self.issue_dialog is None:
            self.issue_dialog = IssueDialog(self)
            self.issue_dialog.action_requested.connect(self.resolve_issues)
            self.issue_dialog.management_requested.connect(self.manage_replicas)
            self.issue_dialog.rescan_requested.connect(
                lambda path: self.start_scan(path, title="다시 검사")
            )
            self.issue_dialog.table.itemSelectionChanged.connect(self.update_command_buttons)
        self.issue_dialog.set_results(self.scan_path, self.scan_results, self.scan_complete)
        self.update_command_buttons()
        self.issue_dialog.show()
        self.issue_dialog.raise_()
        self.issue_dialog.activateWindow()

    def resolve_issues(self, entries):
        if self._repair_busy or self._preparing:
            return
        entries = sorted(
            {
                entry.key: entry
                for entry in entries
                if entry.assessment.status in {Status.DIRECTORY, Status.UNDER, Status.UNVERIFIED}
            }.values(),
            key=lambda entry: (
                entry.assessment.status != Status.DIRECTORY,
                entry.path.count("/"),
                entry.key,
            ),
        )
        if not entries or not self.available():
            return
        if self.endpoints_busy(self.entry_endpoints(entries)):
            self.statusBar().showMessage("해당 경로의 작업이 끝난 뒤 다시 요청하세요.")
            return
        self._preparing = True
        self.set_repair_busy(True)
        folders = {}

        def register(offset=0):
            try:
                if not self._closing:
                    # Yield between batches so a large selection cannot block window events.
                    for entry in entries[offset : offset + 32]:
                        status = entry.assessment.status
                        action = {
                            Status.DIRECTORY: "누락된 폴더 생성",
                            Status.UNDER: "추천 계정으로 복제",
                            Status.UNVERIFIED: "정밀 검증",
                        }[status]
                        after, parent = [], entry.key.rpartition("/")[0]
                        while parent:
                            if parent in folders:
                                after.append(folders[parent])
                            parent = parent.rpartition("/")[0]
                        task = self.transfers.submit(
                            f"{action} · /{entry.path}",
                            lambda context, entry=entry: self.repair_issue(entry, context),
                            after=tuple(after),
                            queue="transfer" if status == Status.UNDER else "other",
                        )
                        self._task_retries[task.id] = (
                            lambda entry=entry: self.resolve_issues((entry,)),
                            True,
                            {entry.key},
                        )
                        self._pending_repairs[task.id] = entry
                        self._repair_tasks.add(task.id)
                        self._repair_total += 1
                        if status == Status.DIRECTORY:
                            folders[entry.key] = task.id
                    offset += 32
                    if offset < len(entries):
                        QTimer.singleShot(0, lambda: register(offset))
                        return
            except Exception as error:
                if not self._closing:
                    message(self, f"/{entry.path}: {error}")
            self._preparing = False
            self.refresh_repair_progress()
            self.update_command_buttons()

        register()

    def set_repair_busy(self, busy):
        self._repair_busy = busy
        if not busy:
            self._repair_tasks.clear()
            self._repair_total = 0
            self._pending_repairs.clear()
            self._repair_progress = ""
        self.update_command_buttons()

    def show_task_progress(self, row, state, detail):
        if row is None or self._closing:
            return
        task_id = row.data(0, Qt.ItemDataRole.UserRole)
        repairing = task_id in self._repair_tasks
        if not repairing and task_id not in self._delete_targets:
            return
        if not repairing:
            task = self.transfers.tasks[task_id]
            summary = task.stats.get("detail", "")
            if state == "결과 재확인":
                detail = " · ".join(filter(None, (f"삭제 {task.state}", detail, summary)))
            else:
                detail = " · ".join(filter(None, (summary, detail)))
        row.setText(1, state)
        row.setText(2, detail)
        row.setToolTip(2, detail)
        if not repairing:
            return
        self._repair_progress = f"{state} · {detail}" if detail else state
        if self._repair_total:
            finished = self._repair_total - len(self._repair_tasks)
            self._repair_progress = (
                f"{finished}/{self._repair_total}개 작업 종료 · {self._repair_progress}"
            )
        if self.issue_dialog is not None and not self._releasing_task_rechecks:
            self.issue_dialog.set_progress(self._repair_progress)

    def refresh_repair_progress(self):
        if not self._repair_tasks:
            if not self._preparing:
                self.set_repair_busy(False)
            return
        rows = [self._task_rows[task_id] for task_id in self._repair_tasks]
        row = next((item for item in rows if item.text(1) != "대기"), rows[0])
        self.show_task_progress(row, row.text(1), row.text(2) or row.text(0))

    def queue_task_refresh(self, *, browse=False):
        if browse and self._task_browse_request != (self.generation, self.path):
            # A listing already started before this task finished.
            self.generation += 1
            self._task_browse_request = (self.generation, self.path)
            self.begin_refresh(*self._task_browse_request, self.vfs.cached(self.path))
        if not self._task_refresh_running and not self._task_refresh_timer.isActive():
            self._task_refresh_timer.start(0)

    def flush_task_refreshes(self):
        self._task_refresh_timer.stop()
        if self._task_refresh_running:
            return
        requests, self._task_rechecks = self._task_rechecks, []
        browse, self._task_browse_request = self._task_browse_request, None
        if browse != (self.generation, self.path) or self.mode != "browse":
            if browse is not None and self._refreshing == browse:
                self._refreshing = None
                self.refresh_action.setEnabled(True)
            browse = None
        if self._closing or self._shutdown_done:
            for _, _, failed, _ in requests:
                failed("프로그램을 종료하는 중입니다.")
            return
        if not requests and browse is None:
            self.update_configuration_buttons()
            return
        self._task_refresh_running = True

        def read(context):
            snapshots = {}

            def load_folder(path, context, *, on_progress=None):
                if path not in snapshots:
                    on_partial = None
                    if browse and path == browse[1]:

                        def on_partial(snapshot):
                            self.partial_listing.emit(browse[0], snapshot)

                    snapshots[path] = self.vfs.refresh(
                        path, context, on_partial, on_progress=on_progress
                    )
                return snapshots[path]

            results = []
            for index, (recheck, _, _, _) in enumerate(requests):
                check_context = replace(
                    context,
                    progress=lambda value, index=index: context.progress(
                        {**value, "recheck_index": index}
                    ),
                )
                try:
                    result = recheck(check_context, load_folder)
                except Exception as error:
                    results.append((None, str(error)))
                else:
                    results.append((result, None))
            if browse and browse[1] not in snapshots and not context.cancelled.is_set():
                try:
                    load_folder(browse[1], context)
                except Exception as error:
                    logger.warning("task browse refresh failed error=%s", error_summary(error))
            return results, snapshots.get(browse[1]) if browse else None

        def finish(results, snapshot):
            try:
                current = {entry.key: entry for entry in self.scan_results} if requests else {}
                changed, reports, failures = False, [], []
                for (_, apply, failed, _), (result, error) in zip(requests, results):
                    if error is not None:
                        failures.append(partial(failed, error))
                        continue
                    try:
                        applied, report = apply(result, current)
                    except Exception as error:
                        failures.append(partial(failed, str(error)))
                    else:
                        changed |= applied
                        reports.append(report)
                try:
                    if changed:
                        self.publish_rechecked_issues(current)
                finally:
                    self._releasing_task_rechecks = True
                    try:
                        report_results(reports)
                    finally:
                        self._releasing_task_rechecks = False
                    if reports:
                        if self._repair_busy:
                            self.refresh_repair_progress()
                        self.update_command_buttons()
                    report_results(failures)
                if browse and self._refreshing == browse:
                    if snapshot is None:
                        self._refreshing = None
                        self.refresh_action.setEnabled(True)
                        if self.mode == "browse" and browse == (self.generation, self.path):
                            self.refresh()
                    else:
                        self.finish_refresh(browse[0], snapshot)
            finally:
                self._task_refresh_running = False
                if self._task_rechecks or self._task_browse_request is not None:
                    self.queue_task_refresh()

        def report_results(reports):
            for report in reports:
                try:
                    report()
                except Exception as error:
                    logger.error("task recheck callback failed error=%s", error_summary(error))

        def progress(value):
            requests[value["recheck_index"]][3](value)

        def failed(error):
            finish([(None, error)] * len(requests), None)

        try:
            self.workers.submit(read, lambda result: finish(*result), failed, progress)
        except Exception as error:
            failed(str(error))

    def publish_rechecked_issues(self, current):
        self.scan_results = tuple(current.values())
        path = self.scan_path
        if self.mode == "issues":
            self.sort_generation += 1
            self.file_model.set_entries(self.scan_results)
            self.banner.setText(f"폴더 검사 결과 · /{path} · {len(current):,}개 문제")
        if self.issue_dialog is not None:
            selected = {entry.key for entry in self.issue_dialog.selected_entries()}
            self.issue_dialog.set_results(
                path, self.scan_results, self.scan_complete, selected_keys=selected
            )

    def recheck_issues(
        self, keys, *, task_id=None, on_finished=None, proofs=(), refresh_browse=False
    ):
        tracked = task_id in self._repair_tasks or task_id in self._delete_targets
        row = self._task_rows.get(task_id) if tracked else None
        started = monotonic()

        def finish(outcome, detail=None):
            log = logger.warning if outcome in {"failed", "incomplete"} else logger.info
            log(
                "issue recheck ended task=%s outcome=%s elapsed_ms=%.0f",
                task_id or "-",
                outcome,
                (monotonic() - started) * 1000,
            )
            try:
                if row is not None and detail is not None:
                    self.show_task_progress(row, self.transfers.tasks[task_id].state, detail)
                if not self._closing and outcome in {"complete", "incomplete"}:
                    self.statusBar().showMessage(f"선택 문제 {detail}")
            finally:
                if on_finished:
                    on_finished()

        original = {entry.key: entry for entry in self.scan_results if entry.key in keys}
        if refresh_browse and not self._closing:
            self.queue_task_refresh(browse=True)
        if not original or self.vfs is None or self._closing:
            if tracked:
                reason = (
                    "closing" if self._closing else "no_backend" if self.vfs is None else "no_match"
                )
                finish(reason, "재확인 종료" if task_id in self._repair_tasks else None)
            elif on_finished:
                on_finished()
            return
        path = self.scan_path
        logger.info(
            "issue recheck queued task=%s entries=%d scan_path=%s",
            task_id or "-",
            len(original),
            diagnostic_ref(path_key(path or "")),
        )
        self.show_task_progress(row, "결과 재확인", "처리한 경로를 다시 조회하는 중…")

        def apply(result, current):
            if self._closing or self.scan_path != path:
                return False, partial(finish, "superseded", "재확인 종료")
            updates, complete = result
            matched, removed = False, 0
            for key, original_entry in original.items():
                # A newer scan or recheck owns any entries it has replaced.
                if current.get(key) is not original_entry:
                    continue
                matched = True
                entry = updates[key]
                if entry is None or entry.assessment.status == Status.HEALTHY:
                    del current[key]
                    removed += 1
                else:
                    current[key] = entry
            if not matched:
                return False, partial(finish, "superseded", "재확인 종료")
            self.scan_complete &= complete
            outcome = "complete" if complete else "incomplete"
            return True, partial(finish, outcome, f"재확인 완료 ({removed:,}개 해결)")

        def failed(error):
            try:
                if not self._closing:
                    message(self, f"선택 문제를 재확인하지 못해 목록을 유지합니다.\n{error}")
            finally:
                finish("failed", f"재확인 실패 (목록 유지) · {error}")

        def recheck(context, load_folder=None):
            logger.info("issue recheck started task=%s trace=%s", task_id or "-", context.trace_id)
            return self.vfs.recheck(original.values(), context, proofs=proofs, load_folder=load_folder)

        def progress(value):
            if row is not None and self._task_rows.get(task_id) is row:
                self.show_task_progress(row, "결과 재확인", value.get("detail", ""))

        if task_id is not None:
            self._task_rechecks.append((recheck, apply, failed, progress))
            self.queue_task_refresh()
            return

        def done(result):
            current = {entry.key: entry for entry in self.scan_results}
            try:
                changed, report = apply(result, current)
            except Exception as error:
                failed(str(error))
                return
            try:
                if changed:
                    self.publish_rechecked_issues(current)
            finally:
                report()

        try:
            self.workers.submit(recheck, done, failed)
        except Exception as error:
            logger.warning(
                "issue recheck submit failed task=%s error=%s", task_id or "-", error_summary(error)
            )
            failed(error)

    def delete_replica(self, replica):
        self.request_delete((replica,), replica_request=True)

    def request_delete(self, replicas, *, replica_request=False):
        if not replicas or self._preparing or self._closing or None in self._delete_targets:
            return
        if replica_request and self._replica_busy:
            return
        if not self.available():
            return
        parent = (self.replica_dialog if replica_request else None) or self
        if replica_request:
            self.set_replica_busy(True)
        try:
            targets = set(self.entry_endpoints(replicas))
            targets.update(self.engine.endpoint(replica) for replica in replicas)
            if self.endpoints_busy(targets):
                self.statusBar().showMessage("해당 경로의 작업이 끝난 뒤 다시 요청하세요.")
                return
            # Hold the targets while confirmation is open, before the queue assigns an ID.
            self._delete_targets[None] = targets
            self.update_command_buttons()
            labels = [
                f"{self.account_label(replica.account_id)} · /{replica.path}"
                for replica in replicas
            ]
            prompt = (
                "선택한 항목을 하위 항목까지 모두 삭제하시겠습니까?"
                if any(replica.is_dir for replica in replicas)
                else "선택한 항목을 정말 삭제하시겠습니까?"
            )
            approved = confirm(parent, "삭제", "\n".join(labels), prompt)
            if not approved or self._closing:
                return
            groups = {}
            for replica in sorted(replicas, key=lambda item: item.path.count("/")):
                key = path_key(replica.path)
                root = next((path for path in self.parent_paths(key) if path in groups), key)
                groups.setdefault(root, []).append(replica)
            for group in groups.values():
                self.enqueue_delete(tuple(group), replica_request=replica_request)
        except Exception as error:
            logger.warning("delete request failed error=%s", error_summary(error))
            if not self._closing:
                message(parent, error)
        finally:
            self._delete_targets.pop(None, None)
            if replica_request and self._replica_task_id is None:
                self.set_replica_busy(False)
            else:
                self.update_command_buttons()

    def enqueue_delete(self, replicas, *, replica_request):
        first = replicas[0]
        label = (
            f"{self.account_label(first.account_id)} · /{first.path}"
            if len(replicas) == 1 else f"/{first.path}"
        )
        task = self.transfers.submit(
            f"삭제 · {label}",
            lambda context: self.engine.execute_delete(replicas, context, approved=True),
            queue="other",
        )
        if replica_request:
            self._replica_task_id = task.id
        targets = set(self.entry_endpoints(replicas))
        targets.update(self.engine.endpoint(replica) for replica in replicas)
        self._delete_targets[task.id] = targets
        self._task_retries[task.id] = (
            lambda: self.request_delete(replicas, replica_request=replica_request),
            False,
            {path_key(replica.path) for replica in replicas},
        )
        logger.info(
            "delete protection registered task=%s targets=%d selected=%d path=%s",
            task.id, len(targets), len(replicas), diagnostic_ref(path_key(first.path)),
        )

    def replica_action(self, action, value):
        if action == "delete":
            self.delete_replica(value)
            return
        if self._replica_busy or self._preparing:
            return
        if action == "verify":
            self.verify(value)
            return
        if action not in {"download", "replicate"} or not self.available():
            return
        if self.paths_overlap((value.path,), self.busy_path_keys()):
            return
        self.set_replica_busy(True)

        def failed(error):
            try:
                if not self._closing:
                    message(self, error)
            finally:
                self.set_replica_busy(False)

        if action == "download":
            try:
                self.download_replica(value, replica_request=True)
            except Exception as error:
                failed(error)
        elif action == "replicate":
            accounts = [
                a
                for a in self.settings.accounts
                if a.id != value.account_id
                and a.enabled
                and not a.readonly
                and not is_excluded(a, value.path)
            ]
            if not accounts:
                failed("사용 가능한 다른 계정이 없습니다.")
                return

            def recommend(context):
                quotas, caps, available = {}, {}, []
                for account in accounts:
                    try:
                        caps[account.id] = self.backend.features(account, context)
                        quotas[account.id] = self.engine.quota(account, context)
                    except BackendError as error:
                        if error.kind == ErrorKind.CANCELLED:
                            raise
                        continue
                    available.append(account)
                return choose_accounts(
                    available, quotas, value.size or 0, capabilities=caps, required=1
                )[0].id

            def selected(recommended):
                try:
                    if self._closing:
                        self.set_replica_busy(False)
                        return
                    ordered = sorted(accounts, key=lambda a: a.id != recommended)
                    labels = [a.label + (" (추천)" if a.id == recommended else "") for a in ordered]
                    name, ok = QInputDialog.getItem(
                        self, "복제", "대상 계정", labels, editable=False
                    )
                    if ok:
                        account = ordered[labels.index(name)]
                        self.prepare(
                            lambda context: self.engine.plan_replicate(value, account, context),
                            replica_request=True,
                        )
                    else:
                        self.set_replica_busy(False)
                except Exception as error:
                    failed(error)

            try:
                self.workers.submit(recommend, selected, failed)
            except Exception as error:
                failed(error)

    def read_replica_hash(self, replica, context, *, index, total):
        context.check()
        context.describe(
            f"정밀 검증 {index}/{total} · {self.account_label(replica.account_id)} · /{replica.path}",
            replica.size,
        )
        expected = self.engine.expected(self.engine.endpoint(replica), context)
        if expected.version.is_dir:
            raise ValueError("파일이 폴더로 변경되었습니다. 다시 검사하세요.")
        digest = self.backend.hash_file(expected.endpoint, context)
        context.check()
        return expected, digest

    def save_verification(self, reads, context):
        for index, (expected, _) in enumerate(reads, 1):
            context.describe(
                f"원본 재확인 {index}/{len(reads)} · "
                f"{self.account_label(expected.endpoint.account_id)} · /{expected.endpoint.path}"
            )
            self.engine.assert_expected(expected, context)
        context.check()
        results = tuple((expected.version, digest) for expected, digest in reads)
        stored = self.vfs.record_verification(results)
        return results, stored, self.vfs.verification_store.error

    def read_verification(self, replicas, context):
        reads = tuple(
            self.read_replica_hash(replica, context, index=index, total=len(replicas))
            for index, replica in enumerate(replicas, 1)
        )
        return self.save_verification(reads, context)

    def repair_issue(self, entry, context):
        if entry.assessment.status == Status.UNVERIFIED:
            return self.verify_issue(entry, context)
        if entry.assessment.status == Status.UNDER:
            # A retry may only need verification after its previous copy already succeeded.
            updates, _ = self.vfs.recheck((entry,), context)
            current = updates[entry.key]
            if current is not None and current.assessment.complete:
                if current.assessment.status == Status.UNVERIFIED:
                    return self.verify_entry(current, context)
                if current.assessment.status == Status.HEALTHY:
                    return OperationResult(
                        "문제 해결",
                        (StepResult("", entry.path, "현재 상태 확인", "ok", "이미 정상"),),
                    )
        result = self.engine.execute_repair(entry, context, approved=True)
        if entry.assessment.status != Status.UNDER or result.outcome != "완료":
            return result
        try:
            context.set_phase("stage=post_copy_verification write_result=reported_success")
            context.describe(f"복제 후 검증 상태 조회 · /{entry.path}")
            updates, _ = self.vfs.recheck((entry,), context)
            current = updates[entry.key]
            if current is None or not current.assessment.complete:
                raise ValueError("복제 후 항목의 현재 상태를 확인하지 못했습니다.")
            if current.assessment.status == Status.UNVERIFIED:
                verification = self.verify_entry(current, context)
                return replace(
                    result,
                    steps=result.steps + verification.steps,
                    verification=verification.verification,
                )
        except Exception as error:
            cancelled = isinstance(error, BackendError) and error.kind == ErrorKind.CANCELLED
            context.record_failure(error)
            return replace(
                result,
                steps=result.steps
                + (
                    StepResult(
                        "",
                        entry.path,
                        "복제 후 정밀 검증",
                        "cancelled" if cancelled else "error",
                        str(error),
                    ),
                ),
            )
        return result

    def verify_issue(self, entry, context):
        updates, _ = self.vfs.recheck((entry,), context)
        current = updates[entry.key]
        if current is None:
            raise ValueError("항목이 없어졌습니다. 다시 검사하세요.")
        return self.verify_entry(current, context)

    def verify_entry(self, current, context):
        cached = (
            current.assessment.complete
            and current.assessment.status in {Status.HEALTHY, Status.CONFLICT, Status.OVER}
            and all(replica.verified_sha256 and not replica.is_dir for replica in current.replicas)
        )
        if cached:
            results = tuple((replica, replica.verified_sha256) for replica in current.replicas)
            storage = f"저장된 검증 결과 {len(results)}개 재사용. "
            error = ""
        elif current.assessment.status == Status.UNVERIFIED:
            results, stored, error = self.read_verification(current.replicas, context)
            storage = f"재사용할 검증 결과 {stored}/{len(results)}개 저장. "
        else:
            raise ValueError(
                f"상태가 {current.assessment.status.value}(으)로 달라졌습니다. 다시 검사하세요."
            )
        equal = len({digest for _, digest in results}) == 1
        summary = (
            ("선택한 복제본의 SHA-256 일치" if equal else "선택한 복제본의 내용 상이")
            + " · "
            + storage
            + (error + " " if error else "")
            + "재사용할 식별 정보가 없으면 새 검사에서 다시 검증해야 합니다."
        )
        context.describe(summary)
        return OperationResult(
            "정밀 검증",
            tuple(
                StepResult(r.account_id, r.path, "SHA-256", "ok", digest) for r, digest in results
            )
            + (StepResult("", current.path, "검증 결과", "ok", summary),),
            verification=() if cached else results,
        )

    def verify(self, replicas):
        if self._verification_targets or self._replica_busy or self._preparing:
            return
        replicas = tuple(replicas)
        if not replicas or not self.available() or any(r.is_dir for r in replicas):
            return
        try:
            endpoints = tuple(self.engine.endpoint(replica) for replica in replicas)
        except (ValueError, StopIteration) as error:
            message(self, str(error) or "계정 목록이 바뀌었습니다. 목록을 새로고침하세요.")
            return
        if self.endpoints_busy(endpoints):
            return
        self._verification_targets = endpoints
        self.update_command_buttons()

        def finish():
            self._verification_targets = ()
            self.update_command_buttons()

        def failed(error):
            try:
                if not self._closing:
                    message(self, error)
            finally:
                finish()

        cost = sum(r.size or 0 for r in replicas)

        def done(result):
            try:
                if self._closing:
                    finish()
                    return
                results, stored, error = result
                cached = self.vfs.cached(self.path)
                if cached and self.mode == "browse":
                    self.show_snapshot(cached)
                equal = len({digest for _, digest in results}) == 1
                show_text(
                    self,
                    "정밀 검증 결과",
                    ("선택한 복제본의 SHA-256 일치" if equal else "선택한 복제본의 내용 상이")
                    + f"\n재사용할 검증 결과 {stored}/{len(results)}개 저장.\n"
                    + (error + "\n" if error else "")
                    + "재사용할 식별 정보가 없는 파일은 새 검사에서 다시 검증해야 합니다.\n"
                    + "\n".join(
                        f"{self.account_label(r.account_id)}: {digest}" for r, digest in results
                    ),
                )
                self.recheck_issues(
                    {path_key(replica.path) for replica, _ in results},
                    on_finished=finish,
                    proofs=results,
                )
            except Exception as error:
                failed(error)

        jobs = []
        pending = set()
        registration_error = None

        def read_finished(task):
            pending.remove(task.id)
            if pending:
                return
            if registration_error is not None:
                failed(registration_error)
                return
            error = next((job.error for job in jobs if job.state == "실패"), None)
            if error is not None:
                failed(error)
                return
            if any(job.state != "완료" or job.context.cancelled.is_set() for job in jobs):
                finish()
                return
            try:
                reads = tuple(job.result for job in jobs)
                self.workers.submit(lambda context: self.save_verification(reads, context), done, failed)
            except Exception as error:
                failed(error)

        try:
            if (
                self._closing
                or not confirm(
                    self,
                    "정밀 검증",
                    f"{len(replicas)}개 복제본의 실제 바이트 약 {size_text(cost)}를 읽어 SHA-256을 계산합니다.",
                )
                or self._closing
            ):
                finish()
                return
            for index, replica in enumerate(replicas, 1):
                task = self.transfers.submit(
                    f"정밀 검증 · {self.account_label(replica.account_id)} · /{replica.path}",
                    lambda context, replica=replica, index=index: self.read_replica_hash(
                        replica, context, index=index, total=len(replicas)
                    ),
                    queue="other",
                )
                jobs.append(task)
                pending.add(task.id)
                self._verification_reads[task.id] = read_finished
        except Exception as error:
            if pending:
                registration_error = error
            else:
                failed(error)

    def copy_paths(self, move):
        paths = [entry.path for entry in self.selected()]
        if not paths:
            return
        mime = QMimeData()
        mime.setData(INTERNAL_MIME, json.dumps({"paths": paths, "move": move}).encode())
        QApplication.clipboard().setMimeData(mime)

    def paste(self):
        mime = QApplication.clipboard().mimeData()
        payload = bytes(mime.data(INTERNAL_MIME))
        try:
            value = json.loads(payload) if payload else {}
            copy_only = not (isinstance(value, dict) and value.get("move") is True)
        except ValueError:
            return
        self.dropped(
            [url.toLocalFile() for url in mime.urls() if url.isLocalFile()],
            payload,
            self.path,
            copy_only,
        )

    def dropped(self, local, internal, folder, copy_only):
        folder = self.path if folder is None else folder
        if local:
            self.upload_paths(local, folder)
            return
        try:
            paths = json.loads(internal)
            if isinstance(paths, dict):
                paths = paths.get("paths")
            if not isinstance(paths, list) or not paths or not all(isinstance(p, str) for p in paths):
                raise ValueError("잘못된 내부 파일 목록입니다.")
            for path in paths:
                validate_path(path)
        except ValueError, TypeError:
            return

        def build(context):
            plans, sources_by_parent, directories = [], {}, {}
            sources = {path_key(path) for path in paths}
            destinations = {
                path_key("/".join(filter(None, (folder, PurePosixPath(path).name))))
                for path in paths
            }
            all_paths = sources | destinations
            if (
                len(all_paths) != 2 * len(paths)
                or any(parent in all_paths for path in all_paths for parent in self.parent_paths(path))
            ):
                raise ValueError("원본 또는 대상 경로가 겹칩니다.")
            for path in paths:
                parent = path.rpartition("/")[0]
                if parent not in sources_by_parent:
                    snapshot = self.vfs.refresh(parent, context)
                    if not snapshot.complete:
                        raise ValueError("원본 폴더 조회가 완전하지 않습니다.")
                    self.engine.remember_listings(snapshot, directories)
                    sources_by_parent[parent] = {entry.path: entry for entry in snapshot.entries}
                entry = sources_by_parent[parent].get(path)
                if entry is None:
                    raise ValueError(f"원본 항목이 없습니다. /{path}")
                destination = "/".join(filter(None, [folder, entry.name]))
                plans.append(
                    self.engine.plan_move(
                        entry.replicas, destination, context,
                        copy_only=copy_only, directories=directories,
                    )
                )
            return MutationPlan(
                "복사" if copy_only else "이동",
                tuple(c for p in plans for c in p.copies),
                tuple(d for p in plans for d in p.deletions),
                tuple(d for p in plans for d in p.mkdirs),
                tuple(d for p in plans for d in p.rmdirs),
                renames=tuple(item for plan in plans for item in plan.renames),
            )

        self.prepare(build, enqueue=self.enqueue_copy_move)

    def enqueue_copy_move(self, plan):
        files = {}
        for copy in plan.copies:
            files.setdefault(path_key(copy.target.path), []).append(copy)
        action = plan.label
        moving = action == "이동"
        split = bool(plan.mkdirs) or len(files) > 1
        after = ()
        if split:
            check = self.transfers.submit(
                f"{action} · 실행 전 확인",
                lambda context: self.engine.preflight(plan, context),
                queue="other",
            )
            self._pending_plans[check.id] = MutationPlan(check.label)
            after = (check.id,)
        directories = self.enqueue_directories(plan.mkdirs, action, after=after)
        dependencies = [*after, *(task.id for task in directories.values())]
        transfers = []
        for key, copies in files.items():
            target = copies[0].target.path
            label = f"{action} · /{target}"
            parent = next((path for path in self.parent_paths(key) if path in directories), None)

            def rebuild(context, copies=copies):
                plans = tuple(
                    self.engine.plan_move(
                        (copy.source.version,), copy.target.path, context, copy_only=not moving
                    )
                    for copy in copies
                )
                if any(len(item.copies) != 1 for item in plans):
                    raise ValueError("원본이 파일에서 폴더로 변경되었습니다.")
                return MutationPlan(
                    action,
                    copies=tuple(item.copies[0] for item in plans),
                    deletions=tuple(source for item in plans for source in item.deletions),
                )

            task = self.enqueue_plan(
                MutationPlan(label, copies=tuple(copies), precise=moving)
                if split else replace(plan, label=label),
                rebuild,
                after=(directories[parent].id,) if parent else after,
            )
            dependencies.append(task.id)
            transfers.append(task)

        if moving and split:
            deletion = MutationPlan(
                "이동 · 원본 삭제",
                deletions=plan.deletions,
                rmdirs=plan.rmdirs,
                move_directories=plan.mkdirs,
            )

            def delete_originals(context):
                verified = dict(pair for task in transfers for pair in task.result.copied_targets)
                if any(copy.target not in verified for copy in plan.copies):
                    raise ValueError("이동 대상의 검증 결과를 확인할 수 없습니다.")
                targets = tuple(
                    ExpectedItem(copy.target, verified[copy.target]) for copy in plan.copies
                )
                return self.engine.execute(
                    replace(deletion, move_targets=targets), context, approved=True
                )

            cleanup = self.enqueue_plan(
                deletion, None, after=tuple(dependencies), execute=delete_originals
            )
            # Keep copied destinations protected until deleting the originals has finished.
            self._pending_plans[cleanup.id] = plan

    def scan(self):
        self.start_scan(self.path)

    def start_scan(self, path, *, title="현재 폴더 검사"):
        if self._scan_busy:
            return
        if not self.available():
            return
        self._scan_busy = True
        self.update_command_buttons()
        task = None
        try:
            if (
                not confirm(
                    self,
                    title,
                    f"사용 중인 계정의 /{path} 폴더와 모든 하위 폴더를 조회합니다. 내용을 자동 다운로드하거나 파일을 변경하지 않습니다.",
                )
                or self._closing
            ):
                return
            self._scan_run = FolderScan(path)
            task = self.enqueue_scan_folder(path)
            self._scan_task = task.id
            logger.info("scan scope task=%s path=%s", task.id, diagnostic_ref(path_key(path)))
        except Exception as error:
            logger.warning("scan request failed error=%s", error_summary(error))
            if not self._closing:
                message(self, error)
        finally:
            if task is None:
                self._scan_busy = False
                self._scan_run = None
            self.update_command_buttons()

    def enqueue_scan_folder(self, path):
        def scan(context):
            context.describe(f"폴더 조회 · /{path}")
            result = self.vfs.scan_folder(path, context)
            context.describe(f"{result[2]:,}개 검사 · {len(result[0]):,}개 문제 · /{path}")
            return result

        task = self.transfers.submit(f"폴더 검사 · /{path} (읽기 전용)", scan, queue="scan")
        self._scan_run.pending.add(task.id)
        self._scan_run.seen.add(path_key(path))
        return task

    def finish_scan_folder(self, task):
        run = self._scan_run
        run.pending.remove(task.id)
        if task.state == "완료" and not task.context.cancelled.is_set() and task.result is not None:
            issues, complete, count, children = task.result
            run.has_results = True
            run.complete &= complete
            run.checked += count
            run.issues.update((entry.key, entry) for entry in issues)
            try:
                for path in children:
                    if path_key(path) not in run.seen:
                        self.enqueue_scan_folder(path)
            except Exception as error:
                run.interrupted = True
                message(self, error)
        else:
            run.interrupted = True
        if run.pending:
            return

        self._scan_run = None
        self._scan_busy = False
        if not run.has_results:
            return
        self.scan_path = run.path
        self.scan_results = issues = tuple(run.issues[key] for key in sorted(run.issues))
        self.scan_complete = complete = run.complete and not run.interrupted
        logger.info(
            "scan result task=%s checked=%d issues=%d complete=%s",
            self._scan_task, run.checked, len(issues), complete,
        )
        self.resolve_action.setEnabled(True)
        if self.mode == "issues":
            self.file_model.set_entries(issues)
        state = "완료" if complete else "미완료"
        self.banner.setText(
            f"폴더 검사 {state} · /{run.path} · {run.checked:,}개 검사 · {len(issues):,}개 문제"
        )
        if issues and not run.interrupted:
            self.show_issues()
        elif self.issue_dialog is not None:
            self.issue_dialog.set_results(self.scan_path, issues, complete)

    def task_row_visible(self, task_id):
        row = self._task_rows.get(task_id)
        if row is None or not self.task_tree.isVisible():
            return False
        rect = self.task_tree.visualItemRect(row)
        return (
            rect.height() > 0
            and rect.top() < self.task_tree.viewport().height()
            and rect.bottom() >= 0
        )

    def task_state_changed(self, row, column):
        if column != 1 or self._closing:
            return
        task_id = row.data(0, Qt.ItemDataRole.UserRole)
        if task_id not in self._task_rows:
            return
        state = row.text(1)
        active = state.endswith("진행 중") or state in {"취소 요청", "결과 재확인"}
        if active == (task_id in self._active_task_rows):
            if (
                self._task_scroll_anchor is not None
                and not self._active_task_rows
                and not self.transfers.busy
            ):
                self._task_scroll_anchor = None
            return
        if self._active_task_rows:
            self._task_scroll_anchor = next(
                (key for key in self._active_task_rows if self.task_row_visible(key)), None
            )
        if active:
            self._active_task_rows[task_id] = row
            if self.task_row_visible(task_id):
                self._task_scroll_anchor = task_id
        else:
            self._active_task_rows.pop(task_id, None)
        if not self._task_scroll_pending:
            self._task_scroll_pending = True
            # Coalesce completion -> recheck changes before choosing the row to follow.
            QTimer.singleShot(0, self.follow_task_progress)

    def follow_task_progress(self):
        self._task_scroll_pending = False
        if self._closing or self._shutdown_done:
            return
        if not self.task_row_visible(self._task_scroll_anchor):
            self._task_scroll_anchor = None
            return
        task_id = next(reversed(self._active_task_rows), None)
        if task_id is None:
            if not self.transfers.busy:
                self._task_scroll_anchor = None
            return
        if not self.task_row_visible(task_id):
            horizontal = self.task_tree.horizontalScrollBar().value()
            self.task_tree.scrollToItem(
                self._active_task_rows[task_id], QAbstractItemView.ScrollHint.EnsureVisible
            )
            self.task_tree.horizontalScrollBar().setValue(horizontal)
        self._task_scroll_anchor = task_id

    def update_task(self, task):
        if (
            self._closing
            or task.id in self._reported_tasks
            or self.transfers.tasks.get(task.id) is not task
        ):
            return
        self.update_capacity_label()
        row = self._task_rows.get(task.id)
        if row is None:
            row = QTreeWidgetItem([task.label])
            row.setToolTip(0, task.label)
            row.setData(0, Qt.ItemDataRole.UserRole, task.id)
            self.task_tree.addTopLevelItem(row)
            self._task_rows[task.id] = row
        state_changed = row.data(1, Qt.ItemDataRole.UserRole) != task.state
        row.setData(1, Qt.ItemDataRole.UserRole, task.state)
        row.setText(1, task.state)
        total = task.stats.get("totalBytes") or 0
        if total > 0 and task.state == "진행 중":
            row.setText(
                1, f"{min(100, task.stats.get('bytes', 0) * 100 / total):.0f}% · {task.state}"
            )
        detail = task.stats.get("detail", "")
        if task.id in self._delete_targets and task.error:
            detail = " · ".join(filter(None, ("삭제 0개 완료 · 중단", detail, task.error)))
        elif task.state in {"실패", "실패 / 결과 확인 필요", "일부 성공", "완료"}:
            steps = task.result.steps if isinstance(task.result, OperationResult) else ()
            problem = next(
                (step.message for step in reversed(steps) if step.state in {"error", "rejected"}), ""
            ) or task.error
            if problem and problem not in detail:
                detail = " · ".join(filter(None, (problem, detail)))
            elif task.state == "완료":
                fallback = next(
                    (step for step in reversed(steps) if step.action in {"대체 업로드", "전송 결과 확인"}),
                    None,
                )
                if fallback is not None:
                    label = self.account_label(fallback.account_id)
                    detail = (
                        f"{fallback.action} 완료 · {label} · {fallback.message} · /{fallback.path}"
                    )
        if "bytes" in task.stats and (total > 0 or task.stats["bytes"] > 0):
            amount = f"{size_text(task.stats['bytes'])} / {size_text(task.stats.get('totalBytes'))}"
            detail = f"{detail} · {amount}" if detail else amount
        row.setText(2, detail)
        row.setToolTip(2, detail)
        row.setText(3, size_text(task.stats.get("speed", 0)) + "/s")
        if task.id in self._repair_tasks:
            self.show_task_progress(row, row.text(1), detail)
        if (
            task.state not in {"대기", "진행 중", "취소 요청"}
            and task.id not in self._reported_tasks
        ):
            self._reported_tasks.add(task.id)
            logger.info("task result received task=%s state=%s", task.id, task.state)
            restore_key = self._restore_tasks.pop(task.id, None)
            if restore_key is not None:
                self._restore_pending.discard(restore_key)
            if self._scan_run is not None and task.id in self._scan_run.pending:
                self._finished_tasks.add(task.id)
                self.finish_scan_folder(task)
                self.update_command_buttons()
                return
            verification = self._verification_reads.pop(task.id, None)
            if verification is not None:
                self._finished_tasks.add(task.id)
                verification(task)
                self.update_command_buttons()
                return
            keys = set(self._task_retries.get(task.id, (None, False, ()))[2])
            keys.update(path_key(path) for path in getattr(task.result, "affected_paths", ()))
            refresh_browse = False

            def release():
                self._pending_plans.pop(task.id, None)
                self._pending_repairs.pop(task.id, None)
                self._delete_targets.pop(task.id, None)
                self._finished_tasks.add(task.id)
                if task.id in self._repair_tasks:
                    self._repair_tasks.remove(task.id)
                    if not self._releasing_task_rechecks:
                        self.refresh_repair_progress()
                if task.id == self._replica_task_id:
                    self.set_replica_busy(False)
                else:
                    self.update_command_buttons()
                logger.info("task ui released task=%s", task.id)

            # For a registered plan, these keys identify its remote write destinations.
            if self.vfs and (keys or task.id not in self._pending_plans):
                current = self.path if self.mode == "browse" else None
                refresh_browse = self.vfs.mark_changed(keys, current=current)
            if (
                task.state in {"완료", "일부 성공"}
                and not task.context.cancelled.is_set()
                and not self._closing
            ):
                if task.id == self._replica_task_id and task.id in self._delete_targets:
                    self.set_replica_busy(False)
                self.recheck_issues(
                    keys,
                    task_id=task.id,
                    on_finished=release,
                    proofs=getattr(task.result, "verification", ()),
                    refresh_browse=refresh_browse,
                )
            else:
                if refresh_browse:
                    self.queue_task_refresh(browse=True)
                release()
            self.update_capacity_label()
        if state_changed and not self._preparing and None not in self._delete_targets:
            self.update_command_buttons()

    def task_removal_available(self):
        # Later repair batches can still submit dependents by task ID.
        return not self._closing and not (self._preparing and self._repair_busy)

    def finished_tasks_with_states(self, states):
        for task_id in self._finished_tasks:
            if self.transfers.tasks[task_id].state in states:
                yield task_id

    def task_context_menu(self, point=None):
        tree = self.task_tree
        row = tree.currentItem() if point is None else tree.itemAt(point)
        if row is None or self._closing:
            return
        task = self.transfers.tasks.get(row.data(0, Qt.ItemDataRole.UserRole))
        if task is None:
            return
        if point is None:
            tree.scrollToItem(row)
            point = tree.visualItemRect(row).intersected(tree.viewport().rect()).center()
        menu = QMenu(tree)
        cancel = menu.addAction("작업 취소")
        cancel.setEnabled(task.state in {"대기", "진행 중"})
        retry = menu.addAction("최신 상태로 재시도...")
        retry.setEnabled(self.task_retry_available(task))
        details = menu.addAction("작업 결과")
        remove = menu.addAction("이 작업 지우기")
        remove.setEnabled(task.id in self._finished_tasks and self.task_removal_available())
        chosen = menu.exec(tree.viewport().mapToGlobal(point))
        menu.deleteLater()
        if self._closing or self.transfers.tasks.get(task.id) is not task:
            return
        if chosen == cancel:
            self.cancel_task(task)
        elif chosen == retry:
            self.retry_task(task)
        elif chosen == details:
            self.task_details(task)
        elif chosen == remove:
            self.remove_finished_tasks((task.id,))

    def remove_finished_tasks(self, task_ids):
        if not self.task_removal_available():
            return
        removed = self.transfers.remove_finished(self._finished_tasks.intersection(task_ids))
        if not removed:
            return
        self._finished_tasks.difference_update(removed)
        self._reported_tasks.difference_update(removed)
        if self._task_scroll_anchor in removed:
            self._task_scroll_anchor = None
        indexes = []
        for task_id in removed:
            self._task_retries.pop(task_id, None)
            row = self._task_rows.pop(task_id)
            indexes.append(self.task_tree.indexOfTopLevelItem(row))
        for index in sorted(indexes, reverse=True):
            self.task_tree.takeTopLevelItem(index)
        self.update_command_buttons()

    def cancel_task(self, task):
        if not self._closing and task.state in {"대기", "진행 중"}:
            self.transfers.cancel(task.id)

    def task_retry_available(self, task):
        retry = self._task_retries.get(task.id)
        return (
            retry is not None
            and retry[0] is not None
            and not self._closing
            and not self._preparing
            and task.state not in {"대기", "진행 중", "취소 요청"}
            and not (self._repair_busy and retry[1])
            and not self.paths_overlap(retry[2], self.busy_path_keys())
        )

    def retry_task(self, task):
        if self.task_retry_available(task):
            retry, _, _ = self._task_retries[task.id]
            retry()

    def task_details(self, task):
        lines = [task.label, task.state, task.error]
        if task.action == "정밀 검증" and isinstance(task.result, tuple):
            lines.append(f"SHA-256 · {task.result[1]}")
        if hasattr(task.result, "steps"):
            states = {
                "ok": "완료", "skipped": "건너뜀", "rejected": "거부", "error": "실패",
                "cancelled": "취소됨",
            }
            lines.extend(
                f"{step.action} · {states.get(step.state, step.state)} · "
                f"{self.account_label(step.account_id)}:/{step.path} {step.message}"
                for step in task.result.steps
            )
        show_text(self, "작업 결과", "\n".join(lines))

    def configuration_available(self, *, notify=True):
        if self._closing:
            return False
        available = not (
            self._repair_busy
            or self._explorer_drag
            or self._pending_plans
            or self._delete_targets
            or self._verification_targets
            or self._task_browse_request is not None
            or self.transfers.busy
            or any(task_id not in self._reported_tasks for task_id in self.transfers.tasks)
            or self.workers.configuration_pending
        )
        if notify and not available:
            message(self, "진행 중인 작업이 끝난 뒤 계정과 설정을 변경할 수 있습니다.")
        return available

    def accounts_dialog(self):
        if self._closing or self._account_dialog is not None or not self.accounts_action.isEnabled():
            return
        dialog = self._account_dialog = AccountDialog(
            self.account_manager,
            self,
            changes_available=lambda: self.configuration_available(notify=False),
        )
        self._accounts_dirty = False
        dialog.changed.connect(lambda: setattr(self, "_accounts_dirty", True))
        try:
            dialog.exec()
        finally:
            self._account_dialog = None
            dialog.deleteLater()
        if self._closing:
            return
        if self._accounts_dirty:
            self.accounts_changed()
        else:
            self.update_capacity_label()

    def accounts_changed(self):
        self.vfs.accounts = list(self.settings.accounts)
        self.vfs.capabilities.clear()
        self.engine.accounts = list(self.settings.accounts)
        self.vfs.cache.invalidate()
        self.tree_model.reset_accounts(self.settings.accounts)
        self.file_model.labels = {a.id: a.label for a in self.settings.accounts}
        self.scope = ""
        self.navigate("")
        self.refresh_capacity(force=False)

    def show_licenses(self):
        LicensesDialog(self).exec()

    def update_configuration_buttons(self):
        if self._account_dialog is not None:
            self._account_dialog.update_account_buttons()
        self.update_settings_save()

    def settings_save_available(self, dialog, *, notify=False):
        return not self._closing and (
            not dialog.requires_idle() or self.configuration_available(notify=notify)
        )

    def update_settings_save(self, *args):
        if self._settings_dialog is not None:
            self._settings_dialog.set_save_available(
                self.settings_save_available(self._settings_dialog)
            )

    def settings_dialog(self):
        if self._closing or self._settings_dialog is not None:
            return
        dialog = self._settings_dialog = SettingsDialog(self.settings, self)
        dialog.transfers.valueChanged.connect(self.update_settings_save)
        dialog.config.textChanged.connect(self.update_settings_save)
        try:
            self.update_settings_save()
            if dialog.exec() != QDialog.DialogCode.Accepted or not self.settings_save_available(
                dialog, notify=True
            ):
                return
            updated = dialog.value()
            self.store.save(updated)
        except (ValueError, OSError) as error:
            message(self, error)
            return
        finally:
            self._settings_dialog = None
            dialog.deleteLater()
        previous, self.settings = self.settings, updated
        if self.account_manager:
            self.account_manager.settings = updated
        if self.account_manager and updated.rclone_config != previous.rclone_config:
            active_config = str(self.store.config_path) if self.store.external_config else ""
            self.accounts_action.setEnabled(updated.rclone_config == active_config)
            if updated.rclone_config != active_config:
                self.banner.setText(
                    "rclone config 변경은 재시작 후 적용됩니다. "
                    "재시작 후 계정 관리에서 사용할 리모트를 선택하세요."
                )
        if updated.transfers != previous.transfers:
            self.transfers.resize_idle_transfer_pool(updated.transfers)
        self.transfers.retries = updated.retries
        if self.vfs:
            self.vfs.cache.ttl = updated.cache_ttl
        logger.setLevel(updated.log_level)

    def open_trash(self, account_id):
        if account_id in self._trash_loading:
            return
        if not self.available():
            return
        account = next(a for a in self.settings.accounts if a.id == account_id)
        cap = trash_capabilities(account)
        if not cap.list_items:
            if cap.web_url:
                QDesktopServices.openUrl(QUrl(cap.web_url))
            else:
                message(self, "이 계정은 휴지통 조회를 지원하지 않습니다.")
            return
        adapter = GoogleTrashAdapter(self.backend, account)
        self._trash_loading.add(account_id)

        def done(items):
            try:
                if not self._closing:
                    self.trash_dialog(adapter, items, cap)
            finally:
                self._trash_loading.discard(account_id)

        def failed(error):
            try:
                if not self._closing:
                    message(self, error)
            finally:
                self._trash_loading.discard(account_id)

        try:
            self.workers.submit(lambda context: adapter.list_items(context), done, failed)
        except Exception as error:
            failed(error)

    def trash_dialog(self, adapter, items, cap):
        dialog = QDialog(self)
        dialog.setWindowTitle("휴지통 · " + adapter.account.label)
        dialog.resize(850, 460)
        layout = QVBoxLayout(dialog)
        table = QTableWidget(len(items), 3)
        table.setWordWrap(False)
        table.setHorizontalHeaderLabels(["이름", "원래 경로", "삭제 시각"])
        table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        for row, item in enumerate(items):
            for column, value in enumerate(
                (item.name, item.original_path or "알 수 없음", item.deleted_at or "알 수 없음")
            ):
                table.setItem(row, column, QTableWidgetItem(value))
        table.horizontalHeader().setStretchLastSection(True)
        table.resizeColumnsToContents()
        layout.addWidget(table)
        buttons = QHBoxLayout()
        restore = QPushButton("선택 항목 복원...")

        def update_restore(*args):
            row = table.currentRow()
            restore.setEnabled(
                cap.restore_item
                and row >= 0
                and (adapter.account.id, items[row].id) not in self._restore_pending
            )

        table.itemSelectionChanged.connect(update_restore)
        update_restore()

        def run_restore():
            row = table.currentRow()
            if row < 0 or not cap.restore_item or self._closing:
                return
            item = items[row]
            key = (adapter.account.id, item.id)
            if key in self._restore_pending:
                return
            self._restore_pending.add(key)
            update_restore()
            task = None
            try:
                if (
                    not confirm(
                        dialog,
                        "선택 항목 복원",
                        f"{item.original_path or item.name}\n충돌이 있으면 복원하지 않습니다.",
                    )
                    or self._closing
                ):
                    return
                task = self.transfers.submit(
                    "선택 항목 복원",
                    lambda context: adapter.restore(item, context, approved=True),
                    queue="other",
                )
                self._restore_tasks[task.id] = key
                dialog.accept()
            except Exception as error:
                if not self._closing:
                    message(dialog, error)
            finally:
                if task is None:
                    self._restore_pending.discard(key)
                    update_restore()

        restore.clicked.connect(run_restore)
        web = QPushButton("웹 휴지통")
        web.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(cap.web_url)))
        buttons.addWidget(restore)
        buttons.addWidget(web)
        layout.addLayout(buttons)
        self.task_changed.connect(update_restore)
        try:
            dialog.exec()
        finally:
            self.task_changed.disconnect(update_restore)

    def closeEvent(self, event):
        if self._shutdown_done:
            event.accept()
            return
        event.ignore()
        if not self._closing:
            if (self.transfers.busy or self.workers.pending) and not confirm(
                self,
                "프로그램 종료",
                "진행 중인 작업을 취소하고 종료 완료를 기다립니다. 원격 부분 결과는 보존합니다.",
            ):
                return
            self._closing = True
            self.update_command_buttons()
            self.transfers.cancel_all()
            self.workers.cancel_all()
        QTimer.singleShot(50, self.finish_close)

    def finish_close(self):
        if self._shutdown_done or self._shutdown_started:
            return
        if self.transfers.busy or self.workers.pending or self._explorer_drag:
            self.statusBar().showMessage("진행 중인 작업이 끝나기를 기다리는 중…")
            QTimer.singleShot(100, self.finish_close)
            return
        self._shutdown_started = True
        self.transfers.close()
        if self.vfs:
            self.vfs.close()
        try:
            self.store.save(self.settings)
        except (ValueError, OSError) as error:
            message(self, error)

        def done(success):
            if self._shutdown_done:
                return
            if success:
                self._shutdown_done = True
                self.close()
                return
            if confirm(
                self,
                "종료 지연",
                "원격 작업의 취소 완료를 확인하지 못했습니다.\n확인을 누르면 더 기다립니다. 취소를 누르면 정리 없이 종료합니다.",
            ):
                self._shutdown_started = False
                QTimer.singleShot(1000, self.finish_close)
            else:
                self._shutdown_done = True
                self.close()

        if self.backend:
            self.workers.submit(
                lambda context: self.backend.shutdown(), done, lambda error: done(False)
            )
        else:
            done(True)
