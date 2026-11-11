import json
import logging
from dataclasses import replace
from threading import Event
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QMimeData, QPoint, QPointF, Qt, QUrl
from PySide6.QtGui import QDrag, QDragEnterEvent, QDragMoveEvent, QDropEvent
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QPushButton,
    QTableWidget,
    QTextEdit,
)

from cloud_cluster.domain import BackendError, Endpoint, ErrorKind, Status
from cloud_cluster.gui import window as window_module
from cloud_cluster.gui.dialogs import confirm
from cloud_cluster.gui.window import MainWindow
from cloud_cluster.settings import Settings, SettingsStore
from cloud_cluster.transfers import TaskContext, TransferTask
from cloud_cluster.trash import TrashItem, capabilities


@pytest.fixture
def requests(qtbot, monkeypatch, backend, accounts, tmp_path):
    for account, kind in zip(accounts[:2], ("md5", "sha1")):
        for path in ("x", "y"):
            backend.put(account.fs, path, b"same")
            backend.hash_types[account.fs, path] = kind
    window = MainWindow(backend, Settings(accounts=accounts), SettingsStore(tmp_path / "ui"))
    qtbot.addWidget(window)
    window.show()
    qtbot.waitUntil(lambda: window.snapshot is not None and not window.workers.pending)
    entries = window.snapshot.entries
    window.scan_path = ""
    window.scan_results, window.scan_complete, _ = window.vfs.scan("")
    window.manage_replicas(entries)
    pending, queued, errors, prompts = [], [], [], []

    def submit(function, done=None, failed=None, progress=None, **kwargs):
        context = TaskContext(progress=progress or (lambda value: None))
        pending.append((function, done, failed, context))
        return context

    def queue(label, operation, *, after=(), queue="transfer"):
        task = TransferTask(str(len(queued) + 1), label, TaskContext())
        queued.append((task, operation))
        window.transfers.tasks[task.id] = task
        window.update_task(task)
        return task

    def confirm(parent, title, text, *args):
        prompts.append(title)
        return True

    refresh = window.refresh
    submit_transfer = window.transfers.submit
    monkeypatch.setattr(window.workers, "submit", submit)
    monkeypatch.setattr(window.transfers, "submit", queue)
    monkeypatch.setattr(window, "refresh", lambda *args, **kwargs: None)
    monkeypatch.setattr(window, "refresh_capacity", lambda: None)
    monkeypatch.setattr(window_module, "confirm", confirm)
    monkeypatch.setattr(window_module, "show_text", lambda *args: None)
    monkeypatch.setattr(window_module, "message", lambda *args: errors.append(str(args[-1])))
    try:
        yield SimpleNamespace(
            window=window,
            pending=pending,
            queued=queued,
            errors=errors,
            prompts=prompts,
            entries=entries,
            refresh=refresh,
            submit_transfer=submit_transfer,
        )
    finally:
        window._shutdown_done = True
        window.vfs.close()
        window.transfers.close()
        window.close()


def complete(job):
    function, done, _, context = job
    done(function(context))


def drop_on(view, point, mime, modifiers=Qt.KeyboardModifier.NoModifier):
    point = QPointF(point)
    args = (
        Qt.DropAction.CopyAction | Qt.DropAction.MoveAction,
        mime,
        Qt.MouseButton.LeftButton,
        modifiers,
    )
    for event in (QDragEnterEvent(point, *args), QDragMoveEvent(point, *args)):
        QApplication.sendEvent(view.viewport(), event)
        if not event.isAccepted():
            return False
    event = QDropEvent(point, *args)
    QApplication.sendEvent(view.viewport(), event)
    return event.isAccepted()


@pytest.mark.parametrize(
    "source, target, copy_only",
    [
        ("table", "tree", False),
        ("table", "tree", True),
        ("tree", "table", True),
        ("tree", "tree", False),
    ],
)
def test_internal_drag_between_folder_tree_and_file_list(
    requests, qtbot, monkeypatch, backend, accounts, source, target, copy_only
):
    window = requests.window
    a = accounts[0]
    backend.put(a.fs, "source/child.txt", b"child")
    backend.put(a.fs, "dest", None)
    entries = window.vfs.refresh("").entries
    window.file_model.set_entries(entries)
    tree, model = window.tree, window.tree_model
    root = model.index(0, 0)
    model.set_children(model.node(root), entries)
    tree.expand(root)
    file_indexes = {entry.path: window.proxy.index(i, 0) for i, entry in enumerate(entries)}
    tree_indexes = {node.path: model.index_for(node) for node in model.node(root).children}

    source_view, target_view = getattr(window, source), getattr(window, target)
    if source == "tree":
        source_index = tree_indexes["source"]
        source_path = "source/child.txt"
    else:
        source_index = file_indexes["x"]
        source_path = "x"
    target_index = (tree_indexes if target == "tree" else file_indexes)["dest"]

    modifiers = Qt.KeyboardModifier.ControlModifier if copy_only else Qt.KeyboardModifier.NoModifier

    def drag_to_target(drag, actions, default):
        point = target_view.visualRect(target_index).center()
        assert drop_on(target_view, point, drag.mimeData(), modifiers)

    monkeypatch.setattr(QDrag, "exec", drag_to_target)
    origin = source_view.visualRect(source_index).topLeft() + QPoint(8, 8)
    dragged = origin + QPoint(0, 35)
    qtbot.mousePress(source_view.viewport(), Qt.MouseButton.LeftButton, pos=origin)
    qtbot.mouseMove(source_view.viewport(), dragged)
    qtbot.mouseRelease(source_view.viewport(), Qt.MouseButton.LeftButton, pos=dragged)

    assert len(requests.pending) == 1
    builder, ready, _, context = requests.pending[0]
    plan = builder(context)
    assert {copy.source.endpoint.path for copy in plan.copies} == {source_path}
    assert {copy.target.path for copy in plan.copies} == {f"dest/{source_path}"}
    assert bool(plan.deletions) is not copy_only
    ready(plan)
    assert requests.prompts == ["복사" if copy_only else "이동"]
    assert requests.queued


def test_folder_tree_accepts_local_upload_to_unified_root_only_on_valid_rows(requests, monkeypatch):
    window = requests.window
    tree, model = window.tree, window.tree_model
    uploads = []
    monkeypatch.setattr(
        window, "upload_paths", lambda paths, folder: uploads.append((paths, folder))
    )
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile("C:/local/folder")])

    assert drop_on(tree, tree.visualRect(model.index(0, 0)).center(), mime)
    assert uploads == [(["C:/local/folder"], "")]
    group = model.index(1, 0)
    tree.expand(group)
    account = model.index(0, 0, group)
    for point in (
        tree.visualRect(account).center(),
        tree.visualRect(model.index(2, 0)).center(),
        tree.viewport().rect().bottomLeft(),
    ):
        assert not drop_on(tree, point, mime)
    assert len(uploads) == 1


def cannot_start(*args, **kwargs):
    raise RuntimeError("작업을 시작하지 못했습니다.")


def complete_verification_reads(requests, *, fail_first=False):
    for index, (task, read) in enumerate(requests.queued):
        if fail_first and index == 0:
            task.state, task.error = "실패", "read failed"
        else:
            task.result, task.state = read(task.context), "완료"
        requests.window.update_task(task)


