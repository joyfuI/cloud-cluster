from dataclasses import replace
from types import SimpleNamespace

import pytest
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QDockWidget, QLabel

from cloud_cluster.domain import BackendError, ErrorKind, Status
from cloud_cluster.gui import window as window_module
from cloud_cluster.gui.dialogs import IssueDialog
from cloud_cluster.gui.window import MainWindow
from cloud_cluster.settings import Settings, SettingsStore
from cloud_cluster.transfers import TaskContext, TransferTask


@pytest.fixture
def repair_ui(qtbot, monkeypatch, backend, accounts, tmp_path):
    backend.put(accounts[0].fs, "folder/file", b"keep")
    window = MainWindow(backend, Settings(accounts=accounts), SettingsStore(tmp_path / "ui"))
    qtbot.addWidget(
        window, before_close_func=lambda widget: setattr(widget, "_shutdown_done", True)
    )
    qtbot.waitUntil(lambda: not window.workers.pending)
    window.scan_path = ""
    window.scan_results, window.scan_complete, _ = window.vfs.scan("")
    window.show_issues()
    plans, queued, errors, contexts, dependencies, prompts = [], [], [], [], {}, []

    def plan(function, done=None, failed=None, progress=None, **kwargs):
        plans.append((function, done, failed))
        context = TaskContext(progress=progress or (lambda value: None))
        contexts.append(context)
        return context

    def submit(label, operation, *, after=(), wait_for=(), queue="transfer"):
        task = TransferTask(str(len(queued) + 1), label, TaskContext())
        task.context.progress = lambda value: window.transfers._progress(task, value)
        queued.append((task, operation))
        dependencies[task.id] = after
        window.transfers.tasks[task.id] = task
        window.update_task(task)
        return task

    monkeypatch.setattr(window.workers, "submit", plan)
    monkeypatch.setattr(window.transfers, "submit", submit)
    monkeypatch.setattr(window, "refresh", lambda: None)
    monkeypatch.setattr(window, "refresh_capacity", lambda: None)
    monkeypatch.setattr(window_module, "message", lambda parent, error: errors.append(str(error)))
    monkeypatch.setattr(window_module, "confirm", lambda *args: prompts.append(args[1]) or True)
    try:
        yield SimpleNamespace(
            window=window,
            dialog=window.issue_dialog,
            plans=plans,
            queued=queued,
            errors=errors,
            contexts=contexts,
            dependencies=dependencies,
            prompts=prompts,
        )
    finally:
        window._shutdown_done = True
        window.vfs.close()
        window.transfers.close()
        window.close()


def run_task(ui, index=0):
    task, operation = ui.queued[index]
    dependencies = tuple(ui.window.transfers.tasks[key] for key in ui.dependencies[task.id])
    ui.window.transfers._run(task, operation, dependencies)
    return task


def finish_recheck(ui):
    ui.window.flush_task_refreshes()
    operation, done, _ = ui.plans[-1]
    done(operation(ui.contexts[-1]))


@pytest.fixture
def bulk_repair_ui(repair_ui, qtbot):
    ui = repair_ui
    source = next(
        entry for entry in ui.window.scan_results if entry.assessment.status == Status.UNDER
    )
    ui.window.scan_results = tuple(
        replace(source, key=f"item-{index:05}", path=f"item-{index:05}") for index in range(2951)
    )
    ui.window.show_issues()
    ui.dialog.table.selectAll()
    try:
        yield ui
    finally:
        ui.window._closing = True
        qtbot.waitUntil(lambda: not ui.window._preparing)
        ui.window.transfers.on_change = lambda task: None


def test_bulk_repair_registration_yields_and_does_not_rescan_every_selection(
    bulk_repair_ui, qtbot, monkeypatch, backend
):
    ui = bulk_repair_ui
    updates, heartbeat = [], []
    update = ui.window.update_command_buttons
    monkeypatch.setattr(ui.window, "update_command_buttons", lambda: (updates.append(1), update()))
    backend.calls.clear()
    QTimer.singleShot(0, lambda: heartbeat.append(len(ui.queued)))
    ui.dialog.resolve_button.click()
    assert 0 < len(ui.queued) < 2951 and ui.window._preparing
    registered = len(ui.queued)
    ui.window.resolve_issues(ui.window.scan_results)
    assert len(ui.queued) == registered
    qtbot.waitUntil(lambda: not ui.window._preparing, timeout=5000)
    assert len(heartbeat) == 1 and 0 < heartbeat[0] < 2951
    assert len(ui.queued) == len(ui.window._pending_repairs) == 2951
    assert ui.window.task_tree.topLevelItemCount() == 2951
    assert len(updates) <= 3 and not backend.calls and not ui.prompts
    assert ui.window._repair_busy and not ui.dialog.resolve_button.isEnabled()


def test_bulk_repair_stops_registration_after_close(bulk_repair_ui, qtbot):
    ui = bulk_repair_ui
    ui.dialog.resolve()
    registered = len(ui.queued)
    ui.window._closing = True
    qtbot.waitUntil(lambda: not ui.window._preparing)
    assert len(ui.queued) == registered < 2951
    assert len(ui.window._pending_repairs) == registered and not ui.errors


