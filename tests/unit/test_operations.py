from dataclasses import replace
from threading import Barrier

import pytest

from cloud_cluster.domain import BackendError, CopyRejected, Endpoint, ErrorKind
from cloud_cluster.operations import OperationEngine, local_endpoint
from cloud_cluster.transfers import TaskContext


@pytest.mark.parametrize("operation", ["replicate", "move", "delete", "mkdir"])
@pytest.mark.parametrize("changed", [{"readonly": True}, {"enabled": False}])
def test_account_restrictions_rechecked_before_any_write(backend, accounts, operation, changed):
    a, b, _ = accounts
    backend.put(a.fs, "file", b"keep")
    backend.put(b.fs, "file", b"keep")
    engine = OperationEngine(backend, accounts)
    source = backend.stat(Endpoint(a.fs, "file", a.id))
    target = b if operation == "replicate" else a
    if operation == "replicate":
        plan = engine.plan_replicate(source, b)
    elif operation == "move":
        plan = engine.plan_move([source], "renamed")
    elif operation == "delete":
        plan = engine.plan_delete([source])
    else:
        plan = engine.plan_mkdir("folder")
    before = dict(backend.nodes)
    backend.calls.clear()
    engine.accounts = [replace(ac, **changed) if ac.id == target.id else ac for ac in accounts]
    with pytest.raises(ValueError):
        engine.execute(plan, TaskContext(), approved=True)
    assert backend.nodes == before and backend.calls == []


def test_copy_requires_approval_and_does_not_repair_automatically(backend, accounts):
    a, b, _ = accounts
    backend.put(a.fs, "x", b"original")
    engine = OperationEngine(backend, accounts)
    replica = backend.stat(Endpoint(a.fs, "x", a.id))
    plan = engine.plan_replicate(replica, b)
    with pytest.raises(PermissionError):
        engine.execute(plan, TaskContext())
    assert (b.fs, "x") not in backend.nodes
    result = engine.execute(plan, TaskContext(), approved=True)
    assert result.outcome == "완료"
    assert backend.nodes[a.fs, "x"].data == backend.nodes[b.fs, "x"].data


def test_replacement_copies_directly_without_backup_or_predeletion(backend, accounts):
    a, b, _ = accounts
    backend.put(a.fs, "x", b"new")
    backend.put(b.fs, "x", b"old")
    engine = OperationEngine(backend, accounts)
    plan = engine.plan_replicate(backend.stat(Endpoint(a.fs, "x", a.id)), b)
    backend.calls.clear()
    result = engine.execute(plan, TaskContext(), approved=True)
    assert result.outcome == "완료"
    assert backend.nodes[b.fs, "x"].data == b"new"
    assert {key for key in backend.nodes} == {(a.fs, "x"), (b.fs, "x")}
    assert backend.calls[: backend.calls.index(("copy", b.fs, "x"))] == [
        ("list", a.fs, ""),
        ("stat", a.fs, "x"),
        ("list", b.fs, ""),
        ("stat", b.fs, "x"),
    ]
    assert [call for call in backend.calls if call[0] in {"copy", "delete"}] == [
        ("copy", b.fs, "x")
    ]
    assert "기존 파일 덮어쓰기" in plan.summary and "보존 후 교체" not in plan.summary


def test_changed_destination_invalidates_approval(backend, accounts):
    a, b, _ = accounts
    backend.put(a.fs, "x", b"a")
    engine = OperationEngine(backend, accounts)
    plan = engine.plan_replicate(backend.stat(Endpoint(a.fs, "x", a.id)), b)
    backend.put(b.fs, "x", b"external")
    result = engine.execute(plan, TaskContext(), approved=True)
    assert result.outcome != "완료"
    assert backend.nodes[b.fs, "x"].data == b"external"


def test_partial_delete_leaves_remaining_file(backend, accounts):
    a, b, _ = accounts
    for account in (a, b):
        backend.put(account.fs, "x", b"data")
    engine = OperationEngine(backend, accounts)
    selected = [backend.stat(Endpoint(ac.fs, "x", ac.id)) for ac in (a, b)]
    backend.failures["delete", b.fs, "x"] = BackendError(ErrorKind.NETWORK)
    context = TaskContext()
    result = engine.execute_delete(selected, context, approved=True)
    assert result.outcome == "일부 성공"
    assert context.detail.startswith("삭제 1/2개 완료 · 중단 · 파일 삭제 · 2/2 · b · /x")
    assert (a.fs, "x") not in backend.nodes
    assert backend.nodes[b.fs, "x"].data == b"data"
    assert not any(call[0] == "copy" for call in backend.calls)


