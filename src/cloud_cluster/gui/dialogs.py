import json
import re
from dataclasses import replace
from math import ceil
from pathlib import Path

from PySide6.QtCore import (
    QCollator,
    QItemSelection,
    QItemSelectionModel,
    QLocale,
    QSignalBlocker,
    Qt,
    QTimer,
    QUrl,
    Signal,
)
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QStyle,
    QTableWidget,
    QTableWidgetItem,
    QTextBrowser,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ..accounts import TRASH_CLEANUP_PROVIDERS
from ..domain import Status
from .models import size_text
from .rclone_help_ko import AUTH_DATA_CHOICES, AUTH_LABELS_KO, translate_auth_text
from .workers import WorkerPool


def message(parent, error):
    QMessageBox.warning(parent, "Cloud Cluster", str(error))


def confirm(parent, title, details, prompt="작업 범위와 영향을 확인하세요."):
    dialog, _, _ = _confirmation_dialog(parent, title, details, prompt)
    return dialog.exec() == QDialog.DialogCode.Accepted


def confirm_upload_overwrite(parent, details, index):
    dialog, edit, buttons = _confirmation_dialog(
        parent, "업로드 덮어쓰기", details[index], "같은 경로의 파일을 덮어쓰시겠습니까?"
    )
    buttons.button(QDialogButtonBox.StandardButton.Ok).setText("덮어쓰기")
    buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("건너뛰기")
    if index == len(details) - 1:
        return dialog.exec() == QDialog.DialogCode.Accepted, False

    apply_all = QCheckBox("남은 모든 항목에 적용")
    layout = dialog.layout()
    layout.insertWidget(layout.indexOf(buttons), apply_all)

    def show_targets(checked):
        edit.setPlainText("\n\n".join(details[index:]) if checked else details[index])
        dialog.adjustSize()

    clicked = False

    def record_click():
        nonlocal clicked
        clicked = True

    apply_all.toggled.connect(show_targets)
    buttons.clicked.connect(record_click)
    approved = dialog.exec() == QDialog.DialogCode.Accepted
    return approved, clicked and apply_all.isChecked()


def _confirmation_dialog(parent, title, details, prompt):
    dialog = QDialog(parent)
    dialog.setWindowTitle(title)
    dialog.setFixedWidth(min(520, dialog.screen().availableGeometry().width() - 32))
    layout = QVBoxLayout(dialog)
    heading = QHBoxLayout()
    icon = QLabel()
    style = dialog.style()
    icon_size = style.pixelMetric(QStyle.PixelMetric.PM_MessageBoxIconSize)
    icon.setPixmap(style.standardIcon(QStyle.StandardPixmap.SP_MessageBoxWarning).pixmap(icon_size))
    heading.addWidget(icon)
    label = QLabel(prompt)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    heading.addWidget(label, 1)
    layout.addLayout(heading)

    edit = None
    if details:
        edit = QTextEdit()
        edit.setAccessibleName("작업 내용")
        edit.setReadOnly(True)
        edit.setTabChangesFocus(True)
        layout.addWidget(edit)
        margins = layout.contentsMargins()
        frame = 2 * edit.frameWidth()
        document = edit.document()
        max_height = 12 * edit.fontMetrics().lineSpacing() + 2 * document.documentMargin()

        def resize_details():
            document.setTextWidth(dialog.width() - margins.left() - margins.right() - frame)
            edit.setFixedHeight(ceil(min(document.size().height(), max_height)) + frame)

        edit.textChanged.connect(resize_details)
        edit.setPlainText(details)

    buttons = QDialogButtonBox(
        QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
    )
    buttons.accepted.connect(dialog.accept)
    buttons.rejected.connect(dialog.reject)
    layout.addWidget(buttons)
    cancel = buttons.button(QDialogButtonBox.StandardButton.Cancel)
    cancel.setDefault(True)
    cancel.setFocus()
    return dialog, edit, buttons


