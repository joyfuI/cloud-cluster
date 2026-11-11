from dataclasses import replace
from threading import Event
from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QDialog, QPushButton

from cloud_cluster.accounts import AccountManager
from cloud_cluster.domain import (
    BackendCapabilities,
    BackendError,
    CloudAccount,
    Endpoint,
    ErrorKind,
    QuotaSnapshot,
    Replica,
)
from cloud_cluster.gui import dialogs
from cloud_cluster.gui.dialogs import AccountDialog, AuthDialog, SettingsDialog
from cloud_cluster.gui.window import MainWindow
from cloud_cluster.operations import MutationPlan
from cloud_cluster.settings import Settings, SettingsStore


def test_empty_trash_from_account_management(qtbot, monkeypatch, backend, tmp_path):
    drive = CloudAccount("drive", "drive-test", "drive")
    box = CloudAccount("box", "box-test", "box")
    onedrive = CloudAccount("one", "one-test", "onedrive")
    pcloud = CloudAccount("pcloud", "pcloud-test", "pcloud")
    settings = Settings(accounts=[drive, box, onedrive, pcloud])
    store = SettingsStore(tmp_path / "ui")
    manager = AccountManager(backend, store, settings)
    started, release = Event(), Event()
    delete_started, release_delete, later_started = Event(), Event(), Event()
    calls, prompts = [], []

    def cleanup(account, context):
        calls.append(account)
        if account == drive:
            started.set()
            assert release.wait(10)

    def delete(replicas, context, *, approved):
        delete_started.set()
        assert release_delete.wait(10)
        raise ValueError("선행 삭제 실패")

    def confirm(parent, title, details, prompt):
        prompts.append(details)
        return len(prompts) > 1

    providers = {account.remote: account.provider for account in settings.accounts}
    monkeypatch.setattr(
        backend, "rpc", lambda method, *, name: {"type": providers[name]}, raising=False
    )
    monkeypatch.setattr(backend, "empty_trash", cleanup, raising=False)
    monkeypatch.setattr(
        backend, "features",
        lambda account, context: BackendCapabilities(quota=True, trash_cleanup=account != pcloud),
    )
    monkeypatch.setattr(
        backend, "about",
        lambda account, context: QuotaSnapshot(
            4096, 2048 if account == drive and release.is_set() else 1024
        ),
    )
    monkeypatch.setattr(dialogs, "confirm", confirm)
    window = MainWindow(backend, settings, store, manager)
    qtbot.addWidget(window)
    window.show()
    monkeypatch.setattr(window.engine, "execute_delete", delete)

    def interact(dialog):
        qtbot.waitUntil(lambda: not dialog.quota_workers.pending)
        button = dialog.empty_trash_button
        for row in (2, 3):
            dialog.table.selectRow(row)
            assert not button.isEnabled()
        dialog.table.selectRow(0)
        assert button.isEnabled()
        assert all(not b.isEnabled() for b in dialog.account_buttons)
        button.click()
        assert len(window.transfers.tasks) == 1
        button.click()
        trash = next(task for task in window.transfers.tasks.values() if task.action == "휴지통 비우기")
        assert trash.state == "대기" and not started.is_set()
        assert not button.isEnabled()
        button.clicked.emit()
        assert len(window.transfers.tasks) == 2
        window.cancel_task(trash)
        assert trash.state == "취소됨" and not started.is_set()
        assert button.isEnabled()
        button.click()
        trash = next(reversed(window.transfers.tasks.values()))
        assert trash.state == "대기"

        dialog.table.selectRow(1)
        assert button.isEnabled()
        button.click()
        qtbot.waitUntil(lambda: calls == [box])
        assert trash.state == "대기" and not started.is_set()

        later = window.enqueue_plan(
            MutationPlan("새 폴더", mkdirs=(Endpoint(drive.fs, "later", drive.id),)),
            None,
            execute=lambda context: later_started.set(),
        )
        assert later.state == "대기"
        release_delete.set()
        qtbot.waitUntil(started.is_set)
        assert not later_started.is_set()
        dialog.table.selectRow(0)
        assert not button.isEnabled()
        release.set()
        qtbot.waitUntil(later_started.is_set)
        qtbot.waitUntil(lambda: dialog.table.item(0, 4).text() == "2.0 KB")
        qtbot.waitUntil(button.isEnabled)
        assert not window._trash_tasks
        assert trash.state == "완료"
        return QDialog.DialogCode.Rejected

    monkeypatch.setattr(AccountDialog, "exec", interact)
    try:
        qtbot.waitUntil(lambda: window.snapshot is not None and not window.workers.pending)
        window.enqueue_delete((Replica(drive.id, "before", False),), replica_request=False)
        qtbot.waitUntil(delete_started.is_set)
        window.accounts_dialog()
        assert calls == [box, drive]
        assert all("실행 시점" in text and "영구 삭제" in text for text in prompts)
    finally:
        release_delete.set()
        release.set()
        qtbot.waitUntil(lambda: not window.transfers.busy and not window.workers.pending)
        window._shutdown_done = True
        window.vfs.close()
        window.transfers.close()
        window.close()


