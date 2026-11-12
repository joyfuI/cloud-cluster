import hashlib
import sqlite3
from dataclasses import replace
from threading import Barrier

import pytest

from cloud_cluster.domain import BackendError, ErrorKind, Status
from cloud_cluster.gui import window as window_module
from cloud_cluster.gui.window import MainWindow
from cloud_cluster.operations import local_endpoint
from cloud_cluster.settings import Settings, SettingsStore
from cloud_cluster.transfers import TaskContext
from cloud_cluster.verification import VerificationStore


@pytest.mark.parametrize(
    "case",
    ["comparable", "cached", "different_hashes", "partial", "overwrite", "recovered", "changed", "extra"],
)
def test_upload_saves_only_needed_verification_without_reading_cloud_again(
    qtbot, monkeypatch, backend, accounts, tmp_path, case
):
    a, b, c = accounts
    source = tmp_path / "x"
    data = b"same"
    source.write_bytes(data)
    local = local_endpoint(source)
    backend.put(local.fs, local.path, data)
    backend.hash_types[a.fs, "x"] = "md5"
    backend.hash_types[b.fs, "x"] = "md5" if case in {"comparable", "extra"} else "sha1"
    if case in {"partial", "overwrite", "recovered"}:
        backend.no_hashes.add(b.fs)
    if case == "overwrite":
        backend.put(b.fs, "x", b"prev")
    if case == "extra":
        backend.put(c.fs, "x", data)
        backend.hash_types[c.fs, "x"] = "sha1"
        accounts[2] = replace(c, readonly=True)

    get_replica = backend.replica

    def replica(fs, path, account_id):
        item = get_replica(fs, path, account_id)
        if account_id == "local":
            hashes = {kind: hashlib.new(kind, data).hexdigest() for kind in ("md5", "sha1")}
            item = replace(item, hashes=hashes)
        return item

    monkeypatch.setattr(backend, "replica", replica)
    if case == "changed":
        hash_file = backend.hash_file

        def hash_then_change(endpoint, context):
            digest = hash_file(endpoint, context)
            backend.put(b.fs, "x", b"diff")
            return digest

        monkeypatch.setattr(backend, "hash_file", hash_then_change)
    if case == "recovered":
        copy_file = backend.copy_file

        def copy_then_lose_response(origin, target, context):
            copy_file(origin, target, context)
            if target.account_id == b.id:
                raise BackendError(ErrorKind.NETWORK)

        monkeypatch.setattr(backend, "copy_file", copy_then_lose_response)

    window = MainWindow(backend, Settings(accounts=accounts), SettingsStore(tmp_path / "ui"))
    qtbot.addWidget(window)
    try:
        qtbot.waitUntil(lambda: window.snapshot is not None and not window.workers.pending)
        writes = []
        store = window.vfs.verification_store
        if case == "cached":
            digest = hashlib.sha256(data).hexdigest()
            for account in (a, b):
                backend.put(account.fs, "x", data)
            assert store.record(
                (account, backend.replica(account.fs, "x", account.id), digest)
                for account in (a, b)
            ) == 2
        record = store.record

        def capture(proofs):
            proofs = tuple(proofs)
            writes.append(tuple(account.id for account, _, _ in proofs))
            return record(proofs)

        monkeypatch.setattr(store, "record", capture)
        plan = window.engine.plan_upload(source, "x")
        task, = window.enqueue_uploads(plan)
        qtbot.waitUntil(lambda: task.id in window._finished_tasks and not window.workers.pending)
        assert task.state == "완료"

        def reads():
            return [fs for operation, fs, _ in backend.calls if operation == "hash"]

        expected_reads = {
            "comparable": [],
            "cached": [],
            "overwrite": [local.fs, b.fs],
            "recovered": [local.fs, b.fs],
        }.get(case, [local.fs])
        assert reads() == expected_reads
        expected_writes = {
            "comparable": [], "cached": [], "changed": [], "partial": [(a.id,)]
        }.get(case, [(a.id, b.id)])
        assert [tuple(sorted(ids)) for ids in writes] == expected_writes

        issues, complete, _ = window.vfs.scan("")
        assert complete
        if case in {"partial", "extra", "changed"}:
            entry, = issues
            assert entry.assessment.status == Status.UNVERIFIED
            if case != "changed":
                result = window.verify_issue(entry, TaskContext())
                assert "SHA-256 일치" in result.steps[-1].message
                assert reads() == [*expected_reads, (c if case == "extra" else b).fs]
                issues, _, _ = window.vfs.scan("")
                assert [entry.assessment.status for entry in issues] == (
                    [Status.OVER] if case == "extra" else []
                )
        else:
            assert not issues
    finally:
        qtbot.waitUntil(lambda: not window.transfers.busy and not window.workers.pending)
        window._shutdown_done = True
        window.vfs.close()
        window.transfers.close()
        window.close()