def test_verify_is_single_request_through_confirm_result_reopen_and_recheck(requests, monkeypatch):
    window, replicas = requests.window, requests.entries[0].replicas

    def reenter(*args):
        assert window._verification_targets
        window.verify(replicas)
        window.replica_action("verify", replicas)
        return True

    monkeypatch.setattr(window_module, "confirm", reenter)
    window.replica_dialog.action_buttons["verify"].click()
    assert len(requests.queued) == len(replicas) and not requests.pending
    window.replica_dialog.action_buttons["verify"].click()
    window.replica_dialog.choose("delete")
    window.replica_dialog.entry_selector.setCurrentIndex(1)
    assert {
        action for action, button in window.replica_dialog.action_buttons.items() if button.isEnabled()
    } == {"download", "replicate", "delete"}
    window.manage_replicas(requests.entries)
    assert not window.replica_dialog.action_buttons["verify"].isEnabled()
    monkeypatch.setattr(window_module, "show_text", reenter)
    complete_verification_reads(requests)
    complete(requests.pending[0])
    assert len(requests.pending) == 2 and window._verification_targets
    window.verify(replicas)
    assert len(requests.pending) == 2
    complete(requests.pending[1])
    assert not window._verification_targets
    assert window.replica_dialog.action_buttons["verify"].isEnabled()
    assert [entry.path for entry in window.scan_results] == ["y"]


def test_download_other_file_while_verification_is_rechecking(requests, monkeypatch, tmp_path):
    window = requests.window
    x, y = requests.entries
    picks = []

    def destination(*args, **kwargs):
        picks.append(True)
        return str(tmp_path / "out"), ""

    monkeypatch.setattr(window_module.QFileDialog, "getSaveFileName", destination)
    window.verify(x.replicas)
    complete_verification_reads(requests)
    complete(requests.pending[0])
    assert window._verification_targets and len(requests.pending) == 2
    assert not window.configuration_available(notify=False)
    window.replica_action("download", x.replicas[0])
    assert not picks
    window.replica_dialog.entry_selector.setCurrentIndex(1)
    window.replica_dialog.action_buttons["download"].click()
    assert picks == [True]
    complete(requests.pending[2])
    task, _ = requests.queued[-1]
    assert window._pending_plans[task.id].copies[0].source.endpoint.path == y.path
    complete(requests.pending[1])
    assert not window._verification_targets and window._replica_task_id == task.id


def test_download_auto_source_balances_active_tasks_and_rotates_ties(requests, monkeypatch, tmp_path):
    window = requests.window
    x, y = requests.entries
    healthy = replace(y, assessment=replace(y.assessment, status=Status.HEALTHY))
    a, b = healthy.replicas

    assert window.choose_replica(healthy) == a
    assert window.choose_replica(healthy) == b
    assert window.choose_replica(healthy) == a

    window._explorer_drag = True
    task = window.enqueue_dragged_file(x, b.account_id, tmp_path / x.name)
    assert task is not None
    assert window.choose_replica(healthy, scope=b.account_id) == b
    for state in ("대기", "진행 중", "취소 요청"):
        task.state = state
        assert window.choose_replica(healthy) == a

    monkeypatch.setattr(
        window_module.QInputDialog, "getItem", lambda *args, **kwargs: (args[3][-1], True)
    )
    assert window.choose_replica(x) == x.replicas[-1]
    task.state = "완료"
    # The transfer can finish before the UI releases its plan.
    assert task.id in window._pending_plans
    queued = window.enqueue_dragged_file(healthy, "", tmp_path / healthy.name)
    assert queued is not None
    assert window._pending_plans[queued.id].copies[0].source.endpoint.account_id == b.account_id
    assert not requests.errors


@pytest.mark.parametrize("entry_point", ["replica", "button", "double_click"])
def test_download_only_confirms_an_existing_target_in_the_latest_plan(
    requests, monkeypatch, tmp_path, entry_point
):
    window = requests.window
    replica = requests.entries[0].replicas[0]
    window.settings.download_dir = str(tmp_path)
    destination = tmp_path / (replica.name if entry_point == "double_click" else "out")
    target = (tmp_path.as_posix(), destination.name)
    picks, confirmations = [], []

    def pick(parent, title, suggested, *, options):
        assert options == window_module.QFileDialog.Option.DontConfirmOverwrite
        assert suggested == str(tmp_path / replica.name)
        picks.append(suggested)
        return str(destination), ""

    def confirm_overwrite(parent, title, details):
        assert title == "다운로드"
        assert str(destination) in details and "기존 파일 덮어쓰기" in details
        confirmations.append(details)
        return len(confirmations) == 2

    monkeypatch.setattr(window_module.QFileDialog, "getSaveFileName", pick)
    monkeypatch.setattr(window_module, "confirm", confirm_overwrite)
    monkeypatch.setattr(window, "choose_replica", lambda entry: entry.replicas[0])
    if entry_point == "replica":
        window.replica_action("download", replica)
    elif entry_point == "button":
        window.table.selectRow(0)
        window.download_action.trigger()
    else:
        window.table.doubleClicked.emit(window.proxy.index(0, 0))
    complete(requests.pending[0])
    task, execute = requests.queued[0]
    assert not confirmations
    assert len(picks) == (0 if entry_point == "double_click" else 1)

    window.backend.put(*target, b"existing")
    task.result = execute(task.context)
    task.state = task.result.outcome
    assert task.state == "실패 / 결과 확인 필요"
    window.update_task(task)

    window.retry_task(task)
    complete(requests.pending[1])
    assert len(confirmations) == len(requests.queued) == 1
    assert window.backend.nodes[target].data == b"existing"

    window.retry_task(task)
    complete(requests.pending[2])
    assert len(confirmations) == len(requests.queued) == 2
    retry, execute = requests.queued[-1]
    retry.result = execute(retry.context)
    retry.state = retry.result.outcome
    window.update_task(retry)
    assert retry.state == "완료"
    assert window.backend.nodes[target].data == b"same"


@pytest.mark.parametrize(
    "ending", ["cancel", "start", "read", "recheck", "recheck_start", "closing"]
)
def test_verify_releases_controls_on_every_exit(requests, monkeypatch, ending):
    window, replicas = requests.window, requests.entries[0].replicas
    if ending == "cancel":
        monkeypatch.setattr(window_module, "confirm", lambda *args: False)
    if ending == "start":
        monkeypatch.setattr(window.transfers, "submit", cannot_start)
    window.verify(replicas)
    if ending == "read":
        complete_verification_reads(requests, fail_first=True)
    elif ending in {"recheck", "recheck_start", "closing"}:
        complete_verification_reads(requests)
        if ending == "closing":
            window._closing = True
        elif ending == "recheck_start":
            monkeypatch.setattr(window.workers, "submit", cannot_start)
        complete(requests.pending[0])
        if ending == "recheck":
            requests.pending[1][2]("recheck failed")
    assert not window._verification_targets
    assert window.replica_dialog.action_buttons["verify"].isEnabled() == (ending != "closing")
    assert bool(requests.errors) == (ending in {"start", "read", "recheck", "recheck_start"})