def test_bulk_close_cancels_tasks_without_updating_every_row(bulk_repair_ui, qtbot, monkeypatch):
    ui = bulk_repair_ui
    ui.dialog.resolve()
    qtbot.waitUntil(lambda: not ui.window._preparing)
    ui.window._closing = True
    updates = []
    monkeypatch.setattr(ui.window, "update_command_buttons", lambda: updates.append(1))
    ui.window.transfers.cancel_all()
    assert all(task.context.cancelled.is_set() for task, _ in ui.queued)
    assert not updates


def test_bulk_repair_later_registration_failure_preserves_registered_work(
    bulk_repair_ui, qtbot, monkeypatch
):
    ui = bulk_repair_ui
    submit = ui.window.transfers.submit

    def fail_later(*args, **kwargs):
        if len(ui.queued) == 32:
            raise RuntimeError("후속 등록 실패")
        return submit(*args, **kwargs)

    monkeypatch.setattr(ui.window.transfers, "submit", fail_later)
    ui.dialog.resolve()
    qtbot.waitUntil(lambda: not ui.window._preparing)
    assert len(ui.queued) == len(ui.window._repair_tasks) == 32
    assert ui.window._repair_busy and ui.window.scan_results == ui.dialog.entries
    assert ui.errors == ["/item-00032: 후속 등록 실패"]


def test_repair_progress_does_not_repeat_global_button_checks(repair_ui, monkeypatch):
    ui = repair_ui
    ui.dialog.resolve()
    task = ui.queued[0][0]
    task.state = "진행 중"
    ui.window.update_task(task)
    updates = []
    update = ui.window.update_command_buttons
    monkeypatch.setattr(ui.window, "update_command_buttons", lambda: (updates.append(1), update()))
    for index in range(100):
        task.context.describe(f"상태 조회 {index}")
    assert not updates
    assert "상태 조회 99" in ui.dialog.progress_label.text()
    ui.window.cancel_task(task)
    assert updates == [1] and task.state == "취소 요청"


def test_repair_folder_dependencies_survive_registration_batches(repair_ui, qtbot):
    ui = repair_ui
    folder, file = ui.window.scan_results
    entries = tuple(
        replace(folder, key=f"folder-{index:02}", path=f"folder-{index:02}") for index in range(33)
    ) + (replace(file, key="folder-00/file", path="folder-00/file"),)
    ui.window.resolve_issues(entries)
    assert ui.window._preparing
    parent = ui.queued[0][0]
    parent.state = "실패"
    ui.window.update_task(parent)
    assert not ui.window.clear_failed_button.isEnabled()
    ui.window.remove_finished_tasks((parent.id,))
    assert parent.id in ui.window.transfers.tasks
    qtbot.waitUntil(lambda: not ui.window._preparing)
    child = ui.queued[-1][0]
    assert len(ui.queued) == 34 and ui.dependencies[child.id] == (parent.id,)
    assert ui.window.clear_failed_button.isEnabled()


def test_issue_results_restore_large_selection_with_one_notification(bulk_repair_ui, qtbot):
    entries = bulk_repair_ui.window.scan_results
    dialog = IssueDialog()
    qtbot.addWidget(dialog)
    notifications = []
    dialog.table.itemSelectionChanged.connect(lambda: notifications.append(1))
    dialog.set_results("", entries, True, selected_keys={entry.key for entry in entries})
    assert notifications == [1]
    assert dialog.selected_entries() == entries
    assert "2,951개 선택" in dialog.selection_summary.text()
    assert dialog.resolve_button.isEnabled()


def test_issue_results_restore_disjoint_selection_once(repair_ui):
    ui = repair_ui
    source = ui.window.scan_results[1]
    entries = tuple(replace(source, key=str(i), path=str(i)) for i in range(6))
    notifications = []
    ui.dialog.table.itemSelectionChanged.connect(lambda: notifications.append(1))
    ui.dialog.set_results("", entries, True, selected_keys={"0", "1", "3", "5"})
    assert notifications == [1]
    assert [entry.key for entry in ui.dialog.selected_entries()] == ["0", "1", "3", "5"]


@pytest.fixture
def verification_ui(repair_ui, backend, accounts, monkeypatch):
    for account, kind in zip(accounts[:2], ("md5", "sha1")):
        backend.put(account.fs, "verify", b"same")
        backend.hash_types[account.fs, "verify"] = kind
    window = repair_ui.window
    window.scan_results, window.scan_complete, _ = window.vfs.scan("")
    window.show_issues()
    row = next(i for i, entry in enumerate(window.scan_results) if entry.path == "verify")
    repair_ui.dialog.table.selectRow(row)
    return repair_ui