def test_account_buttons_disable_during_provider_lookup_and_recover(qtbot, monkeypatch):
    account = CloudAccount("a", "a", "local")
    started, release = Event(), Event()
    calls = []

    def providers():
        calls.append(True)
        started.set()
        assert release.wait(5)
        return []

    manager = SimpleNamespace(
        settings=Settings(accounts=[account]), quota=lambda *args: None, providers=providers
    )
    dialog = AccountDialog(manager)
    qtbot.addWidget(dialog)
    qtbot.waitUntil(lambda: not dialog.quota_workers.pending)
    dialog.table.selectRow(0)
    monkeypatch.setattr(dialog, "_create", lambda *args: None)
    try:
        dialog.create()
        dialog.create()
        dialog.reauthenticate()
        qtbot.waitUntil(started.is_set)
        assert len(calls) == 1
        assert not dialog.toggle_button.isEnabled()
        assert not any(button.isEnabled() for button in dialog.account_buttons)
    finally:
        release.set()
        qtbot.waitUntil(lambda: not dialog.workers.pending)
        dialog.reject()
    assert dialog.toggle_button.isEnabled()
    assert all(button.isEnabled() for button in dialog.account_buttons)


@pytest.mark.parametrize("reauthenticate", [False, True])
@pytest.mark.parametrize(
    "provider, permission, default",
    [
        ("drive", "scope", "drive.readonly"),
        ("onedrive", "access_scopes", "Files.Read.All offline_access"),
        ("hidrive", "scope_access", "ro"),
        ("google photos", "read_only", True),
    ],
)
def test_auth_dialog_has_no_initial_options_and_fits_long_descriptions(
    qtbot, qapp, reauthenticate, provider, permission, default
):
    account = CloudAccount("a", "cloud", provider, "existing") if reauthenticate else None
    manager = SimpleNamespace(
        settings=Settings(accounts=[account] if account else []), quota=lambda *args: None
    )
    accounts_dialog = AccountDialog(manager)
    qtbot.addWidget(accounts_dialog)
    qtbot.waitUntil(lambda: not accounts_dialog.quota_workers.pending)
    assert "계정 추가..." in [button.text() for button in accounts_dialog.findChildren(QPushButton)]
    providers = [
        {
            "Name": provider,
            "Description": provider,
            "Options": [
                {"Name": "client_id", "Help": "OAuth Client Id.\n\nLeave blank normally."},
                {"Name": permission, "Default": default},
            ],
        },
        {
            "Name": "s3",
            "Description": "Amazon S3 Compliant Storage Providers " + "Provider, " * 60,
            "Options": [{"Name": "future_option", "Help": "Future provider option help."}],
        },
    ]
    dialog = AuthDialog(manager, providers, account, accounts_dialog)
    qtbot.addWidget(dialog)
    if not reauthenticate:
        index = next(
            i
            for i in range(dialog.provider.count())
            if dialog.provider.itemData(i)["Name"] == provider
        )
        dialog.provider.setCurrentIndex(index)
    dialog.show()
    qapp.processEvents()
    assert dialog.height() == 360
    assert dialog.windowTitle() == ("재인증" if reauthenticate else "계정 추가")
    assert dialog.windowTitle() + "..." in [
        button.text() for button in accounts_dialog.findChildren(QPushButton)
    ]
    assert dialog.next_button.text() == ("재인증" if reauthenticate else "추가")
    assert not hasattr(dialog, "root")
    assert not hasattr(dialog, "fields")
    assert not hasattr(dialog, "options_form")
    assert dialog.question_scroll.isHidden()
    assert dialog.width() <= 620
    assert dialog.minimumSizeHint().width() <= 620
    if not reauthenticate:
        index = next(
            i for i in range(dialog.provider.count()) if dialog.provider.itemData(i)["Name"] == "s3"
        )
        dialog.provider.setCurrentIndex(index)
        qapp.processEvents()
        assert dialog.width() <= 620
        assert not hasattr(dialog, "fields")
    dialog.response({
        "State": "*all-set,0,false",
        "Option": {"Name": "client_id", "Help": "OAuth Client Id.\n\nLeave blank normally."},
    })
    assert dialog.help.text() == "OAuth 클라이언트 ID입니다.\n\n일반적으로 비워 두세요."
    assert not dialog.question_scroll.isHidden()
    qapp.processEvents()
    assert dialog.height() == 360
    dialog.response({
        "State": "*all-set,1,true",
        "Option": {"Name": "long_help", "Help": "긴 설정 안내입니다.\n" * 30},
    })
    qtbot.waitUntil(lambda: dialog.question_scroll.verticalScrollBar().maximum() > 0)
    assert dialog.height() == 360
    dialog.reject()