def test_delete_on_provider_outside_previous_allowlist(backend, accounts):
    account = replace(accounts[0], provider="s3")
    backend.put(account.fs, "folder/file", b"data")
    engine = OperationEngine(backend, [account])
    folder = backend.stat(Endpoint(account.fs, "folder", account.id))

    plan = engine.plan_delete([folder])
    with pytest.raises(PermissionError):
        engine.execute(plan, TaskContext())
    assert (account.fs, "folder/file") in backend.nodes

    assert engine.execute(plan, TaskContext(), approved=True).outcome == "완료"
    assert not backend.nodes


def test_bulk_copy_and_delete_share_parent_listings_within_each_phase(backend, accounts):
    account = accounts[0]
    engine = OperationEngine(backend, [account])
    backend.put(account.fs, "dest", None)
    paths = [f"src/file{i}" for i in range(100)]
    for path in paths:
        backend.put(account.fs, path, b"data")
    directories = {}
    for path in paths:
        engine.plan_move(
            (backend.replica(account.fs, path, account.id),),
            path.replace("src/", "dest/"),
            copy_only=True,
            directories=directories,
        )
    assert [path for op, _, path in backend.calls if op == "list"] == ["src", "dest"]
    assert backend.calls.count(("stat", account.fs, paths[-1])) == 1

    backend.calls.clear()
    folder = backend.replica(account.fs, "src", account.id)
    result = engine.execute_delete((folder,), TaskContext(), approved=True)
    assert result.outcome == "완료"
    first_delete = backend.calls.index(("delete", account.fs, paths[0]))
    assert backend.calls[:first_delete].count(("list", account.fs, "src")) == 4
    assert backend.calls[first_delete:].count(("list", account.fs, "src")) == len(paths) - 1
    assert backend.calls.count(("stat", account.fs, paths[-1])) == 3


@pytest.mark.parametrize("failure", [ErrorKind.CHANGED, ErrorKind.NETWORK])
def test_approved_delete_checks_all_selected_versions_before_writing(backend, accounts, failure):
    for account in accounts[:2]:
        backend.put(account.fs, "x", b"original")
    selected = tuple(backend.list_dir(account, "")[0] for account in accounts[:2])
    engine = OperationEngine(backend, accounts)
    with pytest.raises(PermissionError):
        engine.execute_delete(selected, TaskContext())

    account = accounts[1]
    if failure == ErrorKind.CHANGED:
        backend.put(account.fs, "x", b"replaced")
    else:
        backend.failures["list", account.fs, ""] = BackendError(failure)
    with pytest.raises(BackendError) as error:
        engine.execute_delete(selected, TaskContext(), approved=True)
    assert error.value.kind == failure
    assert not any(call[0] in {"delete", "rmdir"} for call in backend.calls)


def test_overlapping_delete_targets_abort_when_a_selected_child_changes(backend, accounts):
    account = accounts[0]
    backend.put(account.fs, "folder/child", b"original")
    folder = backend.list_dir(account, "")[0]
    child = backend.list_dir(account, folder.path)[0]

    def progress(value):
        if value["detail"] == "하위 항목 조회 · a · /folder":
            backend.put(account.fs, child.path, b"replaced")

    with pytest.raises(BackendError) as error:
        OperationEngine(backend, accounts).execute_delete(
            (child, folder), TaskContext(progress=progress), approved=True
        )
    assert error.value.kind == ErrorKind.CHANGED
    assert not any(call[0] in {"delete", "rmdir"} for call in backend.calls)


def test_delete_checks_a_replaced_sibling_folder_before_reading_its_new_children(backend, accounts):
    account = accounts[0]
    backend.put(account.fs, "src/a", None)
    backend.put(account.fs, "src/b", None)
    selected = backend.replica(account.fs, "src", account.id)
    replacement = "src/b/new.txt"

    def progress(value):
        if value["detail"] == "삭제 대상 확인 · a · /src/b":
            backend.put(account.fs, "src/b", None)
            backend.put(account.fs, replacement, b"new data")

    with pytest.raises(BackendError) as error:
        OperationEngine(backend, accounts).execute_delete(
            (selected,), TaskContext(progress=progress), approved=True
        )
    assert error.value.kind == ErrorKind.CHANGED
    assert backend.nodes[account.fs, replacement].data == b"new data"
    assert not any(
        op in {"delete", "rmdir"} or (op == "list" and path == "src/b")
        for op, _, path in backend.calls
    )