class AccountDialog(QDialog):
    changed = Signal()

    def __init__(
        self, manager, parent=None, *, changes_available=lambda: True,
        empty_trash=None, empty_trash_available=lambda account: True,
    ):
        super().__init__(parent)
        self.manager = manager
        self.changes_available = changes_available
        self.submit_empty_trash = empty_trash
        self.empty_trash_available = empty_trash_available
        self.workers = WorkerPool(self)
        self.quota_workers = WorkerPool(self)
        self.quotas = {}
        self.trash_support = {}
        self.setWindowTitle("계정 관리")
        self.resize(760, 430)
        layout = QVBoxLayout(self)
        self.table = QTableWidget(0, 5)
        self.table.setWordWrap(False)
        self.table.setHorizontalHeaderLabels(
            ["계정", "제공업체", "사용 여부", "전체 용량", "사용 가능 용량"]
        )
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.table)
        buttons = QHBoxLayout()
        self.account_buttons = []
        for label, slot in [
            ("계정 추가...", self.create),
            ("재인증...", self.reauthenticate),
            ("사용 여부 변경", self.toggle),
            ("계정 삭제...", self.delete),
        ]:
            button = QPushButton(label)
            button.clicked.connect(slot)
            buttons.addWidget(button)
            if slot == self.toggle:
                self.toggle_button = button
            else:
                self.account_buttons.append(button)
        self.empty_trash_button = QPushButton("휴지통 비우기...")
        self.empty_trash_button.clicked.connect(self.empty_trash)
        buttons.addWidget(self.empty_trash_button)
        self.table.itemSelectionChanged.connect(self.update_account_buttons)
        self.workers.busy_changed.connect(self.update_account_buttons)
        self.quota_workers.busy_changed.connect(self.update_account_buttons)
        self.change_notice = QLabel("진행 중인 작업이 끝나면 계정을 변경할 수 있습니다.")
        self.change_notice.setWordWrap(True)
        layout.addWidget(self.change_notice)
        layout.addLayout(buttons)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.button(QDialogButtonBox.StandardButton.Close).setText("닫기")
        close.rejected.connect(self.reject)
        layout.addWidget(close)
        self.reload()

    def reload(self):
        self.table.setRowCount(len(self.manager.settings.accounts))
        for row, account in enumerate(self.manager.settings.accounts):
            if (
                self.submit_empty_trash is not None
                and account.provider in TRASH_CLEANUP_PROVIDERS
                and account.id not in self.trash_support
            ):
                self.trash_support[account.id] = False
                self.quota_workers.submit(
                    lambda context, account=account: self.manager.supports_empty_trash(account, context),
                    lambda supported, account_id=account.id: self.trash_support.update(
                        {account_id: supported}
                    ),
                )
            if account.id not in self.quotas:
                self.quotas[account.id] = None
                self.quota_workers.submit(
                    lambda context, account=account: self.manager.quota(account, context),
                    lambda quota, account_id=account.id: self.quota_ready(account_id, quota),
                    lambda error, account_id=account.id: self.quota_ready(account_id, None, error),
                )
            total, free, error = self.quotas[account.id] or ("조회 중…", "조회 중…", "")
            for column, value in enumerate(
                (
                    account.label,
                    account.provider,
                    "사용" if account.enabled else "사용 안 함",
                    total,
                    free,
                )
            ):
                self.table.setItem(row, column, QTableWidgetItem(value))
                if column >= 3:
                    self.table.item(row, column).setToolTip(error)
        self.table.resizeColumnsToContents()
        self.update_account_buttons()

    def quota_ready(self, account_id, quota, error=""):
        values = (
            ("조회 실패", "조회 실패", error)
            if error
            else ("미지원", "미지원", "")
            if quota is None
            else (size_text(quota.total), size_text(quota.free), "")
        )
        self.quotas[account_id] = values
        for row, account in enumerate(self.manager.settings.accounts):
            if account.id == account_id:
                for column, value in zip((3, 4), values[:2]):
                    item = self.table.item(row, column)
                    item.setText(value)
                    item.setToolTip(error)
                break
        self.table.resizeColumnsToContents()

    def selected(self):
        rows = self.table.selectionModel().selectedRows()
        if rows and rows[0].row() < len(self.manager.settings.accounts):
            return self.manager.settings.accounts[rows[0].row()]
        return None

    def update_account_buttons(self):
        available = self.changes_available()
        self.change_notice.setVisible(not available)
        idle = available and not self.workers.pending
        config_edit_available = idle and not self.quota_workers.pending
        for button in self.account_buttons:
            button.setEnabled(config_edit_available)
        account = self.selected()
        self.empty_trash_button.setEnabled(self.can_empty_trash(account))
        self.toggle_button.setEnabled(account is not None and idle)
        self.toggle_button.setText(
            "사용 여부 변경"
            if account is None
            else "사용 안 함으로 변경"
            if account.enabled
            else "사용으로 변경"
        )

    def config_edit_available(self):
        return (
            not self.workers.pending
            and not self.quota_workers.pending
            and self.changes_available()
        )

    def can_empty_trash(self, account):
        return bool(
            account and account.enabled and not account.readonly and not self.workers.pending
            and self.trash_support.get(account.id) and self.empty_trash_available(account)
        )

    def empty_trash(self):
        account = self.selected()
        if not self.can_empty_trash(account):
            return
        details = (
            f"{account.label} · {account.provider}\n\n"
            "실행 시점에 이 리모트의 휴지통에 있는 항목을 영구 삭제합니다. "
            "대기 중 휴지통에 들어간 항목과 앱 밖에서 삭제했거나 앱 관리 폴더 밖에 있던 항목도 "
            "포함될 수 있습니다. 삭제 후 복구할 수 없습니다."
        )
        if account.provider == "pcloud":
            details += "\n\npCloud는 rclone에 설정된 사용자 이름과 비밀번호로 인증한 계정의 휴지통을 비웁니다."
        if not confirm(self, "휴지통 비우기", details, "휴지통을 비우시겠습니까?"):
            return
        if account != self.selected() or not self.can_empty_trash(account):
            return
        try:
            self.submit_empty_trash(account)
        except Exception as error:
            message(self, error)
        else:
            self.status.setText("휴지통 비우기를 등록했습니다. 작업 목록에서 진행 상태를 확인하세요.")

    def create(self):
        if not self.config_edit_available():
            return
        self.status.setText("제공업체 설정을 읽는 중…")
        self.workers.submit(
            lambda context: self.manager.providers(),
            self._create,
            lambda error: message(self, error),
        )

    def _create(self, providers, account=None):
        if not self.changes_available():
            self.status.clear()
            return
        dialog = AuthDialog(self.manager, providers, account, self)
        accepted = dialog.exec() == QDialog.DialogCode.Accepted

        def done(result):
            if accepted and account is None:
                try:
                    self.manager.set_enabled(dialog.name.text(), True)
                except (ValueError, OSError) as error:
                    message(self, error)
            self.quotas.clear()
            self.trash_support.clear()
            self.reload()
            self.changed.emit()

        # Cancelled authentication may still leave a remote in the native config.
        self.workers.submit(
            lambda context: self.manager.refresh(),
            done,
            lambda error: message(self, error),
        )

    def reauthenticate(self):
        if not self.config_edit_available():
            return
        account = self.selected()
        if account:
            self.workers.submit(
                lambda context: self.manager.providers(),
                lambda providers: self._create(providers, account),
                lambda error: message(self, error),
            )

    def toggle(self):
        if self.workers.pending or not self.changes_available():
            return
        account = self.selected()
        if account:
            try:
                self.manager.set_enabled(account.remote, not account.enabled)
            except (ValueError, OSError) as error:
                message(self, error)
                return
            self.reload()
            self.changed.emit()

    def reject(self):
        if self.workers.pending:
            self.status.setText("진행 중인 계정 작업을 마친 뒤 닫을 수 있습니다.")
            return
        if self.quota_workers.pending:
            self.quota_workers.cancel_all()
            self.status.setText("용량 조회 취소를 기다리는 중…")
            QTimer.singleShot(50, self.reject)
            return
        super().reject()

    def delete(self):
        if not self.config_edit_available():
            return
        account = self.selected()
        if not account or not confirm(
            self,
            "계정 삭제",
            f"{account.label} 계정을 rclone config와 앱의 사용 목록에서 삭제합니다.\n"
            "이 계정의 저장된 인증 정보가 제거됩니다. 클라우드 파일과 서비스의 OAuth 승인은 유지됩니다.\n"
            "같은 config를 사용하는 다른 프로그램이나 이 리모트를 참조하는 설정도 영향을 받습니다.",
        ):
            return
        if not self.config_edit_available():
            return
        self.status.setText("계정 삭제 중…")

        def finished(error=None):
            self.reload()
            self.changed.emit()
            self.status.setText("계정 삭제 결과를 확인하세요." if error else "계정 삭제 완료")
            if error:
                message(self, error)

        self.workers.submit(
            lambda context: self.manager.delete(account.remote),
            lambda result: finished(),
            finished,
        )