def test_mixed_repairs_include_verification_and_keep_each_task_locked_until_recheck(
    verification_ui, backend, monkeypatch
):
    ui = verification_ui
    window, dialog = ui.window, ui.dialog
    dialog.table.selectAll()
    backend.calls.clear()
    dialog.resolve()
    assert len(ui.queued) == 3 and not backend.calls and not ui.prompts
    verify = next(task for task, _ in ui.queued if task.label == "정밀 검증 · /verify")
    entry = window._pending_repairs[verify.id]
    window.manage_replicas((entry,))
    assert not window.replica_dialog.action_buttons["verify"].isEnabled()
    window.verify(entry.replicas)
    window.resolve_issues((entry,))
    assert not ui.plans and len(ui.queued) == 3
    for index in range(len(ui.queued)):
        assert run_task(ui, index).state == "완료"
    assert window._repair_busy and not ui.plans
    assert window._task_rows[verify.id].text(1) == "결과 재확인"
    dialog.table.clearSelection()
    dialog.close()
    window.show_issues()
    window.flush_task_refreshes()
    assert len(ui.plans) == 1
    rendered = []
    set_results = dialog.set_results

    def count_results(path, entries, complete, **kwargs):
        rendered.append(entries)
        set_results(path, entries, complete, **kwargs)

    monkeypatch.setattr(dialog, "set_results", count_results)
    finish_recheck(ui)
    assert not window._repair_busy
    assert rendered == [()]
    assert all("1개 해결" in window._task_rows[task.id].text(2) for task, _ in ui.queued)
    assert window.scan_results == dialog.entries == ()
    assert not window._pending_repairs and not ui.prompts
    assert "2/2개 저장" in verify.result.steps[-1].message


@pytest.mark.parametrize("different", [False, True])
def test_later_verification_reuses_content_hashes_for_another_path(
    verification_ui, backend, accounts, different
):
    ui = verification_ui
    data = (b"same", b"diff" if different else b"same")
    for account, kind, value in zip(accounts[:2], ("md5", "sha1"), data):
        for path in ("other", "verify"):
            backend.put(account.fs, path, value)
            backend.hash_types[account.fs, path] = kind
    ui.window.scan_results, ui.window.scan_complete, _ = ui.window.vfs.scan("")
    entries = [entry for entry in ui.window.scan_results if entry.path in {"other", "verify"}]
    ui.window.resolve_issues(entries)
    assert run_task(ui, 0).state == "완료"
    finish_recheck(ui)
    reads = sum(call[0] == "hash" for call in backend.calls)
    task = run_task(ui, 1)
    assert task.state == "완료"
    assert "저장된 검증 결과 2개 재사용" in task.result.steps[-1].message
    assert ("내용 상이" if different else "SHA-256 일치") in task.result.steps[-1].message
    assert sum(call[0] == "hash" for call in backend.calls) == reads == 2
    finish_recheck(ui)
    for entry in ui.window.scan_results:
        if entry.path in {"other", "verify"}:
            assert different and entry.assessment.status == Status.CONFLICT
    assert not ui.window._repair_busy and not ui.errors


def test_cached_verification_does_not_complete_with_a_failed_account_lookup(
    verification_ui, backend, accounts
):
    ui = verification_ui
    entry = ui.dialog.selected_entries()[0]
    backend.put(accounts[1].fs, "verify", b"diff")
    ui.window.verify_issue(entry, TaskContext())
    reads = sum(call[0] == "hash" for call in backend.calls)
    backend.failures["list", accounts[2].fs, ""] = BackendError(ErrorKind.NETWORK)
    with pytest.raises(ValueError, match="상태"):
        ui.window.verify_issue(entry, TaskContext())
    assert sum(call[0] == "hash" for call in backend.calls) == reads == 2


@pytest.mark.parametrize(
    "result",
    ["same", "different", "added", "no_evidence", "save", "changed", "cancelled", "hash", "lookup", "state"],
)
def test_issue_verification_only_removes_confirmed_matches(
    verification_ui, backend, accounts, monkeypatch, result
):
    ui = verification_ui
    window, dialog = ui.window, ui.dialog
    entry = dialog.selected_entries()[0]
    if result == "different":
        backend.put(accounts[1].fs, entry.path, b"diff")
    elif result == "added":
        backend.put(accounts[2].fs, entry.path, b"same")
    elif result == "no_evidence":
        original_replica = backend.replica
        monkeypatch.setattr(
            backend, "replica",
            lambda *args: replace(original_replica(*args), hashes={}, object_id="", version="", modified=None)
        )
    elif result == "save":
        window.vfs.verification_store.path = window.store.directory
    elif result == "lookup":
        backend.failures["list", accounts[0].fs, ""] = BackendError(ErrorKind.NETWORK)
    elif result == "state":
        del backend.nodes[accounts[1].fs, entry.path]
    hash_file = backend.hash_file
    progress = []

    def read(endpoint, context=None, **kwargs):
        digest = hash_file(endpoint, context, **kwargs)
        progress.append(context.detail)
        if endpoint.account_id == accounts[1].id:
            if result == "changed":
                backend.put(accounts[0].fs, entry.path, b"diff")
            elif result == "cancelled":
                context.cancelled.set()
            elif result == "hash":
                raise BackendError(ErrorKind.NETWORK)
        return digest

    monkeypatch.setattr(backend, "hash_file", read)
    assert "정밀 검증 1개" in dialog.selection_summary.text()
    assert "검증 읽기 약 8 B" in dialog.selection_summary.text()
    dialog.resolve()
    task = run_task(ui)
    if result in {"same", "different", "added", "no_evidence", "save"}:
        assert task.state == "완료" and window._repair_busy
        finish_recheck(ui)
        found = next((item for item in window.scan_results if item.key == entry.key), None)
        expected = {
            "different": Status.CONFLICT,
            "added": Status.OVER,
        }.get(result, Status.UNVERIFIED)
        if result in {"same", "no_evidence", "save"}:
            assert found is None
        else:
            assert found.assessment.status == expected
        stored = "0/2" if result in {"no_evidence", "save"} else ("3/3" if result == "added" else "2/2")
        assert stored + "개 저장" in task.result.steps[-1].message
        total = 3 if result == "added" else 2
        assert len(progress) == total
        assert f"정밀 검증 {total}/{total}" in progress[-1] and "/verify" in progress[-1]
    else:
        assert task.state == ("취소됨" if result == "cancelled" else "실패")
        assert entry in window.scan_results and not ui.plans
        if result in {"lookup", "state"}:
            assert not progress
        # Read errors, cancellation, and changed sources must never save partial proofs.
        for account in accounts[:2]:
            if (account.fs, entry.path) in backend.nodes:
                replica = backend.replica(account.fs, entry.path, account.id)
                restored = window.vfs.verification_store.restore(account, (replica,))
                assert not restored[0].verified_sha256
    assert not window._repair_busy and not window._pending_repairs and not ui.prompts
    assert backend.nodes[accounts[0].fs, entry.path].data == (
        b"diff" if result == "changed" else b"same"
    )


