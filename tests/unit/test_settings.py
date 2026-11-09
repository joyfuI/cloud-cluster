import json
from types import SimpleNamespace

import pytest

from cloud_cluster import settings as settings_module
from cloud_cluster.accounts import AccountManager
from cloud_cluster.domain import CloudAccount
from cloud_cluster.settings import Settings, SettingsStore


def test_default_directory_is_original_executable_folder(tmp_path, monkeypatch):
    executable_dir = tmp_path / "portable"
    executable_dir.mkdir()
    monkeypatch.setattr(
        settings_module,
        "__compiled__",
        SimpleNamespace(containing_dir=executable_dir),
        raising=False,
    )
    monkeypatch.setattr(settings_module, "__file__", str(tmp_path / "unpacked/settings.py"))
    monkeypatch.chdir(tmp_path)
    store = SettingsStore()
    store.save(Settings())
    store.check_config()
    assert store.path == executable_dir / "settings.json"
    assert store.config_path == executable_dir / "rclone.conf"
    assert not list(executable_dir.glob("*.dpapi"))


def test_source_default_directory_and_explicit_override(tmp_path, monkeypatch):
    monkeypatch.delattr(settings_module, "__compiled__", raising=False)
    monkeypatch.setattr(
        settings_module, "__file__", str(tmp_path / "src/cloud_cluster/settings.py")
    )
    assert SettingsStore().directory == tmp_path
    monkeypatch.setattr(
        settings_module, "__compiled__", SimpleNamespace(containing_dir=tmp_path), raising=False
    )
    assert SettingsStore(tmp_path / "custom").directory == tmp_path / "custom"


def test_settings_roundtrip_without_key_files(tmp_path):
    store = SettingsStore(tmp_path / "app")
    settings = Settings(accounts=[CloudAccount("a", "drive", "drive")], enabled_remotes=["drive"])
    store.save(settings)
    assert store.load() == settings
    assert store.load().accounts == []
    raw = json.loads(store.path.read_text(encoding="utf-8"))
    assert raw["enabled_remotes"] == ["drive"]
    assert "accounts" not in raw
    store.check_config()
    assert not list(store.directory.glob("*.dpapi"))
    assert "token" not in store.path.read_text(encoding="utf-8").lower()


def test_invalid_value_is_reset_while_other_settings_are_preserved(tmp_path):
    store = SettingsStore(tmp_path / "app")
    store.path.write_text('{"transfers": "invalid", "retries": 4}', encoding="utf-8")
    settings = store.load()
    assert settings.transfers == Settings().transfers
    assert settings.retries == 4
    assert json.loads(store.path.read_text(encoding="utf-8"))["transfers"] == settings.transfers
    assert store.load() == settings


@pytest.mark.parametrize(
    "content",
    [b"RCLONE_ENCRYPT_V0:\nsecret", b"secret-token", b"[remote]\nsecret-token", b"\xff"],
)
def test_encrypted_or_corrupt_config_is_preserved_without_exposing_contents(tmp_path, content):
    store = SettingsStore(tmp_path / "app")
    store.config_path.write_bytes(content)
    old_key = store.directory / "config-key.dpapi"
    old_key.write_bytes(b"old-protected-password")
    with pytest.raises(ValueError) as error:
        AccountManager(None, store, Settings()).open()
    assert "secret" not in str(error.value)
    assert store.config_path.read_bytes() == content
    assert old_key.read_bytes() == b"old-protected-password"


@pytest.mark.parametrize("content", ["", "\n  \n", "# no remotes\n; empty config\n"])
def test_empty_config_is_valid_after_last_account_deletion(tmp_path, content):
    store = SettingsStore(tmp_path / "app")
    store.config_path.write_text(content, encoding="utf-8")
    before = store.config_path.read_bytes()
    backend = SimpleNamespace(rpc=lambda method, **kw: {"remotes": []})
    manager = AccountManager(backend, store, Settings())
    assert store.check_config() == []
    assert manager.open() == []
    assert manager.settings.accounts == []
    assert store.config_path.read_bytes() == before


def test_symbolic_config_is_rejected_even_if_target_is_missing(tmp_path, monkeypatch):
    store = SettingsStore(tmp_path / "app")
    monkeypatch.setattr(
        type(store.config_path), "is_symlink", lambda path: path == store.config_path
    )
    with pytest.raises(ValueError, match="심볼릭 링크"):
        store.check_config()


def test_external_plain_config_is_used_without_copy_or_password(tmp_path):
    source = tmp_path / "original.conf"
    content = "[original]\ntype = local\nvalue = 100%\n"
    source.write_text(content, encoding="utf-8")
    store = SettingsStore(tmp_path / "app")
    store.select_config(str(source))
    calls = []

    def rpc(method, **parameters):
        calls.append((method, parameters))
        if method == "config/listremotes":
            return {"remotes": ["original"]}
        return {"type": "local"} if method == "config/get" else {}

    manager = AccountManager(SimpleNamespace(rpc=rpc), store, Settings())
    assert manager.open() == ["original"]
    assert calls == [
        ("config/setpath", {"path": str(source)}),
        ("config/listremotes", {}),
        ("config/get", {"name": "original"}),
    ]
    assert manager.settings.accounts == [
        CloudAccount(
            "remote:original",
            "original",
            "local",
            enabled=False,
        )
    ]
    assert source.read_text(encoding="utf-8") == content
    assert not list(store.directory.glob("*.dpapi"))
    source.unlink()
    with pytest.raises(FileNotFoundError):
        store.check_config()


