import base64
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import pytest
from PySide6.QtCore import QPoint, Qt, QTimer
from PySide6.QtWidgets import QApplication, QDialog

from cloud_cluster.accounts import READ_WRITE_PARAMETERS, AccountManager
from cloud_cluster.domain import BackendError, CloudAccount, Endpoint, ErrorKind, QuotaSnapshot
from cloud_cluster.gui import dialogs
from cloud_cluster.gui.dialogs import AuthDialog
from cloud_cluster.gui.rclone_help_ko import HELP_KO, translate_auth_text
from cloud_cluster.operations import OperationEngine
from cloud_cluster.rclone import NATIVE_DIR, RcloneAdapter
from cloud_cluster.settings import Settings, SettingsStore
from cloud_cluster.transfers import TaskContext

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not (NATIVE_DIR / "librclone.dll").exists(), reason="build DLL first"),
]


@pytest.fixture(scope="module")
def native(tmp_path_factory):
    root = tmp_path_factory.mktemp("native")
    backend = RcloneAdapter()
    store = SettingsStore(root / "settings")
    manager = AccountManager(backend, store, Settings())
    manager.open()
    manager.configure("cc_local_test", "local", {})
    assert b"[cc_local_test]" in store.config_path.read_bytes()
    assert not list(store.directory.glob("*.dpapi"))
    yield backend, root
    assert backend.shutdown()


def test_actual_dll_listing_copy_hash_and_plain_config(native):
    backend, root = native
    source, target = root / "source", root / "target"
    source.mkdir()
    target.mkdir()
    data = "한글 파일\n".encode() * 128
    (source / "원본.txt").write_bytes(data)
    src, dst = Endpoint(source.as_posix(), "원본.txt"), Endpoint(target.as_posix(), "사본.txt")
    updates = []
    backend.copy_file(src, dst, TaskContext(progress=updates.append))
    assert (target / "사본.txt").read_bytes() == data
    assert updates and updates[-1]["bytes"] == len(data)
    assert backend.hash_file(dst) == hashlib.sha256(data).hexdigest()
    account = CloudAccount("local-test", "cc_local_test", "local", target.as_posix())
    assert backend.list_dir(account, "")[0].path == "사본.txt"
    assert backend.stat(Endpoint(target.as_posix(), "missing")) is None
    for i in range(1000):
        assert backend.rpc("rc/noop", n=i)["n"] == i


