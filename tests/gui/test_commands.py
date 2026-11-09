from threading import Event
from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QDialog, QFileDialog, QInputDialog

from cloud_cluster.domain import CloudAccount, Listing, OperationResult, Replica, StepResult
from cloud_cluster.gui import window as window_module
from cloud_cluster.gui.dialogs import AccountDialog, ReplicaDialog, SettingsDialog
from cloud_cluster.gui.window import MainWindow
from cloud_cluster.replicas import merge
from cloud_cluster.settings import Settings, SettingsStore
from cloud_cluster.transfers import TaskContext, TransferTask


@pytest.mark.parametrize(
    ("step", "expected"),
    [
        (
            StepResult("", "", "중단", "rejected", "Box · 파일 크기 제한 초과"),
            "Box · 파일 크기 제한 초과",
        ),
        (
            StepResult("alt", "file.txt", "대체 업로드", "ok", "Box · 파일 크기 제한 초과"),
            "대체 업로드 완료 · 다른 계정 · Box · 파일 크기 제한 초과 · /file.txt",
        ),
    ],
)
def test_upload_task_row_keeps_failure_or_fallback_reason(qtbot, tmp_path, step, expected):
    window = MainWindow(None, Settings(), SettingsStore(tmp_path / "ui"))
    qtbot.addWidget(window)
    window.settings.accounts = [CloudAccount("alt", "다른 계정", "drive")]
    result = OperationResult("파일 업로드", (step,))
    task = TransferTask("upload", result.label, TaskContext(), state=result.outcome, result=result)
    window.transfers.tasks[task.id] = task
    try:
        window.update_task(task)
        row = window._task_rows[task.id]
        assert row.text(2) == expected
        assert row.toolTip(2) == expected
    finally:
        qtbot.waitUntil(lambda: not window.workers.pending)
        window.close()
        qtbot.waitUntil(lambda: window._shutdown_done)


def test_command_names_match_opened_dialogs(qtbot, monkeypatch, tmp_path):
    settings = Settings()
    manager = SimpleNamespace(settings=settings)
    window = MainWindow(None, settings, SettingsStore(tmp_path / "ui"), manager)
    qtbot.addWidget(window)
    replica = Replica("a", "file.txt", size=0)
    entry = merge([Listing("a", (replica,))], {})[0]
    try:
        for action, dialog in [
            (window.accounts_action, AccountDialog(manager)),
            (window.settings_action, SettingsDialog(settings)),
            (window.properties_action, ReplicaDialog([entry], [])),
        ]:
            qtbot.addWidget(dialog)
            assert action.text() == dialog.windowTitle().split(" · ")[0]
            assert not action.text().endswith("...")

        captions = []

        def record(caption, result):
            captions.append(caption)
            return result

        monkeypatch.setattr(
            QFileDialog, "getOpenFileNames", lambda parent, title: record(title, ([], ""))
        )
        monkeypatch.setattr(
            QFileDialog, "getExistingDirectory", lambda parent, title: record(title, "")
        )
        monkeypatch.setattr(
            QFileDialog,
            "getSaveFileName",
            lambda parent, title, *args, **kwargs: record(title, ("", "")),
        )
        monkeypatch.setattr(
            QInputDialog, "getText", lambda parent, title, *args, **kw: record(title, ("", False))
        )
        monkeypatch.setattr(window, "selected", lambda: [entry])
        for action in (
            window.upload_action,
            window.folder_upload_action,
            window.mkdir_action,
            window.rename_action,
        ):
            assert action.text().endswith("...")
            action.trigger()
            assert captions.pop() == action.text().removesuffix("...")
        window.download_replica(replica)
        assert captions.pop() == window.download_action.text().removesuffix("...")
    finally:
        qtbot.waitUntil(lambda: not window.workers.pending)
        window.close()
        qtbot.waitUntil(lambda: window._shutdown_done)


