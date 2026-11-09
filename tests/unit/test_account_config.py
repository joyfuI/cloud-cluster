from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from cloud_cluster.accounts import AccountManager
from cloud_cluster.domain import (
    BackendCapabilities,
    BackendError,
    CloudAccount,
    ErrorKind,
    QuotaSnapshot,
    Replica,
)
from cloud_cluster.settings import Settings, SettingsStore
from cloud_cluster.transfers import TaskContext
from cloud_cluster.verification import VerificationStore


@pytest.mark.parametrize("kind", ["object-version", "object-metadata", "path-metadata"])
def test_verification_reuse_survives_connection_settings_and_other_remote_changes(tmp_path, kind):
    config = {
        "a": {"type": "mega", "pass": "private-value"},
        "wrapped": {"type": "alias", "remote": "a:"},
    }

    def rpc(method, **parameters):
        return (
            {"remotes": list(config)}
            if method == "config/listremotes"
            else config[parameters["name"]]
        )

    manager = AccountManager(SimpleNamespace(rpc=rpc), SettingsStore(tmp_path), Settings())
    manager.refresh()
    account = manager.settings.accounts[0]
    replica = Replica(
        account.id,
        "x",
        size=4,
        modified=datetime(2026, 11, 4, tzinfo=UTC),
        object_id="object" if kind != "path-metadata" else "",
        version="v1" if kind == "object-version" else "",
    )
    store = VerificationStore(tmp_path / "content_hashes.db")
    digest = "d" * 64
    assert store.record([(account, replica, digest)]) == 1
    config["a"]["pass"] = "updated-credentials"
    config["extra"] = {"type": "drive"}
    config["wrapped"]["remote"] = "extra:"
    manager.refresh()
    refreshed = next(a for a in manager.settings.accounts if a.id == account.id)
    assert refreshed == account
    assert VerificationStore(store.path).restore(refreshed, (replica,))[0].verified_sha256 == digest


def test_generic_metadata_version_is_not_a_trusted_content_revision():
    from cloud_cluster.rclone import RcloneAdapter

    replica = RcloneAdapter.replica(
        "a", {"Path": "x", "ID": "id", "Metadata": {"version": "user-editable"}}
    )
    assert replica.object_id == "id" and replica.version == ""


@pytest.mark.parametrize(
    "provider, parameters, expected",
    [
        ("drive", {"scope": "drive.readonly"}, {"scope": "drive"}),
        (
            "onedrive",
            {"access_scopes": "Files.Read.All offline_access", "disable_site_permission": "true"},
            {
                "access_scopes": (
                    "Files.Read Files.ReadWrite Files.Read.All Files.ReadWrite.All "
                    "Sites.Read.All offline_access"
                ),
                "disable_site_permission": "true",
            },
        ),
        (
            "hidrive",
            {"scope_access": "ro", "scope_role": "user"},
            {"scope_access": "rw", "scope_role": "user"},
        ),
        ("google photos", {"read_only": "true"}, {"read_only": "false"}),
        ("dropbox", {"client_id": "custom-id"}, {"client_id": "custom-id"}),
        (
            "koofr",
            {"provider": "koofr", "password": "A" * 32},
            {"provider": "koofr", "password": "A" * 32},
        ),
    ],
)
@pytest.mark.parametrize(
    "kwargs, method, options",
    [
        ({}, "config/create", {"all": True}),
        ({"reconnect": True}, "config/update", {"all": True}),
        (
            {"state": "next", "answer": "yes"},
            "config/update",
            {"continue": True, "state": "next", "result": "yes"},
        ),
    ],
)
def test_permissions_and_plain_password_mode_apply_to_each_configuration_step(
    provider, parameters, expected, kwargs, method, options
):
    calls = []

    def run(method, context, **request):
        calls.append((method, request))
        return {"State": "next"}

    backend = SimpleNamespace(rpc=lambda method: {"remotes": []}, run=run)
    manager = AccountManager(backend, SimpleNamespace(check_config=lambda: None), Settings())
    before = dict(parameters)
    assert manager.configure("cloud_1", provider, parameters, **kwargs) == {"State": "next"}
    request = {
        "name": "cloud_1",
        "parameters": {**expected, "config_auth_no_browser": "true"},
        "opt": {"nonInteractive": True, "noOutput": True, "obscure": True, **options},
    }
    if method == "config/create":
        request["type"] = provider
    assert calls == [(method, request)]
    assert parameters == before