def test_cancelled_verification_retries_only_its_file_without_confirmation(verification_ui):
    ui = verification_ui
    window = ui.window
    ui.dialog.resolve()
    task = ui.queued[0][0]
    window.cancel_task(task)
    assert run_task(ui).state == "취소됨" and not window._repair_busy
    window.retry_task(task)
    window.retry_task(task)
    assert len(ui.queued) == 2 and window._repair_busy
    assert run_task(ui, 1).state == "완료"
    finish_recheck(ui)
    assert all(entry.path != "verify" for entry in window.scan_results)
    assert any(entry.path == "folder/file" for entry in window.scan_results)
    assert not window._repair_busy and not ui.prompts


@pytest.fixture
def copy_verification_ui(repair_ui, backend, accounts):
    backend.put(accounts[0].fs, "copy", b"same")
    backend.no_hashes.update(a.fs for a in accounts)
    window = repair_ui.window
    window.scan_results, window.scan_complete, _ = window.vfs.scan("")
    window.show_issues()
    row = next(i for i, entry in enumerate(window.scan_results) if entry.path == "copy")
    repair_ui.dialog.table.selectRow(row)
    return repair_ui


@pytest.mark.parametrize("mode", ["no_hashes", "different_algorithms", "common_hash"])
def test_copy_repair_verifies_uncomparable_content_in_same_task(
    copy_verification_ui, backend, accounts, mode
):
    ui = copy_verification_ui
    if mode != "no_hashes":
        backend.no_hashes.clear()
        if mode == "different_algorithms":
            for account, kind in zip(accounts, ("md5", "sha1", "sha1")):
                backend.hash_types[account.fs, "copy"] = kind
    ui.dialog.resolve()
    assert not backend.calls or all(call[0] != "hash" for call in backend.calls)
    task = run_task(ui)
    assert task.state == "완료" and len(ui.queued) == 1 and ui.window._repair_busy
    reads = [call for call in backend.calls if call[0] == "hash"]
    assert len(reads) == (0 if mode == "common_hash" else 2)
    assert all(path == "copy" for _, _, path in reads)
    if mode != "common_hash":
        assert any(step.action == "SHA-256" for step in task.result.steps)
        assert len(task.result.verification) == 2
    finish_recheck(ui)
    assert all(entry.path != "copy" for entry in ui.window.scan_results)
    assert not ui.window._repair_busy and not ui.prompts
    assert len([call for call in backend.calls if call[0] == "hash"]) == len(reads)
    if mode == "no_hashes":
        assert all(e.path != "copy" for e in ui.window.vfs.scan("")[0])


@pytest.mark.parametrize("identifier", ["object", "path"])
def test_hashless_copy_reuses_metadata_proof_on_later_scan(
    copy_verification_ui, backend, accounts, monkeypatch, identifier
):
    from datetime import UTC, datetime, timedelta

    from cloud_cluster.verification import VerificationStore

    ui = copy_verification_ui
    replica = backend.replica

    def without_version(fs, path, account_id):
        current = replica(fs, path, account_id)
        return replace(
            current,
            object_id=current.object_id if identifier == "object" else "",
            version="",
            modified=(
                datetime(2026, 11, 4, tzinfo=UTC)
                + timedelta(seconds=backend.nodes[fs, path].version)
            ),
        )

    monkeypatch.setattr(backend, "replica", without_version)
    ui.dialog.resolve()
    task = run_task(ui)
    assert task.state == "완료" and "2/2개 저장" in task.result.steps[-1].message
    finish_recheck(ui)
    assert all(e.path != "copy" for e in ui.window.scan_results)
    ui.window.vfs.verification_store = VerificationStore(ui.window.vfs.verification_store.path)
    reads = sum(call[0] == "hash" for call in backend.calls)
    assert all(e.path != "copy" for e in ui.window.vfs.scan("")[0])
    assert sum(call[0] == "hash" for call in backend.calls) == reads == 2
    target = next(a for a in accounts[1:] if (a.fs, "copy") in backend.nodes)
    backend.put(target.fs, "copy", b"diff")
    current = next(e for e in ui.window.vfs.scan("")[0] if e.path == "copy")
    assert current.assessment.status == Status.UNVERIFIED
    assert not next(r for r in current.replicas if r.account_id == target.id).verified_sha256


