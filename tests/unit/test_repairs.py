from dataclasses import replace
from pathlib import PurePosixPath
from threading import Event, Thread

import pytest

from cloud_cluster.domain import (
    BackendCapabilities,
    BackendError,
    CopyRejected,
    ErrorKind,
    QuotaSnapshot,
)
from cloud_cluster.operations import OperationEngine
from cloud_cluster.transfers import TaskContext
from cloud_cluster.vfs import VirtualFileSystem


def read_entry(backend, accounts, path):
    vfs = VirtualFileSystem(backend, accounts)
    parent = str(PurePosixPath(path).parent)
    try:
        snapshot = vfs.refresh("" if parent == "." else parent)
        return next(entry for entry in snapshot.entries if entry.path == path)
    finally:
        vfs.close()


@pytest.mark.parametrize("restriction", ["unsupported", "readonly", "disabled"])
def test_directory_repair_only_creates_missing_supported_writable_folders(
    backend, accounts, restriction
):
    a, b, c = accounts
    if restriction == "unsupported":
        backend.caps[c.id] = BackendCapabilities(empty_directories=False)
    else:
        c = replace(c, **{"readonly": True} if restriction == "readonly" else {"enabled": False})
    accounts = [a, b, c]
    backend.put(a.fs, "folder/keep", b"keep")
    entry = read_entry(backend, accounts, "folder")
    engine = OperationEngine(backend, accounts)
    plan = engine.plan_repair(entry)
    assert [(e.account_id, e.path) for e in plan.mkdirs] == [(b.id, "folder")]
    assert not plan.copies and not plan.deletions
    before = dict(backend.nodes)
    with pytest.raises(PermissionError):
        engine.execute(plan, TaskContext())
    assert backend.nodes == before
    assert engine.execute(plan, TaskContext(), approved=True).outcome == "완료"
    assert backend.nodes[b.fs, "folder"].data is None
    assert backend.nodes[a.fs, "folder/keep"].data == b"keep"
    assert (c.fs, "folder") not in backend.nodes


def test_directory_repair_keeps_partial_success(backend, accounts):
    a, b, c = accounts
    backend.put(a.fs, "folder", None)
    engine = OperationEngine(backend, accounts)
    plan = engine.plan_repair(read_entry(backend, accounts, "folder"))
    backend.failures["mkdir", b.fs, "folder"] = BackendError(ErrorKind.PERMISSION)
    result = engine.execute(plan, TaskContext(), approved=True)
    assert result.outcome == "일부 성공"
    assert backend.nodes[c.fs, "folder"].data is None
    assert (b.fs, "folder") not in backend.nodes


def test_directory_repair_with_only_readonly_missing_account_is_rejected(backend, accounts):
    a, b, _ = accounts
    accounts = [a, replace(b, readonly=True)]
    backend.put(a.fs, "folder", None)
    engine = OperationEngine(backend, accounts)
    with pytest.raises(ValueError, match="쓰기 계정"):
        engine.plan_repair(read_entry(backend, accounts, "folder"))


@pytest.mark.parametrize("reserved, target_id", [(0, "c"), (80, "b")])
def test_replica_repair_recommends_by_remaining_ratio_and_reserved_space(
    backend, accounts, reserved, target_id
):
    a, b, c = accounts
    backend.put(a.fs, "file", b"0123456789")
    backend.quotas[b.id] = QuotaSnapshot(1_000, 700)
    backend.quotas[c.id] = QuotaSnapshot(100, 90)
    engine = OperationEngine(backend, accounts)
    engine._reserved[c.id] = reserved
    before = dict(backend.nodes)
    plan = engine.plan_repair(read_entry(backend, accounts, "file"))
    assert backend.nodes == before
    (copy,) = plan.copies
    assert copy.target.account_id == target_id and copy.previous is None
    assert copy.source.endpoint.account_id == a.id
    assert [item.target.account_id for item in plan.alternates] == [
        account.id for account in (b, c) if account.id != target_id
    ]
    assert engine.execute(plan, TaskContext(), approved=True).outcome == "완료"
    target = next(account for account in accounts if account.id == target_id)
    assert backend.nodes[target.fs, "file"].data == backend.nodes[a.fs, "file"].data
    assert len([1 for _, path in backend.nodes if path == "file"]) == 2


