from dataclasses import replace
from threading import Event

import pytest

from cloud_cluster.domain import BackendError, ErrorKind, Status
from cloud_cluster.transfers import TaskContext
from cloud_cluster.vfs import VirtualFileSystem


def test_task_invalidation_only_stales_related_paths_and_fences_an_active_read(backend, accounts):
    account = accounts[0]
    backend.put(account.fs, "parent/changed/child/file", b"old")
    backend.put(account.fs, "parent/sibling/file", b"keep")
    vfs = VirtualFileSystem(backend, [account])
    try:
        related = ("", "parent", "parent/changed", "parent/changed/child")
        sibling = "parent/sibling"
        for path in (*related, sibling):
            vfs.refresh(path)
        previous = vfs.cached("parent/changed/child")
        backend.put(account.fs, "parent/changed/child/file", b"longer")

        def invalidate(_):
            assert not vfs.mark_changed(("parent/changed",), current=sibling)

        old_read = vfs.refresh("parent/changed/child", on_partial=invalidate)
        assert old_read.entries != previous.entries
        assert all(vfs.cached(path).stale for path in related)
        assert vfs.cached("parent/changed/child").entries == previous.entries
        assert not vfs.cached(sibling).stale
        vfs.refresh("parent/changed/child")
        assert not vfs.cached("parent/changed/child").stale
    finally:
        vfs.close()


@pytest.mark.parametrize(
    ("account_changes", "stale"),
    [
        ({"provider": "alias"}, True),
        ({"provider": "alias", "enabled": False}, False),
        ({"root": "root/parent"}, True),
    ],
)
def test_task_invalidation_covers_alternative_remote_roots(
    backend, accounts, account_changes, stale
):
    a, b, _ = accounts
    b = replace(b, **account_changes)
    backend.put(a.fs, "unrelated/file", b"data")
    vfs = VirtualFileSystem(backend, [a, b])
    try:
        vfs.refresh("unrelated")
        assert vfs.mark_changed(("parent/changed",), current="unrelated") == stale
        assert vfs.cached("unrelated").stale == stale
    finally:
        vfs.close()


def test_recheck_reports_the_folder_and_accounts_still_waiting(backend, accounts):
    backend.put(accounts[0].fs, "parent/file", b"keep")
    vfs = VirtualFileSystem(backend, accounts)
    release = Event()
    try:
        selected = vfs.refresh("parent").entries
        original_stat = backend.stat

        def stat(endpoint, context=None):
            if endpoint.account_id == accounts[-1].id and endpoint.path == "parent":
                assert release.wait(5)
            return original_stat(endpoint, context)

        backend.stat = stat
        messages = []

        def progress(value):
            messages.append(value["detail"])
            if value["detail"].endswith("응답 대기: c"):
                release.set()

        updated, complete = vfs.recheck(selected, TaskContext(progress=progress))
        assert release.is_set() and complete
        assert updated[selected[0].key].assessment.status == Status.UNDER
        assert messages[0] == "1/1개 폴더 · /parent · 응답 대기: a, b, c"
        assert messages[-1] == "1/1개 폴더 · /parent · 조회 결과 병합 중"
    finally:
        release.set()
        vfs.close()


def test_disabled_remote_is_not_queried_or_used_for_replica_comparison(backend, accounts):
    a, b, c = accounts
    for account in (a, b):
        backend.put(account.fs, "x", b"same")
    backend.put(c.fs, "x", b"different")
    vfs = VirtualFileSystem(backend, [a, b, replace(c, enabled=False)])
    try:
        snapshot = vfs.refresh("")
        assert snapshot.complete
        assert snapshot.entries[0].assessment.status == Status.HEALTHY
        assert {item.account_id for item in snapshot.listings} == {a.id, b.id}
        assert all(fs != c.fs for _, fs, _ in backend.calls)
        issues, complete, count = vfs.scan("")
        assert issues == () and complete and count == 1
    finally:
        vfs.close()


def test_cache_refresh_and_failed_listing_preserves_uncertainty(backend, accounts):
    a, b, _ = accounts
    backend.put(a.fs, "x", b"same")
    backend.put(b.fs, "x", b"same")
    vfs = VirtualFileSystem(backend, [a, b], ttl=0)
    try:
        first = vfs.refresh("")
        assert first.entries[0].assessment.status == Status.HEALTHY
        assert vfs.cached("").stale
        backend.failures["list", b.fs, ""] = BackendError(ErrorKind.NETWORK)
        second = vfs.refresh("")
        assert second.entries[0].assessment.status == Status.UNKNOWN
        assert backend.nodes[b.fs, "x"].data == b"same"
    finally:
        vfs.close()


def test_partial_refresh_keeps_cached_files_for_pending_account(backend, accounts, monkeypatch):
    a, b, _ = accounts
    backend.put(a.fs, "removed", b"old")
    backend.put(b.fs, "keep", b"old")
    vfs = VirtualFileSystem(backend, [a, b], ttl=0)
    release = Event()
    try:
        vfs.refresh("")
        del backend.nodes[a.fs, "removed"]
        backend.put(b.fs, "added", b"new")
        original_list = backend.list_dir

        def list_dir(account, path, context=None):
            if account.id == b.id:
                assert release.wait(5)
            return original_list(account, path, context)

        monkeypatch.setattr(backend, "list_dir", list_dir)
        partials = []

        def on_partial(snapshot):
            partials.append(snapshot)
            release.set()

        final = vfs.refresh("", on_partial=on_partial)
        first, last = partials
        assert not first.complete
        assert [entry.path for entry in first.entries] == ["keep"]
        assert first.entries[0].assessment.status == Status.UNDER
        assert first.entries[0].assessment.count == 1
        assert final.complete and final.entries == last.entries
        assert [entry.path for entry in final.entries] == ["added", "keep"]
    finally:
        release.set()
        vfs.close()


