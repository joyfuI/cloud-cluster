from threading import Event

import pytest
from PySide6.QtCore import Qt

from cloud_cluster.domain import EntryAssessment, LogicalEntry, Replica, Status
from cloud_cluster.gui import window as window_module
from cloud_cluster.gui.dialogs import IssueDialog, ReplicaDialog
from cloud_cluster.gui.window import FolderScan, MainWindow
from cloud_cluster.settings import Settings, SettingsStore
from cloud_cluster.transfers import TaskContext, TransferTask


@pytest.mark.parametrize("path, issue_count, count", [("", 2, 4), ("folder", 0, 1)])
def test_folder_scan_keeps_start_path_and_shows_empty_results(
    qtbot, monkeypatch, backend, accounts, tmp_path, path, issue_count, count
):
    for account in accounts[:2]:
        backend.put(account.fs, "folder/same", b"same")
    backend.put(accounts[0].fs, "outside/issue", b"only copy")
    window = MainWindow(backend, Settings(accounts=accounts[:2]), SettingsStore(tmp_path / "ui"))
    qtbot.addWidget(window)
    window.show()
    try:
        qtbot.waitUntil(lambda: window.snapshot is not None and not window.workers.pending)
        window.navigate(path)
        qtbot.waitUntil(lambda: not window.workers.pending)
        prompts, queued = [], []

        def confirm(parent, title, text):
            prompts.append((title, text))
            return True

        def submit(label, operation, *, queue):
            assert queue == "scan"
            task = TransferTask(str(len(queued)), label, TaskContext())
            window.transfers.tasks[task.id] = task
            queued.append((task, operation))
            return task

        monkeypatch.setattr(window_module, "confirm", confirm)
        monkeypatch.setattr(window.transfers, "submit", submit)
        assert window.scan_action.text() == "현재 폴더 검사..."
        window.scan_action.trigger()
        assert prompts[0][0] == "현재 폴더 검사"
        assert f"/{path} 폴더와 모든 하위 폴더" in prompts[0][1]

        window.navigate("outside")
        qtbot.waitUntil(lambda: not window.workers.pending)
        issues_index = window.tree_model.index(2, 0)
        window.tree_clicked(issues_index)
        assert len(window.file_model.entries) == 1
        task, operation = queued[0]
        assert task.label == f"폴더 검사 · /{path} (읽기 전용)"
        for task, operation in queued:
            task.result, task.state = operation(task.context), "완료"
            window.update_task(task)
        assert window.scan_path == path
        assert window.scan_complete and sum(task.result[2] for task, _ in queued) == count
        assert len(queued) == (1 if path else 3)
        assert len(window.file_model.entries) == issue_count
        assert "폴더 검사 완료" in window.banner.text()
        assert window.resolve_action.isEnabled()
        if issue_count:
            assert window.issue_dialog.isVisible()
            assert window.issue_dialog.path == path
            assert len(window.issue_dialog.entries) == issue_count
            window.issue_dialog.close()
            window.update_task(task)
            assert not window.issue_dialog.isVisible()
        else:
            assert window.issue_dialog is None

        window.tree_clicked(issues_index)
        assert len(window.file_model.entries) == issue_count
        assert window.banner.text() == f"폴더 검사 결과 · /{path}"
    finally:
        qtbot.waitUntil(lambda: not window.workers.pending)
        window._shutdown_done = True
        window.vfs.close()
        window.transfers.close()
        window.close()


