from dataclasses import replace
from types import SimpleNamespace

import pytest

from cloud_cluster.domain import (
    BackendCapabilities,
    BackendError,
    CloudAccount,
    ErrorKind,
    QuotaSnapshot,
)
from cloud_cluster.gui import window as window_module
from cloud_cluster.gui.models import size_text
from cloud_cluster.gui.window import MainWindow
from cloud_cluster.settings import Settings, SettingsStore
from cloud_cluster.transfers import TaskContext, TransferTask


@pytest.fixture
def capacity_ui(qtbot, backend, accounts, tmp_path):
    backend.quotas = {
        "a": QuotaSnapshot(1000, 100),
        "b": QuotaSnapshot(100, 100),
        "c": QuotaSnapshot(100, 100),
    }
    window = MainWindow(backend, Settings(accounts=accounts), SettingsStore(tmp_path / "ui"))
    qtbot.addWidget(
        window, before_close_func=lambda widget: setattr(widget, "_shutdown_done", True)
    )
    window.show()
    qtbot.waitUntil(lambda: not window.workers.pending)
    try:
        yield SimpleNamespace(window=window, backend=backend, accounts=accounts)
    finally:
        qtbot.waitUntil(lambda: not window.workers.pending)
        window._closing = window._shutdown_done = True
        window.vfs.close()
        window.transfers.close()
        window.close()


def hold_queries(ui, monkeypatch):
    queries = []

    def submit(operation, done=None, failed=None, progress=None, **kwargs):
        context = TaskContext()
        queries.append((operation, done, failed, context))
        return context

    monkeypatch.setattr(ui.window.workers, "submit", submit)
    return queries


def complete(query):
    operation, done, _, context = query
    done(operation(context))


def test_status_capacity_uses_replica_policy_and_survives_status_messages(capacity_ui):
    window = capacity_ui.window
    assert window.capacity_label.text() == "통합 용량(추정): 남은 110 B / 전체 200 B"
    window.statusBar().showMessage("현재 폴더 조회 중")
    assert window.capacity_label.isVisible()
    assert window.capacity_label.text() == "통합 용량(추정): 남은 110 B / 전체 200 B"
    assert not window.capacity_label.toolTip()


def test_capacity_refresh_does_not_list_files(capacity_ui, qtbot):
    ui = capacity_ui
    ui.backend.calls.clear()
    ui.window.refresh_capacity()
    qtbot.waitUntil(lambda: not ui.window.workers.pending)
    assert not ui.backend.calls
    assert ui.window.capacity_label.text().endswith("남은 110 B / 전체 200 B")


def test_capacity_excludes_disabled_readonly_and_excluded_account_roots(capacity_ui, qtbot):
    ui = capacity_ui
    a, b, c = ui.accounts
    ui.window.settings.accounts = [
        a,
        b,
        replace(c, enabled=False),
        CloudAccount("readonly", "readonly", "drive", readonly=True),
        CloudAccount("excluded", "excluded", "pikpak", root="My Pack"),
    ]
    ui.window.refresh_capacity()
    qtbot.waitUntil(lambda: not ui.window.workers.pending)
    assert set(ui.window._capacity_quotas) == {"a", "b"}
    assert ui.window.capacity_label.text().endswith("남은 100 B / 전체 100 B")


@pytest.mark.parametrize("count", [0, 1])
def test_less_than_two_writable_accounts_has_no_replicated_capacity(capacity_ui, count):
    ui = capacity_ui
    ui.window.settings.accounts = ui.accounts[:count]
    ui.window.refresh_capacity()
    assert ui.window.capacity_label.text().endswith("남은 0 B / 전체 0 B")


def test_missing_backend_with_configured_accounts_does_not_report_zero_capacity(capacity_ui):
    window = capacity_ui.window
    window.engine = None
    window.refresh_capacity()
    assert window.capacity_label.text().endswith("남은 확인 불가 / 전체 확인 불가")


@pytest.mark.parametrize("kind", ["unsupported", "error", "missing", "zero"])
def test_quota_unknown_failure_and_zero_are_distinct(capacity_ui, qtbot, monkeypatch, kind):
    ui = capacity_ui
    if kind == "unsupported":
        ui.backend.caps["b"] = BackendCapabilities(quota=False)
    elif kind == "error":
        about = ui.backend.about

        def read(account, context=None):
            if account.id == "b":
                raise BackendError(ErrorKind.NETWORK)
            return about(account, context)

        monkeypatch.setattr(ui.backend, "about", read)
    else:
        ui.backend.quotas["b"] = QuotaSnapshot(100, None if kind == "missing" else 0)
    ui.window.refresh_capacity()
    qtbot.waitUntil(lambda: not ui.window.workers.pending)
    text = ui.window.capacity_label.text()
    assert not ui.window.capacity_label.toolTip()
    if kind == "zero":
        assert text.endswith("남은 100 B / 전체 200 B")
    elif kind == "missing":
        assert text.endswith("남은 확인 불가 / 전체 200 B")
    else:
        assert text.endswith("남은 확인 불가 / 전체 확인 불가")


def test_capacity_coalesces_refreshes_while_a_query_is_pending(capacity_ui, monkeypatch):
    ui = capacity_ui
    queries = hold_queries(ui, monkeypatch)
    ui.window.refresh_capacity()
    ui.window.refresh_capacity()
    ui.window.refresh_capacity()
    assert len(queries) == 1 and "갱신 중" in ui.window.capacity_label.text()
    complete(queries[0])
    assert len(queries) == 2
    complete(queries[1])
    assert not ui.window._capacity_loading and len(queries) == 2
    assert ui.window.capacity_label.text().endswith("남은 110 B / 전체 200 B")


