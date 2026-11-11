from dataclasses import replace
from threading import Event

import pytest
from PySide6.QtCore import Qt

from cloud_cluster.domain import Status
from cloud_cluster.gui import window as window_module
from cloud_cluster.gui.window import MainWindow
from cloud_cluster.settings import Settings, SettingsStore
from cloud_cluster.transfers import TaskContext, TransferTask


@pytest.mark.parametrize("scope", ["", "a"])
def test_right_folder_navigation_expands_matching_tree(
    qtbot, backend, accounts, tmp_path, scope
):
    for account in accounts[:2]:
        backend.put(account.fs, "folder/nested/deeper/file.txt", b"same")
    window = MainWindow(backend, Settings(accounts=accounts[:2]), SettingsStore(tmp_path / "ui"))
    qtbot.addWidget(window)
    window.show()
    try:
        qtbot.waitUntil(lambda: window.snapshot is not None and not window.workers.pending)
        window.navigate("", scope=scope)
        qtbot.waitUntil(lambda: not window.workers.pending)
        window.tree.collapseAll()

        for path in ("folder", "folder/nested"):
            window.table.doubleClicked.emit(window.proxy.index(0, 0))
            qtbot.waitUntil(lambda: not window.workers.pending)
            node = window.tree_model.node(window.tree.currentIndex())
            assert (window.path, node.path, node.scope) == (path, path, scope)
            index = window.tree.currentIndex()
            while index.isValid():
                assert window.tree.isExpanded(index)
                index = index.parent()

        calls = list(backend.calls)
        for step, path in ((-1, "folder"), (1, "folder/nested")):
            window.history_step(step)
            qtbot.waitUntil(lambda: not window.workers.pending)
            assert window.tree_model.node(window.tree.currentIndex()).path == path
        window.up()
        qtbot.waitUntil(lambda: not window.workers.pending)
        assert window.tree_model.node(window.tree.currentIndex()).path == "folder"
        assert backend.calls == calls
    finally:
        qtbot.waitUntil(lambda: not window.workers.pending)
        window._shutdown_done = True
        window.vfs.close()
        window.transfers.close()
        window.close()


@pytest.mark.parametrize("scope", ["", "a"])
def test_address_navigation_loads_missing_ancestors(qtbot, backend, accounts, tmp_path, scope):
    backend.put(accounts[0].fs, "folder/nested/deeper/file.txt", b"same")
    window = MainWindow(backend, Settings(accounts=accounts[:2]), SettingsStore(tmp_path / "ui"))
    qtbot.addWidget(window)
    window.show()
    try:
        qtbot.waitUntil(lambda: window.snapshot is not None and not window.workers.pending)
        window.navigate("", scope=scope)
        qtbot.waitUntil(lambda: not window.workers.pending)
        window.tree.collapseAll()
        window.address.setText("/folder/nested/deeper")
        window.address.returnPressed.emit()
        qtbot.waitUntil(lambda: not window.workers.pending)
        node = window.tree_model.node(window.tree.currentIndex())
        assert (node.path, node.scope) == ("folder/nested/deeper", scope)
        index = window.tree.currentIndex().parent()
        while index.isValid():
            assert window.tree.isExpanded(index)
            index = index.parent()
        assert {path for op, _, path in backend.calls if op == "list"} == {
            "", "folder", "folder/nested", "folder/nested/deeper"
        }
    finally:
        qtbot.waitUntil(lambda: not window.workers.pending)
        window._shutdown_done = True
        window.vfs.close()
        window.transfers.close()
        window.close()


@pytest.mark.parametrize("scope", ["", "a"])
def test_navigation_selects_known_folder_while_listing_is_pending(
    qtbot, monkeypatch, backend, accounts, tmp_path, scope
):
    a, b = accounts[:2]
    for account in (a, b):
        backend.put(account.fs, "folder/nested/file.txt", b"same")
    window = MainWindow(backend, Settings(accounts=[a, b]), SettingsStore(tmp_path / "ui"))
    qtbot.addWidget(window)
    window.show()
    started, release = Event(), Event()
    list_dir = backend.list_dir

    def delayed(account, path, context=None):
        if account.id == b.id and path == "folder":
            started.set()
            assert release.wait(5)
        return list_dir(account, path, context)

    try:
        qtbot.waitUntil(lambda: window.snapshot is not None and not window.workers.pending)
        window.navigate("", scope=scope)
        qtbot.waitUntil(lambda: not window.workers.pending)
        window.tree.collapseAll()
        monkeypatch.setattr(backend, "list_dir", delayed)
        window.table.doubleClicked.emit(window.proxy.index(0, 0))
        qtbot.waitUntil(started.is_set)
        index = window.tree.currentIndex()
        node = window.tree_model.node(index)
        assert (node.path, node.scope) == ("folder", scope)
        assert window.tree.isExpanded(index.parent())

        release.set()
        qtbot.waitUntil(lambda: not window.workers.pending)
        assert window.tree.isExpanded(window.tree.currentIndex())
        assert sum(op == "list" and path == "folder" for op, _, path in backend.calls) == 2
    finally:
        release.set()
        qtbot.waitUntil(lambda: not window.workers.pending)
        window._shutdown_done = True
        window.vfs.close()
        window.transfers.close()
        window.close()