@pytest.mark.parametrize(
    "state, answer_is_password, obscure",
    [
        ("*all-set,5,false", True, True),
        ("*all-set,5,true", True, True),
        ("*all-set,5,false", False, False),
        ("config_password", True, False),
    ],
)
def test_only_edited_static_password_answers_are_obscured(state, answer_is_password, obscure):
    calls = []

    def rpc(method, **parameters):
        calls.append((method, parameters))
        return {"obscured": "converted-password"}

    def run(method, context, **parameters):
        calls.append((method, parameters))
        return {}

    manager = AccountManager(
        SimpleNamespace(rpc=rpc, run=run), SimpleNamespace(check_config=lambda: None), Settings()
    )
    manager.configure(
        "saved", "koofr", {}, state=state, answer="answer", answer_is_password=answer_is_password
    )
    assert [method for method, _ in calls] == (
        ["core/obscure", "config/update"] if obscure else ["config/update"]
    )
    if obscure:
        assert calls[0][1] == {"clear": "answer"}
    assert calls[-1][1]["opt"]["result"] == ("converted-password" if obscure else "answer")


@pytest.mark.parametrize(
    "name",
    [
        "개인 드라이브",
        "회사  백업",
        "한",
        "123",
        "local",
        "_remote",
        ".",
        "..",
        "+remote",
        "@remote",
        "user+junkmail@example.com",
        "r-emote-",
        "remote--",
        "remote -",
        "remote - backup",
        "blåbær",
        "chữ Quốc ngữ",
        "숫자Ⅷ²",
        "긴" * 65,
    ],
)
def test_remote_names_survive_configuration_and_whitelist_reload(tmp_path, name):
    remotes, calls = [], []

    def run(method, context, **request):
        calls.append((method, request["name"]))
        if method == "config/create":
            remotes.append(request["name"])
        return {}

    def rpc(method, **parameters):
        return {"remotes": remotes} if method == "config/listremotes" else {"type": "local"}

    backend = SimpleNamespace(rpc=rpc, run=run)
    store = SettingsStore(tmp_path / "app")
    manager = AccountManager(backend, store, Settings())
    manager.configure(name, "local", {})
    manager.configure(name, "local", {}, reconnect=True)
    manager.configure(name, "local", {}, state="next", answer="yes")
    manager.refresh()
    assert not manager.settings.accounts[0].enabled
    manager.set_enabled(name, True)
    account = manager.settings.accounts[0]
    assert calls == [
        (method, name) for method in ("config/create", "config/update", "config/update")
    ]
    assert account.remote == name
    assert account.id == "remote:" + name
    assert account.id != "local"
    loaded = store.load()
    assert loaded.enabled_remotes == [name]
    AccountManager(backend, store, loaded).open()
    assert loaded.accounts == [account]


@pytest.mark.parametrize(
    "name",
    [
        "",
        " ",
        "-remote",
        "remote-",
        " remote",
        "remote ",
        "remote\t",
        "\tremote",
        "rem\note",
        "rem:ote",
        "rem/ote",
        "rem\\ote",
        "[remote]",
        "remote,scope=x",
        "remote!",
        "remote\u00a0name",
        "remote\u2003name",
        "remote\u0301",
        "remote😀",
        "remote\x00",
        "＿remote",
        "remote" + "-" * 10_000 + "!",
    ],
)
def test_invalid_remote_names_are_rejected_without_backend_calls_or_overwriting_settings(
    tmp_path, name
):
    store = SettingsStore(tmp_path / "app")
    manager = AccountManager(None, store, Settings())
    for kwargs in ({}, {"reconnect": True}, {"state": "next"}):
        with pytest.raises(ValueError, match="리모트 이름"):
            manager.configure(name, "local", {}, **kwargs)
    with pytest.raises(ValueError, match="리모트 이름"):
        manager.delete(name)
    store.save(Settings())
    before = store.path.read_bytes()
    with pytest.raises(ValueError, match="리모트 이름"):
        store.save(Settings(enabled_remotes=[name]))
    assert store.path.read_bytes() == before


