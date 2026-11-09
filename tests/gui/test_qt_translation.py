import pytest
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QInputDialog,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTextEdit,
)

from cloud_cluster.gui import install_korean_translation
from cloud_cluster.gui.dialogs import SettingsDialog, confirm, message
from cloud_cluster.settings import Settings


@pytest.fixture
def korean_qt(qapp):
    translator = install_korean_translation(qapp)
    assert not translator.isEmpty()
    yield
    qapp.removeTranslator(translator)


def test_settings_buttons_are_korean_and_keep_accept_reject_roles(qtbot, korean_qt):
    for button, expected, result in (
        (QDialogButtonBox.StandardButton.Ok, "확인", QDialog.DialogCode.Accepted),
        (QDialogButtonBox.StandardButton.Cancel, "취소", QDialog.DialogCode.Rejected),
    ):
        dialog = SettingsDialog(Settings())
        qtbot.addWidget(dialog)
        buttons = dialog.findChild(QDialogButtonBox)
        assert buttons.button(button).text() == expected
        buttons.button(button).click()
        assert dialog.result() == result


@pytest.mark.parametrize("accepted", [False, True])
def test_confirmation_shows_full_text_and_keeps_korean_buttons(
    qapp, qtbot, monkeypatch, korean_qt, accepted
):
    short = "<b>대상 확인</b>"
    wrapped = " / ".join(["긴 파일 이름"] * 20)
    long = "/" + "길" * 600 + "\n" + "\n".join(f"파일/{index}.txt" for index in range(180))
    heights = []

    def execute(dialog):
        qtbot.addWidget(dialog)
        dialog.show()
        qapp.processEvents()
        buttons = dialog.findChild(QDialogButtonBox)
        ok = buttons.button(QDialogButtonBox.StandardButton.Ok)
        cancel = buttons.button(QDialogButtonBox.StandardButton.Cancel)
        assert ok.text() == "확인" and cancel.text() == "취소"
        assert cancel.isDefault()
        assert set(dialog.findChildren(QPushButton)) == {ok, cancel}
        (edit,) = dialog.findChildren(QTextEdit)
        assert edit.isReadOnly() and edit.toPlainText() == details
        assert edit.height() < 20 * edit.fontMetrics().lineSpacing()
        heights.append(dialog.height())
        assert (edit.verticalScrollBar().maximum() > 0) == (details == long)
        assert edit.horizontalScrollBar().maximum() == 0
        if accepted:
            ok.click()
        else:
            qtbot.keyClick(dialog, Qt.Key.Key_Return)
        assert not dialog.isVisible()
        return dialog.result()

    monkeypatch.setattr(QDialog, "exec", execute)
    for details in (short, wrapped, long):
        assert confirm(None, "계정 삭제", details) is accepted
    assert heights[0] < heights[1] < heights[2]


@pytest.mark.parametrize("entrypoint", ["warning", "information", "critical"])
def test_static_message_boxes_use_korean_ok(qapp, qtbot, korean_qt, entrypoint):
    labels = []

    def close():
        box = qapp.activeModalWidget()
        qtbot.addWidget(box)
        button = box.button(QMessageBox.StandardButton.Ok)
        labels.append(button.text())
        button.click()

    QTimer.singleShot(0, close)
    if entrypoint == "warning":
        message(None, "오류")
    else:
        getattr(QMessageBox, entrypoint)(None, "Cloud Cluster", "안내")
    assert labels == ["확인"]


@pytest.mark.parametrize("kind", ["text", "item"])
def test_input_dialogs_translate_buttons_without_changing_values(qapp, qtbot, korean_qt, kind):
    def accept():
        dialog = qapp.activeModalWidget()
        qtbot.addWidget(dialog)
        assert dialog.okButtonText() == "확인" and dialog.cancelButtonText() == "취소"
        dialog.accept()

    QTimer.singleShot(0, accept)
    if kind == "text":
        result = QInputDialog.getText(None, "이름 변경", "새 이름", text="Cancel")
    else:
        result = QInputDialog.getItem(None, "복제본 선택", "계정", ["Cancel"], editable=False)
    assert result == ("Cancel", True)


def test_qt_file_dialog_and_edit_menu_use_korean(qtbot, korean_qt, tmp_path):
    dialog = QFileDialog(None, "파일 선택", str(tmp_path))
    dialog.setOption(QFileDialog.Option.DontUseNativeDialog)
    qtbot.addWidget(dialog)
    labels = [b.text().replace("&", "") for b in dialog.findChildren(QPushButton)]
    assert "열기(O)" in labels and "취소" in labels
    editor = QLineEdit("Cancel")
    qtbot.addWidget(editor)
    menu = editor.createStandardContextMenu()
    try:
        labels = [a.text().replace("&", "").split("\t")[0] for a in menu.actions()]
        assert any("복사" in text for text in labels)
        assert any("붙여넣기" in text for text in labels)
        assert editor.text() == "Cancel"
    finally:
        menu.deleteLater()