class AuthDialog(QDialog):
    def __init__(self, manager, providers, account=None, parent=None):
        super().__init__(parent)
        self.manager, self.providers, self.account = manager, providers, account
        self.workers = WorkerPool(self)
        self.context, self.state, self.question, self.polling = None, None, None, False
        self.question_is_password = False
        self.current_result, self.pending_history = None, None
        self.history, self.settings_question = [], False
        self.opened_urls, self.closing = set(), False
        self.setWindowTitle("재인증" if account else "계정 추가")
        self.resize(560, 360)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.name = QLineEdit(
            account.remote if account else "cloud_" + str(len(manager.settings.accounts) + 1)
        )
        self.name.setEnabled(account is None)
        self.provider = QComboBox()
        self.provider.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.provider.setMinimumContentsLength(24)
        collator = QCollator(QLocale("ko_KR"))
        collator.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        for provider in sorted(
            providers, key=lambda p: collator.sortKey(p.get("Description") or p["Name"])
        ):
            self.provider.addItem(provider.get("Description") or provider["Name"], provider)
        selected_provider = account.provider if account else "drive"
        for index in range(self.provider.count()):
            if self.provider.itemData(index)["Name"] == selected_provider:
                self.provider.setCurrentIndex(index)
                break
        self.provider.setEnabled(account is None)
        form.addRow("remote 이름", self.name)
        form.addRow("제공업체", self.provider)
        layout.addLayout(form)
        content = QWidget()
        content_layout = QVBoxLayout(content)
        self.question_scroll = QScrollArea()
        self.question_scroll.setWidgetResizable(True)
        self.question_scroll.setWidget(content)
        layout.addWidget(self.question_scroll)
        self.question_scroll.hide()
        self.help = QLabel()
        self.help.setWordWrap(True)
        self.help.setTextFormat(Qt.TextFormat.PlainText)
        content_layout.addWidget(self.help)
        self.question_box = QWidget()
        self.question_form = QFormLayout(self.question_box)
        content_layout.addWidget(self.question_box)
        content_layout.addStretch()
        self.question_box.hide()
        buttons = QHBoxLayout()
        self.previous_button = QPushButton("이전")
        self.previous_button.clicked.connect(self.previous)
        self.previous_button.setEnabled(False)
        self.next_button = QPushButton("재인증" if account else "추가")
        self.next_button.clicked.connect(self.next)
        self.cancel_button = QPushButton("취소")
        self.cancel_button.clicked.connect(self.reject)
        self.browser_button = QPushButton("인증 페이지 다시 열기")
        self.browser_button.clicked.connect(self.reopen_browser)
        for button in (
            self.browser_button, self.previous_button, self.next_button, self.cancel_button
        ):
            buttons.addWidget(button)
        layout.addLayout(buttons)
        self.browser_button.hide()
        self.timer = QTimer(self)
        self.timer.setInterval(500)
        self.timer.timeout.connect(self.poll)

    def next(self):
        if self.context or self.closing or not self.next_button.isEnabled():
            return
        try:
            provider = self.provider.currentData()["Name"]
            answer = ""
            if self.question:
                if isinstance(self.question, QComboBox):
                    index = self.question.currentIndex()
                    answer = (
                        self.question.itemData(index)
                        if index >= 0 and self.question.currentText() == self.question.itemText(index)
                        else self.question.currentText()
                    )
                else:
                    answer = self.question.text()
                option = (self.current_result or {}).get("Option") or {}
                name = option.get("Name", "답변")
                if not answer and provider == "drive" and name == "client_id":
                    raise ValueError("Google Drive OAuth client_id를 입력하세요.")
                if option.get("Required") and not answer:
                    raise ValueError(f"{AUTH_LABELS_KO.get(name, name)} 값을 입력하세요.")
            self.run_step(self.state, answer)
        except ValueError as error:
            message(self, error)

    def run_step(self, state, answer):
        if self.context or self.closing:
            return
        self.next_button.setEnabled(False)
        self.previous_button.setEnabled(False)
        self.name.setEnabled(False)
        self.provider.setEnabled(False)
        self.browser_button.hide()
        self.timer.start()
        remote = self.name.text()
        provider = self.provider.currentData()["Name"]
        answer_is_password = (
            self.question_is_password
            and isinstance(self.question, QLineEdit)
            and self.question.isModified()
        )
        self.pending_history = (
            (self.current_result, answer, answer_is_password) if self.settings_question else None
        )
        try:
            self.context = self.workers.submit(
                lambda context: self.manager.configure(
                    remote,
                    provider,
                    {},
                    context,
                    state=state,
                    answer=answer,
                    reconnect=self.account is not None and state is None,
                    answer_is_password=answer_is_password,
                ),
                self.response,
                self.failed,
            )
        except Exception as error:
            self.failed(error)

    def response(self, result):
        self.context = None
        self.browser_button.hide()
        if self.closing:
            self.timer.stop()
            super().reject()
            return
        self.state = result.get("State") or None
        option = result.get("Option")
        self.settings_question = bool(
            option and self.state
            and (self.state.startswith("*all-set,") or self.state == "*all-advanced")
        )
        if self.settings_question:
            if (
                self.pending_history
                and self.state != self.pending_history[0].get("State")
                and not result.get("Error")
            ):
                self.history.append(self.pending_history)
        else:
            self.history.clear()
        self.current_result, self.pending_history = result, None
        self.previous_button.setEnabled(bool(self.history))
        self.question_is_password = bool(option and option.get("IsPassword"))
        self.name.setEnabled(False)
        self.provider.setEnabled(False)
        if option:
            if self.question_scroll.isHidden():
                self.question_scroll.show()
            name = option.get("Name", "답변")
            while self.question_form.rowCount():
                self.question_form.removeRow(0)
            examples = option.get("Examples") or []
            default = option.get("Default")
            if isinstance(default, bool):
                default = str(default).lower()
            else:
                default = str(default) if default is not None else ""
            default = option.get("DefaultStr", default)
            if examples:
                self.question = QComboBox()
                self.question.setEditable(not option.get("Exclusive", False))
                self.question.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
                for example in examples:
                    label = example.get("Help") or str(example["Value"])
                    self.question.addItem(
                        label if name in AUTH_DATA_CHOICES else translate_auth_text(label),
                        str(example["Value"]),
                    )
            elif isinstance(option.get("Default"), bool):
                self.question = QComboBox()
                self.question.addItem("예", "true")
                self.question.addItem("아니요", "false")
            else:
                self.question = QLineEdit(default)
                if option.get("IsPassword"):
                    self.question.setEchoMode(QLineEdit.EchoMode.Password)
            if isinstance(self.question, QComboBox):
                self.question.setSizeAdjustPolicy(
                    QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
                )
                self.question.setMinimumContentsLength(24)
                index = self.question.findData(default)
                if index >= 0:
                    self.question.setCurrentIndex(index)
                elif self.question.isEditable():
                    self.question.setEditText(default)
            required = option.get("Required") or (
                name == "client_id" and self.provider.currentData()["Name"] == "drive"
            )
            label = AUTH_LABELS_KO.get(name, name) + (" *" if required else "")
            self.question_form.addRow(label, self.question)
            self.question_box.show()
            self.question.setFocus()
            messages = (result.get("Error") or "", option.get("Help") or "")
            self.help.setText("\n".join(translate_auth_text(text) for text in messages if text))
            self.next_button.setText("다음")
            self.next_button.setEnabled(True)
        elif self.state:
            QTimer.singleShot(
                0,
                lambda: self.run_step(self.state, result.get("Result", "")),
            )
        else:
            self.timer.stop()
            self.accept()

    def previous(self):
        if self.context or self.closing or not self.previous_button.isEnabled():
            return
        result, answer, modified = self.history.pop()
        self.response(result)
        if isinstance(self.question, QComboBox):
            index = self.question.findData(answer)
            if index >= 0:
                self.question.setCurrentIndex(index)
            elif self.question.isEditable():
                self.question.setEditText(answer)
        else:
            self.question.setText(answer)
            self.question.setModified(modified)

    def failed(self, error):
        self.context = None
        self.pending_history = None
        self.timer.stop()
        self.browser_button.hide()
        if self.closing:
            super().reject()
            return
        self.next_button.setEnabled(True)
        self.previous_button.setEnabled(bool(self.history))
        if self.state is None:
            self.name.setEnabled(self.account is None)
            self.provider.setEnabled(self.account is None)
        message(self, error)

    def poll(self):
        if self.polling or not self.timer.isActive() or self.closing:
            return
        self.polling = True

        def done(result):
            self.polling = False
            url = result.get("authUrl") or ""
            available = (
                self.timer.isActive()
                and not self.closing
                and result.get("status") == "running"
                and url.startswith("http://127.0.0.1:53682/")
            )
            self.browser_button.setVisible(available)
            if available and url not in self.opened_urls:
                self.opened_urls.add(url)
                QDesktopServices.openUrl(QUrl(url))

        def failed(error):
            self.polling = False
            self.browser_button.hide()

        try:
            self.workers.submit(
                lambda context: self.manager.backend.rpc("config/oauthstatus"), done, failed
            )
        except Exception as error:
            failed(error)

    def reopen_browser(self):
        self.opened_urls.clear()
        self.poll()

    def reject(self):
        if self.closing:
            return
        self.browser_button.hide()
        self.previous_button.setEnabled(False)
        self.history.clear()
        self.pending_history = None
        if self.context:
            self.closing = True
            self.cancel_button.setEnabled(False)
            self.context.cancelled.set()
            self.help.setText("인증 취소 완료를 기다리는 중…")
            try:
                self.workers.submit(
                    lambda context: self.manager.backend.rpc("config/oauthstop"),
                    failed=lambda error: None,
                )
            except Exception:
                self.help.setText(
                    "취소 요청을 보내지 못했습니다. 진행 중인 인증의 종료를 기다립니다."
                )
            return
        self.timer.stop()
        super().reject()