@pytest.mark.parametrize("failure", ["different", "changed", "hash", "cancelled", "save", "lookup"])
def test_copy_verification_failure_preserves_copy_and_can_retry_without_copying_again(
    copy_verification_ui, backend, accounts, monkeypatch, failure
):
    ui = copy_verification_ui
    original_copy, original_hash = backend.copy_file, backend.hash_file
    copies, reads = [], []

    def copy(source, target, context=None):
        original_copy(source, target, context)
        copies.append(target)
        if target.path == "copy":
            if failure == "different":
                backend.put(target.fs, target.path, b"diff")
            elif failure == "lookup":
                backend.failures["list", accounts[2].fs, ""] = BackendError(ErrorKind.NETWORK)

    def read(endpoint, context=None, **kwargs):
        digest = original_hash(endpoint, context, **kwargs)
        reads.append(endpoint)
        if len(reads) == 2:
            if failure == "changed":
                backend.put(accounts[0].fs, "copy", b"diff")
            elif failure == "hash":
                raise BackendError(ErrorKind.NETWORK)
            elif failure == "cancelled":
                context.cancelled.set()
        return digest

    monkeypatch.setattr(backend, "copy_file", copy)
    monkeypatch.setattr(backend, "hash_file", read)
    if failure == "save":
        # Hash keys make the store attempt a write; the current byte proof must still work.
        backend.no_hashes.clear()
        for account, kind in zip(accounts, ("md5", "sha1", "sha1")):
            backend.hash_types[account.fs, "copy"] = kind
        ui.window.vfs.verification_store.path = ui.window.store.directory
    ui.dialog.resolve()
    task = run_task(ui)
    assert len(copies) == 1 and task.result.steps[1].state == "ok"
    assert sum((a.fs, "copy") in backend.nodes for a in accounts) == 2
    assert task.state == ("완료" if failure in {"different", "save"} else "일부 성공")
    if failure != "cancelled":
        assert ui.window._repair_busy
        finish_recheck(ui)
        found = next((e for e in ui.window.scan_results if e.path == "copy"), None)
        if failure == "save":
            assert found is None and "0/2개 저장" in task.result.steps[-1].message
        else:
            assert found is not None
            assert found.assessment.status == {
                "different": Status.CONFLICT,
                "lookup": Status.UNKNOWN,
            }.get(failure, Status.UNVERIFIED)
    assert not ui.window._repair_busy
    if failure in {"hash", "cancelled"}:
        ui.window.retry_task(task)
        retried = run_task(ui, 1)
        assert retried.state == "완료" and len(copies) == 1
        finish_recheck(ui)
        assert all(e.path != "copy" for e in ui.window.scan_results)
    assert not ui.prompts


@pytest.mark.parametrize(
    "terminal", ["완료", "일부 성공", "실패 / 결과 확인 필요", "실패", "취소됨"]
)
def test_repair_blocks_repeat_submission_through_its_own_recheck(repair_ui, qtbot, terminal):
    window, dialog = repair_ui.window, repair_ui.dialog
    selection = dialog.selected_entries()
    qtbot.mouseClick(dialog.resolve_button, Qt.MouseButton.LeftButton)
    assert len(repair_ui.queued) == 1 and not repair_ui.plans and window._repair_busy
    assert not dialog.resolve_button.isEnabled() and dialog.resolve_button.text() == "문제 해결 중…"
    qtbot.mouseClick(dialog.resolve_button, Qt.MouseButton.LeftButton)
    dialog.resolve()
    window.resolve_issues(selection)
    dialog.table.clearSelection()
    dialog.table.selectAll()
    dialog.close()
    window.show_issues()
    assert len(repair_ui.queued) == 1 and not dialog.resolve_button.isEnabled()
    task, _ = repair_ui.queued[0]
    unrelated = TransferTask("unrelated", "other", TaskContext(), state="완료")
    window.transfers.tasks[unrelated.id] = unrelated
    window.update_task(unrelated)
    assert window._repair_tasks == {task.id}
    for state in ("대기", "진행 중", "취소 요청"):
        task.state = state
        window.update_task(task)
        window.resolve_issues(selection)
        assert len(repair_ui.queued) == 1 and window._repair_busy
    task.state = terminal
    window.update_task(task)
    if terminal in {"완료", "일부 성공"}:
        assert window._repair_busy and not dialog.resolve_button.isEnabled()
        finish_recheck(repair_ui)
    assert not window._repair_busy and not window._repair_tasks
    assert not window._pending_repairs and not repair_ui.prompts
    assert dialog.resolve_button.isEnabled() and dialog.resolve_button.text() == "선택 문제 해결"


@pytest.mark.parametrize("ending", ["empty", "closing", "preparing", "submit_error"])
def test_repair_without_registration_does_not_leave_a_lock(repair_ui, monkeypatch, ending):
    window = repair_ui.window
    entries = repair_ui.dialog.selected_entries()
    if ending == "empty":
        entries = ()
    elif ending == "closing":
        window._closing = True
    elif ending == "preparing":
        window._preparing = True
    else:

        def fail(*args, **kwargs):
            raise RuntimeError("등록 실패")

        monkeypatch.setattr(window.transfers, "submit", fail)
    window.resolve_issues(entries)
    assert not repair_ui.queued and not repair_ui.plans
    assert not window._repair_busy and not window._pending_repairs
    assert repair_ui.dialog.progress_label.isHidden() and not repair_ui.prompts
    if ending == "submit_error":
        assert repair_ui.errors == ["/folder: 등록 실패"]