@pytest.mark.parametrize("action", ["download", "delete", "replicate"])
@pytest.mark.parametrize("terminal", ["완료", "일부 성공", "실패", "취소됨"])
def test_replica_actions_block_repeats_until_their_own_task_and_recheck_finish(
    requests, monkeypatch, tmp_path, action, terminal
):
    window, replica = requests.window, requests.entries[0].replicas[0]
    monkeypatch.setattr(
        window_module.QFileDialog,
        "getSaveFileName",
        lambda *args, **kwargs: (str(tmp_path / "out"), ""),
    )
    monkeypatch.setattr(
        window_module.QInputDialog,
        "getItem",
        lambda parent, title, label, choices, **kwargs: (choices[-1], True),
    )
    window.replica_action(action, replica)
    window.replica_action(action, replica)
    assert window._replica_busy
    if action == "delete":
        assert not requests.pending and len(requests.prompts) == 1
    else:
        assert len(requests.pending) == 1
        if action == "replicate":
            complete(requests.pending[0])
        complete(requests.pending[-1])
    assert len(requests.queued) == 1 and window._replica_task_id == requests.queued[0][0].id
    window.manage_replicas(requests.entries)
    assert not any(button.isEnabled() for button in window.replica_dialog.action_buttons.values())
    window.replica_action(action, replica)
    task = requests.queued[0][0]
    for state in ("대기", "진행 중", "취소 요청"):
        task.state = state
        window.update_task(task)
        assert window._replica_busy
    generation = window.generation
    task.state = terminal
    window.update_task(task)
    if action == "download":
        assert window.generation == generation
    if terminal in {"완료", "일부 성공"} and action != "download":
        assert window._replica_busy == (action == "replicate")
        assert not any(
            button.isEnabled() for button in window.replica_dialog.action_buttons.values()
        )
        if action == "delete":
            row = window._task_rows[task.id]
            assert row.text(1) == "결과 재확인" and row.text(2).startswith(f"삭제 {terminal} · ")
        window.flush_task_refreshes()
        complete(requests.pending[-1])
    assert not window._replica_busy and not window._pending_plans
    assert all(button.isEnabled() for button in window.replica_dialog.action_buttons.values())
    count = len(requests.pending)
    window.update_task(task)
    assert len(requests.pending) == count


@pytest.mark.parametrize("second_rechecking", [False, True])
def test_replica_delete_recheck_only_blocks_related_paths(requests, second_rechecking, caplog):
    caplog.set_level(logging.INFO, logger="cloud_cluster")
    window = requests.window
    x, y = requests.entries
    window.show_issues()
    issue = window.issue_dialog
    issue.manage_button.click()
    window.replica_dialog.action_buttons["delete"].click()
    first, delete = requests.queued[0]
    first.context.progress = lambda value: setattr(first, "stats", value)
    first.result = delete(first.context)
    first.state = first.result.outcome
    window.update_task(first)
    window.flush_task_refreshes()
    first_recheck = requests.pending[0]
    row = window._task_rows[first.id]
    assert row.text(1) == "결과 재확인" and "삭제 1/1개 완료" in row.text(2)

    assert not window.replica_dialog.action_buttons["delete"].isEnabled()
    assert not window.configuration_available()
    window.delete_replica(x.replicas[1])
    assert len(requests.prompts) == 1
    issue.table.selectRow(1)
    issue.manage_button.click()
    assert window.replica_dialog.entry.path == y.path
    window.replica_dialog.action_buttons["delete"].click()
    second, delete = requests.queued[1]
    second.result = delete(second.context)
    second.state = second.result.outcome
    if second_rechecking:
        window.update_task(second)
        window.flush_task_refreshes()
        assert len(requests.pending) == 1

    complete(first_recheck)
    assert window._replica_busy == (not second_rechecking)
    assert not window.replica_dialog.action_buttons["delete"].isEnabled()
    assert row.text(1) == "완료" and row.text(2).startswith("삭제 1/1개 완료 · 재확인 완료")
    first_recheck[-1].progress({"detail": "늦은 진행 신호", "recheck_index": 0})
    assert row.text(1) == "완료" and row.text(2).startswith("삭제 1/1개 완료 · 재확인 완료")

    if not second_rechecking:
        window.update_task(second)
    window.flush_task_refreshes()
    complete(requests.pending[-1])
    assert window.replica_dialog.action_buttons["delete"].isEnabled()
    assert not window._delete_targets
    assert window.configuration_available()
    messages = caplog.messages
    assert any("issue action blockers=path_overlap " in message for message in messages)
    assert any("issue action blockers=- " in message for message in messages)
    for task in (first, second):
        receipt = messages.index(f"task result received task={task.id} state=완료")
        queued = next(
            index
            for index, message in enumerate(messages)
            if message.startswith(f"issue recheck queued task={task.id} ")
        )
        rechecked = next(
            index
            for index, message in enumerate(messages)
            if message.startswith(f"issue recheck ended task={task.id} outcome=complete ")
        )
        released = messages.index(f"task ui released task={task.id}")
        assert receipt < queued < rechecked < released


@pytest.mark.parametrize(
    "ending", ["picker", "recommend", "target", "plan", "confirm", "submit", "closing"]
)
def test_replica_actions_release_controls_when_not_queued(requests, monkeypatch, tmp_path, ending):
    window, replica = requests.window, requests.entries[0].replicas[0]
    if ending == "picker":
        monkeypatch.setattr(
            window_module.QFileDialog, "getSaveFileName", lambda *args, **kwargs: ("", "")
        )
        window.replica_action("download", replica)
    elif ending in {"recommend", "target"}:
        window.replica_action("replicate", replica)
        if ending == "recommend":
            requests.pending[0][2]("quota failed")
        else:
            monkeypatch.setattr(
                window_module.QInputDialog, "getItem", lambda *args, **kwargs: ("", False)
            )
            complete(requests.pending[0])
    elif ending == "plan":
        monkeypatch.setattr(
            window_module.QFileDialog,
            "getSaveFileName",
            lambda *args, **kwargs: (str(tmp_path / "out"), ""),
        )
        window.replica_action("download", replica)
        requests.pending[0][2]("plan failed")
    else:
        if ending == "confirm":
            monkeypatch.setattr(window_module, "confirm", lambda *args: False)
        elif ending == "submit":
            monkeypatch.setattr(window.transfers, "submit", cannot_start)
        else:

            def close_during_confirmation(*args):
                window._closing = True
                return True

            monkeypatch.setattr(window_module, "confirm", close_during_confirmation)
        window.replica_action("delete", replica)
    assert not requests.queued and not window._replica_busy and not window._preparing
    assert not window._delete_targets
    assert window.replica_dialog.action_buttons["delete"].isEnabled() == (ending != "closing")


def test_generic_plans_reject_overlapping_queued_work_but_allow_other_paths(requests):
    window = requests.window
    x, y = (entry.replicas[0] for entry in requests.entries)

    def build_x(context):
        return window.engine.plan_delete((x,), context)

    window.prepare(build_x)
    window.prepare(build_x)
    assert len(requests.pending) == 1 and not window.delete_action.isEnabled()
    complete(requests.pending[0])
    window.prepare(build_x)
    complete(requests.pending[1])
    assert len(requests.queued) == 1 and len(requests.prompts) == 1
    window.prepare(lambda context: window.engine.plan_delete((y,), context))
    complete(requests.pending[2])
    assert len(requests.queued) == 2