def test_pending_config_change_blocks_account_management_until_restart(
    qtbot, monkeypatch, tmp_path
):
    settings = Settings(enabled_remotes=["saved"])
    manager = SimpleNamespace(settings=settings)
    store = SettingsStore(tmp_path / "ui")
    window = MainWindow(None, settings, store, manager)
    qtbot.addWidget(window)
    selected = str(tmp_path / "other.conf")
    (tmp_path / "other.conf").write_text("[other]\ntype = local\n", encoding="utf-8")

    def select(dialog):
        dialog.config.setText(selected)
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(SettingsDialog, "exec", select)
    try:
        qtbot.waitUntil(lambda: not window.workers.pending)
        assert window.accounts_action.isEnabled()
        window.settings_dialog()
        assert not window.accounts_action.isEnabled()
        assert manager.settings.rclone_config == selected
        assert manager.settings.enabled_remotes == []
        assert store.load().enabled_remotes == []
        assert "재시작" in window.banner.text()
        selected = ""
        window.settings_dialog()
        assert window.accounts_action.isEnabled()
    finally:
        qtbot.waitUntil(lambda: not window.workers.pending)
        window.close()
        qtbot.waitUntil(lambda: window._shutdown_done)


def test_account_management_allows_reading_while_changes_wait(qtbot, monkeypatch, tmp_path):
    settings = Settings(accounts=[CloudAccount("remote:a", "a", "local")])
    manager = SimpleNamespace(settings=settings, quota=lambda account, context: None)
    window = MainWindow(None, settings, SettingsStore(tmp_path / "ui"), manager)
    qtbot.addWidget(window)
    release = Event()
    task = TransferTask("finished-before-signal", "upload", TaskContext(), state="완료")
    window.transfers.tasks[task.id] = task
    window.workers.submit(lambda context: release.wait(5))

    def unexpected_notice(*args):
        pytest.fail("account management should open without a warning")

    def inspect(dialog):
        qtbot.waitUntil(lambda: not dialog.quota_workers.pending)
        assert dialog.table.item(0, 0).text() == "a"
        assert dialog.table.item(0, 3).text() == "미지원"
        dialog.table.selectRow(0)
        buttons = [*dialog.account_buttons, dialog.toggle_button]
        assert not dialog.change_notice.isHidden()
        assert all(not button.isEnabled() for button in buttons)
        for change in (dialog.create, dialog.reauthenticate, dialog.toggle, dialog.delete):
            change()
        assert not dialog.workers.pending

        release.set()
        qtbot.waitUntil(lambda: not window.workers.pending)
        assert all(not button.isEnabled() for button in buttons)
        window.update_task(task)
        assert all(button.isEnabled() for button in buttons)
        assert dialog.change_notice.isHidden()

    monkeypatch.setattr(window_module, "message", unexpected_notice)
    monkeypatch.setattr(AccountDialog, "exec", inspect)
    try:
        window.accounts_action.trigger()
        assert task.id in window._reported_tasks
        assert window._account_dialog is None
    finally:
        release.set()
        qtbot.waitUntil(lambda: not window.workers.pending)
        window.close()
        qtbot.waitUntil(lambda: window._shutdown_done)


