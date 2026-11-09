from types import SimpleNamespace

import pytest
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QAbstractItemView, QApplication, QDockWidget, QPushButton

from cloud_cluster.gui import window as window_module
from cloud_cluster.gui.window import MainWindow
from cloud_cluster.settings import Settings, SettingsStore
from cloud_cluster.transfers import TaskContext, TransferTask


@pytest.fixture
def task_ui(qtbot, tmp_path):
    window = MainWindow(None, Settings(), SettingsStore(tmp_path / "ui"))
    qtbot.addWidget(window, before_close_func=lambda widget: setattr(widget, "_shutdown_done", True))
    window.resize(1000, 600)
    window.task_tree.setFixedHeight(145)
    window.task_tree.setColumnWidth(3, 200)
    window.show()
    tasks = [TransferTask(str(index), f"task {index}", TaskContext()) for index in range(100)]
    for task in tasks:
        window.transfers.tasks[task.id] = task
        window.update_task(task)
    tree = window.task_tree
    qtbot.waitUntil(lambda: tree.verticalScrollBar().maximum() > 0)
    qtbot.waitUntil(lambda: tree.horizontalScrollBar().maximum() > 0)
    try:
        yield SimpleNamespace(window=window, tree=tree, tasks=tasks)
    finally:
        window._closing = window._shutdown_done = True
        window.transfers.close()
        window.close()


def start(ui, index):
    task = ui.tasks[index]
    task.state = "진행 중"
    ui.window.update_task(task)
    return ui.window._task_rows[task.id]


def watch(ui, index):
    row = ui.window._task_rows[ui.tasks[index].id]
    ui.tree.scrollToItem(row, QAbstractItemView.ScrollHint.PositionAtCenter)
    return row


def test_individual_commands_share_the_row_menu(task_ui, qtbot, monkeypatch):
    ui = task_ui
    window = ui.window
    for task, state in zip(
        ui.tasks, ("완료", "실패", "일부 성공", "취소됨", "진행 중", "취소 요청")
    ):
        task.state = state
        window.update_task(task)
    retried, shown = [], []
    window._task_retries["1"] = (lambda: retried.append("1"), False, ())
    monkeypatch.setattr(window_module, "show_text", lambda parent, title, body: shown.append(body))
    ui.tree.setCurrentItem(window._task_rows["0"])

    def use_menu(label, expected_enabled, *, index=None):
        def choose():
            menu = QApplication.activePopupWidget()
            try:
                actions = {action.text(): action for action in menu.actions()}
                assert set(actions) == {
                    "작업 취소",
                    "최신 상태로 재시도...",
                    "작업 결과",
                    "이 작업 지우기",
                }
                enabled = {text for text, action in actions.items() if action.isEnabled()}
                assert enabled == expected_enabled
                qtbot.mouseClick(
                    menu,
                    Qt.MouseButton.LeftButton,
                    pos=menu.actionGeometry(actions[label]).center(),
                )
            finally:
                menu.close()

        timer = QTimer()
        timer.setSingleShot(True)
        timer.timeout.connect(choose)
        timer.start(0)
        try:
            if index is None:
                qtbot.keyClick(ui.tree, Qt.Key.Key_F10, Qt.KeyboardModifier.ShiftModifier)
            else:
                row = watch(ui, index)
                ui.tree.customContextMenuRequested.emit(ui.tree.visualItemRect(row).center())
            assert not timer.isActive()
        finally:
            timer.stop()
            if index is None:
                # Clear the synthetic Shift state left by the offscreen popup's event loop.
                qtbot.keyRelease(ui.tree, Qt.Key.Key_F10)

    use_menu("작업 취소", {"작업 취소", "작업 결과"}, index=4)
    assert ui.tasks[4].state == "취소 요청" and not ui.tasks[0].context.cancelled.is_set()
    failed_actions = {"최신 상태로 재시도...", "작업 결과", "이 작업 지우기"}
    use_menu("최신 상태로 재시도...", failed_actions, index=1)
    assert retried == ["1"]
    use_menu("이 작업 지우기", failed_actions, index=1)
    assert set(window._task_rows) == {task.id for task in ui.tasks} - {"1"}
    assert ui.tree.currentItem() is window._task_rows["0"] and "1" not in window._task_retries

    window.activateWindow()
    ui.tree.setFocus()
    qtbot.waitUntil(ui.tree.hasFocus)
    use_menu("작업 결과", {"작업 결과", "이 작업 지우기"})
    assert shown == ["task 0\n완료\n"]
    assert QApplication.keyboardModifiers() == Qt.KeyboardModifier.NoModifier