def test_repair_registers_every_item_without_lookup_or_confirmation(
    repair_ui, backend, monkeypatch
):
    window = repair_ui.window
    submit = window.transfers.submit
    backend.calls.clear()

    def reenter(label, operation, **kwargs):
        assert window._preparing and window._repair_busy
        window.resolve_issues(window.scan_results)
        return submit(label, operation, **kwargs)

    monkeypatch.setattr(window.transfers, "submit", reenter)
    window.resolve_issues((*reversed(window.scan_results), window.scan_results[0]))
    assert not backend.calls and not repair_ui.plans and not repair_ui.prompts
    assert len(repair_ui.queued) == window.task_tree.topLevelItemCount() == 2
    assert len(window._repair_tasks) == len(window._pending_repairs) == 2
    folder, file = (task for task, _ in repair_ui.queued)
    assert repair_ui.dependencies[file.id] == (folder.id,)
    assert window._task_rows[file.id].toolTip(0) == file.label


def test_repair_shows_planning_execution_and_recheck_in_each_task(repair_ui):
    window, dialog = repair_ui.window, repair_ui.dialog
    assert [dock.windowTitle() for dock in window.findChildren(QDockWidget)] == ["작업"]
    assert not any(
        "여러 항목을 선택할 수 있습니다" in label.text() for label in dialog.findChildren(QLabel)
    )
    dialog.table.selectAll()
    window.resolve_issues(dialog.selected_entries())
    messages = []
    for index, (task, _) in enumerate(repair_ui.queued):
        progress = task.context.progress

        def observe(value):
            progress(value)
            messages.append(dialog.progress_label.text())

        task.context.progress = observe
        run_task(repair_ui, index)
        row = window._task_rows[task.id]
        assert row.text(1) == "결과 재확인" and window._repair_busy
        window.update_task(task)
        assert row.text(1) == "결과 재확인"
    assert any("계획 조회" in text and "/folder" in text for text in messages)
    assert any("용량 조회" in text and "/folder/file" in text for text in messages)
    assert any("폴더 생성 · 1/2" in text for text in messages)
    assert any("전송 · 1/1" in text and "/folder/file" in text for text in messages)
    assert any("전송 검증 · 1/1" in text for text in messages)
    dialog.close()
    window.show_issues()
    assert not dialog.progress_label.isHidden()
    window.flush_task_refreshes()
    assert len(repair_ui.plans) == 1
    finish_recheck(repair_ui)
    assert not window._repair_busy
    last = row.text(2)
    assert row.text(1) == "완료" and "1개 해결" in last
    assert dialog.progress_label.isHidden() and not repair_ui.prompts
    repair_ui.contexts[-1].progress({"detail": "late recheck", "recheck_index": 1})
    task.context.progress({"detail": "late planning"})
    assert row.text(2) == last and dialog.progress_label.isHidden()


def test_repair_retry_queues_only_once_without_another_confirmation(repair_ui):
    window, dialog = repair_ui.window, repair_ui.dialog
    window.resolve_issues(dialog.selected_entries())
    original, _ = repair_ui.queued[0]
    original.state = "실패"
    window.update_task(original)
    window.retry_task(original)
    window.retry_task(original)
    window.resolve_issues(dialog.selected_entries())
    assert len(repair_ui.queued) == 2 and not repair_ui.plans and not repair_ui.prompts
    retry, _ = repair_ui.queued[1]
    window.update_task(original)
    assert window._repair_tasks == {retry.id} and not dialog.resolve_button.isEnabled()
    run_task(repair_ui, 1)
    assert window._repair_busy
    finish_recheck(repair_ui)
    assert not window._repair_busy and not window._pending_repairs


def test_other_mutations_and_their_retries_still_confirm(repair_ui):
    window = repair_ui.window
    window.prepare(lambda context: window.engine.plan_mkdir("other", context))
    build, ready, _ = repair_ui.plans[0]
    ready(build(TaskContext()))
    task, _ = repair_ui.queued[0]
    assert not window._repair_busy and repair_ui.prompts == ["새 폴더"]
    task.state = "실패"
    window.update_task(task)
    window.retry_task(task)
    build, ready, _ = repair_ui.plans[1]
    ready(build(TaskContext()))
    assert len(repair_ui.queued) == 2 and repair_ui.prompts == ["새 폴더", "새 폴더"]


def test_completed_repair_rechecks_original_selection_and_preserves_other_rows(repair_ui, backend):
    window, dialog = repair_ui.window, repair_ui.dialog
    folder, file = window.scan_results
    window.tree_clicked(window.tree_model.index(2, 0))
    window.resolve_issues((folder,))
    task = run_task(repair_ui)
    assert task.state == "완료" and window._repair_busy
    dialog.table.selectRow(1)
    window.path = "another-folder"
    backend.calls.clear()
    finish_recheck(repair_ui)
    assert window.scan_results == dialog.entries == (file,)
    assert window.scan_results[0] is file and dialog.selected_entries() == (file,)
    assert tuple(window.file_model.entries) == (file,)
    assert window.path == "another-folder" and window.scan_path == ""
    assert {path for operation, _, path in backend.calls if operation == "list"} == {""}
    assert not window._repair_busy and "1개 문제" in dialog.heading.text()
    count = len(repair_ui.plans)
    window.update_task(task)
    assert len(repair_ui.plans) == count