@pytest.mark.parametrize("reauthenticate", [False, True])
def test_provider_display_names_are_sorted_and_default_selection_is_preserved(
    qtbot, reauthenticate
):
    providers = [
        {"Name": "zzz", "Description": "가나다 저장소"},
        {"Name": "onedrive", "Description": "OneDrive"},
        {"Name": "drive", "Description": "Google Drive"},
        {"Name": "aaa", "Description": "나다라 저장소"},
        {"Name": "dropbox", "Description": "Dropbox"},
        {"Name": "alpha"},
    ]
    account = CloudAccount("a", "saved", "onedrive") if reauthenticate else None
    manager = SimpleNamespace(settings=Settings(accounts=[account] if account else []))
    dialog = AuthDialog(manager, providers, account)
    qtbot.addWidget(dialog)
    assert [dialog.provider.itemText(i) for i in range(dialog.provider.count())] == [
        "alpha",
        "Dropbox",
        "Google Drive",
        "OneDrive",
        "가나다 저장소",
        "나다라 저장소",
    ]
    assert dialog.provider.currentData()["Name"] == ("onedrive" if reauthenticate else "drive")
    assert dialog.provider.isEnabled() is not reauthenticate
    dialog.reject()


@pytest.mark.parametrize("reauthenticate", [False, True])
def test_auth_initial_request_only_sends_name_and_provider(qtbot, monkeypatch, reauthenticate):
    account = CloudAccount("a", "saved", "koofr") if reauthenticate else None
    manager = SimpleNamespace(settings=Settings(accounts=[account] if account else []))
    dialog = AuthDialog(manager, [{"Name": "koofr"}], account)
    qtbot.addWidget(dialog)
    calls, operations = [], []
    manager.configure = lambda *args, **kwargs: calls.append((args, kwargs))
    monkeypatch.setattr(dialog.workers, "submit", lambda op, *args: operations.append(op) or object())
    dialog.next()
    operations[0](None)
    args, kwargs = calls[0]
    assert args[1:3] == ("koofr", {})
    assert kwargs["reconnect"] is reauthenticate
    assert kwargs["state"] is None
    dialog.context = None
    dialog.timer.stop()
    dialog.reject()


@pytest.mark.parametrize("accepted", [False, True])
@pytest.mark.parametrize("reauthenticate", [False, True])
def test_auth_refreshes_config_and_only_successful_additions_are_enabled(
    qtbot, monkeypatch, tmp_path, accepted, reauthenticate
):
    remotes = {"existing": "dropbox"}

    def rpc(method, **parameters):
        return (
            {"remotes": list(remotes)}
            if method == "config/listremotes"
            else {"type": remotes[parameters["name"]]}
        )

    store = SettingsStore(tmp_path / "app")
    manager = AccountManager(
        SimpleNamespace(rpc=rpc, features=lambda *args: BackendCapabilities()), store, Settings()
    )
    manager.refresh()
    account = manager.settings.accounts[0] if reauthenticate else None
    dialog = AccountDialog(manager)
    qtbot.addWidget(dialog)
    qtbot.waitUntil(lambda: not dialog.quota_workers.pending)

    def authenticate(auth):
        remotes[auth.name.text()] = "dropbox"
        if accepted:
            auth.response({})
            assert auth.result() == QDialog.DialogCode.Accepted
        return QDialog.DialogCode.Accepted if accepted else QDialog.DialogCode.Rejected

    monkeypatch.setattr(AuthDialog, "exec", authenticate)
    monkeypatch.setattr(
        dialog.workers, "submit", lambda operation, done, failed: done(operation(None))
    )
    changed = []
    dialog.changed.connect(lambda: changed.append(True))
    dialog._create([{"Name": "dropbox"}], account)
    expected = ["cloud_2"] if accepted and not reauthenticate else []
    assert manager.settings.enabled_remotes == expected
    assert store.load().enabled_remotes == expected
    assert {a.remote for a in manager.settings.accounts} == set(remotes)
    assert all(
        a.root == "" and a.enabled == (a.remote in expected) for a in manager.settings.accounts
    )
    assert dialog.table.rowCount() == len(remotes)
    assert changed == [True]
    qtbot.waitUntil(lambda: not dialog.quota_workers.pending)
    dialog.reject()