@pytest.mark.parametrize("last", ["lookup", "task"])
def test_settings_can_be_edited_before_saving_is_available(qtbot, monkeypatch, tmp_path, last):
    store = SettingsStore(tmp_path / "ui")
    window = MainWindow(None, Settings(), store)
    manager = window.transfers
    qtbot.addWidget(window)
    release = Event()
    notices = []
    monkeypatch.setattr(window_module, "message", lambda *args: notices.append(args))
    task = TransferTask("finished-before-signal", "upload", TaskContext(), state="완료")
    window.transfers.tasks[task.id] = task
    window.workers.submit(lambda context: release.wait(5))

    def edit(dialog):
        assert dialog.save_button.isEnabled()
        dialog.transfers.setValue(3)
        assert not dialog.save_notice.isHidden()
        assert not dialog.save_button.isEnabled()
        dialog.save_button.click()
        assert not store.path.exists()
        if last == "lookup":
            window.update_task(task)
            assert not dialog.save_button.isEnabled()
            release.set()
            qtbot.waitUntil(dialog.save_button.isEnabled)
        else:
            release.set()
            qtbot.waitUntil(lambda: not window.workers.pending)
            assert not dialog.save_button.isEnabled()
            window.update_task(task)
            assert dialog.save_button.isEnabled()
        assert dialog.save_notice.isHidden()
        dialog.save_button.click()
        return dialog.result()

    monkeypatch.setattr(SettingsDialog, "exec", edit)
    try:
        window.settings_dialog()
        assert store.load().transfers == window.settings.transfers == 3
        assert window.transfers is manager and manager.tasks[task.id] is task
        assert task.id in window._finished_tasks and task.id in window._task_rows
        assert not notices
    finally:
        release.set()
        qtbot.waitUntil(lambda: not window.workers.pending)
        window.close()
        qtbot.waitUntil(lambda: window._shutdown_done)


def test_live_settings_preserve_active_tasks(qtbot, monkeypatch, tmp_path, backend):
    store = SettingsStore(tmp_path / "ui")
    window = MainWindow(backend, Settings(), store)
    qtbot.addWidget(window)
    release, started = Event(), Event()
    manager = window.transfers
    previous_level = window_module.logger.level

    def hold(context):
        started.set()
        assert release.wait(5)

    def edit(dialog):
        dialog.log_level.setCurrentText("DEBUG")
        dialog.download.setText(str(tmp_path))
        dialog.ttl.setValue(600)
        dialog.retries.setValue(4)
        dialog.config.setText(str(tmp_path / "other.conf"))
        assert not dialog.save_button.isEnabled()
        dialog.config.clear()
        assert dialog.save_button.isEnabled() and dialog.save_notice.isHidden()
        dialog.save_button.click()
        return dialog.result()

    monkeypatch.setattr(SettingsDialog, "exec", edit)
    try:
        task = manager.submit("upload", hold)
        qtbot.waitUntil(started.is_set)
        row = window._task_rows[task.id]
        window.settings_dialog()
        assert window.transfers is manager and window._task_rows[task.id] is row
        assert task.state == "진행 중" and not task.context.cancelled.is_set()
        assert task.context.retries == 2
        assert manager.submit("next", lambda context: None).context.retries == 4
        assert window.vfs.cache.ttl == 600
        saved = store.load()
        assert (saved.log_level, saved.download_dir, saved.cache_ttl, saved.retries) == (
            "DEBUG", str(tmp_path), 600, 4
        )
    finally:
        release.set()
        window_module.logger.setLevel(previous_level)
        qtbot.waitUntil(lambda: not manager.busy and not window.workers.pending)
        window._shutdown_done = True
        window.vfs.close()
        manager.close()
        window.close()


def test_replication_choices_exclude_provider_reserved_path(qtbot, monkeypatch, backend, tmp_path):
    accounts = [
        CloudAccount("p", "pikpak", "pikpak"),
        CloudAccount("a", "a", "local"),
        CloudAccount("b", "b", "local"),
    ]
    backend.put(accounts[1].fs, "My Pack/file.txt", b"data")
    window = MainWindow(backend, Settings(accounts=accounts), SettingsStore(tmp_path / "ui"))
    qtbot.addWidget(window)
    labels = []

    def choose(parent, title, prompt, choices, **kwargs):
        labels.extend(choices)
        return "", False

    monkeypatch.setattr(QInputDialog, "getItem", choose)
    try:
        qtbot.waitUntil(lambda: not window.workers.pending)
        window.replica_action("replicate", Replica("a", "My Pack/file.txt", size=4))
        qtbot.waitUntil(lambda: not window.workers.pending)
        assert labels == ["b (추천)"]
    finally:
        qtbot.waitUntil(lambda: not window.workers.pending)
        window._shutdown_done = True
        window.vfs.close()
        window.transfers.close()
        window.close()