def test_config_is_account_source_and_external_additions_stay_disabled(tmp_path):
    config = {"saved": "drive", "unused": "onedrive"}

    def rpc(method, **parameters):
        if method == "config/listremotes":
            return {"remotes": list(config)}
        return {"type": config[parameters["name"]]} if method == "config/get" else {}

    store = SettingsStore(tmp_path / "app")
    settings = Settings(enabled_remotes=["saved", "removed"])
    manager = AccountManager(SimpleNamespace(rpc=rpc), store, settings)
    assert manager.open() == ["saved", "unused"]
    assert [(a.id, a.provider, a.enabled) for a in settings.accounts] == [
        ("remote:saved", "drive", True),
        ("remote:unused", "onedrive", False),
    ]
    config["new"] = "dropbox"
    del config["unused"]
    config["saved"] = "alias"
    manager.refresh()
    assert [(a.remote, a.provider, a.enabled) for a in settings.accounts] == [
        ("saved", "alias", True),
        ("new", "dropbox", False),
    ]
    manager.set_enabled("new", True)
    manager.set_enabled("saved", False)
    assert store.load().enabled_remotes == ["new", "removed"]
    loaded = store.load()
    AccountManager(SimpleNamespace(rpc=rpc), store, loaded).open()
    assert [(a.remote, a.enabled) for a in loaded.accounts] == [("saved", False), ("new", True)]


def test_whitelist_save_failure_keeps_current_state(tmp_path, monkeypatch):
    store = SettingsStore(tmp_path / "app")
    backend = SimpleNamespace(
        rpc=lambda method, **kw: (
            {"remotes": ["saved"]} if method == "config/listremotes" else {"type": "local"}
        )
    )
    settings = Settings()
    manager = AccountManager(backend, store, settings)
    manager.refresh()

    def fail(settings):
        raise OSError("read-only")

    monkeypatch.setattr(store, "save", fail)
    with pytest.raises(OSError):
        manager.set_enabled("saved", True)
    assert settings.enabled_remotes == []
    assert not settings.accounts[0].enabled


@pytest.mark.parametrize("invalid", ["bad:name", "no-provider"])
def test_invalid_config_refresh_keeps_previous_accounts(tmp_path, invalid):
    backend = SimpleNamespace(
        rpc=lambda method, **kw: {"remotes": [invalid]} if method == "config/listremotes" else {}
    )
    settings = Settings()
    manager = AccountManager(backend, SettingsStore(tmp_path / "app"), settings)
    with pytest.raises(ValueError):
        manager.refresh()
    assert settings.accounts == []


@pytest.fixture
def deletion_manager(tmp_path):
    store = SettingsStore(tmp_path / "app")
    remotes = {"remove": "local", "keep": "local"}
    calls = []

    def write_config():
        store.config_path.write_text(
            "".join(f"[{name}]\ntype = {provider}\n\n" for name, provider in remotes.items()),
            encoding="utf-8",
        )

    def rpc(method, **parameters):
        calls.append((method, parameters))
        if method == "config/listremotes":
            return {"remotes": list(remotes)}
        if method == "config/get":
            return {"type": remotes[parameters["name"]]}
        if method == "config/delete":
            del remotes[parameters["name"]]
            write_config()
        return {}

    write_config()
    settings = Settings(enabled_remotes=["remove", "keep"])
    store.save(settings)
    manager = AccountManager(SimpleNamespace(rpc=rpc), store, settings)
    manager.open()
    calls.clear()
    return manager, remotes, calls