def test_approved_replica_directory_uses_listing_metadata_for_confirmation(
    backend, accounts, monkeypatch
):
    account = accounts[0]
    backend.put(account.fs, "folder/child", b"data")
    selected = backend.list_dir(account, "")[0]
    original_stat = backend.stat

    def stat(endpoint, context=None):
        item = original_stat(endpoint, context)
        return replace(item, version="synthetic") if item and item.is_dir else item

    monkeypatch.setattr(backend, "stat", stat)
    result = OperationEngine(backend, accounts).execute_delete(
        (selected,), TaskContext(), approved=True
    )
    assert result.outcome == "완료"
    assert not backend.nodes


def test_partial_move_keeps_originals(backend, accounts):
    a, b, _ = accounts
    for account in (a, b):
        backend.put(account.fs, "old/x", b"data")
    engine = OperationEngine(backend, accounts)
    plan = engine.plan_move([backend.stat(Endpoint(ac.fs, "old", ac.id)) for ac in (a, b)], "new")
    backend.failures["copy", b.fs, "new/x"] = BackendError(ErrorKind.NETWORK)
    result = engine.execute(plan, TaskContext(), approved=True)
    assert result.outcome == "일부 성공"
    assert backend.nodes[a.fs, "old/x"].data == backend.nodes[b.fs, "old/x"].data
    assert backend.nodes[a.fs, "new/x"].data == b"data"


def test_cancel_does_not_remove_existing_data(backend, accounts):
    a, b, _ = accounts
    backend.put(a.fs, "x", b"data")
    engine = OperationEngine(backend, accounts)
    plan = engine.plan_replicate(backend.stat(Endpoint(a.fs, "x", a.id)), b)
    context = TaskContext()
    context.cancelled.set()
    result = engine.execute(plan, context, approved=True)
    assert result.outcome == "취소됨"
    assert backend.nodes[a.fs, "x"].data == b"data"


@pytest.mark.parametrize("existing", [False, True])
def test_copy_response_failure_is_not_blindly_retried(backend, accounts, existing):
    a, b, _ = accounts
    backend.put(a.fs, "x", b"data")
    if existing:
        backend.put(b.fs, "x", b"old")
    engine = OperationEngine(backend, accounts)
    plan = engine.plan_replicate(backend.stat(Endpoint(a.fs, "x", a.id)), b)
    original_copy = backend.copy_file

    def lost_response(source, target, context=None):
        original_copy(source, target, context)
        raise BackendError(ErrorKind.NETWORK)

    backend.copy_file = lost_response
    result = engine.execute(plan, TaskContext(), approved=True)
    assert result.outcome != "완료"
    assert len([c for c in backend.calls if c[0] == "copy"]) == 1
    assert backend.nodes[b.fs, "x"].data == b"data"
    assert not any(call[0] == "delete" for call in backend.calls)
    assert "현재 대상 상태를 다시 확인" in result.steps[-1].message


@pytest.mark.parametrize("failure", [ErrorKind.NETWORK, ErrorKind.CANCELLED])
def test_failed_transfer_never_predeletes_previous_destination(backend, accounts, failure):
    a, b, _ = accounts
    backend.put(a.fs, "x", b"new")
    backend.put(b.fs, "x", b"old")
    engine = OperationEngine(backend, accounts)
    plan = engine.plan_replicate(backend.stat(Endpoint(a.fs, "x", a.id)), b)
    backend.failures["copy", b.fs, "x"] = BackendError(failure)
    result = engine.execute(plan, TaskContext(), approved=True)
    assert result.outcome != "완료"
    assert backend.nodes[b.fs, "x"].data == b"old"
    assert not any(call[0] == "delete" for call in backend.calls)