@pytest.mark.parametrize("source", ["replica", "file_list", "account_list"])
def test_delete_confirmation_shows_selected_accounts_before_any_lookup(
    requests, monkeypatch, backend, source
):
    window = requests.window
    backend.calls.clear()
    if source == "account_list":
        window.scope = requests.entries[0].replicas[0].account_id

    window.table.selectRow(0)
    parent = window.replica_dialog if source == "replica" else window
    details = "a · /x\nb · /x" if source == "file_list" else "a · /x"
    confirmations = []

    def cancel(dialog):
        confirmations.append(dialog)
        assert dialog.parentWidget() is parent
        assert "선택한 항목을 정말 삭제하시겠습니까?" in [
            label.text() for label in dialog.findChildren(QLabel)
        ]
        assert dialog.findChild(QTextEdit).toPlainText() == details
        buttons = dialog.findChild(QDialogButtonBox)
        assert buttons.button(QDialogButtonBox.StandardButton.Cancel).isDefault()
        assert not backend.calls and not requests.pending and not requests.queued
        return QDialog.DialogCode.Rejected

    monkeypatch.setattr(window_module, "confirm", confirm)
    monkeypatch.setattr(QDialog, "exec", cancel)
    if source == "replica":
        window.replica_dialog.action_buttons["delete"].click()
    else:
        window.delete_action.trigger()
    assert len(confirmations) == 1
    assert not backend.calls and not requests.queued and not requests.errors
    assert not window._delete_targets
    assert window.replica_dialog.action_buttons["delete"].isEnabled()


@pytest.mark.parametrize("source", ["replica", "file_list"])
def test_delete_retry_confirms_original_target_before_queueing(
    requests, backend, monkeypatch, source
):
    window = requests.window
    confirmations = []

    def confirm_target(_parent, title, details, *_args):
        if title == "삭제":
            confirmations.append(details)
        return True

    monkeypatch.setattr(window_module, "confirm", confirm_target)
    backend.calls.clear()
    if source == "replica":
        window.replica_dialog.action_buttons["delete"].click()
    else:
        window.table.selectRow(0)
        window.delete_action.trigger()
    task, _ = requests.queued[0]
    task.state = "실패"
    window.update_task(task)
    window.replica_dialog.entry_selector.setCurrentIndex(1)
    window.table.selectRow(1)
    window.retry_task(task)
    expected = "a · /x" if source == "replica" else "a · /x\nb · /x"
    assert confirmations == [expected, expected] and len(requests.queued) == 2
    assert not backend.calls and not requests.pending
    assert requests.queued[1][0].label == task.label


@pytest.mark.parametrize("source", ["replica", "file_list"])
def test_folder_delete_plans_in_queue_and_rechecks_its_children(
    requests, backend, accounts, monkeypatch, source
):
    window = requests.window
    account = accounts[0]
    backend.put(account.fs, "folder/child", b"data")
    snapshot = window.vfs.refresh("")
    folder = next(entry for entry in snapshot.entries if entry.is_dir)
    window.show_snapshot(snapshot)
    row = next(
        i for i in range(window.proxy.rowCount()) if window.proxy.index(i, 0).data() == "folder"
    )
    window.table.selectRow(row)
    window.manage_replicas((folder,))

    def confirm_folder(_parent, title, details, prompt):
        assert title == "삭제" and details == "a · /folder"
        assert "하위 항목" in prompt
        assert not backend.calls
        return True

    monkeypatch.setattr(window_module, "confirm", confirm_folder)
    backend.calls.clear()
    if source == "replica":
        window.replica_dialog.action_buttons["delete"].click()
    else:
        window.delete_action.trigger()
    assert not backend.calls and not requests.pending
    task, operation = requests.queued[0]
    assert window.endpoints_busy((window.engine.endpoint(folder.replicas[0]),))
    progress = []
    task.context.progress = lambda value: progress.append(value["detail"])
    task.result = operation(task.context)
    assert progress == [
        "삭제 대상 확인 중",
        "삭제 대상 확인 · a · /folder",
        "하위 항목 조회 · a · /folder",
        "삭제 대상 확인 · a · /folder/child",
        "하위 조회 후 폴더 확인 · a · /folder",
        "삭제 전 확인 · 1/2 · a · /folder/child",
        "삭제 직전 확인 · 1/2 · a · /folder/child",
        "파일 삭제 · 1/2 · a · /folder/child",
        "빈 폴더 삭제 · 2/2 · a · /folder",
        "삭제 2/2개 완료",
    ]
    assert (account.fs, "folder") not in backend.nodes

    checked = []

    def recheck(keys, *, on_finished, **kwargs):
        checked.extend(sorted(keys))
        other = accounts[1]
        assert window.endpoints_busy((Endpoint(other.fs, "folder/child", other.id),))
        on_finished()

    monkeypatch.setattr(window, "recheck_issues", recheck)
    task.state = task.result.outcome
    window.update_task(task)
    assert checked == ["folder", "folder/child"]
    assert not window._delete_targets


def test_new_folder_skips_confirmation_and_rechecks_conflicts_on_retry(
    requests, monkeypatch, backend, accounts
):
    window = requests.window
    monkeypatch.setattr(window_module.QInputDialog, "getText", lambda *args: ("folder", True))
    window.mkdir_action.trigger()
    complete(requests.pending[0])
    task, operation = requests.queued[0]
    assert not requests.prompts

    conflict = (accounts[-1].fs, "folder")
    backend.put(*conflict, b"existing file")
    task.result = operation(task.context)
    task.state = task.result.outcome
    window.update_task(task)
    assert task.state == "실패 / 결과 확인 필요"
    assert backend.nodes[conflict].data == b"existing file"
    assert not any((account.fs, "folder") in backend.nodes for account in accounts[:-1])

    del backend.nodes[conflict]
    window.retry_task(task)
    complete(requests.pending[1])
    retry, operation = requests.queued[1]
    assert not requests.prompts
    assert operation(retry.context).outcome == "완료"
    assert all(backend.nodes[account.fs, "folder"].data is None for account in accounts)


@pytest.mark.parametrize("kind", ["local_target", "ancestor"])
def test_queue_guard_checks_local_destinations_and_ancestor_paths(requests, tmp_path, kind):
    window = requests.window
    if kind == "local_target":
        x, y = (entry.replicas[0] for entry in requests.entries)
        destination = tmp_path / "out"
        window.prepare(lambda context: window.engine.plan_download(x, destination, context))
        complete(requests.pending[0])
        window.prepare(lambda context: window.engine.plan_download(y, destination, context))
    else:
        window.prepare(lambda context: window.engine.plan_mkdir("folder", context))
        complete(requests.pending[0])
        window.prepare(lambda context: window.engine.plan_mkdir("folder/sub", context))
    complete(requests.pending[1])
    assert len(requests.queued) == 1