@pytest.mark.parametrize("status", [None, Status.CONFLICT, Status.UNVERIFIED, Status.UNKNOWN])
def test_completed_copy_keeps_any_problem_found_on_recheck(repair_ui, backend, accounts, status):
    window = repair_ui.window
    folder, file = window.scan_results
    window.resolve_issues((file,))
    assert run_task(repair_ui).state == "완료"
    if status == Status.CONFLICT:
        backend.put(accounts[1].fs, file.path, b"diff")
    elif status == Status.UNVERIFIED:
        backend.no_hashes.update(a.fs for a in accounts)
    elif status == Status.UNKNOWN:
        backend.failures["list", accounts[1].fs, "folder"] = BackendError(ErrorKind.NETWORK)
    finish_recheck(repair_ui)
    assert window.scan_results[0] is folder
    if status is None:
        assert window.scan_results == (folder,)
    else:
        assert window.scan_results[1].assessment.status == status
        assert window.scan_complete == (status != Status.UNKNOWN)
    assert not window._repair_busy


def test_one_planning_failure_does_not_prevent_other_items_from_running(
    repair_ui, backend, accounts
):
    window = repair_ui.window
    for path in ("bad", "good"):
        backend.put(accounts[0].fs, path, b"keep")
    window.scan_results, window.scan_complete, _ = window.vfs.scan("")
    selected = [entry for entry in window.scan_results if entry.path in {"bad", "good"}]
    backend.failures["stat", accounts[1].fs, "bad"] = BackendError(ErrorKind.NETWORK)
    window.resolve_issues(selected)
    failed = run_task(repair_ui, 0)
    successful = run_task(repair_ui, 1)
    assert failed.state == "실패" and "연결 오류" in failed.error
    assert successful.state == "완료" and not repair_ui.errors and not repair_ui.prompts
    finish_recheck(repair_ui)
    assert not window._repair_busy
    assert sum((account.fs, "bad") in backend.nodes for account in accounts) == 1
    assert sum((account.fs, "good") in backend.nodes for account in accounts) == 2
    assert any(entry.path == "bad" for entry in window.scan_results)


def test_partial_repair_removes_only_confirmed_successes(repair_ui, backend, accounts):
    window = repair_ui.window
    repair_ui.dialog.table.selectAll()
    window.resolve_issues(window.scan_results)
    for account in accounts[1:]:
        backend.failures["copy-any", account.fs, ""] = BackendError(ErrorKind.PERMISSION)
    assert run_task(repair_ui, 0).state == "완료"
    assert run_task(repair_ui, 1).state != "완료"
    finish_recheck(repair_ui)
    assert [entry.path for entry in window.scan_results] == ["folder/file"]
    assert window.scan_results[0].assessment.status == Status.UNDER
    assert repair_ui.dialog.resolve_button.isEnabled()


def test_repair_tasks_can_be_cancelled_and_retried_individually(repair_ui, backend, accounts):
    window, dialog = repair_ui.window, repair_ui.dialog
    backend.put(accounts[0].fs, "other", b"other")
    window.scan_results, window.scan_complete, _ = window.vfs.scan("")
    window.show_issues()
    dialog.table.selectAll()
    window.resolve_issues(dialog.selected_entries())
    assert len(repair_ui.queued) == window.task_tree.topLevelItemCount() == 3
    cancelled = next(task for task, _ in repair_ui.queued if task.label.endswith("/folder/file"))
    window.cancel_task(cancelled)
    assert cancelled.context.cancelled.is_set() and window._repair_busy
    for index in range(len(repair_ui.queued)):
        run_task(repair_ui, index)
    assert cancelled.state == "취소됨" and window._repair_busy
    finish_recheck(repair_ui)
    assert not window._repair_busy and not window._pending_repairs
    assert [entry.path for entry in window.scan_results] == ["folder/file"]
    assert sum((account.fs, "other") in backend.nodes for account in accounts) == 2
    assert sum((account.fs, "folder/file") in backend.nodes for account in accounts) == 1
    window.retry_task(cancelled)
    assert len(repair_ui.queued) == 4 and not repair_ui.prompts
    retry, _ = repair_ui.queued[-1]
    assert window._repair_tasks == {retry.id}
    assert window._pending_repairs[retry.id].path == "folder/file"
    run_task(repair_ui, 3)
    finish_recheck(repair_ui)
    assert not window._repair_busy and not window.scan_results
    assert backend.nodes[accounts[0].fs, "folder/file"].data == b"keep"


