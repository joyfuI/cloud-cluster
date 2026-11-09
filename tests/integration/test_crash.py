import subprocess
import sys

import pytest

from cloud_cluster.rclone import NATIVE_DIR

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not (NATIVE_DIR / "librclone.dll").exists(), reason="build DLL first"),
]

SCRIPT = """
import os, sys
from pathlib import Path
from cloud_cluster.accounts import AccountManager
from cloud_cluster.domain import CloudAccount
from cloud_cluster.operations import OperationEngine
from cloud_cluster.rclone import RcloneAdapter
from cloud_cluster.settings import Settings, SettingsStore
from cloud_cluster.transfers import TaskContext
from cloud_cluster.vfs import VirtualFileSystem
root = Path(sys.argv[1])
backend = RcloneAdapter()
manager = AccountManager(backend, SettingsStore(root / ('config-' + sys.argv[2])), Settings())
manager.open()
accounts = [CloudAccount(a, ':local:', 'local', (root/a).as_posix()) for a in ('a', 'b')]
if sys.argv[2] == 'delete':
    engine = OperationEngine(backend, accounts)
    replicas = [backend.list_dir(a, '')[0] for a in accounts]
    delete = backend.delete_file
    def terminate(endpoint, context=None):
        delete(endpoint, context)
        os._exit(17)
    backend.delete_file = terminate
    engine.execute_delete(replicas, TaskContext(), approved=True)
else:
    vfs = VirtualFileSystem(backend, accounts)
    snapshot = vfs.refresh('')
    assert len(snapshot.entries) == 1
    assert snapshot.entries[0].assessment.status.value == '복제 부족'
    vfs.close()
    backend.shutdown()
"""


def test_actual_process_exit_during_delete_and_fresh_rescan(tmp_path):
    for account in ("a", "b"):
        (tmp_path / account).mkdir()
        (tmp_path / account / "x.txt").write_bytes(b"preserve me")
    first = subprocess.run(
        [sys.executable, "-c", SCRIPT, str(tmp_path), "delete"], timeout=30, capture_output=True
    )
    assert first.returncode == 17, first.stderr.decode(errors="replace")
    assert not (tmp_path / "a/x.txt").exists()
    assert (tmp_path / "b/x.txt").read_bytes() == b"preserve me"
    second = subprocess.run(
        [sys.executable, "-c", SCRIPT, str(tmp_path), "rescan"], timeout=30, capture_output=True
    )
    assert second.returncode == 0, second.stderr.decode(errors="replace")