@pytest.mark.parametrize("restriction", ["readonly", "disabled", "space", "size", "quota_error"])
def test_replica_repair_skips_ineligible_recommended_accounts(backend, accounts, restriction):
    a, b, c = accounts
    backend.put(a.fs, "file", b"0123456789")
    backend.quotas[b.id] = QuotaSnapshot(100, 50)
    backend.quotas[c.id] = QuotaSnapshot(100, 100)
    if restriction in {"readonly", "disabled"}:
        c = replace(c, **{"readonly": True} if restriction == "readonly" else {"enabled": False})
    elif restriction == "space":
        backend.quotas[c.id] = QuotaSnapshot(100, 9)
    elif restriction == "size":
        backend.caps[c.id] = BackendCapabilities(quota=True, max_file_size=9)
    else:
        about = backend.about

        def about_with_error(account, context=None):
            if account.id == c.id:
                raise BackendError(ErrorKind.NETWORK)
            return about(account, context)

        backend.about = about_with_error
    accounts = [a, b, c]
    plan = OperationEngine(backend, accounts).plan_repair(read_entry(backend, accounts, "file"))
    assert plan.copies[0].target.account_id == b.id
    assert not plan.alternates


def test_replica_repair_uses_actual_file_size_for_limit_and_space(backend, accounts):
    a, b, _ = accounts
    backend.put(a.fs, "file", b"0123456789")
    backend.caps[b.id] = BackendCapabilities(quota=True, max_file_size=15)
    backend.quotas[b.id] = QuotaSnapshot(100, 10)
    accounts = [a, b]
    plan = OperationEngine(backend, accounts).plan_repair(read_entry(backend, accounts, "file"))
    assert plan.copies[0].target.account_id == b.id


def test_replica_repair_reuses_features_when_fetching_uncached_quota(backend, accounts, monkeypatch):
    a, b, _ = accounts
    backend.put(a.fs, "file", b"data")
    entry = read_entry(backend, [a, b], "file")
    features, about = backend.features, backend.about
    seen_features, seen_quotas = [], []

    def read_features(account, context=None):
        seen_features.append(account.id)
        return features(account, context)

    def read_quota(account, context=None):
        seen_quotas.append(account.id)
        return about(account, context)

    monkeypatch.setattr(backend, "features", read_features)
    monkeypatch.setattr(backend, "about", read_quota)
    plan = OperationEngine(backend, [a, b]).plan_repair(entry)
    assert plan.copies[0].target.account_id == b.id
    assert seen_features == [a.id, b.id] and seen_quotas == [b.id]


def test_replica_repair_does_not_use_excluded_or_disabled_accounts(backend, accounts):
    a, b, c = accounts
    a = replace(a, provider="pikpak", root="")
    c = replace(c, enabled=False)
    accounts = [a, b, c]
    backend.put(b.fs, "My Pack/file", b"keep")
    entry = read_entry(backend, accounts, "My Pack/file")
    backend.calls.clear()
    with pytest.raises(ValueError, match="저장 계정 1개"):
        OperationEngine(backend, accounts).plan_repair(entry)
    assert not any(fs in {a.fs, c.fs} for _, fs, _ in backend.calls)
    assert not any(op in {"copy", "mkdir", "delete"} for op, _, _ in backend.calls)


@pytest.mark.parametrize("change", ["same", "different", "removed", "lookup_error"])
def test_repair_rechecks_state_and_never_treats_lookup_failure_as_absence(
    backend, accounts, change
):
    a, b, _ = accounts
    backend.put(a.fs, "file", b"keep")
    entry = read_entry(backend, accounts, "file")
    if change in {"same", "different"}:
        backend.put(b.fs, "file", b"keep" if change == "same" else b"different")
    elif change == "removed":
        del backend.nodes[a.fs, "file"]
    else:
        backend.failures["list", b.fs, ""] = BackendError(ErrorKind.NETWORK)
    before = dict(backend.nodes)
    with pytest.raises((ValueError, BackendError)):
        OperationEngine(backend, accounts).plan_repair(entry)
    assert backend.nodes == before