@pytest.mark.parametrize("cancel_transfer", [False, True], ids=["copy", "cancel-transfer"])
def test_windows_folder_drop_downloads_virtual_files(
    native, qtbot, monkeypatch, tmp_path, cancel_transfer
):
    from ctypes import POINTER, WINFUNCTYPE, WinDLL, byref, c_long, c_void_p, cast
    from ctypes.wintypes import BOOL, DWORD, LPCWSTR, POINT, ULONG
    from uuid import UUID

    from cloud_cluster.gui import window as window_module
    from cloud_cluster.gui import windows_drag
    from cloud_cluster.gui.windows_drag import CANCEL, COPY, DROP, LEFT_BUTTON, MOVE

    def method(pointer, slot, result, *args):
        table = cast(pointer, POINTER(POINTER(c_void_p)))[0]
        return WINFUNCTYPE(result, c_void_p, *args)(table[slot])

    def iid(value):
        return UUID(value).bytes_le

    backend, _ = native
    source, destination = tmp_path / "source", tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    contents = {"한글.txt": b"hello Windows", "empty.txt": b""}
    for name, data in contents.items():
        (source / name).write_bytes(data)
    account = CloudAccount("source", "cc_local_test", "local", source.as_posix())
    window = window_module.MainWindow(
        backend, Settings(accounts=[account]), SettingsStore(tmp_path / "ui")
    )
    qtbot.addWidget(window)
    window.show()
    qtbot.waitUntil(lambda: window.snapshot is not None and not window.workers.pending)
    prompts = []
    jobs_at_first_stream = []
    file_results = []
    open_stream = windows_drag._open_stream
    get_file = windows_drag.FileDrag._get_file

    def observe_file(drag, index, medium):
        result = get_file(drag, index, medium)
        file_results.append(result)
        return result

    def observe_stream(*args):
        if not jobs_at_first_stream:
            tree = window.task_tree
            jobs_at_first_stream.extend(
                tree.topLevelItem(i).text(0) for i in range(tree.topLevelItemCount())
            )
        return open_stream(*args)

    def record_confirmation(parent, title, details):
        prompts.append(title)
        return True

    monkeypatch.setattr(window_module, "confirm", record_confirmation)
    monkeypatch.setattr(windows_drag, "_open_stream", observe_stream)
    monkeypatch.setattr(windows_drag.FileDrag, "_get_file", observe_file)
    if cancel_transfer:
        wait_for_file = window.wait_for_dragged_file

        def execute(plan, context, *, approved):
            context.wait(8)
            raise RuntimeError("Expected cancellation from the task panel")

        def cancel_from_task_panel(task):
            QTimer.singleShot(0, lambda: window.cancel_task(task))
            return wait_for_file(task)

        monkeypatch.setattr(window.engine, "execute", execute)
        monkeypatch.setattr(window, "wait_for_dragged_file", cancel_from_task_panel)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(QApplication, "platformName", lambda: "windows")
    ole = WinDLL("ole32")
    assert ole.OleInitialize(None) >= 0
    item, target = c_void_p(), c_void_p()
    try:
        create_item = WinDLL("shell32").SHCreateItemFromParsingName
        create_item.argtypes = (LPCWSTR, c_void_p, c_void_p, POINTER(c_void_p))
        assert create_item(
            str(destination), None, iid("43826d1e-e718-42ee-bc55-a1e261c37bfe"), byref(item)
        ) == 0
        bind_to_handler = method(item, 3, c_long, c_void_p, c_void_p, c_void_p, POINTER(c_void_p))
        assert bind_to_handler(
            item, None, iid("3981e225-f559-11d3-8e3a-00c04f6837d5"),
            iid("00000122-0000-0000-c000-000000000046"), byref(target),
        ) == 0
        def drop(cancel):
            drag_started = False

            def perform(data, source, allowed, result):
                nonlocal drag_started
                drag_started = True
                args = (c_void_p, DWORD, POINT, POINTER(DWORD))
                effect = DWORD(allowed)
                assert method(target, 3, c_long, *args)(
                    target, data, LEFT_BUTTON, POINT(), byref(effect)
                ) == 0
                assert effect.value & COPY
                assert not prompts and not window.transfers.tasks
                method(source, 4, c_long, DWORD)(source, effect.value)
                decision = method(source, 3, c_long, BOOL, DWORD)(source, cancel, 0)
                if cancel:
                    assert decision == CANCEL
                    assert method(target, 5, c_long)(target) == 0
                    return CANCEL
                assert decision == DROP
                effect.value = allowed
                assert method(target, 6, c_long, *args)(
                    target, data, 0, POINT(), byref(effect)
                ) == 0
                assert effect.value & MOVE == 0
                return DROP

            monkeypatch.setattr(windows_drag, "_do_drag_drop", perform)
            table = window.table
            table.selectAll()
            assert len(table.selectionModel().selectedRows()) == len(contents)
            origin = table.visualRect(window.proxy.index(0, 0)).topLeft() + QPoint(8, 8)
            moved = origin + QPoint(0, 35)
            qtbot.mousePress(table.viewport(), Qt.MouseButton.LeftButton, pos=origin)
            qtbot.mouseMove(table.viewport(), moved)
            qtbot.mouseRelease(table.viewport(), Qt.MouseButton.LeftButton, pos=moved)
            assert drag_started
            qtbot.waitUntil(lambda: not window._explorer_drag, timeout=10000)

        drop(cancel=True)
        assert not prompts and not window.transfers.tasks and not list(destination.iterdir())
        drop(cancel=False)
        assert not prompts
        if cancel_transfer:
            assert set(file_results) == {0x80270000}  # COPYENGINE_E_USER_CANCELLED
            assert not jobs_at_first_stream and not list(destination.iterdir())
            qtbot.waitUntil(lambda: not window.transfers.busy)
            assert [task.state for task in window.transfers.tasks.values()] == ["취소됨"] * len(contents)
        else:
            assert set(jobs_at_first_stream) == {f"다운로드 · /{name}" for name in contents}
            assert {path.name: path.read_bytes() for path in destination.iterdir()} == contents
            assert all(task.state == "완료" for task in window.transfers.tasks.values())
        assert {path.name: path.read_bytes() for path in source.iterdir()} == contents
        qtbot.waitUntil(lambda: not any(tmp_path.glob("cloud-cluster-drag-*")))
    finally:
        if target:
            method(target, 2, ULONG)(target)
        if item:
            method(item, 2, ULONG)(item)
        ole.OleUninitialize()
        window._shutdown_done = True
        window.vfs.close()
        window.transfers.close()
        window.close()


def test_overlapping_windows_file_requests_stage_distinct_files(monkeypatch, tmp_path):
    from ctypes import pointer

    from cloud_cluster.gui import windows_drag
    from cloud_cluster.gui.file_table import INTERNAL_MIME

    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    staged_paths = []
    release_staging = []
    results = {}

    def request(index):
        medium = pointer(windows_drag.Medium())
        result = drag._get_file(index, medium)
        results[index] = result
        if result == windows_drag.S_OK:
            windows_drag._release_medium(medium)

    def stage(paths, hold):
        staged_paths.extend(paths)
        release_staging.append(hold())
        for index, path in enumerate(paths):
            path.write_bytes(bytes([index]))

        def wait(index):
            if index == 0:
                request(1)
            return True

        return wait

    drag = windows_drag.FileDrag(
        INTERNAL_MIME,
        b"[]",
        [("one.txt", 1), ("two.txt", 1)],
        stage,
        lambda: None,
    )
    try:
        drag._feedback(drag.source, windows_drag.COPY)
        assert drag._continue_drag(drag.source, False, 0) == windows_drag.DROP
        request(0)
        assert results == {0: windows_drag.S_OK, 1: windows_drag.S_OK}
        request(1)
        assert len(staged_paths) == 2 and len(release_staging) == 1
    finally:
        drag._release(drag.data)
        files_kept = all(path.is_file() for path in staged_paths)
        for release in release_staging:
            release()
    assert files_kept and all(not path.parent.exists() for path in staged_paths)


