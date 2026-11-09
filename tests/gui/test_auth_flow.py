from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QLineEdit

from cloud_cluster.domain import CloudAccount
from cloud_cluster.gui import dialogs
from cloud_cluster.gui.dialogs import AuthDialog
from cloud_cluster.settings import Settings
from cloud_cluster.transfers import TaskContext

AUTH_STATUS = {"status": "running", "authUrl": "http://127.0.0.1:53682/auth?state=test"}


@pytest.fixture
def auth_flow(qtbot, monkeypatch):
    account = CloudAccount("a", "saved", "drive")
    manager = SimpleNamespace(settings=Settings(accounts=[account]))
    dialog = AuthDialog(manager, [{"Name": "drive"}], account)
    qtbot.addWidget(dialog)
    dialog.timer.setInterval(60_000)
    pending, opened = [], []

    def submit(operation, done=None, failed=None):
        pending.append((done, failed))
        return TaskContext()

    monkeypatch.setattr(dialog.workers, "submit", submit)
    monkeypatch.setattr(dialogs, "message", lambda *args: None)
    monkeypatch.setattr(
        dialogs.QDesktopServices, "openUrl", lambda url: opened.append(url.toString()) or True
    )
    dialog.show()
    yield dialog, pending, opened
    dialog.context = None
    dialog.timer.stop()
    dialog.reject()


def test_browser_button_only_shows_for_active_local_auth_url(auth_flow):
    dialog, pending, opened = auth_flow
    assert dialog.browser_button.isHidden()
    dialog.poll()
    assert not pending
    dialog.timer.start()
    dialog.poll()
    pending[-1][0](AUTH_STATUS)
    assert dialog.browser_button.isVisible()
    assert opened == [AUTH_STATUS["authUrl"]]

    dialog.poll()
    pending[-1][0](AUTH_STATUS)
    assert len(opened) == 1
    dialog.browser_button.click()
    pending[-1][0](AUTH_STATUS)
    assert len(opened) == 2

    for status in (
        {"status": "stopped"},
        {"status": "running", "authUrl": "https://example.com/untrusted"},
    ):
        dialog.poll()
        pending[-1][0](status)
        assert dialog.browser_button.isHidden()
    assert len(opened) == 2

    dialog.poll()
    pending[-1][0](AUTH_STATUS)
    dialog.poll()
    pending[-1][1]("status unavailable")
    assert dialog.browser_button.isHidden()


@pytest.mark.parametrize("ending", ["complete", "failure", "close", "cancel"])
def test_late_auth_status_cannot_reopen_browser(auth_flow, ending):
    dialog, pending, opened = auth_flow
    dialog.timer.start()
    dialog.poll()
    done = pending[-1][0]
    if ending == "complete":
        dialog.response({})
    elif ending == "failure":
        dialog.failed("failed")
    else:
        if ending == "cancel":
            dialog.context = TaskContext()
        dialog.reject()
    done(AUTH_STATUS)
    assert dialog.browser_button.isHidden()
    assert not opened


def test_translated_question_keeps_wire_values_and_displays_korean(auth_flow, monkeypatch):
    dialog, _, _ = auth_flow
    dialog.response(
        {
            "State": "teamdrive_ok",
            "Option": {
                "Name": "config_change_team_drive",
                "Help": "Configure this as a Shared Drive (Team Drive)?\n",
                "Default": False,
                "Examples": [{"Value": "true", "Help": "Yes"}, {"Value": "false", "Help": "No"}],
            },
        }
    )
    assert dialog.question_form.labelForField(dialog.question).text() == "공유 드라이브 설정"
    assert "공유 드라이브(팀 드라이브)에 연결할까요?" in dialog.help.text()
    assert [dialog.question.itemText(i) for i in range(2)] == ["예", "아니요"]
    assert [dialog.question.itemData(i) for i in range(2)] == ["true", "false"]
    sent = []
    monkeypatch.setattr(dialog, "run_step", lambda *args: sent.append(args))
    dialog.question.setCurrentIndex(1)
    dialog.next()
    assert sent == [("teamdrive_ok", "false")]


def test_real_drive_names_are_not_translated(auth_flow):
    dialog, _, _ = auth_flow
    dialog.response(
        {
            "State": "driveid_final",
            "Option": {
                "Name": "config_driveid",
                "Help": "Select drive you want to use",
                "Examples": [{"Value": "opaque-id", "Help": "Yes"}],
            },
        }
    )
    assert dialog.help.text() == "사용할 드라이브를 선택하세요."
    assert dialog.question.itemText(0) == "Yes"
    assert dialog.question.itemData(0) == "opaque-id"