@pytest.mark.parametrize("failure", [None, "changed", "cancelled", "hash", "save"])
def test_verification_saves_only_completed_stable_reads(
    qtbot, monkeypatch, backend, accounts, tmp_path, failure
):
    accounts = accounts[:2]
    for account, kind in zip(accounts, ("md5", "sha1")):
        backend.put(account.fs, "x", b"same")
        backend.hash_types[account.fs, "x"] = kind
    store = SettingsStore(tmp_path / "ui")
    previous_cache = store.directory / "verification.sqlite3"
    previous_cache.write_bytes(b"leave previous cache untouched")
    window = MainWindow(backend, Settings(accounts=accounts), store)
    qtbot.addWidget(window)
    results, errors = [], []
    monkeypatch.setattr(window_module, "confirm", lambda *args: True)
    monkeypatch.setattr(window_module, "show_text", lambda *args: results.append(args[-1]))
    monkeypatch.setattr(window_module, "message", lambda *args: errors.append(str(args[-1])))
    if failure == "save":
        window.vfs.verification_store.path = store.directory
    original = backend.hash_file
    both_reading = Barrier(2, timeout=5)

    def hash_file(endpoint, context=None, **kwargs):
        both_reading.wait()
        value = original(endpoint, context, **kwargs)
        if endpoint.account_id == accounts[1].id:
            if failure == "changed":
                backend.put(accounts[0].fs, "x", b"diff")
            elif failure == "cancelled":
                context.cancelled.set()
            elif failure == "hash":
                raise BackendError(ErrorKind.NETWORK)
        return value

    monkeypatch.setattr(backend, "hash_file", hash_file)
    try:
        qtbot.waitUntil(lambda: window.snapshot is not None and not window.workers.pending)
        assert window.snapshot.entries[0].assessment.status == Status.UNVERIFIED
        window.verify(window.snapshot.entries[0].replicas)
        qtbot.waitUntil(lambda: not window._verification_targets and not window.workers.pending)
        assert len(window.transfers.tasks) == 2
        saved = VerificationStore(store.directory / "content_hashes.db")
        assert window.vfs.verification_store.path == saved.path or failure == "save"
        assert previous_cache.read_bytes() == b"leave previous cache untouched"
        fresh = backend.list_dir(accounts[0], "")[0]
        if failure in {"changed", "cancelled", "hash"}:
            assert not results and bool(errors) == (failure != "cancelled")
            with sqlite3.connect(saved.path) as db:
                assert db.execute("SELECT count(*) FROM content_hashes").fetchone()[0] == 0
            assert window.vfs.cached("").entries[0].assessment.status == Status.UNVERIFIED
        else:
            assert results and not errors
            assert window.snapshot.entries[0].assessment.status == Status.HEALTHY
            # Keep remote fingerprints usable by mutation planning after verification.
            for replica in window.snapshot.entries[0].replicas:
                expected = window.engine.expected(window.engine.endpoint(replica))
                assert expected.version.fingerprint == replica.fingerprint
            if failure == "save":
                assert "0/2개 저장" in results[0] and "읽거나 쓸 수 없습니다" in results[0]
                assert not saved.path.exists()
            else:
                assert "2/2개 저장" in results[0]
                assert saved.restore(accounts[0], (fresh,))[0].verified_sha256
                count = sum(call[0] == "hash" for call in backend.calls)
                window.vfs.cache.invalidate()
                assert window.vfs.scan("") == ((), True, 1)
                assert sum(call[0] == "hash" for call in backend.calls) == count == 2
    finally:
        qtbot.waitUntil(lambda: not window._verification_targets and not window.workers.pending)
        window._shutdown_done = True
        window.vfs.close()
        window.transfers.close()
        window.close()