@pytest.mark.parametrize("name, child", [("Report.txt", ""), ("Folder", "child.txt")])
def test_actual_dll_case_only_rename(native, tmp_path, name, child):
    backend, _ = native
    account = CloudAccount("local-rename", "cc_local_test", "local", tmp_path.as_posix())
    source = tmp_path / name
    if child:
        source.mkdir()
    (source / child).write_bytes(b"keep")
    engine = OperationEngine(backend, [account])
    plan = engine.plan_move(backend.list_dir(account, ""), name.lower())
    result = engine.execute(plan, TaskContext(), approved=True)
    assert result.outcome == "완료", result.steps
    assert [path.name for path in tmp_path.iterdir()] == [name.lower()]
    assert (tmp_path / name.lower() / child).read_bytes() == b"keep"


def test_copied_plain_config_reopens_in_a_fresh_process(native, tmp_path):
    _, root = native
    destination = tmp_path / "portable"
    destination.mkdir()
    shutil.copyfile(root / "settings/rclone.conf", destination / "rclone.conf")
    script = """
import sys
from pathlib import Path
from cloud_cluster.accounts import AccountManager
from cloud_cluster.rclone import RcloneAdapter
from cloud_cluster.settings import Settings, SettingsStore
store = SettingsStore(Path(sys.argv[1]))
backend = RcloneAdapter()
assert AccountManager(backend, store, Settings()).open() == ['cc_local_test']
assert not list(store.directory.glob('*.dpapi'))
assert backend.shutdown()
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(destination)], timeout=30, capture_output=True
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")


def test_account_quota_reads_local_capacity_with_actual_dll(native):
    backend, root = native
    manager = AccountManager(backend, None, Settings())
    account = CloudAccount("capacity", "cc_local_test", "local", root.as_posix(), enabled=False)
    quota = manager.quota(account)
    assert quota is not None
    assert quota.total is not None and quota.total > 0
    assert quota.free is not None and quota.free >= 0


def test_sqlite_proofs_survive_process_restart_with_actual_dll(tmp_path):
    for name in ("a", "b"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "한글.txt").write_bytes(b"same bytes")
    script = """
import sys
from dataclasses import replace
from pathlib import Path
from cloud_cluster.accounts import AccountManager
from cloud_cluster.domain import CloudAccount, Endpoint, Status
from cloud_cluster.rclone import RcloneAdapter
from cloud_cluster.settings import Settings, SettingsStore
from cloud_cluster.verification import VerificationStore
from cloud_cluster.vfs import VirtualFileSystem
root, mode = Path(sys.argv[1]), sys.argv[2]
backend = RcloneAdapter()
manager = AccountManager(backend, SettingsStore(root / 'app'), Settings())
manager.open()
if mode == 'seed':
    manager.configure('proof_local', 'local', {})
accounts = [CloudAccount(name, 'proof_local', 'local', (root / name).as_posix()) for name in ('a', 'b')]
original = backend.replica
def remote_hash(account_id, item, path=None):
    replica = original(account_id, item, path)
    kind = 'md5' if account_id == 'a' else 'sha1'
    return replace(replica, hashes={kind: replica.hashes[kind]}) if not replica.is_dir else replica
backend.replica = remote_hash
vfs = VirtualFileSystem(backend, accounts, verification_store=VerificationStore(root / 'content_hashes.db'))
try:
    entry = vfs.refresh('').entries[0]
    if mode == 'seed':
        assert entry.assessment.status == Status.UNVERIFIED
        proofs = []
        for replica in entry.replicas:
            endpoint = Endpoint(accounts[0 if replica.account_id == 'a' else 1].fs, replica.path, replica.account_id)
            digest = backend.hash_file(endpoint)
            assert backend.stat(endpoint).fingerprint == replica.fingerprint
            proofs.append((replica, digest))
        assert vfs.record_verification(proofs) == 2
    elif mode in ('reuse', 'renamed'):
        assert entry.assessment.status == Status.HEALTHY
        assert all(r.verified_sha256 for r in entry.replicas)
        def forbidden_read(*args, **kwargs):
            raise AssertionError('scan must not download bytes')
        backend.hash_file = forbidden_read
        assert vfs.scan('') == ((), True, 1)
    else:
        assert entry.assessment.status == Status.UNVERIFIED
        assert not entry.replicas[1].verified_sha256
finally:
    vfs.close()
    assert backend.shutdown()
