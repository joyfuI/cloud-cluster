"""Create ONLY local test data and a separate plain config for packaged GUI QA."""

import sys
from pathlib import Path

from cloud_cluster.accounts import AccountManager
from cloud_cluster.rclone import RcloneAdapter
from cloud_cluster.settings import Settings, SettingsStore


def main():
    directory = Path(sys.argv[1]).resolve()
    workspace = Path(__file__).resolve().parents[2]
    if not directory.is_relative_to(workspace / "build"):
        raise ValueError("Package fixture must be inside workspace/build")
    store = SettingsStore(directory / "app")
    settings = Settings()
    backend = RcloneAdapter()
    manager = AccountManager(backend, store, settings)
    manager.open()
    for name in ("fixture_a", "fixture_b"):
        root = directory / name
        root.mkdir(exist_ok=True)
        (root / "같은 파일.txt").write_text("두 복제본의 내용이 같습니다.", encoding="utf-8")
        (root / "작업").mkdir(exist_ok=True)
        manager.configure(name, "alias", {"remote": root.as_posix()})
    manager.refresh()
    for name in ("fixture_a", "fixture_b"):
        manager.set_enabled(name, True)
    (directory / "fixture_a" / "복제 부족.txt").write_text("수동 복제 대상", encoding="utf-8")
    store.save(settings)
    assert backend.shutdown()


if __name__ == "__main__":
    main()