def test_account_list_uses_config_and_toggle_only_saves_usage(qtbot, tmp_path):
    store = SettingsStore(tmp_path / "app")
    store.config_path.write_text("[existing]\ntype = local\n", encoding="utf-8")
    before = store.config_path.read_bytes()
    backend = SimpleNamespace(
        rpc=lambda method, **kw: (
            {"remotes": ["existing"]} if method == "config/listremotes" else {"type": "local"}
        ),
        features=lambda *args: BackendCapabilities(),
    )
    manager = AccountManager(backend, store, Settings())
    manager.open()
    dialog = AccountDialog(manager)
    qtbot.addWidget(dialog)
    labels = [button.text() for button in dialog.findChildren(QPushButton)]
    assert "기존 리모트 연결..." not in labels
    assert "연결 해제..." not in labels
    assert "계정 삭제..." in labels
    assert "연결·용량 확인" not in labels
    assert dialog.table.columnCount() == 5
    assert not dialog.toggle_button.isEnabled()
    assert dialog.toggle_button.text() == "사용 여부 변경"
    assert dialog.table.item(0, 0).text() == "existing"
    assert dialog.table.item(0, 2).text() == "사용 안 함"
    dialog.table.selectRow(0)
    assert dialog.toggle_button.isEnabled()
    assert dialog.toggle_button.text() == "사용으로 변경"
    for enabled in (True, False):
        dialog.toggle_button.click()
        assert manager.settings.accounts[0].enabled is enabled
        assert dialog.toggle_button.text() == (
            "사용 안 함으로 변경" if enabled else "사용으로 변경"
        )
        assert store.load().enabled_remotes == (["existing"] if enabled else [])
        assert store.config_path.read_bytes() == before
    dialog.table.clearSelection()
    assert dialog.selected() is None
    assert not dialog.toggle_button.isEnabled()
    qtbot.waitUntil(lambda: not dialog.quota_workers.pending)
    dialog.reject()


def test_config_can_change_with_accounts_and_resets_usage(qtbot, tmp_path):
    settings = Settings(
        accounts=[CloudAccount("existing", "existing", "local")], enabled_remotes=["existing"]
    )
    dialog = SettingsDialog(settings)
    qtbot.addWidget(dialog)
    assert dialog.value().enabled_remotes == ["existing"]
    dialog.config.setText(str(tmp_path / "other.conf"))
    updated = dialog.value()
    assert updated.rclone_config == str(tmp_path / "other.conf")
    assert updated.enabled_remotes == []
    assert updated.accounts == settings.accounts
    assert settings.enabled_remotes == ["existing"]


@pytest.mark.parametrize("accepted", [False, True])
def test_account_deletion_confirms_target_and_refreshes_list(qtbot, monkeypatch, accepted):
    settings = Settings(
        accounts=[CloudAccount("remote:remove", "remove", "local")],
        enabled_remotes=["remove"],
    )
    removed, prompts, changed = [], [], []

    def delete(remote):
        removed.append(remote)
        settings.accounts = []
        settings.enabled_remotes = []

    def confirm(parent, title, text):
        prompts.append((title, text))
        return accepted

    manager = SimpleNamespace(settings=settings, delete=delete, quota=lambda *args: None)
    dialog = AccountDialog(manager)
    qtbot.addWidget(dialog)
    qtbot.waitUntil(lambda: not dialog.quota_workers.pending)
    dialog.changed.connect(lambda: changed.append(True))
    monkeypatch.setattr(dialogs, "confirm", confirm)
    dialog.delete()
    assert not prompts and not removed
    dialog.table.selectRow(0)
    dialog.workers.pending.add("busy")
    dialog.delete()
    assert not prompts
    dialog.workers.pending.clear()
    button = next(b for b in dialog.findChildren(QPushButton) if b.text() == "계정 삭제...")
    button.click()
    qtbot.waitUntil(lambda: not dialog.workers.pending)
    assert prompts[0][0] == "계정 삭제"
    assert "remove" in prompts[0][1] and "rclone config" in prompts[0][1]
    assert "클라우드 파일과 서비스의 OAuth 승인은 유지" in prompts[0][1]
    assert removed == (["remove"] if accepted else [])
    assert changed == ([True] if accepted else [])
    assert dialog.table.rowCount() == (0 if accepted else 1)
    dialog.reject()