"""
    for mode in ("seed", "reuse", "renamed", "changed"):
        if mode == "renamed":
            for name in ("a", "b"):
                (tmp_path / name / "한글.txt").rename(tmp_path / name / "이동.txt")
        if mode == "changed":
            (tmp_path / "b/이동.txt").write_bytes(b"diff bytes")
        result = subprocess.run(
            [sys.executable, "-c", script, str(tmp_path), mode],
            timeout=30,
            capture_output=True,
        )
        assert result.returncode == 0, result.stderr.decode(errors="replace")


def test_issue_resolution_verifies_different_native_hash_types(native, qtbot, monkeypatch):
    from dataclasses import replace

    from cloud_cluster.domain import Status
    from cloud_cluster.gui import window as window_module
    from cloud_cluster.gui.window import MainWindow

    backend, root = native
    root = root / "issue-verification"
    accounts = []
    for name in ("verify_a", "verify_b"):
        folder = root / name
        folder.mkdir(parents=True)
        (folder / "검증.txt").write_bytes(b"same original bytes")
        accounts.append(CloudAccount(name, "cc_local_test", "local", folder.as_posix()))
    replica = backend.replica

    def remote_hash(account_id, item, path=None):
        current = replica(account_id, item, path)
        if not current.hashes:
            return current
        kind = "md5" if account_id == "verify_a" else "sha1"
        return replace(current, hashes={kind: current.hashes[kind]})

    monkeypatch.setattr(backend, "replica", remote_hash)
    prompts, errors = [], []
    monkeypatch.setattr(window_module, "confirm", lambda *args: prompts.append(args[1]) or True)
    monkeypatch.setattr(window_module, "message", lambda *args: errors.append(str(args[-1])))
    window = MainWindow(backend, Settings(accounts=accounts), SettingsStore(root / "ui"))
    qtbot.addWidget(window)

    def finished():
        return not window._repair_busy and not window.transfers.busy and not window.workers.pending

    try:
        qtbot.waitUntil(lambda: window.snapshot is not None and not window.workers.pending)
        window.scan_path = ""
        window.scan_results, window.scan_complete, _ = window.vfs.scan("")
        assert window.scan_results[0].assessment.status == Status.UNVERIFIED
        window.show_issues()
        window.issue_dialog.resolve_button.click()
        qtbot.waitUntil(finished, timeout=10_000)
        task = next(iter(window.transfers.tasks.values()))
        assert task.state == "완료" and not prompts and not errors
        assert window.scan_results == window.issue_dialog.entries == ()
        assert "2/2개 저장" in task.result.steps[-1].message
        assert window.vfs.scan("") == ((), True, 1)
        for account in accounts:
            assert (root / account.id / "검증.txt").read_bytes() == b"same original bytes"
    finally:
        qtbot.waitUntil(finished, timeout=10_000)
        window._shutdown_done = True
        window.vfs.close()
        window.transfers.close()
        window.close()


@pytest.mark.parametrize("cache_kind", ["no_time", "object", "path"])
def test_copy_repair_reads_hashless_replicas_and_finishes_with_actual_dll(
    native, qtbot, monkeypatch, cache_kind
):
    from dataclasses import replace

    from cloud_cluster.domain import Status
    from cloud_cluster.gui import window as window_module
    from cloud_cluster.gui.window import MainWindow

    backend, root = native
    root = root / f"{cache_kind}-copy-repair"
    accounts = []
    for name in ("hashless_a", "hashless_b"):
        folder = root / name
        folder.mkdir(parents=True)
        accounts.append(CloudAccount(name, "cc_local_test", "local", folder.as_posix()))
    data = b"actual file bytes" * 4096
    (root / "hashless_a/검증.txt").write_bytes(data)
    replica, hash_file = backend.replica, backend.hash_file
    reads, prompts, errors = [], [], []

    def read(endpoint, context=None, **kwargs):
        reads.append(endpoint)
        return hash_file(endpoint, context, **kwargs)

    def hashless_replica(account_id, item, path=None):
        current = replica(account_id, item, path)
        # Local rclone has no object ID; simulate one while retaining its actual size and time.
        return replace(
            current,
            hashes={},
            version="",
            modified=None if cache_kind == "no_time" else current.modified,
            object_id=f"{account_id}:{current.path}" if cache_kind == "object" else current.object_id,
        )

    monkeypatch.setattr(backend, "replica", hashless_replica)
    monkeypatch.setattr(backend, "hash_file", read)
    monkeypatch.setattr(window_module, "confirm", lambda *args: prompts.append(args[1]) or True)
    monkeypatch.setattr(window_module, "message", lambda *args: errors.append(str(args[-1])))
    window = MainWindow(backend, Settings(accounts=accounts), SettingsStore(root / "ui"))
    qtbot.addWidget(window)

    def finished():
        return not window._repair_busy and not window.transfers.busy and not window.workers.pending

    try:
        qtbot.waitUntil(lambda: window.snapshot is not None and not window.workers.pending)
        window.scan_path = ""
        window.scan_results, window.scan_complete, _ = window.vfs.scan("")
        assert window.scan_results[0].assessment.status == Status.UNDER
        window.show_issues()
        window.issue_dialog.resolve_button.click()
        qtbot.waitUntil(finished, timeout=15_000)
        task = next(iter(window.transfers.tasks.values()))
        assert task.state == "완료" and not prompts and not errors
        assert len(window.transfers.tasks) == 1 and len(reads) == 2
        assert window.scan_results == window.issue_dialog.entries == ()
        assert ("2/2개 저장" if cache_kind != "no_time" else "0/2개 저장") in task.result.steps[-1].message
        if cache_kind != "no_time":
            from cloud_cluster.verification import VerificationStore

            window.vfs.verification_store = VerificationStore(window.vfs.verification_store.path)
            assert window.vfs.scan("") == ((), True, 1)
            assert len(reads) == 2
        else:
            assert window.vfs.scan("")[0][0].assessment.status == Status.UNVERIFIED
            assert not window.vfs.verification_store.path.exists()
        assert not list(root.glob("hashless_*/.cc-*"))
        for account in accounts:
            assert (root / account.id / "검증.txt").read_bytes() == data
    finally:
        qtbot.waitUntil(finished, timeout=15_000)
        window._shutdown_done = True
        window.vfs.close()
        window.transfers.close()
        window.close()


def test_scan_repairs_missing_folders_and_replica_with_actual_dll(native):
    from cloud_cluster.domain import Status
    from cloud_cluster.vfs import VirtualFileSystem

    backend, root = native
    root = root / "repairs"
    root.mkdir()
    accounts = []
    for name in ("repair_a", "repair_b", "repair_c"):
        folder = root / name
        folder.mkdir()
        accounts.append(CloudAccount(name, "cc_local_test", "local", folder.as_posix()))
    source = root / "repair_a/누락 폴더"
    source.mkdir()
    (source / "복제 부족.txt").write_bytes(b"keep original")
    vfs = VirtualFileSystem(backend, accounts)
    engine = OperationEngine(backend, accounts)
    try:
        issues, complete, count = vfs.scan("")
        assert complete and count == 2 and len(issues) == 2
        assert {entry.assessment.status for entry in issues} == {Status.DIRECTORY, Status.UNDER}
        for entry in sorted(issues, key=lambda item: item.assessment.status != Status.DIRECTORY):
            assert engine.execute_repair(entry, TaskContext(), approved=True).outcome == "완료"
        assert all((root / name / "누락 폴더").is_dir() for name in ("repair_b", "repair_c"))
        rechecked, complete = vfs.recheck(issues)
        assert complete and set(rechecked) == {entry.key for entry in issues}
        assert all(entry.assessment.status == Status.HEALTHY for entry in rechecked.values())
        assert vfs.scan("") == ((), True, 2)
        copies = [
            path.read_bytes()
            for account in accounts
            if (path := root / account.id / "누락 폴더/복제 부족.txt").exists()
        ]
        assert copies == [b"keep original", b"keep original"]
        assert not list(root.glob("repair_*/**/.cc-*"))
    finally:
        vfs.close()


def test_provider_exclusions_with_actual_dll_and_local_aliases(tmp_path):
    for name in ("p", "a", "b"):
        folder = tmp_path / name / "My Pack"
        folder.mkdir(parents=True)
        (folder / ("secret.txt" if name == "p" else "shared.txt")).write_bytes(b"keep")
    (tmp_path / "upload.txt").write_bytes(b"upload")
    script = """
