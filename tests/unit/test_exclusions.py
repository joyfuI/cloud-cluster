from dataclasses import replace

import pytest

from cloud_cluster.domain import BackendError, CloudAccount, Endpoint, ErrorKind, Replica, Status
from cloud_cluster.operations import (
    CopyIntent,
    ExpectedItem,
    MutationPlan,
    OperationEngine,
    RenameIntent,
    local_endpoint,
)
from cloud_cluster.replicas import is_excluded
from cloud_cluster.transfers import TaskContext
from cloud_cluster.vfs import VirtualFileSystem


@pytest.fixture
def provider_accounts():
    return [
        CloudAccount("p", "pikpak", "pikpak"),
        CloudAccount("a", "a", "local"),
        CloudAccount("b", "b", "local"),
    ]


@pytest.mark.parametrize(
    "provider, root, path, expected",
    [
        ("pikpak", "", "My Pack", True),
        ("pikpak", "", "My Pack/sub/file.txt", True),
        ("pikpak", "", "MY PACK/file.txt", True),
        ("pikpak", "My Pack", "file.txt", True),
        ("pikpak", "My Pack/sub", "", True),
        ("pikpak", "", "", False),
        ("pikpak", "", "My Pack 2/file.txt", False),
        ("pikpak", "", "My Package/file.txt", False),
        ("pikpak", "", "other/My Pack/file.txt", False),
        ("pikpak", "other", "My Pack/file.txt", False),
        ("drive", "", "My Pack/file.txt", False),
        ("onedrive", "", "My Pack", False),
        ("local", "", "My Pack/file.txt", False),
        ("alias", "", "My Pack/file.txt", False),
    ],
)
def test_exclusions_match_provider_root_and_whole_path_components(provider, root, path, expected):
    assert is_excluded(CloudAccount("a", "remote", provider, root), path) is expected


def test_browsing_and_scan_preserve_other_providers_same_name(backend, provider_accounts):
    p, a, b = provider_accounts
    backend.put(p.fs, "My Pack/secret.txt", b"excluded")
    for account in (a, b):
        backend.put(account.fs, "My Pack/shared.txt", b"same")
        backend.put(account.fs, "normal.txt", b"same")
    before = dict(backend.nodes)
    vfs = VirtualFileSystem(backend, provider_accounts)
    try:
        root = vfs.refresh("")
        folder = next(entry for entry in root.entries if entry.path == "My Pack")
        assert folder.assessment.status == Status.HEALTHY
        assert {r.account_id for r in folder.replicas} == {a.id, b.id}
        child = vfs.refresh("My Pack")
        assert child.complete
        assert {listing.account_id for listing in child.listings} == {a.id, b.id}
        assert [entry.path for entry in child.entries] == ["My Pack/shared.txt"]
        issues, complete, count = vfs.scan("")
        assert issues == () and complete and count == 3
        assert not any(fs == p.fs and path for _, fs, path in backend.calls)
        assert backend.nodes == before
    finally:
        vfs.close()


def test_only_excluded_tree_disappears_and_direct_navigation_does_not_query_it(
    backend, provider_accounts
):
    p = provider_accounts[0]
    backend.put(p.fs, "My Pack/sub/secret.txt", b"keep")
    vfs = VirtualFileSystem(backend, [p])
    try:
        assert vfs.refresh("").entries == ()
        vfs.record_verification(())
        assert vfs.cached("").entries == ()
        issues, complete, count = vfs.scan("")
        assert issues == () and complete and count == 0
        backend.calls.clear()
        assert vfs.refresh("My Pack").entries == ()
        assert vfs.scan("My Pack/sub") == ((), True, 0)
        assert backend.calls == []
        assert backend.nodes[p.fs, "My Pack/sub/secret.txt"].data == b"keep"
    finally:
        vfs.close()


def test_excluded_account_failure_is_ignored_only_for_matching_paths(backend, provider_accounts):
    p, a, b = provider_accounts
    for account in (a, b):
        backend.put(account.fs, "My Pack/shared.txt", b"same")
        backend.put(account.fs, "normal.txt", b"same")
    backend.failures["list", p.fs, ""] = BackendError(ErrorKind.NETWORK)
    vfs = VirtualFileSystem(backend, provider_accounts)
    try:
        snapshot = vfs.refresh("")
        assert not snapshot.complete
        states = {entry.path: entry.assessment.status for entry in snapshot.entries}
        assert states == {"My Pack": Status.HEALTHY, "normal.txt": Status.UNKNOWN}
    finally:
        vfs.close()