def test_scan_reports_mismatch_without_mutations(backend, accounts):
    a, b, _ = accounts
    backend.put(a.fs, "folder/x", b"same")
    vfs = VirtualFileSystem(backend, [a, b])
    try:
        issues, complete, count = vfs.scan("")
        assert complete and count == 2
        assert {r.assessment.status for r in issues} == {Status.DIRECTORY, Status.UNDER}
        assert not any(call[0] in {"copy", "mkdir", "delete", "rmdir"} for call in backend.calls)
    finally:
        vfs.close()


def test_scan_stays_in_requested_folder_and_descendants(backend, accounts):
    a, b, _ = accounts
    backend.put(a.fs, "folder/x", b"left")
    backend.put(b.fs, "folder/x", b"right")
    backend.put(a.fs, "folder/sub/y", b"same")
    backend.put(a.fs, "outside/z", b"outside")
    backend.put(a.fs, "folder-other/z", b"outside")
    before = dict(backend.nodes)
    progress = []
    vfs = VirtualFileSystem(backend, [a, b])
    try:
        issues, complete, count = vfs.scan(
            "folder", on_progress=lambda *update: progress.append(update)
        )
        assert complete and count == 3
        assert {entry.path for entry in issues} == {"folder/x", "folder/sub", "folder/sub/y"}
        assert next(e for e in issues if e.path == "folder/x").assessment.status == Status.CONFLICT
        assert {path for operation, _, path in backend.calls if operation == "list"} == {
            "folder",
            "folder/sub",
        }
        assert all(operation in {"stat", "list"} for operation, _, _ in backend.calls)
        assert backend.nodes == before
        assert progress[-1] == (3, 3, "folder/sub")
    finally:
        vfs.close()


def test_recheck_groups_selected_parents_without_scanning_subtrees(backend, accounts):
    a, b = accounts[:2]
    for path in ("folder/x", "folder/y", "outside/z"):
        backend.put(a.fs, path, b"same")
    vfs = VirtualFileSystem(backend, (a, b))
    try:
        selected = vfs.refresh("folder").entries
        for path in ("folder/x", "folder/y"):
            backend.put(b.fs, path, b"same")
        before = dict(backend.nodes)
        backend.calls.clear()
        updated, complete = vfs.recheck(selected)
        assert complete and set(updated) == {"folder/x", "folder/y"}
        assert all(entry.assessment.status == Status.HEALTHY for entry in updated.values())
        assert [path for operation, _, path in backend.calls if operation == "list"] == [
            "folder",
            "folder",
        ]
        assert all(operation in {"list", "stat"} for operation, _, _ in backend.calls)
        assert backend.nodes == before
    finally:
        vfs.close()


def test_recheck_keeps_unknown_entries_until_absence_is_confirmed(backend, accounts):
    a, b = accounts[:2]
    backend.put(a.fs, "x", b"same")
    vfs = VirtualFileSystem(backend, (a, b))
    try:
        selected = vfs.refresh("").entries
        del backend.nodes[a.fs, "x"]
        backend.failures["list", b.fs, ""] = BackendError(ErrorKind.NETWORK)
        updated, complete = vfs.recheck(selected)
        assert not complete and updated["x"].assessment.status == Status.UNKNOWN
        backend.failures.clear()
        updated, complete = vfs.recheck(selected)
        assert complete and updated == {"x": None}
        vfs.accounts = []
        updated, complete = vfs.recheck(selected)
        assert not complete and updated["x"].assessment.status == Status.UNKNOWN
    finally:
        vfs.close()


@pytest.mark.parametrize("change", [None, "content", "added", "lookup", "missing"])
def test_fresh_byte_proofs_only_apply_to_same_replicas_in_this_recheck(backend, accounts, change):
    import hashlib

    a, b, c = accounts
    for account in (a, b):
        backend.put(account.fs, "x", b"same")
    backend.no_hashes.update(account.fs for account in accounts)
    vfs = VirtualFileSystem(backend, accounts)
    try:
        entry = vfs.refresh("").entries[0]
        proofs = tuple((r, hashlib.sha256(b"same").hexdigest()) for r in entry.replicas)
        if change == "content":
            backend.put(b.fs, "x", b"diff")
        elif change == "added":
            backend.put(c.fs, "x", b"same")
        elif change == "lookup":
            backend.failures["list", c.fs, ""] = BackendError(ErrorKind.NETWORK)
        elif change == "missing":
            del backend.nodes[b.fs, "x"]
        updated, complete = vfs.recheck((entry,), proofs=proofs)
        assert complete == (change != "lookup")
        assert (
            updated[entry.key].assessment.status
            == {
                None: Status.HEALTHY,
                "content": Status.UNVERIFIED,
                "added": Status.UNVERIFIED,
                "lookup": Status.UNKNOWN,
                "missing": Status.UNDER,
            }[change]
        )
        if change is None:
            assert vfs.cached("").entries[0].assessment.status == Status.HEALTHY
            assert vfs.refresh("").entries[0].assessment.status == Status.UNVERIFIED
    finally:
        vfs.close()