@pytest.mark.parametrize(("scope", "from_partial"), [("", True), ("a", True), ("b", False)])
def test_navigation_uses_parent_listing_that_arrives_during_refresh(
    qtbot, monkeypatch, backend, accounts, tmp_path, scope, from_partial
):
    a, b = accounts[:2]
    for account in (a, b):
        backend.put(account.fs, "other/file.txt", b"other")
    window = MainWindow(backend, Settings(accounts=[a, b]), SettingsStore(tmp_path / "ui"))
    qtbot.addWidget(window)
    window.show()
    release = Event()
    list_dir = backend.list_dir

    def delayed(account, path, context=None):
        if account.id == b.id and path == "":
            assert release.wait(5)
        return list_dir(account, path, context)

    def selected():
        node = window.tree_model.node(window.tree.currentIndex())
        return node.path, node.scope

    try:
        qtbot.waitUntil(lambda: window.snapshot is not None and not window.workers.pending)
        window.navigate("", scope=scope)
        qtbot.waitUntil(lambda: not window.workers.pending)
        backend.put((a if from_partial else b).fs, "new/nested/file.txt", b"new")
        monkeypatch.setattr(backend, "list_dir", delayed)
        window.refresh()
        qtbot.waitUntil(lambda: not window.snapshot.complete)
        if from_partial:
            index = window.proxy.index(0, 0)
            assert index.data() == "new"
            window.table.doubleClicked.emit(index)
            assert selected() == ("new", scope)
            window.navigate("other")
            expected = "other"
        else:
            window.address.setText("/new/nested")
            window.address.returnPressed.emit()
            expected = "new/nested"
        qtbot.waitUntil(lambda: window.snapshot is not None and window._refreshing is None)
        assert window.snapshot.path == expected

        release.set()
        qtbot.waitUntil(lambda: not window.workers.pending)
        assert window.path == window.snapshot.path == expected
        assert selected() == (expected, scope)
    finally:
        release.set()
        qtbot.waitUntil(lambda: not window.workers.pending)
        window._shutdown_done = True
        window.vfs.close()
        window.transfers.close()
        window.close()


def test_detached_tree_load_does_not_change_new_navigation(
    qtbot, monkeypatch, backend, accounts, tmp_path
):
    backend.put(accounts[0].fs, "folder/nested/file.txt", b"same")
    backend.put(accounts[0].fs, "other/file.txt", b"other")
    window = MainWindow(backend, Settings(accounts=accounts[:2]), SettingsStore(tmp_path / "ui"))
    qtbot.addWidget(window)
    window.show()
    started, release = Event(), Event()
    list_dir = backend.list_dir

    def delayed(account, path, context=None):
        if path == "folder":
            started.set()
            assert release.wait(5)
        return list_dir(account, path, context)

    try:
        qtbot.waitUntil(lambda: window.snapshot is not None and not window.workers.pending)
        root_rows = window.file_model.entries
        late_sort = []
        with monkeypatch.context() as patch:
            patch.setattr(window.workers, "submit", lambda _, done, **kwargs: late_sort.append(done))
            window.sort_entries(0, Qt.SortOrder.AscendingOrder)
        monkeypatch.setattr(backend, "list_dir", delayed)
        window.navigate("folder/nested")
        assert window.snapshot is None
        assert window.proxy.rowCount() == 0
        late_sort[0](root_rows)
        assert window.proxy.rowCount() == 0
        qtbot.waitUntil(started.is_set)
        window.navigate("")
        assert window.file_model.entries == root_rows
        window.navigate("other")
        qtbot.waitUntil(
            lambda: window.tree_model.node(window.tree.currentIndex()).path == "other"
        )
        release.set()
        qtbot.waitUntil(lambda: not window.workers.pending)
        assert window.path == window.snapshot.path == "other"
        assert window.tree_model.node(window.tree.currentIndex()).path == "other"
    finally:
        release.set()
        qtbot.waitUntil(lambda: not window.workers.pending)
        window._shutdown_done = True
        window.vfs.close()
        window.transfers.close()
        window.close()


