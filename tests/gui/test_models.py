from time import monotonic

from PySide6.QtCore import QPoint, Qt, QThread
from PySide6.QtTest import QAbstractItemModelTester
from PySide6.QtWidgets import QRubberBand

from cloud_cluster.domain import DirectorySnapshot, Listing, Replica
from cloud_cluster.gui.models import EntryProxy, FileTableModel, FolderTreeModel
from cloud_cluster.gui.workers import WorkerPool
from cloud_cluster.replicas import merge


def entries(count):
    return merge(
        [
            Listing(
                a,
                tuple(
                    Replica(a, f"file-{n:05d}.txt", size=n, hashes={"sha256": str(n)})
                    for n in range(count)
                ),
            )
            for a in ("a", "b")
        ],
        {},
    )


def test_file_model_batches_and_shared_proxy(qapp):
    model = FileTableModel()
    tester = QAbstractItemModelTester(model, QAbstractItemModelTester.FailureReportingMode.Warning)
    tester.setUseFetchMore(False)
    model.set_entries(entries(1200))
    assert model.rowCount() == 500
    model.fetchMore()
    assert model.rowCount() == 1000
    proxy = EntryProxy()
    proxy.setSourceModel(model)
    proxy.update_filter(query="00042")
    assert proxy.rowCount() == 1
    assert proxy.index(0, 0).data() == "file-00042.txt"
    assert model.index(0, 0).data(Qt.ItemDataRole.UserRole).path == "file-00000.txt"


def test_folder_model_and_lazy_loading(qapp, accounts):
    model = FolderTreeModel(accounts)
    tester = QAbstractItemModelTester(model, QAbstractItemModelTester.FailureReportingMode.Warning)
    tester.setUseFetchMore(False)
    model.reset_accounts(accounts)
    root_index = model.index(0, 0)
    assert model.canFetchMore(root_index)
    requested = []
    model.load_requested.connect(requested.append)
    model.fetchMore(root_index)
    assert len(requested) == 1
    model.set_children(requested[0], merge([Listing("a", (Replica("a", "folder", True),))], {}))
    assert model.rowCount(root_index) == 1
    child = model.index(0, 0, root_index)
    assert model.parent(child) == root_index
    assert child.data() == "folder"


def test_workers_deliver_results_on_gui_thread(qtbot, qapp):
    pool = WorkerPool()
    result = []
    pool.submit(lambda context: 42, lambda value: result.append((value, QThread.currentThread())))
    qtbot.waitUntil(lambda: bool(result))
    assert result == [(42, qapp.thread())]
    qtbot.waitUntil(lambda: not pool.pending)


def test_worker_failure_logs_location_without_exception_text(qtbot, caplog):
    pool = WorkerPool()
    errors = []
    error = OSError("secret-errno", "secret-detail")

    def fail(context):
        raise error

    context = pool.submit(fail, failed=errors.append)
    qtbot.waitUntil(lambda: not pool.pending)
    assert errors == [str(error)]
    assert f"worker failed trace={context.trace_id}" in caplog.text
    assert "OSError at " in caplog.text
    assert "secret-" not in caplog.text


def test_worker_start_failure_restores_pending_and_busy_signal(monkeypatch):
    import pytest

    pool = WorkerPool()
    changes = []
    pool.busy_changed.connect(lambda: changes.append(bool(pool.pending)))

    def fail(worker):
        raise RuntimeError("cannot start")

    monkeypatch.setattr(pool.pool, "start", fail)
    with pytest.raises(RuntimeError, match="cannot start"):
        pool.submit(lambda context: None)
    assert not pool.pending and changes == [True, False]


def test_late_folder_result_does_not_replace_current_view(qtbot, backend, accounts, tmp_path):
    from cloud_cluster.gui.window import MainWindow
    from cloud_cluster.settings import Settings, SettingsStore

    window = MainWindow(backend, Settings(accounts=accounts[:2]), SettingsStore(tmp_path / "ui"))
    qtbot.addWidget(window)
    window.show()
    qtbot.waitUntil(lambda: not window.workers.pending)
    current = window.generation
    older = DirectorySnapshot("old", (), entries(2), monotonic())
    window.apply_partial(current - 1, older)
    assert window.file_model.entries == ()
    window._shutdown_done = True
    window.vfs.close()
    window.transfers.close()
    window.close()


def test_large_folder_preparation_and_first_batch(qapp):
    start = monotonic()
    rows = entries(50_000)
    model = FileTableModel()
    model.set_entries(rows)
    assert monotonic() - start < 3
    assert len(model.entries) == 50_000 and model.rowCount() == 500