def test_scan_and_folder_creation_run_while_transfer_queue_is_full(
    qtbot, monkeypatch, backend, accounts, tmp_path
):
    settings = Settings(accounts=accounts[:2], transfers=1)
    window = MainWindow(backend, settings, SettingsStore(tmp_path / "ui"))
    qtbot.addWidget(window)
    release_first, release_all, scanning = Event(), Event(), Event()

    def scan(path, context):
        scanning.set()
        while not release_all.wait(0.01):
            context.check()
        return (), True, 0, ()

    monkeypatch.setattr(window.vfs, "scan_folder", scan)
    monkeypatch.setattr(window_module, "confirm", lambda *args: True)
    try:
        qtbot.waitUntil(lambda: not window.workers.pending)
        first = window.transfers.submit("전송", lambda context: release_first.wait(10))
        second = window.transfers.submit("다음 전송", lambda context: release_all.wait(10))
        window.start_scan("")
        scan_task = window.transfers.tasks[window._scan_task]
        plan = window.engine.plan_mkdir("new-folder")
        mkdir = window.enqueue_plan(plan, lambda context: plan)

        qtbot.waitUntil(lambda: scanning.is_set() and mkdir.id in window._reported_tasks)
        assert mkdir.state == "완료" and scan_task.state == "진행 중"
        assert first.state == "진행 중" and second.state == "대기"

        release_first.set()
        qtbot.waitUntil(lambda: second.state == "진행 중")
        assert scan_task.state == "진행 중"

        window.cancel_task(scan_task)
        qtbot.waitUntil(lambda: not window._scan_busy)
        assert scan_task.state == "취소됨" and window.scan_path is None
        assert first.state == "완료" and second.state == "진행 중"

        release_all.set()
        qtbot.waitUntil(lambda: second.id in window._reported_tasks)
        assert second.state == "완료"
    finally:
        release_first.set()
        release_all.set()
        window._shutdown_done = True
        window.transfers.close()
        qtbot.waitUntil(lambda: not window.workers.pending)
        window.vfs.close()
        window.close()


def test_sibling_scan_jobs_overlap_and_cancel_independently(qtbot, monkeypatch, backend, accounts, tmp_path):
    window = MainWindow(backend, Settings(accounts=accounts), SettingsStore(tmp_path / "ui"))
    qtbot.addWidget(window)
    release = Event()
    started = {path: Event() for path in ("left", "right")}

    def scan(path, context):
        if not path:
            return (), True, 2, tuple(started)
        started[path].set()
        assert release.wait(10)
        context.check()
        return (), True, 0, ()

    monkeypatch.setattr(window.vfs, "scan_folder", scan)
    monkeypatch.setattr(window_module, "confirm", lambda *args: True)
    try:
        window.start_scan("")
        qtbot.waitUntil(lambda: all(event.is_set() for event in started.values()))
        _, left, right = window.transfers.tasks.values()
        window.cancel_task(left)
        release.set()
        qtbot.waitUntil(lambda: not window._scan_busy)
        assert left.state == "취소됨" and right.state == "완료"
        assert window.scan_path == "" and not window.scan_complete
    finally:
        release.set()
        qtbot.waitUntil(lambda: not window.transfers.busy and not window.workers.pending)
        window._shutdown_done = True
        window.vfs.close()
        window.transfers.close()
        window.close()


@pytest.mark.parametrize(
    "status, repairable",
    [
        (Status.DIRECTORY, True),
        (Status.UNDER, True),
        (Status.CONFLICT, False),
        (Status.OVER, False),
        (Status.UNKNOWN, False),
        (Status.UNVERIFIED, True),
    ],
)
def test_issue_dialog_offers_the_action_for_selected_status(qtbot, status, repairable):
    entry = LogicalEntry(
        "file", "file", (Replica("a", "file"),), EntryAssessment(status, (), 1, True)
    )
    dialog = IssueDialog()
    qtbot.addWidget(dialog)
    dialog.set_results("original", (entry,), False)
    dialog.show()
    assert dialog.resolve_button.text() == "선택 문제 해결"
    assert dialog.resolve_button.isEnabled() == repairable
    assert dialog.manage_button.isEnabled()
    assert "미완료" in dialog.heading.text()
    if repairable:
        with qtbot.waitSignal(dialog.action_requested) as signal:
            qtbot.mouseClick(dialog.resolve_button, Qt.MouseButton.LeftButton)
        assert signal.args == [(entry,)] and dialog.isVisible()
    with qtbot.waitSignal(dialog.management_requested) as signal:
        qtbot.mouseClick(dialog.manage_button, Qt.MouseButton.LeftButton)
    assert signal.args == [(entry,)]
    with qtbot.waitSignal(dialog.rescan_requested) as signal:
        qtbot.mouseClick(dialog.rescan_button, Qt.MouseButton.LeftButton)
    assert signal.args == ["original"]
    dialog.set_results("original", (), True)
    assert not dialog.resolve_button.isEnabled() and "0개 문제" in dialog.heading.text()
    assert not dialog.manage_button.isEnabled()