def test_file_list_sort_does_not_block_configuration(qtbot, monkeypatch, tmp_path):
    window = MainWindow(None, Settings(), SettingsStore(tmp_path / "ui"))
    qtbot.addWidget(window)
    release = Event()
    submit = window.workers.submit

    def delay(function, done, **kwargs):
        def sort(context):
            assert release.wait(5)
            return function(context)

        return submit(sort, done, **kwargs)

    try:
        qtbot.waitUntil(lambda: not window.workers.pending)
        with monkeypatch.context() as patch:
            patch.setattr(window.workers, "submit", delay)
            window.sort_entries(0, Qt.SortOrder.AscendingOrder)
        assert window.workers.pending and window.configuration_available(notify=False)
    finally:
        release.set()
        qtbot.waitUntil(lambda: not window.workers.pending)
        window.close()
        qtbot.waitUntil(lambda: window._shutdown_done)


def test_task_refresh_keeps_cached_rows_until_their_account_responds(
    qtbot, monkeypatch, backend, accounts, tmp_path
):
    a, b, _ = accounts
    backend.put(a.fs, "removed.txt", b"old")
    backend.put(b.fs, "keep.txt", b"old")
    window = MainWindow(backend, Settings(accounts=[a, b]), SettingsStore(tmp_path / "ui"))
    qtbot.addWidget(window)
    release = Event()
    list_account = window.vfs._list

    def delayed(account, path, context):
        if account.id == b.id:
            assert release.wait(5)
        return list_account(account, path, context)

    try:
        qtbot.waitUntil(lambda: window.snapshot is not None and not window.workers.pending)
        window.status_filter.setCurrentIndex(window.status_filter.findData(Status.UNDER.value))
        del backend.nodes[a.fs, "removed.txt"]
        backend.put(b.fs, "new.txt", b"new")
        monkeypatch.setattr(window.vfs, "_list", delayed)
        task = TransferTask("finished", "delete", TaskContext(), state="완료")
        window.transfers.tasks[task.id] = task
        window.update_task(task)

        qtbot.waitUntil(lambda: not window.snapshot.complete)
        assert window.proxy.rowCount() == 1
        assert window.proxy.index(0, 0).data() == "keep.txt"
        assert window.vfs.cached("").stale

        confirmations = []

        def confirm(_, title, *args):
            confirmations.append(title)
            return False

        monkeypatch.setattr(window_module, "confirm", confirm)
        backend.calls.clear()
        window.table.selectRow(0)
        window.delete_action.trigger()
        assert confirmations == ["삭제"]
        assert not backend.calls
        assert not window.snapshot.complete

        release.set()
        qtbot.waitUntil(lambda: not window.workers.pending)
        assert [entry.path for entry in window.snapshot.entries] == ["keep.txt", "new.txt"]
        assert not window.vfs.cached("").stale
    finally:
        release.set()
        qtbot.waitUntil(lambda: not window.workers.pending)
        window._shutdown_done = True
        window.vfs.close()
        window.transfers.close()
        window.close()


def test_work_paths_in_normal_navigation(qtbot, backend, accounts, tmp_path):
    account = replace(accounts[0], provider="s3")
    preserved = ".cc-preserved-" + "a" * 32 + "-file.txt"
    stage = ".cc-stage-" + "b" * 32 + "-folder"
    for path in (preserved, f"{stage}/child.txt", "file.txt"):
        backend.put(account.fs, path, b"same")
    window = MainWindow(backend, Settings(accounts=[account]), SettingsStore(tmp_path / "ui"))
    qtbot.addWidget(window)
    window.show()
    try:
        qtbot.waitUntil(lambda: window.snapshot is not None and not window.workers.pending)
        assert {
            window.proxy.index(row, 0).data(Qt.ItemDataRole.UserRole).name
            for row in range(window.proxy.rowCount())
        } == {preserved, stage, "file.txt"}
        unified = window.tree_model.index(0, 0)
        assert window.tree_model.index(0, 0, unified).data() == stage
        assert [node.label for node in window.tree_model.root.children] == [
            "통합 클라우드", "연결된 계정", "문제 항목"
        ]
    finally:
        qtbot.waitUntil(lambda: not window.workers.pending)
        window._shutdown_done = True
        window.vfs.close()
        window.transfers.close()
        window.close()
