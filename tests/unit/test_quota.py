from concurrent.futures.thread import ThreadPoolExecutor
from dataclasses import replace
from threading import Event

import pytest

from cloud_cluster import quota as quota_module
from cloud_cluster.accounts import AccountManager
from cloud_cluster.domain import BackendCapabilities, BackendError, ErrorKind, QuotaSnapshot
from cloud_cluster.operations import OperationEngine
from cloud_cluster.quota import QuotaCache
from cloud_cluster.settings import Settings
from cloud_cluster.transfers import TaskContext


def test_hour_cache_local_accounting_expiry_and_forced_refresh(backend, accounts, monkeypatch):
    account = accounts[0]
    now, calls = [10], []
    monkeypatch.setattr(quota_module, "monotonic", lambda: now[0])

    def about(account, context=None):
        calls.append(account.id)
        return QuotaSnapshot(1000, 800)

    monkeypatch.setattr(backend, "about", about)
    cache = QuotaCache(backend)
    assert cache.get(account) == QuotaSnapshot(1000, 800)
    now[0] = 3609
    cache.consume(account, 100)
    assert cache.get(account) == QuotaSnapshot(1000, 700)
    assert calls == [account.id]
    now[0] = 3610
    assert cache.get(account) == QuotaSnapshot(1000, 800)
    now[0] += 1
    assert cache.get(account, force=True) == QuotaSnapshot(1000, 800)
    assert len(calls) == 3
    cache.consume(account, 900)
    assert cache.snapshot((account,))[account.id] == QuotaSnapshot(1000, 0)
    assert cache.snapshot((accounts[1],))[accounts[1].id] == QuotaSnapshot()
    cache.clear()
    assert cache.get(account).free == 800 and len(calls) == 4


@pytest.mark.parametrize("kind", ["unsupported", "missing", "zero", "error", "cancelled"])
def test_cache_preserves_unknown_zero_errors_and_does_not_cache_cancellation(
    backend, accounts, monkeypatch, kind
):
    account, calls = accounts[0], []
    if kind == "unsupported":
        backend.caps[account.id] = BackendCapabilities(quota=False)

    def about(account, context=None):
        calls.append(account.id)
        if kind in {"error", "cancelled"}:
            raise BackendError(ErrorKind.NETWORK if kind == "error" else ErrorKind.CANCELLED)
        return QuotaSnapshot(100, None if kind == "missing" else 0)

    monkeypatch.setattr(backend, "about", about)
    cache = QuotaCache(backend)
    for _ in range(2):
        if kind in {"error", "cancelled"}:
            with pytest.raises(BackendError):
                cache.get(account)
        else:
            value = cache.get(account)
            if kind == "unsupported":
                assert value is None
            else:
                assert value.free == (None if kind == "missing" else 0)
    assert len(calls) == (0 if kind == "unsupported" else 2 if kind == "cancelled" else 1)


def test_concurrent_queries_share_response_and_other_accounts_do_not_wait(
    backend, accounts, monkeypatch
):
    cache, started, release = QuotaCache(backend), Event(), Event()
    calls = []

    def about(account, context=None):
        calls.append(account.id)
        if account == accounts[0]:
            started.set()
            assert release.wait(5)
        return QuotaSnapshot(100, 90)

    monkeypatch.setattr(backend, "about", about)
    context = TaskContext()
    with ThreadPoolExecutor(max_workers=3) as pool:
        first = pool.submit(cache.get, accounts[0])
        try:
            assert started.wait(3)
            waiting = pool.submit(cache.get, accounts[0], context)
            other = pool.submit(cache.get, accounts[1])
            assert other.result(3) == QuotaSnapshot(100, 90)
            context.cancelled.set()
            with pytest.raises(BackendError) as error:
                waiting.result(3)
            assert error.value.kind == ErrorKind.CANCELLED
            shared = pool.submit(cache.get, accounts[0])
        finally:
            release.set()
        assert first.result(3) == shared.result(3) == QuotaSnapshot(100, 90)
    assert sorted(calls) == [accounts[0].id, accounts[1].id]