def test_account_delete_error_refreshes_partial_state(qtbot, monkeypatch):
    settings = Settings(
        accounts=[CloudAccount("remote:remove", "remove", "local")],
        enabled_remotes=["remove"],
    )
    errors, changed = [], []

    def delete(remote):
        settings.accounts = [CloudAccount("remote:remove", "remove", "local", enabled=False)]
        settings.enabled_remotes = []
        raise OSError("delete not confirmed")

    dialog = AccountDialog(
        SimpleNamespace(settings=settings, delete=delete, quota=lambda *args: None)
    )
    qtbot.addWidget(dialog)
    qtbot.waitUntil(lambda: not dialog.quota_workers.pending)
    dialog.table.selectRow(0)
    dialog.changed.connect(lambda: changed.append(True))
    monkeypatch.setattr(dialogs, "confirm", lambda *args: True)
    monkeypatch.setattr(dialogs, "message", lambda parent, error: errors.append(str(error)))
    dialog.delete()
    qtbot.waitUntil(lambda: not dialog.workers.pending)
    assert errors == ["delete not confirmed"]
    assert changed == [True]
    assert dialog.table.item(0, 2).text() == "사용 안 함"
    assert dialog.status.text() == "계정 삭제 결과를 확인하세요."
    dialog.reject()


def test_capacity_rows_load_independently_and_keep_selection(qtbot):
    settings = Settings(
        accounts=[CloudAccount(name, name, "local", enabled=name == "a") for name in "abcd"]
    )
    release = Event()
    calls = []

    def quota(account, context):
        calls.append(account.id)
        if account.id == "a":
            while not release.wait(0.01):
                context.check()
            return QuotaSnapshot(total=1024, free=0)
        if account.id == "b":
            return QuotaSnapshot(total=None, free=2048)
        if account.id == "c":
            return None
        raise BackendError(ErrorKind.AUTH)

    def set_enabled(remote, enabled):
        settings.accounts = [
            replace(a, enabled=enabled) if a.remote == remote else a for a in settings.accounts
        ]

    dialog = AccountDialog(SimpleNamespace(settings=settings, quota=quota, set_enabled=set_enabled))
    qtbot.addWidget(dialog)
    try:
        assert dialog.layout().itemAt(0).widget() is dialog.table
        assert [dialog.table.horizontalHeaderItem(i).text() for i in (3, 4)] == [
            "전체 용량",
            "사용 가능 용량",
        ]
        assert dialog.table.item(0, 3).text() == "조회 중…"
        assert not dialog.toggle_button.isEnabled()
        dialog.table.selectRow(0)
        dialog.toggle_button.click()
        assert not settings.accounts[0].enabled
        assert dialog.toggle_button.text() == "사용으로 변경"
        qtbot.waitUntil(lambda: dialog.table.item(3, 3).text() == "조회 실패")
        assert dialog.table.item(0, 3).text() == "조회 중…"
        assert dialog.table.item(1, 3).text() == "—"
        assert dialog.table.item(1, 4).text() == "2.0 KB"
        assert dialog.table.item(2, 3).text() == "미지원"
        assert dialog.table.item(3, 4).text() == "조회 실패"
        assert dialog.table.item(3, 4).toolTip() == ErrorKind.AUTH.value
        assert all(not button.isEnabled() for button in dialog.account_buttons)
        release.set()
        qtbot.waitUntil(lambda: not dialog.quota_workers.pending)
        assert dialog.table.item(0, 3).text() == "1.0 KB"
        assert dialog.table.item(0, 4).text() == "0 B"
        assert dialog.selected().id == "a"
        assert dialog.toggle_button.isEnabled()
        assert all(button.isEnabled() for button in dialog.account_buttons)
        dialog.reload()
        assert sorted(calls) == list("abcd")
    finally:
        release.set()
        qtbot.waitUntil(lambda: not dialog.quota_workers.pending)
        dialog.reject()


def test_closing_account_dialog_cancels_capacity_queries(qtbot):
    started = Event()

    def quota(account, context):
        started.set()
        context.cancelled.wait(5)
        context.check()

    manager = SimpleNamespace(
        settings=Settings(accounts=[CloudAccount("a", "a", "local")]), quota=quota
    )
    dialog = AccountDialog(manager)
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.waitUntil(started.is_set)
    dialog.reject()
    qtbot.waitUntil(lambda: not dialog.isVisible())
    assert not dialog.quota_workers.pending