@pytest.mark.parametrize("change", ["source", "target"])
def test_each_transfer_rechecks_changes_while_previous_file_is_copying(
    backend, accounts, monkeypatch, change
):
    a, b, _ = accounts
    for name in ("first", "second"):
        backend.put(a.fs, "old/" + name, b"keep")
    engine = OperationEngine(backend, accounts)
    plan = engine.plan_move([backend.stat(Endpoint(a.fs, "old", a.id))], "new")
    original_copy = backend.copy_file

    def copy(source, target, context=None):
        original_copy(source, target, context)
        path = "old/second" if change == "source" else "new/second"
        backend.put(a.fs, path, b"external")

    monkeypatch.setattr(backend, "copy_file", copy)
    result = engine.execute(plan, TaskContext(), approved=True)
    assert result.outcome == "일부 성공"
    assert backend.nodes[a.fs, "old/first"].data == b"keep"
    assert backend.nodes[a.fs, "old/second"].data == (b"external" if change == "source" else b"keep")
    assert not any(call == ("copy", a.fs, "new/second") for call in backend.calls)
    if change == "target":
        assert backend.nodes[a.fs, "new/second"].data == b"external"


def test_failed_verification_keeps_move_source_and_copied_destination(backend, accounts, monkeypatch):
    a = accounts[0]
    backend.put(a.fs, "old", b"keep")
    engine = OperationEngine(backend, accounts)
    plan = engine.plan_move([backend.stat(Endpoint(a.fs, "old", a.id))], "new")
    copy = backend.copy_file

    def corrupt(source, target, context=None):
        copy(source, target, context)
        backend.put(target.fs, target.path, b"oops")

    monkeypatch.setattr(backend, "copy_file", corrupt)
    result = engine.execute(plan, TaskContext(), approved=True)
    assert result.outcome == "일부 성공" and result.steps[0].action == "전송"
    assert backend.nodes[a.fs, "old"].data == b"keep"
    assert backend.nodes[a.fs, "new"].data == b"oops"
    assert not any(call[0] == "delete" for call in backend.calls)


@pytest.mark.parametrize("changed", ["source", "target"])
def test_move_rechecks_both_files_after_reading_sha256(backend, accounts, monkeypatch, changed):
    account = accounts[0]
    backend.no_hashes.add(account.fs)
    backend.put(account.fs, "old", b"keep")
    engine = OperationEngine(backend, accounts)
    plan = engine.plan_move([backend.stat(Endpoint(account.fs, "old", account.id))], "new")
    hash_file = backend.hash_file

    def hash_then_change(endpoint, context=None):
        digest = hash_file(endpoint, context)
        if endpoint.path == "new":
            backend.put(account.fs, "old" if changed == "source" else "new", b"keep")
        return digest

    monkeypatch.setattr(backend, "hash_file", hash_then_change)
    result = engine.execute(plan, TaskContext(), approved=True)

    assert result.outcome == "일부 성공"
    assert {path for fs, path in backend.nodes if fs == account.fs} == {"old", "new"}
    assert not any(call[0] == "delete" for call in backend.calls)


@pytest.mark.parametrize("existing", [False, True])
def test_upload_plans_and_reserves_one_file_size_per_target(backend, accounts, tmp_path, existing):
    from cloud_cluster.domain import BackendCapabilities, QuotaSnapshot

    source = tmp_path / "source.txt"
    source.write_bytes(b"data")
    endpoint = local_endpoint(source)
    backend.put(endpoint.fs, endpoint.path, b"data")
    accounts = accounts[:2]
    for account in accounts:
        if existing:
            backend.put(account.fs, "file.txt", b"previous version")
        backend.quotas[account.id] = QuotaSnapshot(100, 4)
        backend.caps[account.id] = BackendCapabilities(quota=True, max_file_size=4)
    engine = OperationEngine(backend, accounts)
    plan = engine.plan_upload(source, "file.txt")
    for account in accounts:
        assert backend.calls.count(("stat", account.fs, "file.txt")) == 1
    with engine._reserve(plan, TaskContext()):
        assert engine._reserved == {account.id: 4 for account in accounts}
    backend.calls.clear()
    assert engine.execute(plan, TaskContext(), approved=True).outcome == "완료"
    assert not any(engine._reserved.values())
    first_target = plan.copies[0].target
    first_write = backend.calls.index(("copy", first_target.fs, first_target.path))
    source_stat = ("stat", endpoint.fs, endpoint.path)
    assert backend.calls[:first_write].count(source_stat) == 1
    assert backend.calls.count(source_stat) == 4
    for copy in plan.copies:
        assert backend.calls[:first_write].count(("stat", copy.target.fs, copy.target.path)) == 1