@pytest.mark.parametrize(
    "status", [Status.CONFLICT, Status.OVER, Status.UNVERIFIED, Status.UNKNOWN]
)
def test_manual_issues_open_replica_management(qtbot, monkeypatch, tmp_path, status):
    window = MainWindow(None, Settings(), SettingsStore(tmp_path / "ui"))
    qtbot.addWidget(window)
    entry = LogicalEntry(
        "file", "file", (Replica("a", "file"),), EntryAssessment(status, (), 1, True)
    )
    opened = []
    monkeypatch.setattr(window, "manage_replicas", opened.append)
    window.scan_path, window.scan_results = "", (entry,)
    window.show_issues()
    qtbot.mouseClick(window.issue_dialog.manage_button, Qt.MouseButton.LeftButton)
    assert opened == [(entry,)]
    qtbot.waitUntil(lambda: not window.workers.pending)
    window._shutdown_done = True
    window.transfers.close()
    window.close()


@pytest.mark.parametrize("reason", ["cancelled", "failed", "closing"])
def test_scan_does_not_open_issue_dialog_after_cancellation_failure_or_close(
    qtbot, tmp_path, reason
):
    window = MainWindow(None, Settings(), SettingsStore(tmp_path / "ui"))
    qtbot.addWidget(window)
    entry = LogicalEntry(
        "file", "file", (Replica("a", "file"),), EntryAssessment(Status.UNDER, (), 1, True)
    )
    task = TransferTask("scan", "scan", TaskContext(), state="완료", result=((entry,), True, 1, ()))
    window.transfers.tasks[task.id] = task
    window._scan_task = task.id
    window._scan_run = FolderScan("", pending={task.id})
    if reason == "cancelled":
        task.context.cancelled.set()
    elif reason == "failed":
        task.state = "실패"
    else:
        window._closing = True
    window.update_task(task)
    assert window.issue_dialog is None
    qtbot.waitUntil(lambda: not window.workers.pending)
    window._shutdown_done = True
    window.transfers.close()
    window.close()


def test_issue_dialog_repairs_then_rescans_original_scope(
    qtbot, monkeypatch, backend, accounts, tmp_path
):
    a, b = accounts[:2]
    backend.put(a.fs, "folder/file", b"keep")
    window = MainWindow(backend, Settings(accounts=[a, b]), SettingsStore(tmp_path / "ui"))
    qtbot.addWidget(window)
    window.show()
    prompts = []

    def confirm(parent, title, text):
        prompts.append((title, text))
        return True

    monkeypatch.setattr(window_module, "confirm", confirm)

    def finished():
        return (
            not window.workers.pending
            and not window.transfers.busy
            and all(task.id in window._reported_tasks for task in window.transfers.tasks.values())
        )

    try:
        qtbot.waitUntil(finished)
        before = dict(backend.nodes)
        window.scan()
        qtbot.waitUntil(lambda: window.issue_dialog is not None)
        qtbot.waitUntil(finished)
        assert backend.nodes == before
        dialog = window.issue_dialog
        assert len(dialog.entries) == 2
        dialog.table.selectAll()
        qtbot.mouseClick(dialog.resolve_button, Qt.MouseButton.LeftButton)
        qtbot.waitUntil(finished)
        assert not window._repair_busy and not dialog.resolve_button.isEnabled()
        assert dialog.entries == window.scan_results == ()
        assert "0개 문제" in dialog.heading.text() and dialog.isVisible()
        assert backend.nodes[b.fs, "folder/file"].data == b"keep"
        assert [title for title, _ in prompts] == ["현재 폴더 검사"]
        assert len(window.transfers.tasks) == 4
        assert any(task.label.endswith("/folder/file") for task in window.transfers.tasks.values())

        window.navigate("outside")
        qtbot.waitUntil(finished)
        qtbot.mouseClick(dialog.rescan_button, Qt.MouseButton.LeftButton)
        qtbot.waitUntil(finished)
        assert window.path == "outside" and window.scan_path == ""
        assert dialog.isVisible() and dialog.entries == ()
        assert "0개 문제" in dialog.heading.text()
        assert window.scan_results == ()
        assert prompts[-1][0] == "다시 검사"
        dialog.close()
        window.resolve_action.trigger()
        assert dialog.isVisible()
    finally:
        qtbot.waitUntil(finished)
        window._shutdown_done = True
        window.vfs.close()
        window.transfers.close()
        window.close()