def test_file_table_drag_selects_rows_without_starting_a_file_drag(qtbot, monkeypatch, tmp_path):
    from cloud_cluster.gui.window import MainWindow
    from cloud_cluster.settings import Settings, SettingsStore

    window = MainWindow(None, Settings(), SettingsStore(tmp_path / "ui"))
    qtbot.addWidget(window)
    window.show()
    qtbot.waitUntil(lambda: not window.workers.pending)
    window.file_model.set_entries(entries(6))
    table = window.table
    viewport = table.viewport()
    band = table.findChild(QRubberBand)
    left = Qt.MouseButton.LeftButton
    ctrl = Qt.KeyboardModifier.ControlModifier

    def point(row, x=8):
        return QPoint(x, table.visualRect(window.proxy.index(row, 0)).center().y())

    def selected_rows():
        return sorted(index.row() for index in window.selection.selectedRows())

    file_drags = []
    monkeypatch.setattr(table, "startDrag", lambda actions: file_drags.append(selected_rows()))
    below = point(5, 110) + QPoint(0, 45)
    assert not table.indexAt(below).isValid()
    qtbot.mousePress(viewport, left, pos=below)
    qtbot.mouseMove(viewport, point(2))
    assert band.isVisible() and selected_rows() == [2, 3, 4, 5]
    qtbot.mouseMove(viewport, below - QPoint(12, 5))
    assert band.isVisible() and selected_rows() == []
    qtbot.mouseRelease(viewport, left, pos=point(4))
    assert not band.isVisible() and selected_rows() == [4, 5]

    qtbot.mouseClick(viewport, left, pos=point(1))
    qtbot.mouseClick(viewport, left, ctrl, pos=point(4))
    assert selected_rows() == [1, 4]
    qtbot.mousePress(viewport, left, ctrl, pos=below)
    qtbot.mouseMove(viewport, point(2))
    assert selected_rows() == [1, 2, 3, 5]
    qtbot.mouseRelease(viewport, left, ctrl, pos=point(2))

    padding = point(0, table.columnWidth(0) - 10)
    qtbot.mousePress(viewport, left, pos=padding)
    qtbot.mouseMove(viewport, point(3))
    qtbot.mouseRelease(viewport, left, pos=point(3))
    assert selected_rows() == [0, 1, 2, 3] and file_drags == []
    qtbot.mousePress(viewport, left, pos=point(0))
    qtbot.mouseMove(viewport, point(3))
    qtbot.mouseRelease(viewport, left, pos=point(3))
    assert file_drags == [[0, 1, 2, 3]]

    window.file_model.set_entries(entries(60))
    edge = QPoint(80, viewport.height() - 2)
    qtbot.mousePress(viewport, left, pos=padding)
    qtbot.mouseMove(viewport, edge)
    initial_rows = selected_rows()
    qtbot.waitUntil(lambda: len(selected_rows()) > len(initial_rows))
    assert band.isVisible() and selected_rows()[0] == 0
    window.file_model.set_entries(entries(3))
    assert not band.isVisible() and selected_rows() == []
    qtbot.mouseRelease(viewport, left, pos=point(2))


def test_detail_selection_and_clipboard_keep_operation_intent(qtbot, backend, accounts, tmp_path):
    import json

    from PySide6.QtWidgets import QApplication

    from cloud_cluster.gui.file_table import INTERNAL_MIME
    from cloud_cluster.gui.window import MainWindow
    from cloud_cluster.settings import Settings, SettingsStore

    for account in accounts[:2]:
        backend.put(account.fs, "file.txt", b"same")
    window = MainWindow(
        backend, Settings(accounts=accounts[:2]), SettingsStore(tmp_path / "details")
    )
    qtbot.addWidget(window)
    window.show()
    qtbot.waitUntil(lambda: bool(window.file_model.entries) and not window.workers.pending)
    assert window.table.isVisible()
    window.table.selectRow(0)
    assert len(window.selected()) == 1
    window.copy_paths(True)
    payload = json.loads(bytes(QApplication.clipboard().mimeData().data(INTERNAL_MIME)))
    assert payload == {"paths": ["file.txt"], "move": True}
    window.copy_paths(False)
    payload = json.loads(bytes(QApplication.clipboard().mimeData().data(INTERNAL_MIME)))
    assert payload["move"] is False
    # The offscreen clipboard must release its MIME object before QApplication teardown.
    QApplication.clipboard().clear()
    window._shutdown_done = True
    window.vfs.close()
    window.transfers.close()
    window.close()