@pytest.mark.parametrize("terminal", ["완료", "일부 성공", "실패", "취소됨"])
def test_scan_blocks_confirm_reentry_and_reopened_rescan_button(requests, monkeypatch, terminal):
    window = requests.window
    window.show_issues()

    def confirm(*args):
        window.start_scan("")
        assert not window.scan_action.isEnabled()
        assert not window.issue_dialog.rescan_button.isEnabled()
        return True

    monkeypatch.setattr(window_module, "confirm", confirm)
    window.start_scan("")
    window.start_scan("")
    window.show_issues()
    assert len(requests.queued) == 1 and not window.issue_dialog.rescan_button.isEnabled()
    unrelated = TransferTask("other", "other", TaskContext(), state="완료")
    window.transfers.tasks[unrelated.id] = unrelated
    window.update_task(unrelated)
    assert window._scan_busy
    task = requests.queued[0][0]
    task.state = terminal
    window.update_task(task)
    assert not window._scan_busy and window.scan_action.isEnabled()
    assert window.issue_dialog.rescan_button.isEnabled()


@pytest.mark.parametrize("ending", ["cancel", "submit"])
def test_scan_releases_if_not_registered(requests, monkeypatch, ending):
    if ending == "cancel":
        monkeypatch.setattr(window_module, "confirm", lambda *args: False)
    else:
        monkeypatch.setattr(requests.window.transfers, "submit", cannot_start)
    requests.window.start_scan("")
    assert not requests.window._scan_busy and requests.window.scan_action.isEnabled()


@pytest.mark.parametrize("ending", ["완료", "실패", "취소됨"])
def test_double_click_downloads_to_default_and_blocks_repeats(requests, monkeypatch, tmp_path, ending):
    window = requests.window
    download_dir = tmp_path / "new" / "downloads"
    window.settings.download_dir = str(download_dir)
    monkeypatch.setattr(window, "choose_replica", lambda entry: entry.replicas[0])
    index = window.proxy.index(0, 0)
    window.table.doubleClicked.emit(index)
    window.table.doubleClicked.emit(index)
    assert len(requests.pending) == 1
    complete(requests.pending[0])
    window.table.doubleClicked.emit(index)
    assert len(requests.pending) == len(requests.queued) == 1
    assert download_dir.is_dir() and not requests.prompts
    task, operation = requests.queued[0]
    plan = window._pending_plans[task.id]
    assert task.label == "다운로드"
    assert plan.copies[0].target == Endpoint(download_dir.as_posix(), "x")
    task.state = ending
    if ending == "완료":
        task.result = operation(task.context)
        assert task.result.outcome == "완료"
        assert window.backend.nodes[(download_dir.as_posix(), "x")].data == b"same"
    window.update_task(task)
    assert not window.endpoints_busy(plan.endpoints)
    assert not requests.errors


def test_refresh_rejects_repeat_and_late_completion_cannot_unlock_newer_refresh(
    requests, monkeypatch
):
    window = requests.window
    monkeypatch.setattr(window, "refresh", requests.refresh)
    window.refresh()
    generation = window.generation
    window.refresh()
    assert len(requests.pending) == 1 and window.generation == generation
    assert not window.refresh_action.isEnabled()
    window.path = "another"
    window.refresh()
    requests.pending[0][2]("old failure")
    assert not window.refresh_action.isEnabled()
    requests.pending[1][2]("new failure")
    assert window.refresh_action.isEnabled() and window._refreshing is None


def test_mutation_shares_one_fresh_listing_between_browse_and_issue_recheck(
    requests, monkeypatch, backend, accounts
):
    window = requests.window
    monkeypatch.setattr(window, "refresh", requests.refresh)
    window.refresh()
    read, old_done, _, context = requests.pending[0]
    old_snapshot = read(context)
    replica = requests.entries[0].replicas[0]
    window.delete_replica(replica)
    task, delete = requests.queued[0]
    task.result = delete(task.context)
    task.state = task.result.outcome

    backend.calls.clear()
    window.update_task(task)
    window.flush_task_refreshes()
    window.refresh()
    assert len(requests.pending) == 2
    old_done(old_snapshot)
    assert not window.refresh_action.isEnabled()
    complete(requests.pending[1])

    for account in accounts:
        assert backend.calls.count(("list", account.fs, "")) == 1
    assert window.refresh_action.isEnabled()
    shown = next(entry for entry in window.snapshot.entries if entry.path == replica.path)
    issue = next(entry for entry in window.scan_results if entry.path == replica.path)
    assert shown.replicas == issue.replicas
    assert all(item.account_id != replica.account_id for item in shown.replicas)


def test_finished_tasks_share_a_folder_read_and_late_completion_needs_a_new_one(
    requests, qtbot, backend, accounts
):
    window = requests.window
    account = accounts[0]
    backend.put(account.fs, "late", b"data")
    late = backend.replica(account.fs, "late", account.id)
    window.request_delete(tuple(entry.replicas[0] for entry in requests.entries) + (late,))

    def finish_delete(index):
        task, delete = requests.queued[index]
        task.result = delete(task.context)
        task.state = task.result.outcome
        window.update_task(task)
        return task.id

    first = finish_delete(0)
    second = finish_delete(1)
    assert not requests.pending and {first, second} <= window._delete_targets.keys()
    qtbot.waitUntil(lambda: len(requests.pending) == 1)
    backend.calls.clear()
    read, done, _, context = requests.pending[0]
    result = read(context)
    assert all(backend.calls.count(("list", a.fs, "")) == 1 for a in accounts)

    finish_delete(2)
    backend.calls.clear()
    window.flush_task_refreshes()
    assert len(requests.pending) == 1 and window.vfs.cached("").stale
    done(result)
    assert not window.refresh_action.isEnabled() and not window._delete_targets

    qtbot.waitUntil(lambda: len(requests.pending) == 2)
    complete(requests.pending[1])
    assert all(backend.calls.count(("list", a.fs, "")) == 1 for a in accounts)
    assert {entry.path for entry in window.snapshot.entries} == {"x", "y"}
    assert all(len(entry.replicas) == 1 for entry in window.scan_results)
    assert window.refresh_action.isEnabled()


@pytest.mark.parametrize("copy_only, reject", [(True, False), (False, False), (False, True)])
def test_internal_transfers_overlap_and_move_waits_for_all_files(
    requests, qtbot, backend, accounts, monkeypatch, copy_only, reject
):
    window = requests.window
    copy_file = backend.copy_file
    started = {path: Event() for path in ("x", "y")}
    release = Event()

    def gated_copy(source, target, context=None):
        started[source.path].set()
        assert release.wait(10)
        if reject and source.path == "y":
            raise BackendError(ErrorKind.NETWORK)
        return copy_file(source, target, context)

    monkeypatch.setattr(window.transfers, "submit", requests.submit_transfer)
    monkeypatch.setattr(backend, "copy_file", gated_copy)
    try:
        window.dropped([], json.dumps(["x", "y"]), "dest", copy_only)
        build, ready, _, context = requests.pending[0]
        backend.calls.clear()
        plan = build(context)
        assert all(backend.calls.count(("list", account.fs, "")) == 1 for account in accounts)
        assert all(
            backend.calls.count(("stat", account.fs, path)) == 1
            for account in accounts[:2] for path in started
        )
        ready(plan)
        tasks = list(window.transfers.tasks.values())
        assert len(tasks) == (3 if copy_only else 4) and len(requests.prompts) == 1
        qtbot.waitUntil(lambda: all(event.is_set() for event in started.values()))
        assert all((account.fs, path) in backend.nodes for account in accounts[:2] for path in started)
        if not copy_only:
            assert tasks[-1].state == "대기"

        release.set()
        qtbot.waitUntil(lambda: len(window._reported_tasks) == len(tasks))
        for account in accounts[:2]:
            assert backend.nodes[account.fs, "dest/x"].data == b"same"
            assert ((account.fs, "dest/y") in backend.nodes) == (not reject)
            assert all(((account.fs, path) in backend.nodes) == (copy_only or reject) for path in started)
        if not copy_only:
            assert tasks[-1].state == ("실패" if reject else "완료")
    finally:
        release.set()
        qtbot.waitUntil(lambda: not window.transfers.busy)