@pytest.mark.parametrize("default", [False, True])
@pytest.mark.parametrize("examples", [False, True])
def test_auth_boolean_question_selects_backend_default(auth_flow, monkeypatch, default, examples):
    dialog, _, _ = auth_flow
    option = {"Name": "config_fs_advanced", "Default": default}
    if examples:
        option["Examples"] = [
            {"Value": "true", "Help": "Yes"},
            {"Value": "false", "Help": "No"},
        ]
    dialog.response({"State": "*all-advanced", "Option": option})
    expected = str(default).lower()
    assert dialog.question.currentData() == expected
    sent = []
    monkeypatch.setattr(dialog, "run_step", lambda *args: sent.append(args))
    dialog.next()
    assert sent == [("*all-advanced", expected)]


@pytest.mark.parametrize("default", ["second", 0])
def test_auth_choice_selects_backend_default(auth_flow, default):
    dialog, _, _ = auth_flow
    dialog.response(
        {
            "State": "next",
            "Option": {
                "Name": "config_driveid",
                "Default": default,
                "Examples": [{"Value": "first"}, {"Value": str(default)}],
            },
        }
    )
    assert dialog.question.currentData() == str(default)


def test_question_steps_only_send_current_answer(auth_flow, monkeypatch):
    dialog, _, _ = auth_flow
    dialog.response({"State": "*all-set,4,false", "Option": {"Name": "user"}})
    assert not dialog.name.isEnabled() and not dialog.provider.isEnabled()
    dialog.question.setText("changed-user")
    sent = []
    monkeypatch.setattr(dialog, "run_step", lambda *args: sent.append(args))
    dialog.next()
    assert sent == [("*all-set,4,false", "changed-user")]


def test_text_question_uses_formatted_backend_default(auth_flow):
    dialog, _, _ = auth_flow
    dialog.response(
        {
            "State": "*all-set,8,true",
            "Option": {"Name": "encoding", "Default": 50438146, "DefaultStr": "Slash,BackSlash"},
        }
    )
    assert dialog.question.text() == "Slash,BackSlash"


@pytest.mark.parametrize("edited", [False, True])
def test_password_answers_distinguish_user_input_from_unchanged_defaults(auth_flow, edited):
    dialog, _, _ = auth_flow
    calls, operations = [], []
    dialog.manager.configure = lambda *args, **kwargs: calls.append(kwargs)
    dialog.workers.submit = lambda operation, *args: operations.append(operation) or TaskContext()
    dialog.response(
        {
            "State": "*all-set,5,false",
            "Option": {"Name": "password", "IsPassword": True, "Default": "stored-value"},
        }
    )
    if edited:
        dialog.question.selectAll()
        dialog.question.insert("new-password")
    dialog.next()
    operations[0](TaskContext())
    assert calls[0]["answer"] == ("new-password" if edited else "stored-value")
    assert calls[0]["answer_is_password"] is edited


@pytest.mark.parametrize("name", ["user", "password", "client_id"])
def test_empty_required_answers_stay_on_current_question(auth_flow, monkeypatch, name):
    dialog, pending, _ = auth_flow
    errors = []
    monkeypatch.setattr(dialogs, "message", lambda parent, error: errors.append(str(error)))
    dialog.response({
        "State": "*all-set,1,false",
        "Option": {"Name": name, "Required": name != "client_id", "IsPassword": name == "password"},
    })
    dialog.next()
    assert errors and not pending
    assert dialog.question_form.labelForField(dialog.question).text().endswith(" *")
    assert dialog.next_button.isEnabled() and dialog.context is None
    if name == "password":
        assert dialog.question.echoMode() == QLineEdit.EchoMode.Password


@pytest.mark.parametrize("default", ["", "custom-value", "known-value"])
def test_nonexclusive_choices_allow_custom_answers(auth_flow, monkeypatch, default):
    dialog, _, _ = auth_flow
    dialog.response({
        "State": "*all-set,1,false",
        "Option": {
            "Name": "region", "Default": default, "Exclusive": False,
            "Examples": [{"Value": "known-value", "Help": "Known region"}],
        },
    })
    assert dialog.question.isEditable()
    sent = []
    monkeypatch.setattr(dialog, "run_step", lambda *args: sent.append(args))
    dialog.next()
    assert sent == [("*all-set,1,false", default)]
    dialog.question.setEditText("another-region")
    dialog.next()
    assert sent[-1] == ("*all-set,1,false", "another-region")