class IssueDialog(QDialog):
    action_requested = Signal(object)
    management_requested = Signal(object)
    rescan_requested = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._repair_busy = False
        self.setWindowTitle("문제 해결")
        self.resize(980, 480)
        layout = QVBoxLayout(self)
        self.heading = QLabel()
        self.heading.setTextFormat(Qt.TextFormat.PlainText)
        self.heading.setWordWrap(True)
        layout.addWidget(self.heading)
        self.table = QTableWidget(0, 3)
        self.table.setWordWrap(False)
        self.table.setHorizontalHeaderLabels(["경로", "상태", "상세"])
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setColumnWidth(0, 380)
        self.table.setColumnWidth(1, 130)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.itemSelectionChanged.connect(self.update_action)
        selection = QHBoxLayout()
        self.selection_summary = QLabel()
        self.selection_summary.setWordWrap(True)
        selection.addWidget(self.selection_summary, 1)
        select_all = QPushButton("전체 선택")
        select_all.clicked.connect(self.table.selectAll)
        selection.addWidget(select_all)
        clear = QPushButton("선택 해제")
        clear.clicked.connect(self.table.clearSelection)
        selection.addWidget(clear)
        layout.addLayout(selection)
        layout.addWidget(self.table)
        self.progress_label = QLabel()
        self.progress_label.setTextFormat(Qt.TextFormat.PlainText)
        self.progress_label.setWordWrap(True)
        self.progress_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.progress_label.hide()
        layout.addWidget(self.progress_label)
        buttons = QHBoxLayout()
        self.resolve_button = QPushButton("선택 문제 해결")
        self.resolve_button.clicked.connect(self.resolve)
        buttons.addWidget(self.resolve_button)
        self.manage_button = QPushButton("복제본 관리")
        self.manage_button.clicked.connect(
            lambda: self.management_requested.emit(self.selected_entries())
        )
        buttons.addWidget(self.manage_button)
        self.rescan_button = QPushButton("다시 검사...")
        self.rescan_button.clicked.connect(lambda: self.rescan_requested.emit(self.path))
        buttons.addWidget(self.rescan_button)
        buttons.addStretch()
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.rejected.connect(self.reject)
        buttons.addWidget(close)
        layout.addLayout(buttons)
        self.set_results("", (), True)

    def set_results(self, path, entries, complete, *, selected_keys=None):
        self.path, self.entries = path, tuple(entries)
        self.heading.setText(
            f"/{path} · {len(entries):,}개 문제"
            + ("" if complete else " (일부 계정을 확인하지 못해 검사 미완료)")
        )
        with QSignalBlocker(self.table):
            self.table.clearContents()
            self.table.setRowCount(len(entries))
            for row, entry in enumerate(entries):
                for column, value in enumerate(
                    (entry.path, entry.assessment.status.value, " · ".join(entry.assessment.issues))
                ):
                    self.table.setItem(row, column, QTableWidgetItem(value))
            self.table.clearSelection()
            if selected_keys is not None:
                selection, first = QItemSelection(), None
                model = self.table.model()
                for row, entry in enumerate(entries):
                    if entry.key in selected_keys:
                        if first is None:
                            first = row
                    elif first is not None:
                        selection.select(model.index(first, 0), model.index(row - 1, 2))
                        first = None
                if first is not None:
                    selection.select(model.index(first, 0), model.index(len(entries) - 1, 2))
                self.table.selectionModel().select(
                    selection, QItemSelectionModel.SelectionFlag.ClearAndSelect
                )
            elif entries:
                self.table.selectRow(0)
        self.table.itemSelectionChanged.emit()

    def selected_entries(self):
        return tuple(
            self.entries[row]
            for row in sorted(index.row() for index in self.table.selectionModel().selectedRows())
            if row < len(self.entries)
        )

    def update_action(self):
        selected = self.selected_entries()
        folders = sum(entry.assessment.status == Status.DIRECTORY for entry in selected)
        files = sum(entry.assessment.status == Status.UNDER for entry in selected)
        verification = [entry for entry in selected if entry.assessment.status == Status.UNVERIFIED]
        manual = len(selected) - folders - files - len(verification)
        reading = ""
        if verification:
            sizes = [replica.size for entry in verification for replica in entry.replicas]
            reading = (
                " · 검증 읽기 용량 미확인"
                if None in sizes
                else f" · 검증 읽기 약 {size_text(sum(sizes))}"
            )
        self.selection_summary.setText(
            f"{len(selected):,}개 선택 · 폴더 생성 {folders:,}개 · 복제 {files:,}개 · "
            f"정밀 검증 {len(verification):,}개 · 개별 확인 {manual:,}개{reading}"
        )
        self.resolve_button.setEnabled(folders + files + len(verification) > 0 and not self._repair_busy)
        self.resolve_button.setText("문제 해결 중…" if self._repair_busy else "선택 문제 해결")
        self.manage_button.setEnabled(bool(selected))

    def set_repair_busy(self, busy):
        if self._repair_busy != busy:
            self._repair_busy = busy
            self.update_action()

    def set_progress(self, text):
        self.progress_label.setText(text)
        self.progress_label.setVisible(bool(text))

    def set_scan_busy(self, busy):
        self.rescan_button.setEnabled(not busy)

    def resolve(self):
        if self._repair_busy:
            return
        entries = tuple(
            entry
            for entry in self.selected_entries()
            if entry.assessment.status in {Status.DIRECTORY, Status.UNDER, Status.UNVERIFIED}
        )
        if entries:
            self.action_requested.emit(entries)


