import pytest

from cloud_cluster.domain import BackendError, Endpoint, ErrorKind
from cloud_cluster.operations import OperationEngine
from cloud_cluster.transfers import TaskContext


@pytest.mark.parametrize("path,target", [("file.txt", "FILE.txt"), ("folder/x", "Folder")])
def test_case_only_rename_delegates_each_replica_to_backend(backend, accounts, path, target):
    a, b, _ = accounts
    for account in (a, b):
        backend.put(account.fs, path, b"keep")
    source_path = "folder" if "/" in path else path
    engine = OperationEngine(backend, accounts)
    roots = [backend.stat(Endpoint(ac.fs, source_path, ac.id)) for ac in (a, b)]
    plan = engine.plan_move(roots, target)
    assert set(plan.endpoints) == {
        Endpoint(ac.fs, name, ac.id) for ac in (a, b) for name in (source_path, target)
    }
    result = engine.execute(plan, TaskContext(), approved=True)
    assert result.outcome == "완료"
    new_path = target + "/x" if "/" in path else target
    assert backend.nodes[a.fs, new_path].data == backend.nodes[b.fs, new_path].data == b"keep"
    assert (a.fs, path) not in backend.nodes and (b.fs, path) not in backend.nodes
    writes = {"rename", "copy", "delete", "mkdir", "rmdir"}
    assert [call for call in backend.calls if call[0] in writes] == [
        ("rename", a.fs, target),
        ("rename", b.fs, target),
    ]


def test_case_only_rename_rechecks_conflicts_and_retries_partial_success(backend, accounts):
    a, b, _ = accounts
    for account in (a, b):
        backend.put(account.fs, "file.txt", b"keep")
    engine = OperationEngine(backend, accounts)
    roots = [backend.stat(Endpoint(ac.fs, "file.txt", ac.id)) for ac in (a, b)]
    plan = engine.plan_move(roots, "FILE.txt")

    backend.put(b.fs, "FILE.txt", b"unrelated")
    result = engine.execute(plan, TaskContext(), approved=True)
    assert result.outcome == "실패 / 결과 확인 필요"
    assert not any(op == "rename" for op, _, _ in backend.calls)
    assert backend.nodes[b.fs, "FILE.txt"].data == b"unrelated"
    del backend.nodes[b.fs, "FILE.txt"]

    failure = ("rename", b.fs, "FILE.txt")
    backend.failures[failure] = BackendError(ErrorKind.NETWORK)
    assert engine.execute(plan, TaskContext(), approved=True).outcome == "일부 성공"
    del backend.failures[failure]
    roots[0] = backend.stat(Endpoint(a.fs, "FILE.txt", a.id))
    retry = engine.plan_move(roots, "FILE.txt")
    assert [item.target.account_id for item in retry.renames] == [b.id]
    assert engine.execute(retry, TaskContext(), approved=True).outcome == "완료"