@pytest.mark.parametrize(
    ("failure", "expected", "outcome"),
    [
        ("rejected", ["a", "c", "b"], "완료"),
        ("both_rejected", ["a", "c", "b", "d"], "완료"),
        ("alternate_full", ["a", "d", "b"], "완료"),
        ("overwrite", ["a"], "실패"),
        ("changed_alternate", ["a"], "실패 / 결과 확인 필요"),
        ("lost_response", ["a", "b"], "완료"),
        ("unconfirmed", ["a"], "실패 / 결과 확인 필요"),
    ],
)
def test_upload_switches_only_after_confirmed_rejection(
    backend, accounts, tmp_path, monkeypatch, failure, expected, outcome
):
    a, b, c = accounts
    accounts[0] = a = replace(a, provider="box")
    if failure == "both_rejected":
        accounts[1] = b = replace(b, provider="box")
    if failure in {"both_rejected", "alternate_full"}:
        accounts.append(replace(c, id="d", remote="d"))
    source = tmp_path / "source.txt"
    source.write_bytes(b"data")
    endpoint = local_endpoint(source)
    backend.put(endpoint.fs, endpoint.path, b"data")
    if failure == "overwrite":
        backend.put(a.fs, "file.txt", b"old!")
    engine = OperationEngine(backend, accounts)
    plan = engine.plan_upload(source, "file.txt")
    alternate = plan.alternates[0]
    assert alternate.target in plan.endpoints
    assert f"대체 후보 · {c.id}:/file.txt" in plan.summary
    if failure == "changed_alternate":
        backend.put(c.fs, "file.txt", b"external")
    if failure == "alternate_full":
        engine._reserved[c.id] = 90_000
    if failure == "lost_response":
        backend.no_hashes.add(a.fs)
    copy_file = backend.copy_file
    attempts, details = [], []

    def transfer(source, target, context):
        attempts.append(target.account_id)
        if target.account_id == a.id or (failure == "both_rejected" and target.account_id == b.id):
            if failure not in {"lost_response", "unconfirmed"}:
                assert context.retries == (2 if failure == "overwrite" else 0)
                raise CopyRejected(ErrorKind.FILE_SIZE)
            if failure == "lost_response":
                copy_file(source, target, context)
            raise BackendError(ErrorKind.NETWORK)
        copy_file(source, target, context)

    monkeypatch.setattr(backend, "copy_file", transfer)
    result = engine.execute(
        plan, TaskContext(progress=lambda value: details.append(value["detail"])), approved=True
    )

    assert result.outcome == outcome
    assert attempts == expected
    assert {key: size for key, size in engine._reserved.items() if size} == (
        {c.id: 90_000} if failure == "alternate_full" else {}
    )
    assert not any(call[0] == "delete" for call in backend.calls)
    if failure == "rejected":
        assert any("대체 업로드" in detail and "파일 크기 제한 초과" in detail for detail in details)
        assert [(step.account_id, step.state) for step in result.steps if step.action == "전송 거부"] == [
            ("a", "skipped")
        ]
        assert (a.fs, "file.txt") not in backend.nodes
        assert backend.nodes[b.fs, "file.txt"].data == backend.nodes[c.fs, "file.txt"].data == b"data"
    elif failure == "overwrite":
        assert backend.nodes[a.fs, "file.txt"].data == b"old!"
        assert "파일 크기 제한 초과" in result.steps[-1].message
    elif failure == "changed_alternate":
        assert backend.nodes[c.fs, "file.txt"].data == b"external"
    elif failure == "alternate_full":
        assert any(step.account_id == c.id and step.message == "용량 부족" for step in result.steps)
        assert (c.fs, "file.txt") not in backend.nodes
    elif failure == "both_rejected":
        assert [step.account_id for step in result.steps if step.action == "대체 업로드"] == ["c", "d"]
    else:
        assert any("전송 결과 확인 · 연결 오류" in detail for detail in details)
        if failure == "lost_response":
            assert any(step.action == "전송 결과 확인" and step.state == "ok" for step in result.steps)
            assert ("hash", a.fs, "file.txt") in backend.calls
        else:
            assert "연결 오류" in result.steps[-1].message