@pytest.mark.parametrize("persistent", [True, False])
def test_verification_rechecks_issue_list_using_fresh_remote_evidence(
    qtbot, monkeypatch, backend, accounts, tmp_path, persistent
):
    for account, kind in zip(accounts[:2], ("md5", "sha1")):
        backend.put(account.fs, "x", b"same")
        backend.hash_types[account.fs, "x"] = kind
    window = MainWindow(backend, Settings(accounts=accounts[:2]), SettingsStore(tmp_path / "ui"))
    qtbot.addWidget(window)
    monkeypatch.setattr(window_module, "confirm", lambda *args: True)
    monkeypatch.setattr(window_module, "show_text", lambda *args: None)
    try:
        qtbot.waitUntil(lambda: window.snapshot is not None and not window.workers.pending)
        window.scan_path = ""
        window.scan_results, window.scan_complete, _ = window.vfs.scan("")
        window.show_issues()
        window.tree_clicked(window.tree_model.index(2, 0))
        if not persistent:
            window.vfs.verification_store.path = window.store.directory
        window.verify(window.scan_results[0].replicas)
        qtbot.waitUntil(lambda: not window._verification_targets and not window.workers.pending)
        assert window.issue_dialog.isVisible()
        assert window.scan_results == window.issue_dialog.entries
        assert tuple(window.file_model.entries) == window.scan_results
        assert window.scan_results == ()
        if not persistent:
            assert window.vfs.scan("")[0][0].assessment.status == Status.UNVERIFIED
    finally:
        qtbot.waitUntil(lambda: not window._verification_targets and not window.workers.pending)
        window._shutdown_done = True
        window.vfs.close()
        window.transfers.close()
        window.close()


@pytest.mark.parametrize("cancel", [False, True])
def test_issue_verifications_run_in_queue_and_cancel_independently(
    qtbot, monkeypatch, backend, accounts, tmp_path, cancel
):
    from threading import Event

    accounts = accounts[:2]
    for account, kind in zip(accounts, ("md5", "sha1")):
        for name in ("x", "y"):
            backend.put(account.fs, name, name.encode())
            backend.hash_types[account.fs, name] = kind
    started, release = Event(), Event()
    original = backend.hash_file

    def read(endpoint, context=None, **kwargs):
        if endpoint.path == "x" and endpoint.account_id == accounts[0].id:
            started.set()
            while not release.wait(0.01):
                context.check()
        return original(endpoint, context, **kwargs)

    monkeypatch.setattr(backend, "hash_file", read)
    prompts, errors = [], []
    monkeypatch.setattr(window_module, "confirm", lambda *args: prompts.append(args[1]) or True)
    monkeypatch.setattr(window_module, "message", lambda *args: errors.append(str(args[-1])))
    window = MainWindow(backend, Settings(accounts=accounts), SettingsStore(tmp_path / "ui"))
    qtbot.addWidget(window)

    def finished():
        return (
            not window._repair_busy
            and not window.transfers.busy
            and not window.workers.pending
            and all(task.id in window._reported_tasks for task in window.transfers.tasks.values())
        )

    try:
        qtbot.waitUntil(lambda: window.snapshot is not None and not window.workers.pending)
        window.scan_path = ""
        window.scan_results, window.scan_complete, _ = window.vfs.scan("")
        window.show_issues()
        window.issue_dialog.table.selectAll()
        window.issue_dialog.resolve_button.click()
        first, second = window.transfers.tasks.values()
        qtbot.waitUntil(lambda: started.is_set() and second.state == "완료", timeout=5_000)
        assert window._repair_busy and not window.issue_dialog.resolve_button.isEnabled()
        assert not release.is_set() and first.state == "진행 중"
        if cancel:
            window.cancel_task(first)
        else:
            release.set()
        qtbot.waitUntil(finished, timeout=5_000)
        assert first.state == ("취소됨" if cancel else "완료")
        assert [entry.path for entry in window.scan_results] == (["x"] if cancel else [])
        assert not prompts and not errors
        assert all(node.data == path.encode() for (_, path), node in backend.nodes.items())
    finally:
        release.set()
        qtbot.waitUntil(finished, timeout=10_000)
        window._shutdown_done = True
        window.vfs.close()
        window.transfers.close()
        window.close()