@pytest.mark.parametrize(
    "enabled, expected",
    [
        (None, []),
        ("remote", []),
        ([False], []),
        (["remote", "remote"], ["remote"]),
        (["bad:name"], []),
        (
            ["개인 드라이브", {}, "bad:name", "remote", "REMOTE", "개인 드라이브"],
            ["개인 드라이브", "remote", "REMOTE"],
        ),
    ],
)
def test_invalid_enabled_names_are_removed_and_valid_names_kept(tmp_path, enabled, expected):
    store = SettingsStore(tmp_path / "app")
    store.path.write_text(json.dumps({"enabled_remotes": enabled}), encoding="utf-8")
    settings = store.load()
    assert settings.enabled_remotes == expected
    assert json.loads(store.path.read_text(encoding="utf-8"))["enabled_remotes"] == expected
    assert store.load() == settings


def test_unknown_and_runtime_only_keys_are_discarded(tmp_path):
    store = SettingsStore(tmp_path / "app")
    store.path.write_text(
        json.dumps(
            {
                "accounts": [{"remote": "ignored", "enabled": True}],
                "future_option": "ignored",
                "enabled_remotes": ["keep"],
                "transfers": 3,
            }
        ),
        encoding="utf-8",
    )
    settings = store.load()
    assert settings.accounts == []
    assert settings.enabled_remotes == ["keep"]
    assert settings.transfers == 3
    saved = json.loads(store.path.read_text(encoding="utf-8"))
    assert "accounts" not in saved and "future_option" not in saved


@pytest.mark.parametrize(
    "content",
    [b"", b"\xff", b'{"transfers":', b"[]", b"null", b'"secret-token"', b"[" * 1200 + b"]" * 1200],
)
def test_unreadable_json_uses_defaults_and_rewrites_valid_settings(tmp_path, content):
    store = SettingsStore(tmp_path / "app")
    store.path.write_bytes(content)
    assert store.load() == Settings()
    saved = json.loads(store.path.read_text(encoding="utf-8"))
    assert "accounts" not in saved
    assert saved["enabled_remotes"] == []


@pytest.mark.parametrize(
    "name, value",
    [
        ("transfers", True),
        ("transfers", 0),
        ("transfers", 9),
        ("transfers", 2.5),
        ("retries", -1),
        ("retries", 6),
        ("retries", "2"),
        ("cache_ttl", False),
        ("cache_ttl", -1),
        ("cache_ttl", 86401),
        ("log_level", []),
        ("log_level", {}),
        ("log_level", "TRACE"),
        ("download_dir", None),
        ("download_dir", "relative"),
    ],
)
def test_invalid_field_types_and_ranges_use_defaults(tmp_path, name, value):
    store = SettingsStore(tmp_path / "app")
    store.path.write_text(json.dumps({name: value}), encoding="utf-8")
    settings = store.load()
    assert settings == Settings()
    assert json.loads(store.path.read_text(encoding="utf-8"))[name] == getattr(Settings(), name)


@pytest.mark.parametrize("kind", ["missing", "directory", "relative", "type", "symlink"])
def test_invalid_config_reference_is_removed_without_enabling_fallback_remotes(
    tmp_path, monkeypatch, kind
):
    store = SettingsStore(tmp_path / "app")
    store.config_path.write_text("[keep]\ntype = local\n", encoding="utf-8")
    content = store.config_path.read_bytes()
    external = tmp_path / "external.conf"
    value = str(external)
    if kind == "directory":
        external.mkdir()
    elif kind == "relative":
        value = "external.conf"
    elif kind == "type":
        value = [str(external)]
    elif kind == "symlink":
        external.write_text("[external]\ntype = local\n", encoding="utf-8")
        monkeypatch.setattr(type(external), "is_symlink", lambda path: path == external)
    store.path.write_text(
        json.dumps({"rclone_config": value, "enabled_remotes": ["keep"], "transfers": 3}),
        encoding="utf-8",
    )
    settings = store.load()
    assert settings.rclone_config == ""
    assert settings.enabled_remotes == []
    assert settings.transfers == 3
    assert store.config_path.read_bytes() == content
    saved = json.loads(store.path.read_text(encoding="utf-8"))
    assert saved["rclone_config"] == "" and saved["enabled_remotes"] == []


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig"])
def test_valid_settings_are_not_rewritten(tmp_path, encoding):
    store = SettingsStore(tmp_path / "app")
    source = tmp_path / "external.conf"
    source.write_text("[keep]\ntype = local\n", encoding="utf-8")
    store.path.write_text(
        json.dumps({"rclone_config": str(source), "enabled_remotes": ["keep"], "transfers": 8}),
        encoding=encoding,
    )
    before = store.path.read_bytes()
    settings = store.load()
    assert settings.rclone_config == str(source)
    assert settings.enabled_remotes == ["keep"]
    assert settings.transfers == 8
    assert store.path.read_bytes() == before


def test_settings_read_failure_uses_defaults_and_preserves_file(tmp_path, monkeypatch):
    store = SettingsStore(tmp_path / "app")
    store.path.write_text('{"transfers": 4}', encoding="utf-8")
    before = store.path.read_bytes()
    read_text = type(store.path).read_text

    def fail(path, *args, **kwargs):
        if path == store.path:
            raise PermissionError("unreadable")
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(type(store.path), "read_text", fail)
    assert store.load() == Settings()
    assert store.path.read_bytes() == before


def test_cleanup_write_failure_still_returns_usable_settings(tmp_path, monkeypatch):
    store = SettingsStore(tmp_path / "app")
    store.path.write_text('{"transfers": "invalid", "retries": 4}', encoding="utf-8")
    before = store.path.read_bytes()

    def fail(*args):
        raise PermissionError("read-only")

    monkeypatch.setattr(settings_module.os, "replace", fail)
    settings = store.load()
    assert settings.transfers == Settings().transfers
    assert settings.retries == 4
    assert store.path.read_bytes() == before
    assert not list(store.directory.glob("settings-*.tmp"))