@pytest.mark.parametrize(
    ("full", "scenario", "attempts", "outcome"),
    [
        ("b", "new", ["a", "c"], "완료"),
        ("ab", "new", ["c", "d"], "완료"),
        ("bc", "new", ["a", "d"], "완료"),
        ("abd", "exhausted", [], ErrorKind.QUOTA),
        ("b", "overwrite", [], ErrorKind.QUOTA),
        ("b", "changed_primary", [], "실패 / 결과 확인 필요"),
        ("b", "changed_alternate", [], "실패 / 결과 확인 필요"),
        ("a", "rejected_alternate", ["c", "d", "b"], "완료"),
    ],
)
def test_upload_switches_when_primary_space_is_unavailable(
    backend, accounts, tmp_path, monkeypatch, full, scenario, attempts, outcome
):
    a, b, c = accounts
    accounts[2] = c = replace(c, provider="box")
    d = replace(c, id="d", remote="d")
    accounts.append(d)
    source = tmp_path / "source.txt"
    source.write_bytes(b"data")
    endpoint = local_endpoint(source)
    backend.put(endpoint.fs, endpoint.path, b"data")
    if scenario == "overwrite":
        backend.put(b.fs, "file.txt", b"old")
    engine = OperationEngine(backend, accounts)
    plan = engine.plan_upload(source, "file.txt")
    for account in accounts:
        if account.id in full:
            engine.quotas.consume(account, 90_000)
    if scenario in {"changed_primary", "changed_alternate"}:
        target = b if scenario == "changed_primary" else c
        backend.put(target.fs, "file.txt", b"external")

    copy_file, seen = backend.copy_file, []
    before = dict(backend.nodes)

    def transfer(source, target, context):
        if not seen:
            assert {key for key, size in engine._reserved.items() if size} == (
                {"b", "c"} if scenario == "rejected_alternate" else set(attempts)
            )
        seen.append(target.account_id)
        if scenario == "rejected_alternate" and target.account_id == c.id:
            assert context.retries == 0
            raise CopyRejected(ErrorKind.FILE_SIZE)
        copy_file(source, target, context)

    monkeypatch.setattr(backend, "copy_file", transfer)
    if outcome == ErrorKind.QUOTA:
        with pytest.raises(BackendError) as failure:
            engine.execute(plan, TaskContext(), approved=True)
        assert failure.value.kind == outcome
    else:
        result = engine.execute(plan, TaskContext(), approved=True)
        assert result.outcome == outcome
        if outcome == "완료":
            assert any(step.action == "공간 예약" and step.state == "skipped" for step in result.steps)
            assert any(step.action == "대체 업로드" and step.state == "ok" for step in result.steps)
    assert seen == attempts
    assert not any(engine._reserved.values())
    if attempts:
        saved = [account.id for account in accounts if (account.fs, "file.txt") in backend.nodes]
        assert saved == sorted(attempts[-2:])
    else:
        assert backend.nodes == before


def test_upload_preparation_overlaps_account_reads_and_reuses_successful_lookups(
    backend, accounts, tmp_path, monkeypatch
):
    sources = [tmp_path / name for name in ("first.txt", "second.txt")]
    for source in sources:
        source.write_bytes(b"data")
        backend.put(tmp_path.as_posix(), source.name, b"data")
    existing = accounts[-1]
    backend.put(existing.fs, sources[0].name, b"old")
    concurrent_reads = Barrier(len(accounts), timeout=5)
    list_dir, features, about = backend.list_dir, backend.features, backend.about
    feature_calls, quota_calls, details = [], [], []

    def read_directory(account, path, context=None, *, show_hash=True):
        concurrent_reads.wait()
        return list_dir(account, path, context, show_hash=show_hash)

    def read_features(account, context=None):
        feature_calls.append(account.id)
        return features(account, context)

    def read_quota(account, context=None):
        quota_calls.append(account.id)
        return about(account, context)

    monkeypatch.setattr(backend, "list_dir", read_directory)
    monkeypatch.setattr(backend, "features", read_features)
    monkeypatch.setattr(backend, "about", read_quota)
    engine = OperationEngine(backend, accounts)
    context = TaskContext(progress=lambda update: details.append(update["detail"]))

    plan = engine.plan_uploads(sources, "", context)

    account_ids = sorted(account.id for account in accounts)
    assert sorted(feature_calls) == sorted(quota_calls) == account_ids
    for account in accounts:
        assert backend.calls.count(("list", account.fs, "")) == 1
        for source in sources:
            assert backend.calls.count(("stat", account.fs, source.name)) == 1
    assert plan.copies[0].target.account_id == existing.id
    assert plan.copies[0].previous is not None
    assert any("응답 대기: a, b, c" in detail for detail in details)
    assert any("항목 2/2 · second.txt" in detail for detail in details)