class ReplicaDialog(QDialog):
    action_requested = Signal(str, object)

    def __init__(self, entries, accounts, parent=None):
        super().__init__(parent)
        self.action_buttons = {}
        self.entries = tuple(entries)
        self.labels = {a.id: a.label for a in accounts}
        self.resize(1000, 440)
        layout = QVBoxLayout(self)
        self.entry_selector = QComboBox()
        self.entry_selector.setAccessibleName("관리할 항목")
        self.entry_selector.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.entry_selector.setMinimumContentsLength(40)
        for index, entry in enumerate(self.entries):
            self.entry_selector.addItem(
                f"{index + 1}/{len(self.entries)} · {entry.path} · {entry.assessment.status.value}"
            )
        self.entry_selector.setVisible(len(self.entries) > 1)
        self.entry_selector.currentIndexChanged.connect(self.set_entry)
        layout.addWidget(self.entry_selector)
        self.heading = QLabel()
        self.heading.setTextFormat(Qt.TextFormat.PlainText)
        self.heading.setWordWrap(True)
        layout.addWidget(self.heading)
        self.table = QTableWidget(0, 6)
        self.table.setWordWrap(False)
        self.table.setHorizontalHeaderLabels(
            ["계정", "원격 경로", "크기", "수정 시각", "해시", "객체 ID"]
        )
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.table)
        buttons = QHBoxLayout()
        for label, action in [
            ("다운로드...", "download"),
            ("복제...", "replicate"),
            ("삭제...", "delete"),
            ("정밀 검증...", "verify"),
        ]:
            button = QPushButton(label)
            button.clicked.connect(lambda checked=False, name=action: self.choose(name))
            buttons.addWidget(button)
            self.action_buttons[action] = button
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.rejected.connect(self.reject)
        buttons.addWidget(close)
        layout.addLayout(buttons)
        self.set_entry(0)

    def set_entry(self, index):
        self.entry = self.entries[index]
        self.setWindowTitle("복제본 관리 · " + self.entry.name)
        self.heading.setText(
            self.entry.path
            + "\n"
            + self.entry.assessment.status.value
            + " · "
            + self.entry.assessment.verification
        )
        self.table.clearContents()
        self.table.setRowCount(len(self.entry.replicas))
        for row, replica in enumerate(self.entry.replicas):
            values = (
                self.labels.get(replica.account_id, replica.account_id),
                replica.path,
                size_text(replica.size),
                str(replica.modified or "알 수 없음"),
                "\n".join(
                    [f"{k}: {v}" for k, v in replica.hashes.items()]
                    + (
                        [f"정밀 검증 SHA-256: {replica.verified_sha256}"]
                        if replica.verified_sha256
                        else []
                    )
                )
                or "없음",
                replica.object_id,
            )
            for column, value in enumerate(values):
                self.table.setItem(row, column, QTableWidgetItem(value))
        self.table.resizeColumnsToContents()
        self.table.selectRow(0)

    def set_busy(self, busy, *, verification_busy):
        for action, button in self.action_buttons.items():
            button.setEnabled(not (busy or (action == "verify" and verification_busy)))

    def choose(self, action):
        if not self.action_buttons[action].isEnabled():
            return
        row = self.table.currentRow()
        if row < 0:
            return
        value = self.entry.replicas if action == "verify" else self.entry.replicas[row]
        self.action_requested.emit(action, value)


class LicensesDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("오픈소스 라이선스")
        self.resize(820, 600)
        layout = QVBoxLayout(self)
        notice = QLabel(
            "Cloud Cluster는 Qt/PySide6와 Shiboken을 사용하며, 해당 라이브러리와 그 사용에는 "
            "LGPLv3가 적용됩니다.\n사용 고지, 라이선스 원문, 소스 제공 및 라이브러리 교체 안내를 확인하세요."
        )
        notice.setWordWrap(True)
        layout.addWidget(notice)
        self.documents = QComboBox()
        self.documents.setAccessibleName("라이선스 문서")
        self.documents.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.documents.setMinimumContentsLength(40)
        layout.addWidget(self.documents)
        self.text = QTextBrowser()
        self.text.setAccessibleName("라이선스 내용")
        self.text.setOpenExternalLinks(True)
        layout.addWidget(self.text)
        root = Path(__file__).resolve().parents[2 if "__compiled__" in globals() else 3]
        directories = [("", root / "licenses")]
        if "__compiled__" not in globals():
            manifest = root / "src/cloud_cluster/_native/build-info.json"
            try:
                info = json.loads(manifest.read_text(encoding="utf-8-sig"))
                dll_hash = info["sha256"]
                if isinstance(dll_hash, str) and re.fullmatch(r"[0-9A-Fa-f]{64}", dll_hash):
                    directories.append(("go-modules/", root / "build/rclone-licenses" / dll_hash))
            except (OSError, ValueError, KeyError, TypeError):
                pass
        for prefix, directory in directories:
            for path in sorted(directory.rglob("*"), key=lambda p: (p.name != "SOURCES.md", p)):
                if path.is_file():
                    self.documents.addItem(prefix + path.relative_to(directory).as_posix(), path)
        self.documents.currentIndexChanged.connect(self.load_document)
        self.load_document()
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.button(QDialogButtonBox.StandardButton.Close).setText("닫기")
        close.rejected.connect(self.reject)
        layout.addWidget(close)

    def load_document(self):
        path = self.documents.currentData()
        if path is None:
            self.text.setPlainText("라이선스 자료가 없습니다. 배포본을 확인하세요.")
            return
        try:
            text = path.read_text(encoding="utf-8-sig")
        except OSError, UnicodeError:
            self.text.setPlainText("라이선스 자료를 읽을 수 없습니다. 배포본을 확인하세요.")
            return
        if self.documents.currentText() == "SOURCES.md":
            self.text.setMarkdown(text)
        else:
            self.text.setPlainText(text)