import sys
from pathlib import Path
from cloud_cluster.accounts import AccountManager
from cloud_cluster.domain import CloudAccount, Replica, Status
from cloud_cluster.operations import OperationEngine
from cloud_cluster.rclone import RcloneAdapter
from cloud_cluster.settings import Settings, SettingsStore
from cloud_cluster.transfers import TaskContext
from cloud_cluster.vfs import VirtualFileSystem
root = Path(sys.argv[1])
backend = RcloneAdapter()
manager = AccountManager(backend, SettingsStore(root / 'app'), Settings())
manager.open()
for name in ('p', 'a', 'b'):
    manager.configure('rules_' + name, 'alias', {'remote': (root / name).as_posix()})
# Apply PikPak policy to a local alias without contacting a cloud account.
accounts = [CloudAccount(name, 'rules_' + name, 'pikpak' if name == 'p' else 'alias') for name in ('p', 'a', 'b')]
vfs = VirtualFileSystem(backend, accounts)
try:
    snapshot = vfs.refresh('')
    entry = next(item for item in snapshot.entries if item.path == 'My Pack')
    assert {r.account_id for r in entry.replicas} == {'a', 'b'}
    assert entry.assessment.status == Status.HEALTHY
    child = vfs.refresh('My Pack')
    assert {item.account_id for item in child.listings} == {'a', 'b'}
    assert [item.path for item in child.entries] == ['My Pack/shared.txt']
    assert vfs.scan('') == ((), True, 2)
    engine = OperationEngine(backend, accounts)
    plan = engine.plan_upload(root / 'upload.txt', 'My Pack/upload.txt')
    assert {copy.target.account_id for copy in plan.copies} == {'a', 'b'}
    assert engine.execute(plan, TaskContext(), approved=True).outcome == '완료'
    plan = engine.plan_mkdir('My Pack/new')
    assert {endpoint.account_id for endpoint in plan.mkdirs} == {'a', 'b'}
    assert engine.execute(plan, TaskContext(), approved=True).outcome == '완료'
    try:
        engine.plan_download(Replica('p', 'My Pack/secret.txt', size=4), root / 'out.txt')
    except ValueError:
        pass
    else:
        raise AssertionError('Excluded download was allowed')
