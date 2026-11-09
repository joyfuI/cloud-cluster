from pathlib import Path
from types import SimpleNamespace

import pytest

from cloud_cluster.gui import dialogs
from cloud_cluster.gui.window import MainWindow
from cloud_cluster.settings import Settings, SettingsStore


def test_help_menu_opens_bundled_licenses_without_changing_preferences(qtbot, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    settings = Settings()
    window = MainWindow(None, settings, SettingsStore(tmp_path / "app"))
    qtbot.addWidget(window)
    window.show()
    opened = []
    monkeypatch.setattr(dialogs.LicensesDialog, "exec", lambda dialog: opened.append(dialog))
    help_menu = next(
        action.menu() for action in window.menuBar().actions() if action.text() == "도움말"
    )
    action = next(action for action in help_menu.actions() if action.text() == "오픈소스 라이선스")
    action.trigger()
    dialog = opened[0]
    assert dialog.documents.currentText() == "SOURCES.md"
    assert "https://download.qt.io/" in dialog.text.toPlainText()
    assert 'href="https://' in dialog.text.toHtml()
    assert dialog.text.openExternalLinks()
    assert dialog.text.isReadOnly()
    directory = Path(__file__).resolve().parents[2] / "licenses"
    for name in ("LGPL-3.0-only.txt", "GPL-3.0-only.txt", "rclone-MIT.txt"):
        dialog.documents.setCurrentIndex(dialog.documents.findText(name))
        assert dialog.text.toPlainText() == (directory / name).read_text(encoding="utf-8-sig")
    assert window.settings == settings
    qtbot.waitUntil(lambda: not window.workers.pending)
    window.close()
    qtbot.waitUntil(lambda: window._shutdown_done)


@pytest.mark.parametrize("compiled", [False, True])
def test_license_paths_use_payload_or_source_root(qtbot, monkeypatch, tmp_path, compiled):
    root = tmp_path / "payload"
    module_root = root if compiled else root / "src"
    monkeypatch.setattr(dialogs, "__file__", str(module_root / "cloud_cluster/gui/dialogs.py"))
    if compiled:
        monkeypatch.setattr(
            dialogs, "__compiled__", SimpleNamespace(containing_dir=tmp_path / "exe"), raising=False
        )
    else:
        monkeypatch.delattr(dialogs, "__compiled__", raising=False)
    directory = root / "licenses"
    directory.mkdir(parents=True)
    (directory / "SOURCES.md").write_text("# Sources\n\nhttps://www.qt.io/", encoding="utf-8")
    dll_hash = "A" * 64
    if not compiled:
        manifest = module_root / "cloud_cluster/_native/build-info.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_text(f'{{"sha256": "{dll_hash}"}}', encoding="utf-8")
    modules = directory / "go-modules" if compiled else root / "build/rclone-licenses" / dll_hash
    modules.mkdir(parents=True)
    (modules / "NOTICE").write_text("Go module notice", encoding="utf-8-sig")
    monkeypatch.chdir(tmp_path)
    dialog = dialogs.LicensesDialog()
    qtbot.addWidget(dialog)
    assert dialog.documents.currentText() == "SOURCES.md"
    assert dialog.documents.count() == 2
    dialog.documents.setCurrentIndex(dialog.documents.findText("go-modules/NOTICE"))
    assert dialog.text.toPlainText() == "Go module notice"


@pytest.mark.parametrize("unreadable", ["missing", "invalid-encoding"])
def test_license_read_errors_are_visible(qtbot, monkeypatch, tmp_path, unreadable):
    root = tmp_path / "payload"
    monkeypatch.setattr(dialogs, "__file__", str(root / "cloud_cluster/gui/dialogs.py"))
    monkeypatch.setattr(dialogs, "__compiled__", object(), raising=False)
    directory = root / "licenses"
    directory.mkdir(parents=True)
    path = directory / "NOTICE"
    path.write_text("Original notice", encoding="utf-8")
    dialog = dialogs.LicensesDialog()
    qtbot.addWidget(dialog)
    assert dialog.text.toPlainText() == "Original notice"
    if unreadable == "missing":
        path.unlink()
    else:
        path.write_bytes(b"\xff")
    dialog.load_document()
    assert "라이선스 자료를 읽을 수 없습니다." in dialog.text.toPlainText()


def test_missing_payload_licenses_do_not_use_executable_folder(qtbot, monkeypatch, tmp_path):
    monkeypatch.setattr(dialogs, "__file__", str(tmp_path / "payload/cloud_cluster/gui/dialogs.py"))
    monkeypatch.setattr(
        dialogs, "__compiled__", SimpleNamespace(containing_dir=tmp_path), raising=False
    )
    directory = tmp_path / "licenses"
    directory.mkdir()
    (directory / "NOTICE").write_text("Outside payload", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    dialog = dialogs.LicensesDialog()
    qtbot.addWidget(dialog)
    assert dialog.documents.count() == 0
    assert "라이선스 자료가 없습니다." in dialog.text.toPlainText()