@pytest.mark.parametrize("change", ["source", "target", "directory_target"])
def test_repair_rechecks_changes_after_approval(backend, accounts, change):
    a, b, _ = accounts
    accounts = [a, b]
    path = "folder" if change == "directory_target" else "file"
    backend.put(a.fs, path, None if change == "directory_target" else b"keep")
    engine = OperationEngine(backend, accounts)
    plan = engine.plan_repair(read_entry(backend, accounts, path))
    backend.put(a.fs if change == "source" else b.fs, path, b"external")
    before = dict(backend.nodes)
    result = engine.execute(plan, TaskContext(), approved=True)
    assert result.outcome != "완료"
    assert backend.nodes == before


def test_direct_repair_requires_a_request_and_checks_cancellation_before_lookup(backend, accounts):
    backend.put(accounts[0].fs, "file", b"keep")
    entry = read_entry(backend, accounts, "file")
    engine, context = OperationEngine(backend, accounts), TaskContext()
    backend.calls.clear()
    with pytest.raises(PermissionError):
        engine.execute_repair(entry, context)
    context.cancelled.set()
    with pytest.raises(BackendError) as error:
        engine.execute_repair(entry, context, approved=True)
    assert error.value.kind == ErrorKind.CANCELLED and not backend.calls
    assert not engine._active and not engine._reserved


@pytest.mark.parametrize(
    ("failure", "attempts", "outcome"),
    [
        ("rejected", ["b", "c"], "완료"),
        ("reservation", ["c"], "완료"),
        ("lost_response", ["b"], "완료"),
        ("unconfirmed", ["b"], "실패 / 결과 확인 필요"),
        ("changed_alternate", ["b"], "실패 / 결과 확인 필요"),
    ],
)
def test_direct_replica_repair_recovers_like_upload(
    backend, accounts, monkeypatch, failure, attempts, outcome
):
    a, b, c = accounts
    accounts[1] = b = replace(b, provider="box")
    backend.put(a.fs, "file", b"data")
    entry = read_entry(backend, accounts, "file")
    engine = OperationEngine(backend, accounts)
    plan_repair, copy_file = engine.plan_repair, backend.copy_file
    seen, box_checks = [], []

    def plan_then_consume_space(*args):
        plan = plan_repair(*args)
        engine.quotas.consume(b, 90_000)
        return plan

    def transfer(source, target, context):
        seen.append(target.account_id)
        if target.account_id == b.id:
            box_checks.append((context.retries, (c.fs.casefold(), "file") in engine._active))
            if failure == "changed_alternate":
                backend.put(c.fs, "file", b"external")
            if failure in {"rejected", "changed_alternate"}:
                raise CopyRejected(ErrorKind.FILE_SIZE)
            if failure == "lost_response":
                copy_file(source, target, context)
            raise BackendError(ErrorKind.NETWORK)
        return copy_file(source, target, context)

    if failure == "reservation":
        monkeypatch.setattr(engine, "plan_repair", plan_then_consume_space)
    if failure == "lost_response":
        backend.no_hashes.add(b.fs)
    monkeypatch.setattr(backend, "copy_file", transfer)
    result = engine.execute_repair(entry, TaskContext(), approved=True)

    assert seen == attempts and result.outcome == outcome
    assert box_checks == ([(0, True)] if b.id in attempts else [])
    assert not engine._active and not any(engine._reserved.values())
    assert backend.nodes[a.fs, "file"].data == b"data"
    if failure in {"rejected", "reservation"}:
        assert (b.fs, "file") not in backend.nodes
        assert backend.nodes[c.fs, "file"].data == b"data"
        assert any(step.action == "대체 복제" and step.account_id == c.id for step in result.steps)
    elif failure == "changed_alternate":
        assert backend.nodes[c.fs, "file"].data == b"external"
    else:
        assert (c.fs, "file") not in backend.nodes
        if failure == "lost_response":
            assert ("hash", b.fs, "file") in backend.calls
            assert any(step.action == "전송 결과 확인" for step in result.steps)
        else:
            assert ErrorKind.NETWORK.value in result.steps[-1].message