@pytest.mark.parametrize("completed_first", [True, False])
def test_bulk_clear_removes_only_matching_task_history(task_ui, completed_first):
    ui = task_ui
    window = ui.window
    completed, failed = window.clear_completed_button, window.clear_failed_button
    assert ui.tree.parentWidget().findChildren(QPushButton) == [completed, failed]
    assert not completed.isEnabled() and not failed.isEnabled()
    for task, state in zip(
        ui.tasks, ("완료", "실패", "실패 / 결과 확인 필요", "일부 성공", "취소됨", "진행 중", "취소 요청")
    ):
        task.state = state
        window.update_task(task)
    window._task_retries["1"] = (lambda: None, False, ())

    first, second = (completed, failed) if completed_first else (failed, completed)
    assert first.isEnabled() and second.isEnabled()
    first.click()
    removed = {"0"} if completed_first else {"1", "2", "3"}
    survivors = {task.id for task in ui.tasks} - removed
    assert set(window._task_rows) == set(window.transfers.tasks) == survivors
    assert ("1" in window._task_retries) == completed_first
    assert not first.isEnabled() and second.isEnabled()
    second.click()

    survivors = {task.id for task in ui.tasks[4:]}
    assert set(window._task_rows) == set(window.transfers.tasks) == survivors
    assert ui.tree.topLevelItemCount() == len(survivors)
    assert window._reported_tasks == {"4"} and not window._task_retries
    assert not completed.isEnabled() and not failed.isEnabled()
    window.task_changed.emit(ui.tasks[0])
    assert ui.tree.topLevelItemCount() == len(survivors)


def test_follows_new_running_row_without_changing_selection_or_horizontal_position(task_ui, qtbot):
    ui = task_ui
    anchor = start(ui, 30)
    qtbot.wait(1)
    watch(ui, 30)
    ui.tree.setCurrentItem(anchor)
    horizontal = ui.tree.horizontalScrollBar()
    horizontal.setValue(horizontal.maximum())
    value = horizontal.value()
    assert ui.window.task_row_visible("30") and not ui.window.task_row_visible("70")
    start(ui, 70)
    qtbot.waitUntil(lambda: ui.window.task_row_visible("70"))
    assert ui.tree.currentItem() is anchor and anchor.isSelected()
    assert horizontal.value() == value


def test_keeps_browsing_position_for_progress_updates_and_offscreen_task_changes(task_ui, qtbot):
    ui = task_ui
    start(ui, 30)
    qtbot.wait(1)
    ui.tree.verticalScrollBar().setValue(0)
    before = ui.tree.verticalScrollBar().value()
    task = ui.tasks[30]
    task.stats = {"bytes": 25, "totalBytes": 100}
    ui.window.update_task(task)
    start(ui, 70)
    qtbot.wait(1)
    assert ui.tree.verticalScrollBar().value() == before
    assert not ui.window.task_row_visible("30") and not ui.window.task_row_visible("70")
    watch(ui, 30)
    start(ui, 90)
    qtbot.waitUntil(lambda: ui.window.task_row_visible("90"))


def test_follows_the_next_task_across_a_gap_between_completion_and_start(task_ui, qtbot):
    ui = task_ui
    start(ui, 30)
    qtbot.wait(1)
    watch(ui, 30)
    ui.tasks[30].state = "완료"
    ui.window.update_task(ui.tasks[30])
    qtbot.wait(1)
    assert not ui.window._active_task_rows and ui.window._task_scroll_anchor == "30"
    start(ui, 70)
    qtbot.waitUntil(lambda: ui.window.task_row_visible("70"))