def test_account_changes_discard_old_capacity_results(capacity_ui, monkeypatch):
    ui = capacity_ui
    queries = hold_queries(ui, monkeypatch)
    ui.window.refresh_capacity()
    ui.window.settings.accounts = ui.accounts[1:]
    ui.window.refresh_capacity()
    complete(queries[0])
    assert len(queries) == 2 and ui.window._capacity_quotas is None
    complete(queries[1])
    assert set(ui.window._capacity_quotas) == {"b", "c"}
    assert ui.window.capacity_label.text().endswith("남은 100 B / 전체 100 B")


def test_capacity_uses_live_reservations_without_another_remote_query(capacity_ui, monkeypatch):
    ui = capacity_ui
    queries = hold_queries(ui, monkeypatch)
    ui.window.engine._reserved = {"a": 60, "b": 30}
    expected = ui.window.engine.capacity(ui.window._capacity_quotas)
    task = TransferTask("active", "upload", TaskContext(), state="진행 중")
    ui.window.transfers.tasks[task.id] = task
    ui.window.update_task(task)
    assert ui.window.capacity_label.text().endswith(f"남은 {size_text(expected.free)} / 전체 200 B")
    assert not queries


def test_capacity_query_start_failure_keeps_cached_values_without_loading_state(
    capacity_ui, monkeypatch
):
    def fail(*args, **kwargs):
        raise RuntimeError("조회 시작 실패")

    monkeypatch.setattr(capacity_ui.window.workers, "submit", fail)
    capacity_ui.window.refresh_capacity()
    assert not capacity_ui.window._capacity_loading
    assert capacity_ui.window.capacity_label.text().endswith("남은 110 B / 전체 200 B")
    assert not capacity_ui.window.capacity_label.toolTip()


def test_late_capacity_reply_does_not_update_a_closing_window(capacity_ui, monkeypatch):
    ui = capacity_ui
    queries = hold_queries(ui, monkeypatch)
    ui.window.refresh_capacity()
    text = ui.window.capacity_label.text()
    ui.window._closing = True
    complete(queries[0])
    assert ui.window.capacity_label.text() == text and len(queries) == 1


def test_navigation_does_not_query_capacity_but_explicit_refresh_does(
    capacity_ui, qtbot, monkeypatch
):
    calls = []
    monkeypatch.setattr(capacity_ui.window, "refresh_capacity", lambda: calls.append(1))
    capacity_ui.window.navigate("folder")
    qtbot.waitUntil(lambda: not capacity_ui.window.workers.pending)
    assert not calls
    capacity_ui.window.refresh_action.trigger()
    qtbot.waitUntil(lambda: not capacity_ui.window.workers.pending)
    assert calls == [1]


def test_configuration_waits_for_task_completion_signals(
    capacity_ui, qtbot, monkeypatch
):
    window = capacity_ui.window
    notices = []
    monkeypatch.setattr(window_module, "message", lambda *args: notices.append(1))
    task = TransferTask("finished-before-signal", "upload", TaskContext(), state="완료")
    window.transfers.tasks[task.id] = task
    assert not window.transfers.busy and not window.workers.pending
    assert not window.configuration_available() and notices == [1]
    window.update_task(task)
    qtbot.waitUntil(lambda: window.configuration_available(notify=False))
    assert window.configuration_available()


def test_finished_transfer_updates_capacity_without_querying_again(capacity_ui, qtbot, monkeypatch):
    window = capacity_ui.window
    calls = []
    monkeypatch.setattr(window, "refresh_capacity", lambda: calls.append("refresh"))
    for account in capacity_ui.accounts[1:]:
        window.engine.quotas.consume(account, 10)
    task = TransferTask("finished", "upload", TaskContext(), state="완료")
    window.transfers.tasks[task.id] = task
    window.update_task(task)
    qtbot.waitUntil(lambda: not window.workers.pending)
    assert not calls
    assert window.capacity_label.text().endswith("남은 100 B / 전체 200 B")


def test_explicit_refresh_bypasses_quota_cache(capacity_ui, qtbot):
    ui = capacity_ui
    for account in ui.accounts:
        ui.backend.quotas[account.id] = QuotaSnapshot(100, 50)
    ui.window.refresh_action.trigger()
    qtbot.waitUntil(lambda: not ui.window.workers.pending)
    assert ui.window.capacity_label.text().endswith("남은 75 B / 전체 150 B")


def test_manual_replication_reuses_cached_capacity_and_live_reservations(
    capacity_ui, qtbot, monkeypatch
):
    from cloud_cluster.domain import Endpoint

    ui = capacity_ui
    a, b, c = ui.accounts
    ui.backend.put(a.fs, "file", b"data")
    source = ui.backend.stat(Endpoint(a.fs, "file", a.id))
    choices = []

    def choose(parent, title, label, items, **kwargs):
        choices.append(items[0])
        return "", False

    def unexpected(*args, **kwargs):
        pytest.fail("recommendation must reuse the shared capacity cache")

    monkeypatch.setattr(window_module.QInputDialog, "getItem", choose)
    monkeypatch.setattr(ui.backend, "about", unexpected)
    ui.window.engine._reserved[c.id] = 90
    ui.window.replica_action("replicate", source)
    qtbot.waitUntil(lambda: not ui.window.workers.pending)
    ui.window.engine._reserved[c.id] = 0
    ui.window.engine.quotas.consume(b, 80)
    ui.window.replica_action("replicate", source)
    qtbot.waitUntil(lambda: not ui.window.workers.pending)
    assert choices == ["b (추천)", "c (추천)"]
