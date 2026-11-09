import json
import logging
import threading
import time
from types import SimpleNamespace

import pytest

from cloud_cluster._vendor.rclone import Rclone, RcloneException
from cloud_cluster.domain import BackendError, CopyRejected, ErrorKind
from cloud_cluster.operations import OperationEngine, local_endpoint
from cloud_cluster.rclone import RcloneAdapter, _rc_error
from cloud_cluster.transfers import TaskContext, TransferManager


def test_native_string_freed_even_if_json_is_invalid():
    freed = []
    pointer = SimpleNamespace(value=b"invalid-json")
    wrapper = Rclone.__new__(Rclone)
    wrapper.rclone = SimpleNamespace(
        RcloneRPC=lambda method, parameters: SimpleNamespace(Output=pointer, Status=200),
        RcloneFreeString=freed.append,
    )
    with pytest.raises(json.JSONDecodeError):
        wrapper.rpc("rc/noop")
    assert freed == [pointer]


def test_adapter_error_does_not_expose_credentials(caplog):
    caplog.set_level(logging.DEBUG, logger="cloud_cluster")
    adapter = RcloneAdapter.__new__(RcloneAdapter)

    def fail(method, **parameters):
        raise RcloneException({"error": "invalid_grant: secret-token", "input": parameters}, 500)

    adapter._runtime = SimpleNamespace(rpc=fail)
    with pytest.raises(BackendError) as error:
        adapter.rpc("config/create", password="secret-password")
    assert "secret" not in str(error.value)
    assert "method=config/create status=500 kind=AUTH" in caplog.text
    assert "secret" not in caplog.text


def test_file_rc_submission_logs_error_message_without_rc_payload(caplog):
    caplog.set_level(logging.DEBUG, logger="cloud_cluster")
    adapter = RcloneAdapter.__new__(RcloneAdapter)

    def fail(method, **parameters):
        assert "_trace" not in parameters
        raise RcloneException({"error": "open private.txt: permission denied", "input": parameters}, 500)

    adapter._runtime = SimpleNamespace(rpc=fail)
    with pytest.raises(BackendError) as error:
        adapter.run("operations/list", TaskContext(trace_id="example"), fs="secret-remote:")
    assert "rc_status=500" in error.value.diagnostic
    assert "provider_http" not in error.value.diagnostic
    assert "rc error detail trace=example method=operations/list job=-" in caplog.text
    assert "open private.txt: permission denied" in caplog.text
    assert "secret-remote" not in caplog.text


def test_box_preflight_size_error_is_definitive_only_on_a_single_attempt():
    message = 'pre-upload check: Error "file_size_limit_exceeded" (403): File size exceeds limit'
    failure = _rc_error("operations/copyfile", message, single_attempt=True)
    assert isinstance(failure, CopyRejected)
    assert failure.kind == ErrorKind.FILE_SIZE
    assert "failure_side=target" in failure.diagnostic
    assert not isinstance(_rc_error("operations/copyfile", message), CopyRejected)
    assert not isinstance(
        _rc_error("operations/copyfile", "HTTP 403: Access denied", single_attempt=True),
        CopyRejected,
    )


@pytest.mark.parametrize(
    ("level", "message", "side", "metadata"),
    [
        (
            logging.INFO, "failed to open source object: private.txt: permission denied",
            "source", "",
        ),
        (
            logging.ERROR, "operation error S3: PutObject, https response error "
            "StatusCode: 403, api error AccessDenied: private.txt",
            "target", "provider_http=403 provider_code=accessdenied",
        ),
        (
            logging.DEBUG, "googleapi: Error 403: private.txt\nAuthorization: Bearer secret-token "
            "https://example.test/file?sig=secret-signature, insufficientFilePermissions",
            "unknown", "provider_http=403 provider_code=insufficientfilepermissions",
        ),
    ],
)
def test_upload_failure_is_diagnosable_at_each_log_level(
    caplog, backend, accounts, tmp_path, level, message, side, metadata
):
    caplog.set_level(level, logger="cloud_cluster")
    source = tmp_path / "private.txt"
    source.write_bytes(b"data")
    endpoint = local_endpoint(source)
    backend.put(endpoint.fs, endpoint.path, b"data")
    engine = OperationEngine(backend, accounts)
    plan = engine.plan_upload(source, source.name)
    adapter = RcloneAdapter.__new__(RcloneAdapter)
    adapter._jobs = set()
    adapter._jobs_lock = threading.RLock()
    replies = iter(({"jobid": 42}, {"finished": True, "error": message}, {}, {}))
    adapter.rpc = lambda method, **parameters: next(replies)
    backend.copy_file = adapter.copy_file
    finished = threading.Event()
    manager = TransferManager(
        on_change=lambda task: finished.set() if task.result is not None else None
    )
    try:
        task = manager.submit(plan.label, lambda context: engine.execute(plan, context, approved=True))
        assert finished.wait(5)
    finally:
        manager.close()
    assert task.state == "실패 / 결과 확인 필요"
    record = next(record for record in caplog.records if "task finished" in record.message)
    assert record.levelno == logging.ERROR
    for field in (
        task.id, "action=파일 업로드", "stage='전송 결과 확인 · 권한 부족'", "provider='drive'",
        "source_provider='local'", "BackendError/PERMISSION", f"failure_side={side}",
        "write_result=unconfirmed", metadata,
    ):
        assert field in record.message
    assert "private.txt" not in record.message
    assert ("private.txt" in caplog.text) == (level == logging.DEBUG)
    assert "secret-" not in caplog.text and "https://example.test" not in caplog.text
    if level == logging.DEBUG:
        assert f"rc error detail trace={task.id} method=operations/copyfile job=42" in caplog.text
        assert f"phase finished trace={task.id} stage='전송 직전 원본 확인'" in caplog.text