@pytest.mark.parametrize("enabled", [True, False])
def test_delete_removes_only_selected_remote_and_its_usage(deletion_manager, enabled):
    manager, remotes, calls = deletion_manager
    if not enabled:
        manager.set_enabled("remove", False)
    manager.delete("remove")
    assert remotes == {"keep": "local"}
    assert manager.store.check_config() == ["keep"]
    assert manager.settings.enabled_remotes == ["keep"]
    assert manager.store.load().enabled_remotes == ["keep"]
    assert [(a.remote, a.enabled) for a in manager.settings.accounts] == [("keep", True)]
    assert [call for call in calls if call[0] == "config/delete"] == [
        ("config/delete", {"name": "remove"})
    ]


def test_delete_last_account_allows_empty_config_on_restart(deletion_manager):
    manager, remotes, _ = deletion_manager
    manager.delete("remove")
    manager.delete("keep")
    assert remotes == {}
    assert manager.settings.accounts == []
    assert manager.store.check_config() == []
    loaded = manager.store.load()
    assert loaded.enabled_remotes == []
    assert AccountManager(manager.backend, manager.store, loaded).open() == []


def test_delete_aborts_before_native_change_if_usage_cannot_be_saved(deletion_manager, monkeypatch):
    manager, remotes, calls = deletion_manager
    before = manager.store.config_path.read_bytes()

    def fail(settings):
        raise PermissionError("read-only")

    monkeypatch.setattr(manager.store, "save", fail)
    with pytest.raises(PermissionError):
        manager.delete("remove")
    assert calls == []
    assert remotes == {"remove": "local", "keep": "local"}
    assert manager.store.config_path.read_bytes() == before
    assert manager.settings.enabled_remotes == ["remove", "keep"]
    assert all(a.enabled for a in manager.settings.accounts)


@pytest.mark.parametrize("failure", ["before", "after", "unsaved"])
def test_delete_failure_preserves_verified_state_and_removes_usage(
    deletion_manager, monkeypatch, failure
):
    manager, remotes, _ = deletion_manager
    rpc = manager.backend.rpc

    def fail(method, **parameters):
        if method == "config/delete":
            if failure == "after":
                rpc(method, **parameters)
            elif failure == "unsaved":
                del remotes[parameters["name"]]
                return {}
            raise BackendError(ErrorKind.NETWORK)
        return rpc(method, **parameters)

    monkeypatch.setattr(manager.backend, "rpc", fail)
    with pytest.raises(ValueError if failure == "unsaved" else BackendError):
        manager.delete("remove")
    assert manager.settings.enabled_remotes == ["keep"]
    assert manager.store.load().enabled_remotes == ["keep"]
    if failure == "after":
        assert manager.store.check_config() == ["keep"]
        assert [a.remote for a in manager.settings.accounts] == ["keep"]
    else:
        assert manager.store.check_config() == ["remove", "keep"]
        assert not manager.settings.accounts[0].enabled


def test_delete_rejects_missing_or_corrupt_config_before_changes(deletion_manager):
    manager, _, calls = deletion_manager
    with pytest.raises(ValueError, match="찾을 수 없습니다"):
        manager.delete("missing")
    manager.store.config_path.write_bytes(b"secret-token")
    with pytest.raises(ValueError):
        manager.delete("remove")
    assert manager.store.config_path.read_bytes() == b"secret-token"
    assert calls == []
    assert manager.settings.enabled_remotes == ["remove", "keep"]


@pytest.mark.parametrize("supported", [False, True])
def test_account_quota_only_reads_supported_capacity_without_listing_files(supported):
    calls = []
    account = CloudAccount("a", "a", "drive", enabled=False)
    quota = QuotaSnapshot(total=100, free=0)
    context = TaskContext()

    def features(account, context):
        calls.append(("features", account, context))
        return BackendCapabilities(quota=supported)

    def about(account, context):
        calls.append(("about", account, context))
        return quota

    manager = AccountManager(SimpleNamespace(features=features, about=about), None, Settings())
    assert manager.quota(account, context) == (quota if supported else None)
    assert [name for name, _, _ in calls] == (["features", "about"] if supported else ["features"])
    assert all(a is account and c is context for _, a, c in calls)