@pytest.mark.parametrize(
    "change", ["replace file", "remove file", "lookup failure", "remove empty folder"]
)
def test_move_keeps_all_originals_if_a_verified_destination_changes(
    requests, backend, accounts, change
):
    window = requests.window
    a = accounts[0]
    if change == "remove empty folder":
        backend.put(a.fs, "empty", None)
        plan = window.engine.plan_move((backend.replica(a.fs, "empty", a.id),), "dest/empty")
        window.enqueue_copy_move(plan)
        destination, original = "dest/empty", "empty"
    else:
        window.dropped([], json.dumps(["x", "y"]), "dest", False)
        complete(requests.pending[0])
        destination, original = "dest/x", "x"
    for task, run in requests.queued[:-1]:
        task.result = run(task.context)
    if change == "replace file":
        backend.put(a.fs, destination, b"replaced")
    elif change == "lookup failure":
        backend.failures["stat", a.fs, destination] = BackendError(ErrorKind.NETWORK)
    else:
        del backend.nodes[a.fs, destination]
    task, run = requests.queued[-1]
    result = run(task.context)
    assert result.outcome == "실패 / 결과 확인 필요"
    assert (a.fs, original) in backend.nodes
    assert not any(call[0] in {"delete", "rmdir"} for call in backend.calls)


def test_delete_registers_disjoint_selections_separately_and_keeps_overlaps_together(
    requests, backend, accounts
):
    window = requests.window
    account = accounts[0]
    backend.put(account.fs, "folder/sub.txt", b"child")
    folder = backend.replica(account.fs, "folder", account.id)
    child = backend.replica(account.fs, "folder/sub.txt", account.id)
    sibling = requests.entries[0].replicas[0]
    window.request_delete((child, sibling, folder))

    assert len(requests.prompts) == 1
    assert len(requests.queued) == 2
    assert {task.label.rsplit("/", 1)[-1] for task, _ in requests.queued} == {"folder", sibling.path}
    for task, delete in requests.queued:
        assert delete(task.context).outcome == "완료"
    assert (account.fs, "folder") not in backend.nodes
    assert (account.fs, child.path) not in backend.nodes
    assert (account.fs, sibling.path) not in backend.nodes


def test_folder_copy_groups_logical_file_and_keeps_each_accounts_path(requests, backend, accounts):
    window = requests.window
    backend.put(accounts[0].fs, "folder/Report.txt", b"a")
    backend.put(accounts[1].fs, "folder/report.txt", b"b")
    window.dropped([], json.dumps(["folder"]), "dest", True)
    complete(requests.pending[0])
    assert len(requests.queued) == 3
    task, _ = requests.queued[-1]
    assert {copy.target.path for copy in window._pending_plans[task.id].copies} == {
        "dest/folder/Report.txt", "dest/folder/report.txt",
    }


def test_file_upload_and_retry_confirm_overwrite_of_current_version(
    requests, tmp_path, backend, accounts, monkeypatch
):
    window = requests.window
    source = tmp_path / "file.txt"
    source.write_bytes(b"local")
    backend.put(tmp_path.as_posix(), source.name, b"local")
    existing = [(account.fs, source.name) for account in accounts[:2]]
    for target in existing:
        backend.put(*target, b"old")
    prompts = []

    def overwrite(dialog):
        assert dialog.windowTitle() == "업로드 덮어쓰기"
        prompts.append(dialog.findChild(QTextEdit).toPlainText())
        assert dialog.findChild(QCheckBox) is None
        buttons = dialog.findChild(QDialogButtonBox)
        overwrite = buttons.button(QDialogButtonBox.StandardButton.Ok)
        skip = buttons.button(QDialogButtonBox.StandardButton.Cancel)
        assert overwrite.text() == "덮어쓰기" and skip.text() == "건너뛰기"
        assert skip.isDefault()
        overwrite.click()
        return dialog.result()

    monkeypatch.setattr(QDialog, "exec", overwrite)
    monkeypatch.setattr(
        window_module.QFileDialog, "getOpenFileNames", lambda *args: ([str(source)], "")
    )

    window.upload_action.trigger()
    build, done, _, context = requests.pending[0]
    plan = build(context)
    backend.calls.clear()
    done(plan)
    task, upload = requests.queued[0]
    expected = "a · /file.txt\nb · /file.txt"
    assert prompts == [expected] and not backend.calls

    backend.put(*existing[0], b"external")
    task.result = upload(task.context)
    task.state = task.result.outcome
    window.update_task(task)
    assert task.state == "실패 / 결과 확인 필요"
    assert backend.nodes[existing[0]].data == b"external"
    assert backend.nodes[existing[1]].data == b"old"

    window.retry_task(task)
    complete(requests.pending[1])
    retry, upload = requests.queued[1]
    assert prompts == [expected, expected]
    retry.result = upload(retry.context)
    retry.state = retry.result.outcome
    window.update_task(retry)
    assert retry.state == "완료"
    assert all(backend.nodes[target].data == b"local" for target in existing)


@pytest.mark.parametrize(
    "action, uploads",
    [
        ("덮어쓰기", ["second.txt", "third.txt", "new.txt"]),
        ("건너뛰기", ["new.txt"]),
        ("닫기", ["third.txt", "new.txt"]),
    ],
)
def test_upload_overwrite_applies_only_to_remaining_conflicts(
    requests, tmp_path, backend, accounts, monkeypatch, qtbot, action, uploads
):
    paths = [tmp_path / name for name in ("first.txt", "second.txt", "third.txt", "new.txt")]
    for path in paths:
        path.write_bytes(b"local")
        backend.put(tmp_path.as_posix(), path.name, b"local")
    for path in paths[:3]:
        backend.put(accounts[0].fs, path.name, b"old")
    prompts = []

    def choose(dialog):
        edit = dialog.findChild(QTextEdit)
        prompts.append(edit.toPlainText())
        buttons = dialog.findChild(QDialogButtonBox)
        skip = buttons.button(QDialogButtonBox.StandardButton.Cancel)
        overwrite = buttons.button(QDialogButtonBox.StandardButton.Ok)
        apply_all = dialog.findChild(QCheckBox)
        if len(prompts) == 1:
            assert apply_all is not None and not apply_all.isChecked()
            skip.click()
        elif len(prompts) == 2:
            assert not apply_all.isChecked() and skip.isDefault()
            apply_all.click()
            assert edit.toPlainText() == "a · /second.txt\n\na · /third.txt"
            apply_all.click()
            assert edit.toPlainText() == "a · /second.txt"
            apply_all.click()
            if action == "닫기":
                qtbot.keyClick(dialog, Qt.Key.Key_Escape)
            else:
                (overwrite if action == "덮어쓰기" else skip).click()
        else:
            assert action == "닫기" and apply_all is None
            overwrite.click()
        return dialog.result()

    monkeypatch.setattr(QDialog, "exec", choose)
    requests.window.upload_paths(paths, "")
    build, done, _, context = requests.pending[0]
    plan = build(context)
    backend.calls.clear()
    done(plan)

    assert not backend.calls and not requests.errors
    assert prompts == ["a · /first.txt", "a · /second.txt"] + (
        ["a · /third.txt"] if action == "닫기" else []
    )
    assert [task.label.rsplit("/", 1)[-1] for task, _ in requests.queued] == uploads