finally:
    vfs.close()
    assert backend.shutdown()
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)], timeout=45, capture_output=True
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    assert (tmp_path / "p/My Pack/secret.txt").read_bytes() == b"keep"
    assert sorted(path.name for path in (tmp_path / "p/My Pack").iterdir()) == ["secret.txt"]
    assert not (tmp_path / "out.txt").exists()
    for name in ("a", "b"):
        assert (tmp_path / name / "My Pack/upload.txt").read_bytes() == b"upload"
        assert (tmp_path / name / "My Pack/new").is_dir()


def test_visible_provider_tooltips_have_korean_translations(native):
    backend, _ = native
    for provider in backend.rpc("config/providers")["providers"]:
        for option in provider["Options"]:
            name = option["Name"]
            if (
                option.get("Advanced")
                or option.get("Hide")
                or name in {"token", "type"}
                or name in READ_WRITE_PARAMETERS.get(provider["Name"], {})
            ):
                continue
            help_text = option.get("Help", "")
            if not help_text:
                continue
            assert help_text in HELP_KO, f"{provider['Name']}/{name}"
            assert re.search("[가-힣]", HELP_KO[help_text])


@pytest.mark.parametrize("password", ["test-app-password", "A" * 32])
@pytest.mark.parametrize("edit_password", [False, True])
def test_koofr_password_authenticates_after_create_and_gui_reauthentication(
    native, tmp_path, qtbot, monkeypatch, password, edit_password
):
    backend, _ = native
    user = "test@example.invalid"
    authorization = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()
    mount = {"id": "primary", "isPrimary": True, "spaceTotal": 100, "spaceUsed": 25}
    responses = {
        "/api/v2/mounts": {"mounts": [mount]},
        "/api/v2/mounts/primary": mount,
        "/api/v2/mounts/primary/files/info": {"name": "", "type": "dir"},
        "/api/v2/mounts/primary/files/list": {
            "files": [{"name": "test.txt", "type": "file", "size": 3, "hash": "test-hash"}]
        },
    }
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = urlsplit(self.path).path
            authenticated = self.headers.get("Authorization") == authorization
            requests.append((path, authenticated))
            if not authenticated or path not in responses:
                self.send_error(401 if not authenticated else 404)
                return
            body = json.dumps(responses[path]).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    previous_path = backend.rpc("config/paths")["config"]
    manager = AccountManager(backend, SettingsStore(tmp_path / "app"), Settings())
    name = "cc_koofr_password_test"
    created = False
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        assert name not in manager.open()
        created = True
        account = CloudAccount("remote:" + name, name, "koofr")
        errors, before = [], None
        monkeypatch.setattr(dialogs, "message", lambda parent, error: errors.append(str(error)))
        for reconnect in (False, True):
            dialog = AuthDialog(manager, manager.providers(), account if reconnect else None)
            qtbot.addWidget(dialog)
            if not reconnect:
                dialog.name.setText(name)
                dialog.provider.setCurrentIndex(next(
                    i for i in range(dialog.provider.count())
                    if dialog.provider.itemData(i)["Name"] == "koofr"
                ))
            dialog.timer.setInterval(60_000)
            dialog.next()
            qtbot.waitUntil(lambda: not dialog.workers.pending)
            for question in ("provider", "endpoint", "user", "password", "config_fs_advanced"):
                assert not errors
                assert dialog.current_result["Option"]["Name"] == question
                if question == "provider":
                    if reconnect:
                        assert dialog.question.currentData() == "other"
                    dialog.question.setCurrentIndex(dialog.question.findData("other"))
                elif question == "endpoint":
                    if reconnect:
                        assert dialog.question.text() == f"http://127.0.0.1:{server.server_port}"
                    dialog.question.setText(f"http://127.0.0.1:{server.server_port}")
                elif question == "user":
                    dialog.question.setText(user)
                elif question == "password" and (not reconnect or edit_password):
                    dialog.question.selectAll()
                    dialog.question.insert(password)
                elif question == "config_fs_advanced":
                    assert dialog.question.currentData() == "false"
                    dialog.previous()
                    assert dialog.current_result["Option"]["Name"] == "password"
                    dialog.next()
                    qtbot.waitUntil(lambda: not dialog.workers.pending)
                    assert dialog.current_result["Option"]["Name"] == "config_fs_advanced"
                dialog.next()
                qtbot.waitUntil(lambda: not dialog.workers.pending)
            assert not errors
            assert dialog.result() == QDialog.DialogCode.Accepted
            after = backend.rpc("config/get", name=name)["password"]
            assert after != password
            if reconnect:
                assert (after == before) is not edit_password
            before = after
            assert backend.list_dir(account, "")[0].path == "test.txt"
            assert manager.quota(account) == QuotaSnapshot(100 * 1024**2, 75 * 1024**2)
        assert set(responses) <= {path for path, _ in requests}
        assert all(authenticated for _, authenticated in requests)
    finally:
        try:
            if created:
                backend.rpc("config/delete", name=name)
        finally:
            backend.rpc("config/setpath", path=previous_path)
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()