class SettingsDialog(QDialog):
    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("설정")
        self.settings = settings
        layout = QFormLayout(self)
        self.download = QLineEdit(settings.download_dir)
        layout.addRow("기본 다운로드 폴더", self.download)
        self.config = QLineEdit(settings.rclone_config)
        self.config.setPlaceholderText("비워두면 기본 rclone.conf 사용")
        config_row = QHBoxLayout()
        config_row.addWidget(self.config)
        select = QPushButton("파일 선택...")
        select.clicked.connect(self.select_config)
        config_row.addWidget(select)
        layout.addRow("기존 rclone config (재시작 후 적용)", config_row)
        note = QLabel(
            "config에는 클라우드 접속 토큰이 평문으로 저장됩니다.\n"
            "선택한 파일에는 갱신된 토큰이나 새 계정이 저장될 수 있습니다.\n"
            "config를 바꾸면 재시작 후 사용할 리모트를 계정 관리에서 선택하세요."
        )
        note.setWordWrap(True)
        layout.addRow(note)
        self.transfers, self.retries, self.ttl = QSpinBox(), QSpinBox(), QSpinBox()
        for widget, minimum, maximum, value in [
            (self.transfers, 1, 8, settings.transfers),
            (self.retries, 0, 5, settings.retries),
            (self.ttl, 0, 86400, settings.cache_ttl),
        ]:
            widget.setRange(minimum, maximum)
            widget.setValue(value)
        layout.addRow("동시 전송 수", self.transfers)
        layout.addRow("네트워크 재시도 수", self.retries)
        layout.addRow("목록 캐시 만료 (초)", self.ttl)
        self.log_level = QComboBox()
        self.log_level.addItems(["DEBUG", "INFO", "WARNING", "ERROR"])
        self.log_level.setCurrentText(settings.log_level)
        layout.addRow("앱 로그 수준", self.log_level)
        debug_notice = QLabel("DEBUG 로그에는 파일 경로와 계정명이 포함될 수 있습니다. 공유 전에 확인하세요.")
        debug_notice.setWordWrap(True)
        debug_notice.setVisible(settings.log_level == "DEBUG")
        self.log_level.currentTextChanged.connect(
            lambda level: debug_notice.setVisible(level == "DEBUG")
        )
        layout.addRow(debug_notice)
        self.save_notice = QLabel("진행 중인 작업이 끝나면 동시 전송 수나 rclone config를 변경할 수 있습니다.")
        self.save_notice.setWordWrap(True)
        self.save_notice.hide()
        layout.addRow(self.save_notice)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.save_button = buttons.button(QDialogButtonBox.StandardButton.Ok)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

    def set_save_available(self, available):
        self.save_button.setEnabled(available)
        self.save_notice.setVisible(not available)

    def requires_idle(self):
        return (
            self.transfers.value() != self.settings.transfers
            or self.config.text() != self.settings.rclone_config
        )

    def value(self):
        return replace(
            self.settings,
            download_dir=self.download.text(),
            transfers=self.transfers.value(),
            retries=self.retries.value(),
            cache_ttl=self.ttl.value(),
            log_level=self.log_level.currentText(),
            rclone_config=self.config.text(),
            enabled_remotes=(
                []
                if self.config.text() != self.settings.rclone_config
                else list(self.settings.enabled_remotes)
            ),
        )

    def select_config(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "rclone 설정 파일 선택", filter="rclone config (*.conf);;모든 파일 (*)"
        )
        if path:
            self.config.setText(path)


def show_text(parent, title, text):
    dialog = QDialog(parent)
    dialog.setWindowTitle(title)
    dialog.resize(760, 500)
    layout = QVBoxLayout(dialog)
    edit = QPlainTextEdit()
    edit.setReadOnly(True)
    edit.setPlainText(text)
    layout.addWidget(edit)
    close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
    close.button(QDialogButtonBox.StandardButton.Close).setText("닫기")
    close.rejected.connect(dialog.reject)
    layout.addWidget(close)
    dialog.exec()