def test_follows_a_remaining_running_task_when_the_visible_one_finishes(task_ui, qtbot):
    ui = task_ui
    start(ui, 30)
    start(ui, 70)
    qtbot.wait(1)
    watch(ui, 30)
    assert not ui.window.task_row_visible("70")
    ui.tasks[30].state = "완료"
    ui.window.update_task(ui.tasks[30])
    qtbot.waitUntil(lambda: ui.window.task_row_visible("70"))


@pytest.mark.parametrize("state", ["진행 중", "42% · 진행 중", "취소 요청", "결과 재확인"])
def test_visible_active_states_allow_following_the_next_task(task_ui, qtbot, state):
    ui = task_ui
    row = ui.window._task_rows["30"]
    row.setText(1, state)
    qtbot.wait(1)
    watch(ui, 30)
    start(ui, 70)
    qtbot.waitUntil(lambda: ui.window.task_row_visible("70"))


def test_completion_then_recheck_in_one_update_does_not_jump_between_rows(task_ui, qtbot):
    ui = task_ui
    row = start(ui, 30)
    start(ui, 70)
    qtbot.wait(1)
    watch(ui, 30)
    before = ui.tree.verticalScrollBar().value()
    row.setText(1, "완료")
    row.setText(1, "결과 재확인")
    qtbot.wait(1)
    assert ui.tree.verticalScrollBar().value() == before
    assert ui.window.task_row_visible("30") and not ui.window.task_row_visible("70")
    row.setText(1, "완료")
    qtbot.waitUntil(lambda: ui.window.task_row_visible("70"))


def test_user_scrolling_before_the_deferred_follow_preserves_their_position(task_ui, qtbot):
    ui = task_ui
    start(ui, 30)
    qtbot.wait(1)
    watch(ui, 30)
    start(ui, 70)
    ui.tree.verticalScrollBar().setValue(0)
    qtbot.wait(1)
    assert ui.tree.verticalScrollBar().value() == 0


@pytest.mark.parametrize("queued_cancelled", [False, True])
def test_an_idle_queue_does_not_follow_a_later_unseen_task(task_ui, qtbot, queued_cancelled):
    ui = task_ui
    start(ui, 30)
    qtbot.wait(1)
    watch(ui, 30)
    for task in ui.tasks:
        if task.id != "30" and not (queued_cancelled and task.id == "70"):
            task.state = "완료"
            ui.window.update_task(task)
    ui.tasks[30].state = "완료"
    ui.window.update_task(ui.tasks[30])
    qtbot.wait(1)
    if queued_cancelled:
        assert ui.window._task_scroll_anchor == "30"
        ui.tasks[70].state = "취소됨"
        ui.window.update_task(ui.tasks[70])
    assert ui.window._task_scroll_anchor is None
    before = ui.tree.verticalScrollBar().value()
    new = TransferTask("new", "new task", TaskContext(), state="진행 중")
    ui.window.transfers.tasks[new.id] = new
    ui.window.update_task(new)
    qtbot.wait(1)
    assert ui.tree.verticalScrollBar().value() == before


def test_hidden_task_panel_does_not_follow(task_ui, qtbot):
    ui = task_ui
    start(ui, 30)
    qtbot.wait(1)
    watch(ui, 30)
    ui.window.findChild(QDockWidget).hide()
    qtbot.wait(1)
    before = ui.tree.verticalScrollBar().value()
    start(ui, 70)
    qtbot.wait(1)
    assert ui.tree.verticalScrollBar().value() == before


def test_reset_before_a_pending_follow_does_not_access_removed_rows(task_ui, qtbot):
    ui = task_ui
    start(ui, 30)
    qtbot.wait(1)
    watch(ui, 30)
    start(ui, 70)
    assert ui.window._task_scroll_pending
    ui.window._task_rows.clear()
    ui.window._active_task_rows.clear()
    ui.window._task_scroll_anchor = None
    ui.tree.clear()
    qtbot.wait(1)
    assert not ui.window._task_scroll_pending and ui.tree.topLevelItemCount() == 0