def test_remote_names_work_with_actual_dll_and_reloaded_settings(native, tmp_path):
    backend, root = native
    store = SettingsStore(tmp_path / "settings")
    manager = AccountManager(backend, store, Settings())
    folder = tmp_path / "files"
    folder.mkdir()
    (folder / "한글.txt").write_text("이름 규칙 검사", encoding="utf-8")
    created = []
    try:
        existing = set(manager.open())
        for name in (
            "개인 드라이브",
            "회사  백업",
            "계정.+@_1",
            "123",
            ".",
            "숫자Ⅷ²",
            "긴" * 65,
            "remote--",
            "r-emote-",
            "remote",
            "REMOTE",
            "local",
        ):
            manager.configure(name, "alias", {"remote": folder.as_posix()})
            created.append(name)
            manager.refresh()
            manager.set_enabled(name, True)
            account = next(a for a in manager.settings.accounts if a.remote == name)
            assert backend.list_dir(account, "")[0].path == "한글.txt"
        for name in (
            "",
            "-remote",
            "remote-",
            " remote",
            "remote ",
            "rem:ote",
            "rem/ote",
            "rem\note",
            "rem\u0301",
            "rem\tote",
            "rem\u00a0ote",
            "remote😀",
        ):
            with pytest.raises(ValueError, match="리모트 이름"):
                manager.configure(name, "alias", {"remote": folder.as_posix()})
            with pytest.raises(BackendError):
                backend.rpc(
                    "config/create",
                    name=name,
                    type="alias",
                    parameters={"remote": folder.as_posix()},
                    opt={"nonInteractive": True, "noOutput": True},
                )
        loaded = store.load()
        assert loaded.accounts == []
        assert set(loaded.enabled_remotes) == set(created)
        reopened = AccountManager(backend, store, loaded)
        assert set(reopened.open()) == existing | set(created)
        assert loaded.accounts == manager.settings.accounts
    finally:
        for name in created:
            backend.rpc("config/delete", name=name)
        backend.rpc("config/setpath", path=str(root / "settings/rclone.conf"))


def test_direct_config_edits_are_reflected_after_restart_without_enabling_new_remotes(tmp_path):
    directory = tmp_path / "app"
    store = SettingsStore(directory)
    store.save(Settings(enabled_remotes=["allowed", "removed"]))
    files = tmp_path / "files"
    files.mkdir()
    (files / "한글.txt").write_text("whitelist", encoding="utf-8")
    script = """
import sys
from pathlib import Path
from cloud_cluster.accounts import AccountManager
from cloud_cluster.rclone import RcloneAdapter
from cloud_cluster.settings import SettingsStore
from cloud_cluster.vfs import VirtualFileSystem
store = SettingsStore(Path(sys.argv[1]))
settings = store.load()
backend = RcloneAdapter()
manager = AccountManager(backend, store, settings)
assert set(manager.open()) == {'allowed', sys.argv[2]}
assert {a.remote for a in settings.accounts if a.enabled} == {'allowed'}
assert all(a.id == 'remote:' + a.remote and a.root == '' for a in settings.accounts)
vfs = VirtualFileSystem(backend, settings.accounts)
try:
    snapshot = vfs.refresh('')
    assert snapshot.complete
    assert len(snapshot.entries) == 1
    assert snapshot.entries[0].path == '한글.txt'
    assert [item.account_id for item in snapshot.listings] == ['remote:allowed']
finally:
    vfs.close()
store.save(settings)
assert backend.shutdown()
"""
    for other in ("unused", "new_remote"):
        content = (
            f"[allowed]\ntype = alias\nremote = {files.as_posix()}\n\n"
            f"[{other}]\ntype = alias\nremote = {(tmp_path / 'missing').as_posix()}\n"
        )
        store.config_path.write_text(content, encoding="utf-8")
        result = subprocess.run(
            [sys.executable, "-c", script, str(directory), other],
            timeout=30,
            capture_output=True,
        )
        assert result.returncode == 0, result.stderr.decode(errors="replace")
        assert store.config_path.read_text(encoding="utf-8") == content
        assert store.load().enabled_remotes == ["allowed", "removed"]


def test_account_deletion_preserves_files_and_empty_config_reopens(tmp_path):
    for name in ("remove", "keep"):
        folder = tmp_path / name
        folder.mkdir()
        (folder / "한글.txt").write_text("보존할 파일", encoding="utf-8")
    script = """
import sys
from pathlib import Path
from cloud_cluster.accounts import AccountManager
from cloud_cluster.rclone import RcloneAdapter
from cloud_cluster.settings import SettingsStore
root = Path(sys.argv[1])
store = SettingsStore(root / 'app')
settings = store.load()
backend = RcloneAdapter()
manager = AccountManager(backend, store, settings)
assert manager.open() == []
if sys.argv[2] == 'delete':
    for name in ('remove', 'keep'):
        manager.configure(name, 'alias', {'remote': (root / name).as_posix()})
    manager.refresh()
    for name in ('remove', 'keep'):
        manager.set_enabled(name, True)
    manager.delete('remove')
    assert manager.remotes() == ['keep']
    assert store.check_config() == ['keep']
    assert settings.enabled_remotes == ['keep']
    assert [a.remote for a in settings.accounts] == ['keep']
    manager.delete('keep')
    assert manager.remotes() == [] and store.check_config() == []
    assert settings.accounts == [] and settings.enabled_remotes == []
else:
    assert settings.enabled_remotes == [] and settings.accounts == []
    manager.configure('remove', 'alias', {'remote': (root / 'remove').as_posix()})
    manager.refresh()
    assert len(settings.accounts) == 1 and not settings.accounts[0].enabled
assert backend.shutdown()
"""
    for stage in ("delete", "reopen"):
        result = subprocess.run(
            [sys.executable, "-c", script, str(tmp_path), stage], timeout=30, capture_output=True
        )
        assert result.returncode == 0, result.stderr.decode(errors="replace")
        for name in ("remove", "keep"):
            assert (tmp_path / name / "한글.txt").read_text(encoding="utf-8") == "보존할 파일"