def test_direct_repairs_reserve_recommended_space_before_other_plans_and_transfer_concurrently(
    backend, accounts, monkeypatch
):
    a, b, c = accounts
    for name in ("first", "second"):
        backend.put(a.fs, name, b"x" * 20)
    entries = [read_entry(backend, accounts, name) for name in ("first", "second")]
    backend.quotas[b.id], backend.quotas[c.id] = QuotaSnapshot(100, 60), QuotaSnapshot(100, 50)
    engine = OperationEngine(backend, accounts)
    release, started = Event(), Event()
    copy = backend.copy_file

    def copy_file(source, target, context=None):
        if source.path == "first":
            started.set()
            assert release.wait(5)
        return copy(source, target, context)

    monkeypatch.setattr(backend, "copy_file", copy_file)
    results, errors = {}, []

    def execute(index):
        try:
            results[index] = engine.execute_repair(entries[index], TaskContext(), approved=True)
        except Exception as error:
            errors.append(error)

    threads = [Thread(target=execute, args=(index,)) for index in range(2)]
    try:
        threads[0].start()
        assert started.wait(3) and engine._reserved[b.id] == 20
        threads[1].start()
        threads[1].join(3)
        assert not threads[1].is_alive() and results[1].outcome == "완료"
        assert backend.nodes[c.fs, "second"].data == b"x" * 20
        assert not release.is_set() and (b.fs, "first") not in backend.nodes
    finally:
        release.set()
        for thread in threads:
            if thread.ident is not None:
                thread.join(5)
    assert not errors and results[0].outcome == "완료"
    assert not engine._active and not any(engine._reserved.values())
    assert backend.nodes[a.fs, "first"].data == backend.nodes[b.fs, "first"].data


def test_direct_repair_can_be_cancelled_while_another_plan_holds_the_planning_lock(
    backend, accounts, monkeypatch
):
    a = accounts[0]
    for name in ("first", "second"):
        backend.put(a.fs, name, b"keep")
    entries = [read_entry(backend, accounts, name) for name in ("first", "second")]
    engine = OperationEngine(backend, accounts)
    release, started, waiting, finished = Event(), Event(), Event(), Event()
    stat = backend.stat

    def delayed_stat(endpoint, context=None):
        if endpoint.account_id == a.id and endpoint.path == "first":
            started.set()
            assert release.wait(5)
        return stat(endpoint, context)

    monkeypatch.setattr(backend, "stat", delayed_stat)
    context = TaskContext(progress=lambda value: waiting.set())
    errors = {}

    def execute(index, task_context):
        try:
            engine.execute_repair(entries[index], task_context, approved=True)
        except Exception as error:
            errors[index] = error
        finally:
            if index == 1:
                finished.set()

    threads = [
        Thread(target=execute, args=(0, TaskContext())),
        Thread(target=execute, args=(1, context)),
    ]
    try:
        threads[0].start()
        assert started.wait(3)
        threads[1].start()
        assert waiting.wait(3)
        context.cancelled.set()
        assert finished.wait(2) and not release.is_set()
        assert isinstance(errors[1], BackendError) and errors[1].kind == ErrorKind.CANCELLED
    finally:
        release.set()
        for thread in threads:
            if thread.ident is not None:
                thread.join(5)
    assert 0 not in errors and not engine._active
    assert not any(engine._reserved.values())


def test_direct_repair_keeps_execution_validation_for_the_newly_built_plan(
    backend, accounts, monkeypatch
):
    a, b = accounts[:2]
    backend.put(a.fs, "file", b"keep")
    entry = read_entry(backend, [a, b], "file")
    engine = OperationEngine(backend, [a, b])
    plan = engine.plan_repair(entry)
    engine.accounts = [a, replace(b, readonly=True)]
    monkeypatch.setattr(engine, "plan_repair", lambda *args: plan)
    before = dict(backend.nodes)
    with pytest.raises(ValueError, match="읽기 전용"):
        engine.execute_repair(entry, TaskContext(), approved=True)
    assert backend.nodes == before and not engine._active and not engine._reserved