def test_issue_multiselection_separates_bulk_repairs_and_manual_review(qtbot):
    statuses = [Status.DIRECTORY, Status.UNDER, Status.CONFLICT, Status.OVER, Status.UNVERIFIED]
    entries = tuple(
        LogicalEntry(str(i), str(i), (Replica("a", str(i)),), EntryAssessment(status, (), 1, True))
        for i, status in enumerate(statuses)
    )
    dialog = IssueDialog()
    qtbot.addWidget(dialog)
    dialog.set_results("", entries, True)
    dialog.show()
    index = dialog.table.model().index(2, 0)
    qtbot.mouseClick(
        dialog.table.viewport(),
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.ControlModifier,
        dialog.table.visualRect(index).center(),
    )
    assert dialog.selected_entries() == (entries[0], entries[2])
    qtbot.keyClick(dialog.table, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
    assert dialog.selected_entries() == entries
    assert dialog.selection_summary.text() == (
        "5개 선택 · 폴더 생성 1개 · 복제 1개 · 정밀 검증 1개 · 개별 확인 2개 · 검증 읽기 용량 미확인"
    )
    with qtbot.waitSignal(dialog.action_requested) as signal:
        qtbot.mouseClick(dialog.resolve_button, Qt.MouseButton.LeftButton)
    assert signal.args == [(*entries[:2], entries[4])]
    with qtbot.waitSignal(dialog.management_requested) as signal:
        qtbot.mouseClick(dialog.manage_button, Qt.MouseButton.LeftButton)
    assert signal.args == [entries]
    dialog.table.clearSelection()
    assert not dialog.resolve_button.isEnabled() and not dialog.manage_button.isEnabled()
    assert "0개 선택" in dialog.selection_summary.text()
    dialog.table.selectRow(2)
    assert not dialog.resolve_button.isEnabled() and dialog.manage_button.isEnabled()


def test_replica_management_switches_selected_items_and_stays_open_after_actions(qtbot, accounts):
    replicas = [Replica("a", "first", size=1), Replica("b", "second", size=2)]
    entries = tuple(
        LogicalEntry(r.path, r.path, (r,), EntryAssessment(Status.CONFLICT, (), 1, True))
        for r in replicas
    )
    dialog = ReplicaDialog(entries, accounts)
    qtbot.addWidget(dialog)
    dialog.show()
    assert dialog.entry_selector.isVisible() and dialog.entry_selector.count() == 2
    dialog.entry_selector.setCurrentIndex(1)
    assert dialog.windowTitle() == "복제본 관리 · second"
    assert dialog.table.item(0, 1).text() == "second"
    with qtbot.waitSignal(dialog.action_requested) as signal:
        dialog.choose("delete")
    assert signal.args == ["delete", replicas[1]]
    assert dialog.isVisible()
    dialog.entry_selector.setCurrentIndex(0)
    with qtbot.waitSignal(dialog.action_requested) as signal:
        dialog.choose("verify")
    assert signal.args == ["verify", (replicas[0],)]
    assert dialog.isVisible()


def test_replica_management_reopens_and_releases_closed_window(qtbot, monkeypatch, tmp_path):
    window = MainWindow(None, Settings(), SettingsStore(tmp_path / "ui"))
    qtbot.addWidget(window)
    entry = LogicalEntry(
        "file", "file", (Replica("a", "file"),), EntryAssessment(Status.CONFLICT, (), 1, True)
    )
    try:
        monkeypatch.setattr(window, "selected", lambda: [entry])
        window.properties()
        assert window.replica_dialog.isVisible()
        assert not window.replica_dialog.isModal()
        assert not window.replica_dialog.entry_selector.isVisible()
        window.manage_replicas((entry, entry))
        assert window.replica_dialog.entry_selector.count() == 2
        window.replica_dialog.close()
        assert window.replica_dialog is None
        window.properties()
        assert window.replica_dialog.isVisible()
    finally:
        qtbot.waitUntil(lambda: not window.workers.pending)
        window._shutdown_done = True
        window.transfers.close()
        window.close()


def test_first_repair_finishes_while_later_planning_is_pending_without_confirmation(
    qtbot, monkeypatch, backend, accounts, tmp_path
):
    a, b = accounts[:2]
    for name in ("first", "second"):
        backend.put(a.fs, name, b"keep")
    release, started, copying = Event(), Event(), Event()
    original_stat = backend.stat
    original_copy = backend.copy_file

    def stat(endpoint, context=None):
        if endpoint.path == "second" and endpoint.account_id == a.id:
            started.set()
            while not release.wait(0.01):
                context.check()
            context.check()
        return original_stat(endpoint, context)

    monkeypatch.setattr(backend, "stat", stat)

    def copy_file(source, target, context=None):
        if source.path == "first":
            copying.set()
        return original_copy(source, target, context)

    monkeypatch.setattr(backend, "copy_file", copy_file)
    prompts = []
    monkeypatch.setattr(window_module, "confirm", lambda *args: prompts.append(args[1]) or True)
    window = MainWindow(backend, Settings(accounts=[a, b]), SettingsStore(tmp_path / "ui"))
    qtbot.addWidget(window)
    execute = window.engine.execute_repair

    def ordered_repair(entry, context, **kwargs):
        if entry.path == "second":
            assert copying.wait(3)
        return execute(entry, context, **kwargs)

    monkeypatch.setattr(window.engine, "execute_repair", ordered_repair)

    def finished():
        return (
            not window._repair_busy
            and not window.workers.pending
            and not window.transfers.busy
            and all(task.id in window._reported_tasks for task in window.transfers.tasks.values())
        )

    try:
        qtbot.waitUntil(finished)
        window.scan_path = ""
        window.scan_results, window.scan_complete, _ = window.vfs.scan("")
        window.show_issues()
        window.resolve_issues(window.scan_results)
        assert len(window.transfers.tasks) == 2 and not prompts
        first = next(
            task for task in window.transfers.tasks.values() if task.label.endswith("/first")
        )
        second = next(
            task for task in window.transfers.tasks.values() if task.label.endswith("/second")
        )
        qtbot.waitUntil(lambda: first.state == "완료" and started.is_set(), timeout=5_000)
        assert not release.is_set() and second.state == "진행 중"
        assert backend.nodes[b.fs, "first"].data == b"keep"
        assert (b.fs, "second") not in backend.nodes
        window.cancel_task(second)
        qtbot.waitUntil(finished, timeout=5_000)
        assert second.state == "취소됨" and not prompts
        assert [entry.path for entry in window.scan_results] == ["second"]
        assert backend.nodes[a.fs, "second"].data == b"keep"
    finally:
        release.set()
        qtbot.waitUntil(finished, timeout=10_000)
        window._shutdown_done = True
        window.vfs.close()
        window.transfers.close()
        window.close()


def test_cancelling_one_queued_repair_keeps_other_real_queue_tasks_running(
    qtbot, monkeypatch, backend, accounts, tmp_path
):
    a, b = accounts[:2]
    backend.put(a.fs, "folder/file", b"keep")
    backend.put(a.fs, "independent", b"other")
    release, started = Event(), Event()
    mkdir = backend.mkdir

    def delayed_mkdir(endpoint, context=None):
        started.set()
        assert release.wait(5)
        return mkdir(endpoint, context)

    monkeypatch.setattr(backend, "mkdir", delayed_mkdir)
    monkeypatch.setattr(window_module, "confirm", lambda *args: True)
    window = MainWindow(backend, Settings(accounts=[a, b]), SettingsStore(tmp_path / "ui"))
    qtbot.addWidget(window)

    def finished():
        return (
            not window._repair_busy
            and not window.workers.pending
            and not window.transfers.busy
            and all(task.id in window._reported_tasks for task in window.transfers.tasks.values())
        )

    try:
        qtbot.waitUntil(finished)
        window.scan_path = ""
        window.scan_results, window.scan_complete, _ = window.vfs.scan("")
        window.show_issues()
        window.issue_dialog.table.selectAll()
        window.resolve_issues(window.issue_dialog.selected_entries())
        qtbot.waitUntil(lambda: window._repair_total == 3 and started.is_set())
        cancelled = next(
            task for task in window.transfers.tasks.values() if task.label.endswith("/folder/file")
        )
        assert cancelled.state == "대기"
        window.cancel_task(cancelled)
        assert cancelled.state == "취소됨" and window._repair_busy
        assert all(
            not task.context.cancelled.is_set()
            for task in window.transfers.tasks.values()
            if task is not cancelled
        )
        release.set()
        qtbot.waitUntil(finished, timeout=10_000)
        assert len(window.transfers.tasks) == 3
        assert all(
            task.state == "완료"
            for task in window.transfers.tasks.values()
            if task is not cancelled
        )
        assert backend.nodes[b.fs, "independent"].data == b"other"
        assert (b.fs, "folder/file") not in backend.nodes
        assert backend.nodes[a.fs, "folder/file"].data == b"keep"
        assert [entry.path for entry in window.scan_results] == ["folder/file"]
    finally:
        release.set()
        qtbot.waitUntil(finished, timeout=10_000)
        window._shutdown_done = True
        window.vfs.close()
        window.transfers.close()
        window.close()
