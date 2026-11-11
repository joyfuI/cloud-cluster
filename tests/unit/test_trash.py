from dataclasses import replace
from unittest.mock import Mock, call

import pytest

from cloud_cluster.accounts import AccountManager
from cloud_cluster.domain import BackendError, CloudAccount, ErrorKind
from cloud_cluster.rclone import RcloneAdapter
from cloud_cluster.settings import Settings
from cloud_cluster.transfers import TaskContext


@pytest.mark.parametrize("provider", ["drive", "box", "jottacloud", "pikpak", "pcloud"])
def test_empty_trash_only_calls_native_cleanup_for_the_approved_remote(provider):
    account = CloudAccount("target", "target", provider, root="selected-folder")
    adapter = RcloneAdapter.__new__(RcloneAdapter)
    adapter.rpc = Mock(return_value={"type": provider})
    adapter.run = Mock(side_effect=[{"Features": {"CleanUp": True}}, {}])
    manager = AccountManager(adapter, None, Settings(accounts=[account]))
    context = TaskContext()

    assert manager.empty_trash(account, context).outcome == "완료"
    assert adapter.run.call_args_list == [
        call("operations/fsinfo", context, fs=account.fs),
        call("operations/cleanup", context, fs=account.fs),
    ]


@pytest.mark.parametrize(
    "restriction", ["onedrive", "b2", "changed", "disabled", "readonly", "no-feature"]
)
def test_empty_trash_checks_provider_and_remote_state_before_writing(restriction):
    provider = restriction if restriction in {"onedrive", "b2"} else "pcloud"
    account = CloudAccount("target", "target", provider)
    saved_account = replace(account, enabled=False) if restriction == "disabled" else account
    if restriction == "readonly":
        account = saved_account = replace(account, readonly=True)
    adapter = RcloneAdapter.__new__(RcloneAdapter)
    adapter.rpc = Mock(return_value={"type": "onedrive" if restriction == "changed" else provider})
    adapter.run = Mock(return_value={"Features": {"CleanUp": restriction != "no-feature"}})
    manager = AccountManager(adapter, None, Settings(accounts=[saved_account]))

    with pytest.raises(ValueError):
        manager.empty_trash(account, TaskContext())
    assert all(args[0] == "operations/fsinfo" for args, _ in adapter.run.call_args_list)


@pytest.mark.parametrize("kind", [ErrorKind.NETWORK, ErrorKind.CANCELLED])
def test_empty_trash_reports_uncertain_result_without_retrying(kind):
    account = CloudAccount("target", "target", "box")
    adapter = RcloneAdapter.__new__(RcloneAdapter)
    adapter.rpc = Mock(return_value={"type": "box"})
    adapter.run = Mock(side_effect=[{"Features": {"CleanUp": True}}, BackendError(kind)])
    manager = AccountManager(adapter, None, Settings(accounts=[account]))

    result = manager.empty_trash(account, TaskContext())
    assert result.outcome == "실패 / 결과 확인 필요"
    assert "웹 휴지통" in result.steps[0].message
    assert adapter.run.call_count == 2