def test_late_capacity_response_keeps_concurrent_transfer_accounting(
    backend, accounts, monkeypatch
):
    account = accounts[0]
    cache, started, release = QuotaCache(backend), Event(), Event()

    def about(account, context=None):
        started.set()
        assert release.wait(5)
        return QuotaSnapshot(100, 90)

    monkeypatch.setattr(backend, "about", about)
    with ThreadPoolExecutor(max_workers=1) as pool:
        read = pool.submit(cache.get, account, force=True)
        try:
            assert started.wait(3)
            cache.consume(account, 20)
        finally:
            release.set()
        assert read.result(3) == QuotaSnapshot(100, 70)
    assert cache.get(replace(account, enabled=False)) == QuotaSnapshot(100, 70)
    assert cache.snapshot((replace(account, root="different"),))[account.id] == QuotaSnapshot()


def test_account_manager_forces_fresh_capacity_and_shares_it_with_engine(backend, accounts):
    account = accounts[0]
    manager = AccountManager(backend, None, Settings(accounts=accounts))
    engine = OperationEngine(backend, accounts, quotas=manager.quotas)
    backend.quotas[account.id] = QuotaSnapshot(100, 90)
    assert engine.quota(account).free == 90
    backend.quotas[account.id] = QuotaSnapshot(100, 50)
    assert engine.quota(account).free == 90
    assert manager.quota(account).free == engine.quota(account).free == 50


def test_replacement_and_deletion_do_not_credit_unconfirmed_returned_space(backend, accounts):
    from cloud_cluster.domain import Endpoint

    a, b = accounts[:2]
    backend.put(a.fs, "file", b"new")
    backend.put(b.fs, "file", b"previous version")
    backend.quotas[b.id] = QuotaSnapshot(100, 50)
    engine = OperationEngine(backend, accounts)
    assert engine.quota(b).free == 50
    source = backend.stat(Endpoint(a.fs, "file", a.id))
    result = engine.execute(engine.plan_replicate(source, b), TaskContext(), approved=True)
    assert result.outcome == "완료" and engine.quota(b).free == 47
    target = backend.stat(Endpoint(b.fs, "file", b.id))
    result = engine.execute(engine.plan_delete((target,)), TaskContext(), approved=True)
    assert result.outcome == "완료" and engine.quota(b).free == 47


@pytest.mark.parametrize("failure", ["none", "copy", "verification"])
def test_execution_uses_cached_capacity_and_accounts_only_successful_transfers(
    backend, accounts, tmp_path, monkeypatch, failure
):
    from cloud_cluster.operations import local_endpoint

    accounts = accounts[:2]
    source = tmp_path / "file"
    source.write_bytes(b"data")
    endpoint = local_endpoint(source)
    backend.put(endpoint.fs, endpoint.path, b"data")
    for account in accounts:
        backend.quotas[account.id] = QuotaSnapshot(100, 10)
    engine = OperationEngine(backend, accounts)
    plan = engine.plan_upload(source, "file")
    if failure == "copy":
        backend.failures["copy", accounts[1].fs, "file"] = BackendError(ErrorKind.NETWORK)
    elif failure == "verification":

        def fail_verification(*args, **kwargs):
            raise BackendError(ErrorKind.NETWORK)

        monkeypatch.setattr(engine, "_verify", fail_verification)
    observed = []
    copy = backend.copy_file

    def copy_file(source, target, context=None):
        observed.append((engine.quotas.snapshot(accounts), dict(engine._reserved)))
        return copy(source, target, context)

    def unexpected(*args, **kwargs):
        pytest.fail("execution must not query cloud capacity")

    monkeypatch.setattr(backend, "copy_file", copy_file)
    monkeypatch.setattr(backend, "about", unexpected)
    monkeypatch.setattr(backend, "features", unexpected)
    result = engine.execute(plan, TaskContext(), approved=True)
    assert result.outcome == ("완료" if failure == "none" else "일부 성공")
    values = engine.quotas.snapshot(accounts)
    assert values[accounts[0].id].free == 6
    assert values[accounts[1].id].free == (6 if failure == "none" else 10)
    if len(observed) == 2:
        quotas, reserved = observed[1]
        assert quotas[accounts[0].id].free == 6 and reserved[accounts[0].id] == 0
        assert quotas[accounts[1].id].free == 10 and reserved[accounts[1].id] == 4
    assert not any(engine._reserved.values())