def test_list_hashes_can_be_disabled_without_changing_default(accounts):
    adapter = RcloneAdapter.__new__(RcloneAdapter)
    requests = []

    def run(method, context, **parameters):
        requests.append((method, parameters["opt"]["showHash"]))
        return {}

    adapter.run = run
    adapter.list_dir(accounts[0], "")
    adapter.list_dir(accounts[0], "", show_hash=False)
    assert requests == [("operations/list", True), ("operations/list", False)]


def test_queue_concurrency_and_cancel_before_execution(caplog):
    caplog.set_level(logging.INFO, logger="cloud_cluster")
    release = threading.Event()
    finished = threading.Event()
    started = []

    def changed(task):
        if task.label == "previous" and task.state == "완료":
            finished.set()

    manager = TransferManager(workers=1, on_change=changed)

    def operation(context):
        started.append(1)
        assert release.wait(5)

    try:
        previous = manager.submit("previous", lambda context: None)
        assert finished.wait(3)
        manager.resize_idle_transfer_pool(2)
        assert manager.tasks[previous.id] is previous
        manager.submit("one", operation)
        manager.submit("two", operation)
        third = manager.submit("삭제 · /private-folder", operation, queue="transfer")
        deadline = time.monotonic() + 3
        while len(started) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(started) == 2
        manager.cancel(third.id)
        release.set()
        deadline = time.monotonic() + 3
        while manager.busy and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not manager.busy and len(started) == 2 and third.state == "취소됨"
    finally:
        release.set()
        manager.close()
    assert third.context.trace_id == third.id
    assert f"task queued task={third.id} action=삭제 queue=transfer" in caplog.text
    assert f"task cancel requested task={third.id}" in caplog.text
    assert f"task finished task={third.id} state=취소됨" in caplog.text
    assert "private-folder" not in caplog.text


def test_dependent_task_waits_without_using_a_worker_and_can_be_cancelled_alone():
    release, started, independent = threading.Event(), threading.Event(), threading.Event()
    manager = TransferManager(workers=1)
    children = []

    def parent(context):
        started.set()
        assert release.wait(5)

    try:
        first = manager.submit("folder", parent, queue="other")
        assert started.wait(3)
        child = manager.submit("file", lambda context: children.append(1), after=(first.id,))
        other = manager.submit("independent", lambda context: independent.set())
        assert independent.wait(3)
        assert child.state == "대기" and not children
        manager.cancel(child.id)
        assert child.state == "취소됨" and not first.context.cancelled.is_set()
        release.set()
        manager._pools["other"].shutdown(wait=True)
        manager._pools["transfer"].shutdown(wait=True)
        assert first.state == other.state == "완료" and not children
        assert not manager.busy and not manager._waiting
    finally:
        release.set()
        manager.close()


@pytest.mark.parametrize("cancelled", [False, True])
def test_failed_or_cancelled_parent_prevents_only_its_dependent_task(cancelled):
    release, started = threading.Event(), threading.Event()
    manager = TransferManager(workers=2)
    children, others = [], []

    def parent(context):
        started.set()
        assert release.wait(5)
        context.check()
        raise ValueError("folder failed")

    try:
        first = manager.submit("folder", parent, queue="other")
        assert started.wait(3)
        child = manager.submit("file", lambda context: children.append(1), after=(first.id,))
        other = manager.submit("independent", lambda context: others.append(1))
        if cancelled:
            manager.cancel(first.id)
        release.set()
        deadline = time.monotonic() + 3
        while manager.busy and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not manager.busy and not manager._waiting
        assert first.state == ("취소됨" if cancelled else "실패")
        assert child.state == "실패" and "선행 작업" in child.error and not children
        assert other.state == "완료" and others == [1]
    finally:
        release.set()
        manager.close()


def test_removing_a_finished_parent_preserves_a_waiting_child_dependency():
    started, release, ran = threading.Event(), threading.Event(), threading.Event()
    manager = TransferManager(workers=1)

    def wait_for_release(context):
        started.set()
        assert release.wait(5)

    try:
        first = manager.submit("first folder", lambda context: None)
        second = manager.submit("second folder", wait_for_release)
        assert started.wait(3)
        child = manager.submit("file", lambda context: ran.set(), after=(first.id, second.id))
        assert manager.remove_finished((first.id, second.id, child.id)) == {first.id}
        assert child.state == "대기"
        release.set()
        assert ran.wait(3)
    finally:
        release.set()
        manager.close()


def test_queue_start_failure_does_not_leave_a_waiting_task(monkeypatch):
    manager = TransferManager()

    def fail(*args):
        raise RuntimeError("queue unavailable")

    try:
        monkeypatch.setattr(manager._pools["transfer"], "submit", fail)
        with pytest.raises(RuntimeError, match="queue unavailable"):
            manager.submit("file", lambda context: None)
        assert not manager.busy and not manager._waiting
        assert next(iter(manager.tasks.values())).state == "실패"
    finally:
        manager.close()