@pytest.mark.parametrize("with_new_file", [False, True])
@pytest.mark.parametrize("folder_upload", [False, True])
def test_upload_skip_excludes_file_from_all_accounts_and_retry(
    requests, tmp_path, backend, accounts, monkeypatch, with_new_file, folder_upload
):
    window = requests.window
    local_folder = tmp_path / "batch" if folder_upload else tmp_path
    prefix = "batch/" if folder_upload else ""
    if folder_upload:
        local_folder.mkdir()
        for account in accounts:
            backend.put(account.fs, "batch", None)
    skipped = local_folder / "existing.txt"
    fresh = local_folder / "new.txt"
    paths = [skipped, fresh] if with_new_file else [skipped]
    for path in paths:
        path.write_bytes(b"local")
        backend.put(local_folder.as_posix(), path.name, b"local")
    skipped_target = prefix + skipped.name
    existing = accounts[0].fs, skipped_target
    backend.put(*existing, b"old")
    prompts = []

    def skip(dialog):
        prompts.append(dialog.findChild(QTextEdit).toPlainText())
        buttons = dialog.findChild(QDialogButtonBox)
        buttons.button(QDialogButtonBox.StandardButton.Cancel).click()
        return dialog.result()

    monkeypatch.setattr(QDialog, "exec", skip)
    window.upload_paths([local_folder] if folder_upload else paths, "")
    complete(requests.pending[0])
    assert prompts == [f"a · /{skipped_target}"]
    if not with_new_file:
        assert not requests.queued and not requests.errors
        return

    task, upload = requests.queued[0]
    plan = window._pending_plans[task.id]
    assert {copy.target.path for copy in (*plan.copies, *plan.alternates)} == {prefix + fresh.name}
    assert plan.local_sources == (str(fresh),)
    target = plan.copies[0].target
    backend.failures["copy", target.fs, target.path] = BackendError(ErrorKind.NETWORK)
    task.result = upload(task.context)
    task.state = task.result.outcome
    window.update_task(task)
    assert task.state == "실패 / 결과 확인 필요"

    del backend.failures["copy", target.fs, target.path]
    skipped.unlink()
    if folder_upload:
        skipped.mkdir()
    backend.calls.clear()
    window.retry_task(task)
    complete(requests.pending[1])
    retry, upload = requests.queued[1]
    retry.result = upload(retry.context)
    retry.state = retry.result.outcome
    window.update_task(retry)
    assert retry.state == "완료"
    assert prompts == [f"a · /{skipped_target}"]
    assert all(call[2] not in {skipped.name, skipped_target} for call in backend.calls)
    assert backend.nodes[existing].data == b"old"
    assert all((account.fs, skipped_target) not in backend.nodes for account in accounts[1:])


@pytest.mark.parametrize("folder_exists", [False, True])
def test_folder_upload_shares_prepare_listings_but_rechecks_each_file_independently(
    requests, tmp_path, backend, accounts, folder_exists
):
    window = requests.window
    folder = tmp_path / "upload"
    folder.mkdir()
    for name in ("first.txt", "second.txt"):
        source = folder / name
        source.write_bytes(b"local")
        backend.put(folder.as_posix(), name, b"local")
    for account in accounts:
        backend.put(account.fs, "dest/upload" if folder_exists else "dest", None)

    backend.calls.clear()
    window.upload_paths([folder], "dest")
    complete(requests.pending[0])
    assert not requests.prompts
    for account in accounts:
        for path in ("", "dest"):
            assert backend.calls.count(("list", account.fs, path)) == 1
        assert backend.calls.count(("list", account.fs, "dest/upload")) == int(folder_exists)
        assert backend.calls.count(("stat", account.fs, "dest/upload")) == 1

    assert len(requests.queued) == (2 if folder_exists else 3)
    if not folder_exists:
        task, mkdir = requests.queued[0]
        assert mkdir(task.context).outcome == "완료"
    (first, upload_first), (task, upload) = requests.queued[-2:]
    target = window._pending_plans[task.id].copies[-1].target
    backend.put(target.fs, "dest/upload/SECOND.txt", b"external")
    assert upload_first(first.context).outcome == "완료"
    backend.calls.clear()
    assert upload(task.context).outcome == "실패 / 결과 확인 필요"
    assert not any(call[0] in {"copy", "mkdir"} for call in backend.calls)


@pytest.mark.parametrize("fail_mkdir", [False, True])
def test_folder_upload_files_wait_for_mkdir_then_run_as_independent_tasks(
    requests, qtbot, tmp_path, backend, accounts, monkeypatch, fail_mkdir
):
    window = requests.window
    folder = tmp_path / "upload"
    folder.mkdir()
    names = ("first.txt", "second.txt")
    for name in names:
        (folder / name).write_bytes(b"local")
        backend.put(folder.as_posix(), name, b"local")

    mkdir_started, release_mkdir, release_copies = Event(), Event(), Event()
    copying = {name: Event() for name in names}
    mkdir, copy_file = backend.mkdir, backend.copy_file

    def gated_mkdir(endpoint, context=None):
        mkdir_started.set()
        assert release_mkdir.wait(10)
        if fail_mkdir:
            raise BackendError(ErrorKind.NETWORK)
        return mkdir(endpoint, context)

    def gated_copy(source, target, context=None):
        copying[source.path].set()
        assert release_copies.wait(10)
        return copy_file(source, target, context)

    monkeypatch.setattr(backend, "mkdir", gated_mkdir)
    monkeypatch.setattr(backend, "copy_file", gated_copy)
    monkeypatch.setattr(window.transfers, "submit", requests.submit_transfer)
    try:
        window.upload_paths([folder], "")
        complete(requests.pending[0])
        folder_task, first, second = window.transfers.tasks.values()
        qtbot.waitUntil(mkdir_started.is_set)
        assert first.state == second.state == "대기"
        assert len(window._task_rows) == 3
        assert [task.label for task in (first, second)] == [
            f"파일 업로드 · /upload/{name}" for name in names
        ]
        release_mkdir.set()
        if fail_mkdir:
            qtbot.waitUntil(lambda: len(window._finished_tasks) == 3)
            assert first.state == second.state == "실패"
            assert not any(event.is_set() for event in copying.values())
            monkeypatch.setattr(backend, "mkdir", mkdir)
            release_copies.set()
            window.retry_task(first)
            complete(requests.pending[-1])
            qtbot.waitUntil(lambda: len(window._finished_tasks) == 5)
            assert copying[names[0]].is_set() and not copying[names[1]].is_set()
            assert list(window.transfers.tasks.values())[-1].state == "완료"
            return

        qtbot.waitUntil(lambda: all(event.is_set() for event in copying.values()))
        assert folder_task.state == "완료"
        window.cancel_task(first)
        release_copies.set()
        qtbot.waitUntil(lambda: len(window._finished_tasks) == 3)
        assert first.state == "취소됨" and second.state == "완료"

        window.retry_task(first)
        complete(requests.pending[-1])
        qtbot.waitUntil(lambda: len(window._finished_tasks) == 4)
        assert list(window.transfers.tasks.values())[-1].state == "완료"
        for name in names:
            path = f"upload/{name}"
            assert sum(call[0] == "copy" and call[2] == path for call in backend.calls) == 2
            assert [backend.nodes[a.fs, path].data for a in accounts if (a.fs, path) in backend.nodes] == [
                b"local", b"local"
            ]
    finally:
        release_mkdir.set()
        release_copies.set()
        qtbot.waitUntil(lambda: not window.transfers.busy)