def test_partial_registration_keeps_registered_work_locked_until_rechecked(repair_ui, monkeypatch):
    window, dialog = repair_ui.window, repair_ui.dialog
    submit = window.transfers.submit

    def fail_second(label, operation, **kwargs):
        if repair_ui.queued:
            raise RuntimeError("등록 실패")
        return submit(label, operation, **kwargs)

    monkeypatch.setattr(window.transfers, "submit", fail_second)
    window.resolve_issues(window.scan_results)
    assert len(repair_ui.queued) == 1 and window._repair_busy
    assert repair_ui.errors == ["/folder/file: 등록 실패"]
    window.resolve_issues(window.scan_results)
    assert len(repair_ui.queued) == 1 and not repair_ui.plans
    run_task(repair_ui)
    assert window._repair_busy and not dialog.resolve_button.isEnabled()
    finish_recheck(repair_ui)
    assert not window._repair_busy and not window._pending_repairs
    assert [entry.path for entry in window.scan_results] == ["folder/file"]


def test_nested_repair_tasks_wait_for_selected_ancestor_folders(repair_ui, backend, accounts):
    window = repair_ui.window
    backend.put(accounts[0].fs, "deep/inner/file", b"nested")
    window.scan_results, window.scan_complete, _ = window.vfs.scan("")
    selected = [entry for entry in reversed(window.scan_results) if entry.path.startswith("deep")]
    window.resolve_issues(selected)
    outer, inner, file = (task for task, _ in repair_ui.queued)
    assert outer.label.endswith("/deep") and inner.label.endswith("/deep/inner")
    assert file.label.endswith("/deep/inner/file")
    assert repair_ui.dependencies[inner.id] == (outer.id,)
    assert set(repair_ui.dependencies[file.id]) == {outer.id, inner.id}
    assert window._task_rows[file.id].toolTip(0) == file.label


def test_unknown_repair_targets_are_protected_before_planning(repair_ui, backend, accounts):
    window = repair_ui.window
    file = window.scan_results[1]
    window.resolve_issues((file,))
    assert {endpoint.account_id for endpoint in window.busy_endpoints()} == {a.id for a in accounts}
    window.prepare(lambda context: window.engine.plan_delete(file.replicas, context))
    build, ready, _ = repair_ui.plans[0]
    ready(build(TaskContext()))
    assert len(repair_ui.queued) == 1 and not repair_ui.prompts
    backend.put(accounts[0].fs, "other", b"other")
    replica = backend.replica(accounts[0].fs, "other", accounts[0].id)
    window.prepare(lambda context: window.engine.plan_delete((replica,), context))
    build, ready, _ = repair_ui.plans[1]
    ready(build(TaskContext()))
    assert len(repair_ui.queued) == 2 and len(repair_ui.prompts) == 1


def test_configuration_waits_for_the_final_repair_signal_and_recheck(repair_ui):
    window = repair_ui.window
    window.resolve_issues(window.scan_results[:1])
    task, _ = repair_ui.queued[0]
    task.state = "완료"
    assert not window.transfers.busy and not window.workers.pending
    assert not window.configuration_available()
    window.update_task(task)
    assert not window.configuration_available()
    assert window._task_rows[task.id].text(1) == "결과 재확인"
    assert not window.clear_completed_button.isEnabled()
    window.remove_finished_tasks((task.id,))
    assert task.id in window.transfers.tasks
    finish_recheck(repair_ui)
    assert window.configuration_available()
    assert window.clear_completed_button.isEnabled()
    window.clear_completed_button.click()
    assert task.id not in window.transfers.tasks


@pytest.mark.parametrize("reason", ["failure", "start_failure", "scope", "new_scan", "closing"])
def test_recheck_failure_or_late_result_preserves_scan_and_releases_lock(
    repair_ui, monkeypatch, reason
):
    window = repair_ui.window
    window.resolve_issues(window.scan_results[:1])
    task, _ = repair_ui.queued[0]
    task.state = "완료"
    original = window.scan_results
    if reason == "start_failure":

        def fail(*args, **kwargs):
            raise RuntimeError("재확인 시작 실패")

        monkeypatch.setattr(window.workers, "submit", fail)
    window.update_task(task)
    window.flush_task_refreshes()
    if reason != "start_failure":
        check, done, failed = repair_ui.plans[-1]
        result = check(TaskContext())
        if reason == "failure":
            failed("재확인 실패")
        else:
            if reason == "scope":
                window.scan_path = "other"
            elif reason == "new_scan":
                original = tuple(replace(entry) for entry in original)
                window.scan_results = original
            else:
                window._closing = True
            done(result)
    assert window.scan_results is original
    assert not window._repair_busy and not window._pending_repairs
    assert bool(repair_ui.errors) == (reason in {"failure", "start_failure"})


def test_recheck_does_not_reopen_closed_issue_dialog(repair_ui):
    window, dialog = repair_ui.window, repair_ui.dialog
    window.resolve_issues(window.scan_results)
    for index in range(len(repair_ui.queued)):
        run_task(repair_ui, index)
    dialog.close()
    finish_recheck(repair_ui)
    assert not dialog.isVisible() and dialog.entries == window.scan_results == ()
    assert not window._repair_busy


def test_replica_deletion_still_confirms_and_rechecks_without_bulk_lock(repair_ui):
    window = repair_ui.window
    folder, file = window.scan_results
    window.prepare(lambda context: window.engine.plan_delete(file.replicas, context))
    build, ready, _ = repair_ui.plans[0]
    ready(build(TaskContext()))
    assert run_task(repair_ui).state == "완료" and not window._repair_busy
    assert len(repair_ui.prompts) == 1
    finish_recheck(repair_ui)
    assert window.scan_results == repair_ui.dialog.entries == (folder,)