def test_upload_and_mkdir_skip_excluded_provider(backend, provider_accounts, tmp_path):
    p, a, b = provider_accounts
    backend.put(p.fs, "My Pack/secret.txt", b"keep")
    source = tmp_path / "upload.txt"
    source.write_bytes(b"data")
    local = local_endpoint(source)
    backend.put(local.fs, local.path, b"data")
    engine = OperationEngine(backend, provider_accounts)
    plan = engine.plan_upload(source, "My Pack/upload.txt")
    assert {copy.target.account_id for copy in plan.copies} == {a.id, b.id}
    assert engine.execute(plan, TaskContext(), approved=True).outcome == "완료"
    plan = engine.plan_mkdir("My Pack/new")
    assert {endpoint.account_id for endpoint in plan.mkdirs} == {a.id, b.id}
    assert engine.execute(plan, TaskContext(), approved=True).outcome == "완료"
    assert backend.nodes[p.fs, "My Pack/secret.txt"].data == b"keep"
    assert not any(fs == p.fs for _, fs, _ in backend.calls)
    for account in (a, b):
        assert backend.nodes[account.fs, "My Pack/upload.txt"].data == b"data"
        assert backend.nodes[account.fs, "My Pack/new"].data is None
    with pytest.raises(ValueError):
        OperationEngine(backend, [p, a]).plan_upload(source, "My Pack/other.txt")


@pytest.mark.parametrize("operation", ["download", "replicate", "move", "copy", "delete"])
def test_excluded_sources_cannot_be_used_by_operations(
    backend, provider_accounts, tmp_path, operation
):
    p, a, _ = provider_accounts
    backend.put(p.fs, "My Pack/secret.txt", b"keep")
    source = Replica(p.id, "My Pack/secret.txt", size=4)
    engine = OperationEngine(backend, provider_accounts)
    with pytest.raises(ValueError):
        if operation == "download":
            engine.plan_download(source, tmp_path / "out")
        elif operation == "replicate":
            engine.plan_replicate(source, a)
        elif operation in {"move", "copy"}:
            engine.plan_move([source], "normal.txt", copy_only=operation == "copy")
        else:
            engine.plan_delete([source])
    assert backend.calls == []
    assert backend.nodes[p.fs, "My Pack/secret.txt"].data == b"keep"


def test_explicit_replication_and_rename_targets_are_rejected(backend, provider_accounts):
    p, a, _ = provider_accounts
    backend.put(a.fs, "My Pack/shared.txt", b"same")
    backend.put(p.fs, "normal.txt", b"keep")
    engine = OperationEngine(backend, provider_accounts)
    with pytest.raises(ValueError, match="제외"):
        engine.plan_replicate(Replica(a.id, "My Pack/shared.txt", size=4), p)
    backend.calls.clear()
    with pytest.raises(ValueError, match="제외"):
        engine.plan_move([Replica(p.id, "normal.txt", size=4)], "My Pack/new.txt")
    assert not any(path.startswith("My Pack") for _, _, path in backend.calls)
    assert backend.nodes[p.fs, "normal.txt"].data == b"keep"


@pytest.mark.parametrize(
    "operation", ["copy_source", "copy_target", "mkdir", "delete", "rmdir", "final_name"]
)
def test_execution_rechecks_exclusions_before_any_io(backend, provider_accounts, operation):
    p, a, _ = provider_accounts
    allowed = Endpoint(a.fs, "normal.txt", a.id)
    excluded = Endpoint(p.fs, "My Pack/secret.txt", p.id)
    source = ExpectedItem(allowed, Replica(a.id, allowed.path, size=4))
    hidden = ExpectedItem(excluded, Replica(p.id, excluded.path, size=4))
    if operation == "copy_source":
        plan = MutationPlan("copy", copies=(CopyIntent(hidden, allowed, None),))
    elif operation == "copy_target":
        plan = MutationPlan("copy", copies=(CopyIntent(source, excluded, None),))
    elif operation == "mkdir":
        plan = MutationPlan("mkdir", mkdirs=(excluded,))
    elif operation == "delete":
        plan = MutationPlan("delete", deletions=(hidden,))
    elif operation == "rmdir":
        plan = MutationPlan("rmdir", rmdirs=(hidden,))
    else:
        plan = MutationPlan("rename", renames=(RenameIntent(source, excluded),))
    with pytest.raises(ValueError, match="제외"):
        OperationEngine(backend, provider_accounts).execute(plan, TaskContext(), approved=True)
    assert backend.calls == [] and backend.nodes == {}


def test_provider_change_after_planning_cannot_bypass_exclusion(backend, provider_accounts):
    p, a, b = provider_accounts
    normal = replace(p, provider="local")
    backend.put(a.fs, "My Pack/shared.txt", b"same")
    engine = OperationEngine(backend, [normal, a, b])
    plan = engine.plan_replicate(Replica(a.id, "My Pack/shared.txt", size=4), normal)
    engine.accounts = provider_accounts
    backend.calls.clear()
    with pytest.raises(ValueError, match="제외"):
        engine.execute(plan, TaskContext(), approved=True)
    assert backend.calls == []