@pytest.mark.parametrize("result", ["ready", "failed"])
def test_upload_preparation_progress_and_cancel_ignore_late_result(
    requests, tmp_path, backend, result
):
    window = requests.window
    source = tmp_path / "file.txt"
    source.write_bytes(b"local")
    backend.put(tmp_path.as_posix(), source.name, b"local")

    window.upload_paths([source], "")
    build, done, failed, context = requests.pending[0]
    assert window.upload_progress.isVisible() and window.cancel_upload_button.isEnabled()
    context.describe("대상 확인 · 응답 대기: a, b")
    window.statusBar().showMessage("다른 폴더의 목록을 조회하는 중…")
    assert window.upload_progress.isVisible() and "a, b" in window.upload_progress.text()
    plan = build(context) if result == "ready" else None

    window.cancel_upload_button.click()
    context.describe("늦게 도착한 조회 진행")
    assert context.cancelled.is_set() and window._preparing
    assert not window.cancel_upload_button.isEnabled()
    assert "준비 취소 중" in window.upload_progress.text()
    if result == "ready":
        done(plan)
    else:
        failed("취소됨")

    assert not window._preparing and window.upload_action.isEnabled()
    assert not window.upload_progress.isVisible() and not window.cancel_upload_button.isVisible()
    assert not requests.queued and not requests.prompts and not requests.errors


@pytest.mark.parametrize("ending", ["done", "failed", "start", "closing"])
def test_trash_list_does_not_queue_twice_or_reopen_after_close(requests, monkeypatch, ending):
    window = requests.window
    shown = []
    monkeypatch.setattr(
        window_module,
        "GoogleTrashAdapter",
        lambda *args: SimpleNamespace(list_items=lambda context: []),
    )
    monkeypatch.setattr(window, "trash_dialog", lambda *args: shown.append(args))
    if ending == "start":
        monkeypatch.setattr(window.workers, "submit", cannot_start)
    window.open_trash("a")
    if ending != "start":
        window.open_trash("a")
        assert len(requests.pending) == 1
        if ending == "failed":
            requests.pending[0][2]("list failed")
        else:
            if ending == "closing":
                window._closing = True
            complete(requests.pending[0])
    assert not window._trash_loading
    assert bool(shown) == (ending == "done")


def test_restore_guard_survives_reopening_and_reenables_on_completion(
    requests, monkeypatch, accounts
):
    window = requests.window
    item = TrashItem("deleted", "x", "x", None, False)
    adapter = SimpleNamespace(account=accounts[0])
    dialogs = []

    def interact(dialog):
        table = dialog.findChild(QTableWidget)
        button = next(
            b for b in dialog.findChildren(QPushButton) if b.text() == "선택 항목 복원..."
        )
        assert not button.isEnabled()
        table.selectRow(0)
        dialogs.append(dialog)
        if len(dialogs) == 1:

            def confirm(*args):
                button.clicked.emit()
                return True

            monkeypatch.setattr(window_module, "confirm", confirm)
            button.click()
            assert len(requests.queued) == 1
        else:
            assert not button.isEnabled()
            button.clicked.emit()
            assert len(requests.queued) == 1
            task = requests.queued[0][0]
            task.state = "완료"
            window.task_changed.emit(task)
            assert button.isEnabled()
        return QDialog.DialogCode.Rejected

    monkeypatch.setattr(QDialog, "exec", interact)
    window.trash_dialog(adapter, [item], capabilities(accounts[0]))
    assert window._restore_pending
    window.trash_dialog(adapter, [item], capabilities(accounts[0]))
    assert not window._restore_pending


def test_completed_close_ignores_repeated_shutdown_callbacks(requests):
    requests.window._shutdown_done = True
    requests.window.finish_close()
    requests.window.finish_close()
    assert not requests.pending


@pytest.mark.parametrize("action", ["replicate", "refresh", "download"])
def test_read_request_start_failure_releases_every_guard(requests, monkeypatch, action):
    window = requests.window
    monkeypatch.setattr(window.workers, "submit", cannot_start)
    if action == "replicate":
        window.replica_action("replicate", requests.entries[0].replicas[0])
        assert not window._replica_busy
    elif action == "refresh":
        requests.refresh()
        assert window._refreshing is None and window.refresh_action.isEnabled()
    else:
        monkeypatch.setattr(window, "choose_replica", lambda entry: entry.replicas[0])
        window.table.doubleClicked.emit(window.proxy.index(0, 0))
        assert not window._preparing and not requests.queued


@pytest.mark.parametrize("ending", ["cancel", "submit"])
def test_restore_without_registration_releases_item_guard(requests, monkeypatch, accounts, ending):
    window = requests.window
    item = TrashItem("deleted", "x", "x", None, False)
    adapter = SimpleNamespace(account=accounts[0])
    if ending == "cancel":
        monkeypatch.setattr(window_module, "confirm", lambda *args: False)
    else:
        monkeypatch.setattr(window.transfers, "submit", cannot_start)

    def interact(dialog):
        table = dialog.findChild(QTableWidget)
        table.selectRow(0)
        button = next(
            b for b in dialog.findChildren(QPushButton) if b.text() == "선택 항목 복원..."
        )
        button.click()
        assert not window._restore_pending and button.isEnabled()
        return QDialog.DialogCode.Rejected

    monkeypatch.setattr(QDialog, "exec", interact)
    window.trash_dialog(adapter, [item], capabilities(accounts[0]))
    assert not requests.queued


def test_issue_selection_tracks_conflicting_queued_paths(requests, backend, accounts):
    window = requests.window
    backend.put(accounts[0].fs, "only-a", b"one")
    backend.put(accounts[0].fs, "only-b", b"two")
    window.scan_results, window.scan_complete, _ = window.vfs.scan("")
    window.show_issues()
    originals = {entry.path: entry for entry in window.scan_results}
    window.prepare(lambda context: window.engine.plan_delete(originals["only-a"].replicas, context))
    complete(requests.pending[0])
    for path, enabled in (("only-a", False), ("only-b", True)):
        row = next(i for i, entry in enumerate(window.issue_dialog.entries) if entry.path == path)
        window.issue_dialog.table.selectRow(row)
        assert window.issue_dialog.resolve_button.isEnabled() == enabled