@pytest.mark.parametrize(
    "provider, expected",
    [
        ("drive", {"scope": "drive"}),
        (
            "onedrive",
            {
                "access_scopes": (
                    "Files.Read Files.ReadWrite Files.Read.All Files.ReadWrite.All "
                    "Sites.Read.All offline_access"
                )
            },
        ),
        ("hidrive", {"scope_access": "rw"}),
        ("google photos", {"read_only": "false"}),
    ],
)
def test_read_write_permissions_are_not_asked_during_reauthentication(native, provider, expected):
    backend, root = native
    manager = AccountManager(backend, SettingsStore(root / "settings"), Settings())
    name = "cc_scope_test_" + provider.replace(" ", "_")
    schema = next(p for p in manager.providers() if p["Name"] == provider)
    defaults = {option["Name"]: option.get("Default", "") for option in schema["Options"]}
    defaults["config_fs_advanced"] = False
    parameters = {"client_id": "test-client-id", "client_secret": "test-client-secret"}
    for reconnect in (False, True):
        result = manager.configure(name, provider, parameters, reconnect=reconnect)
        for _ in range(len(defaults) + 1):
            option = result["Option"]["Name"]
            if option == "config_is_local":
                break
            assert option not in expected
            value = defaults[option]
            answer = str(value).lower() if isinstance(value, bool) else str(value or "")
            result = manager.configure(
                name, provider, {}, state=result["State"], answer=answer,
            )
        else:
            pytest.fail("OAuth 시작 전 설정 단계가 끝나지 않았습니다.")
        assert re.search("[가-힣]", translate_auth_text(result["Option"]["Help"]))
        assert [translate_auth_text(e["Help"]) for e in result["Option"]["Examples"]] == ["예", "아니요"]
        config = backend.rpc("config/get", name=name)
        assert all(config[key] == value for key, value in expected.items())


def test_actual_running_job_cancellation(native):
    backend, root = native
    (root / "cancel.bin").write_bytes(b"x" * (1024 * 1024))
    (root / "cancel-copy.bin").write_bytes(b"existing file")
    context = TaskContext()
    backend.rpc("core/bwlimit", rate="64k")
    timer = threading.Timer(0.4, context.cancelled.set)
    timer.start()
    try:
        with pytest.raises(BackendError) as error:
            backend.copy_file(
                Endpoint(root.as_posix(), "cancel.bin"),
                Endpoint(root.as_posix(), "cancel-copy.bin"),
                context,
            )
        assert error.value.kind == ErrorKind.CANCELLED
        assert (root / "cancel-copy.bin").read_bytes() == b"existing file"
    finally:
        timer.cancel()
        backend.rpc("core/bwlimit", rate="off")


def test_native_dual_upload_and_verified_replace(native):
    backend, root = native
    accounts = []
    for name in ("one", "two"):
        folder = root / name
        folder.mkdir()
        accounts.append(CloudAccount(name, "cc_local_test", "local", folder.as_posix()))
    source = root / "upload.txt"
    source.write_text("동일한 원본", encoding="utf-8")
    engine = OperationEngine(backend, accounts)
    plan = engine.plan_upload(source, "nested/file.txt")
    result = engine.execute(plan, TaskContext(), approved=True)
    assert result.outcome == "완료", result
    assert (root / "one/nested/file.txt").read_bytes() == (
        root / "two/nested/file.txt"
    ).read_bytes()
    source.write_text("새 버전", encoding="utf-8")
    plan = engine.plan_upload(source, "nested/file.txt")
    result = engine.execute(plan, TaskContext(), approved=True)
    assert result.outcome == "완료", result
    assert (root / "one/nested/file.txt").read_text(encoding="utf-8") == "새 버전"
    assert (root / "two/nested/file.txt").read_text(encoding="utf-8") == "새 버전"
    for name in ("one", "two"):
        assert [p.name for p in (root / name / "nested").iterdir()] == ["file.txt"]


def test_actual_dll_overwrites_same_size_and_timestamp(native):
    backend, root = native
    source, target = root / "same-time-source.txt", root / "same-time-target.txt"
    source.write_bytes(b"new")
    target.write_bytes(b"old")
    shutil.copystat(source, target)
    backend.copy_file(
        Endpoint(root.as_posix(), source.name), Endpoint(root.as_posix(), target.name), TaskContext()
    )
    assert target.read_bytes() == b"new" and source.read_bytes() == b"new"