def test_dual_upload_checks_both_destinations_before_first_copy(backend, accounts, tmp_path):
    source = tmp_path / "source.txt"
    source.write_bytes(b"original")
    endpoint = local_endpoint(source)
    backend.put(endpoint.fs, endpoint.path, b"original")
    engine = OperationEngine(backend, accounts)
    plan = engine.plan_upload(source, "file.txt")
    first, second = (copy.target for copy in plan.copies)
    backend.put(second.fs, second.path, b"external")

    result = engine.execute(plan, TaskContext(), approved=True)

    assert result.outcome == "실패 / 결과 확인 필요"
    assert (first.fs, first.path) not in backend.nodes
    assert backend.nodes[second.fs, second.path].data == b"external"


def test_upload_rejects_multiple_sources_for_same_logical_target(backend, accounts, tmp_path):
    sources = [tmp_path / "first" / "file.txt", tmp_path / "second" / "FILE.txt"]
    for source in sources:
        source.parent.mkdir()
        source.write_bytes(b"data")
        backend.put(source.parent.as_posix(), source.name, b"data")

    engine = OperationEngine(backend, accounts)
    with pytest.raises(ValueError, match="업로드할 대상 경로가 중복됩니다"):
        engine.plan_uploads(sources, "", TaskContext())
    assert not any(call[0] in {"copy", "mkdir"} for call in backend.calls)


@pytest.mark.parametrize("changed", [False, True])
def test_multi_file_upload_rechecks_each_source_once_before_copying(
    backend, accounts, tmp_path, changed
):
    sources = [tmp_path / name for name in ("first.txt", "second.txt")]
    for source in sources:
        source.write_bytes(b"original")
        backend.put(tmp_path.as_posix(), source.name, b"original")
    engine = OperationEngine(backend, accounts)
    plan = engine.plan_uploads(sources, "", TaskContext())
    if changed:
        backend.put(tmp_path.as_posix(), sources[1].name, b"changed")
    backend.calls.clear()

    result = engine.execute(plan, TaskContext(), approved=True)

    if changed:
        assert result.outcome == "실패 / 결과 확인 필요"
        assert backend.calls == [("stat", tmp_path.as_posix(), sources[1].name)]
    else:
        assert result.outcome == "완료"
        first, second = (copy.target for copy in plan.copies[:2])
        first_write = backend.calls.index(("copy", first.fs, first.path))
        reads = backend.calls[:first_write]
        assert [call for call in reads if call[1] == tmp_path.as_posix()] == [
            ("stat", tmp_path.as_posix(), source.name) for source in reversed(sources)
        ]
        assert reads.count(("list", second.fs, "")) == 1


def test_dual_upload_partial_transfer_keeps_success_and_does_not_rebalance(
    backend, accounts, tmp_path
):
    from cloud_cluster.operations import local_endpoint

    source = tmp_path / "source.txt"
    source.write_bytes(b"original")
    endpoint = local_endpoint(source)
    backend.put(endpoint.fs, endpoint.path, b"original")
    engine = OperationEngine(backend, accounts)
    plan = engine.plan_upload(source, "file.txt")
    a, b, c = accounts
    backend.failures["copy", b.fs, "file.txt"] = BackendError(ErrorKind.NETWORK)
    result = engine.execute(plan, TaskContext(), approved=True)
    assert result.outcome == "일부 성공"
    assert backend.nodes[a.fs, "file.txt"].data == b"original"
    assert (c.fs, "file.txt") not in backend.nodes
    assert source.read_bytes() == b"original"


def test_new_folder_attempts_every_active_account(backend, accounts):
    engine = OperationEngine(backend, accounts)
    plan = engine.plan_mkdir("new")
    backend.failures["mkdir", accounts[0].fs, "new"] = BackendError(ErrorKind.PERMISSION)
    result = engine.execute(plan, TaskContext(), approved=True)
    assert result.outcome == "일부 성공"
    assert len(result.steps) == 3
    assert (accounts[1].fs, "new") in backend.nodes and (accounts[2].fs, "new") in backend.nodes