def test_previous_restores_answer_and_only_sends_edited_branch(auth_flow):
    dialog, pending, _ = auth_flow
    first = {"State": "*all-set,0,false", "Option": {"Name": "user"}}
    second = {"State": "*all-set,1,false", "Option": {"Name": "endpoint"}}
    dialog.response(first)
    assert not dialog.previous_button.isEnabled()
    dialog.question.insert("first-user")
    dialog.next()
    dialog.previous()
    assert len(pending) == 1 and not dialog.previous_button.isEnabled()
    pending[-1][0](second)
    assert dialog.previous_button.isEnabled()
    dialog.previous()
    assert dialog.state == first["State"] and dialog.question.text() == "first-user"
    assert not dialog.previous_button.isEnabled() and not dialog.history
    dialog.question.selectAll()
    dialog.question.insert("changed-user")
    dialog.next()
    branch = {"State": "*all-set,3,false", "Option": {"Name": "password", "IsPassword": True}}
    pending[-1][0](branch)
    assert len(dialog.history) == 1
    dialog.previous()
    assert dialog.state == first["State"] and dialog.question.text() == "changed-user"


def test_previous_preserves_password_conversion_after_restoring_input(auth_flow):
    dialog, pending, _ = auth_flow
    dialog.response({
        "State": "*all-set,5,false", "Option": {"Name": "password", "IsPassword": True},
    })
    dialog.question.insert("test-password")
    dialog.next()
    pending[-1][0]({"State": "*all-advanced", "Option": {"Name": "config_fs_advanced", "Default": False}})
    dialog.previous()
    assert dialog.question.text() == "test-password" and dialog.question.isModified()
    assert dialog.question.echoMode() == QLineEdit.EchoMode.Password
    calls, operations = [], []
    dialog.manager.configure = lambda *args, **kwargs: calls.append(kwargs)
    dialog.workers.submit = lambda operation, *args: operations.append(operation) or TaskContext()
    dialog.next()
    operations[0](TaskContext())
    assert calls[0]["answer_is_password"] is True


@pytest.mark.parametrize("ending", ["oauth", "complete", "cancel"])
def test_previous_history_clears_at_authentication_boundary_and_close(auth_flow, ending):
    dialog, pending, _ = auth_flow
    dialog.response({"State": "*all-set,0,false", "Option": {"Name": "user"}})
    dialog.question.insert("test-user")
    dialog.next()
    pending[-1][0]({"State": "*all-advanced", "Option": {"Name": "config_fs_advanced", "Default": False}})
    assert dialog.previous_button.isEnabled()
    if ending == "oauth":
        dialog.response({"State": "*oauth-islocal", "Option": {"Name": "config_is_local", "Default": True}})
    elif ending == "complete":
        dialog.response({})
    else:
        dialog.reject()
    assert not dialog.history and not dialog.previous_button.isEnabled()
    before = len(pending)
    dialog.previous()
    assert len(pending) == before


def test_failed_or_repeated_question_does_not_add_history(auth_flow):
    dialog, pending, _ = auth_flow
    question = {"State": "*all-set,0,false", "Option": {"Name": "user"}}
    dialog.response(question)
    dialog.question.insert("test-user")
    dialog.next()
    pending[-1][1]("test failure")
    assert not dialog.history and not dialog.previous_button.isEnabled()
    dialog.next()
    pending[-1][0]({**question, "Error": "Try again"})
    assert not dialog.history and not dialog.previous_button.isEnabled()


def test_auth_next_and_cancel_do_not_submit_repeated_steps(auth_flow):
    dialog, pending, _ = auth_flow
    dialog.next()
    dialog.next()
    dialog.run_step(None, "")
    assert len(pending) == 1 and not dialog.next_button.isEnabled()
    dialog.reject()
    dialog.reject()
    dialog.next()
    assert len(pending) == 2 and not dialog.cancel_button.isEnabled()
    assert dialog.context.cancelled.is_set()
    pending[0][1]("cancelled")
    assert not dialog.isVisible()


@pytest.mark.parametrize("action", ["next", "poll"])
def test_auth_submit_failure_restores_request_state(auth_flow, monkeypatch, action):
    dialog, _, _ = auth_flow

    def fail(*args, **kwargs):
        raise RuntimeError("worker start failed")

    monkeypatch.setattr(dialog.workers, "submit", fail)
    if action == "next":
        dialog.next()
        assert dialog.context is None and dialog.next_button.isEnabled()
        assert not dialog.timer.isActive()
    else:
        dialog.timer.start()
        dialog.poll()
        assert not dialog.polling and dialog.browser_button.isHidden()
