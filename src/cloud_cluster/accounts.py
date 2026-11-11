"""Account setup reuses rclone's non-interactive configuration state machine."""

from dataclasses import replace
from threading import RLock

from .domain import BackendError, CloudAccount, OperationResult, StepResult
from .quota import QuotaCache
from .replicas import validate_remote_name

READ_WRITE_PARAMETERS = {
    "drive": {"scope": "drive"},
    "onedrive": {
        "access_scopes": (
            "Files.Read Files.ReadWrite Files.Read.All Files.ReadWrite.All "
            "Sites.Read.All offline_access"
        )
    },
    "hidrive": {"scope_access": "rw"},
    "google photos": {"read_only": "false"},
}
# Other CleanUp backends can delete file versions instead of trash.
TRASH_CLEANUP_PROVIDERS = {"drive", "box", "jottacloud", "pikpak", "pcloud"}


class AccountManager:
    def __init__(self, backend, store, settings):
        self.backend, self.store, self.settings = backend, store, settings
        self.quotas = QuotaCache(backend)
        self._lock = RLock()

    def open(self):
        self.store.check_config()
        self.backend.rpc("config/setpath", path=str(self.store.config_path))
        return self.refresh()

    def remotes(self):
        return self.backend.rpc("config/listremotes").get("remotes", [])

    def refresh(self):
        with self._lock:
            accounts = []
            remotes = self.remotes()
            for name in remotes:
                validate_remote_name(name)
                provider = self.backend.rpc("config/get", name=name).get("type")
                if not isinstance(provider, str) or not provider:
                    raise ValueError("rclone config의 제공업체 설정이 잘못되었습니다.")
                accounts.append(
                    CloudAccount(
                        "remote:" + name,
                        name,
                        provider,
                        enabled=name in self.settings.enabled_remotes,
                    )
                )
            self.settings.accounts = accounts
            self.quotas.clear()
            return remotes

    def providers(self):
        return self.backend.rpc("config/providers").get("providers", [])

    def configure(
        self,
        name,
        provider,
        parameters,
        context=None,
        *,
        state=None,
        answer="",
        reconnect=False,
        answer_is_password=False,
    ):
        validate_remote_name(name)
        with self._lock:
            options = {"nonInteractive": True, "noOutput": True, "obscure": True}
            parameters = dict(parameters)
            parameters.update(READ_WRITE_PARAMETERS.get(provider, {}))
            parameters["config_auth_no_browser"] = "true"
            if state is not None:
                if answer_is_password and state.startswith("*all-set,"):
                    # Static config answers are stored directly; OAuth password answers stay plain.
                    answer = self.backend.rpc("core/obscure", clear=answer)["obscured"]
                options.update({"continue": True, "state": state, "result": answer})
            else:
                if not reconnect and name in self.remotes():
                    raise ValueError(
                        "같은 이름의 remote가 이미 있습니다. 계정 목록에서 선택하세요."
                    )
                options["all"] = True
            method = "config/update" if state is not None or reconnect else "config/create"
            request = {"name": name, "parameters": parameters, "opt": options}
            if method == "config/create":
                request["type"] = provider
            result = self.backend.run(method, context, **request)
            self.store.check_config()
            return result

    def set_enabled(self, remote, enabled):
        if not any(a.remote == remote for a in self.settings.accounts):
            raise ValueError("config에서 remote를 찾을 수 없습니다.")
        active = set(self.settings.enabled_remotes)
        if enabled:
            active.add(remote)
        else:
            active.discard(remote)
        updated = replace(self.settings, enabled_remotes=sorted(active))
        self.store.save(updated)
        self.settings.enabled_remotes = updated.enabled_remotes
        self.settings.accounts = [
            replace(a, enabled=enabled) if a.remote == remote else a for a in self.settings.accounts
        ]

    def quota(self, account, context=None):
        return self.quotas.get(account, context, force=True)

    def supports_empty_trash(self, account, context):
        return (
            account.provider in TRASH_CLEANUP_PROVIDERS
            and self.backend.features(account, context).trash_cleanup
        )

    def empty_trash(self, account, context):
        with self._lock:
            context.check()
            if account not in self.settings.accounts or not account.enabled or account.readonly:
                raise ValueError("계정 상태가 변경되었거나 쓰기 가능한 계정이 아닙니다.")
            name = validate_remote_name(account.remote)
            if self.backend.rpc("config/get", name=name).get("type") != account.provider:
                raise ValueError("리모트 설정이 변경되었습니다. 계정 설정을 다시 확인하세요.")
            if not self.supports_empty_trash(account, context):
                raise ValueError("현재 계정 설정에서 휴지통 비우기를 사용할 수 없습니다.")
            action = "휴지통 비우기"
            context.check()
            try:
                self.backend.empty_trash(account, context)
            except BackendError as error:
                context.record_failure(error)
                step = StepResult(
                    account.id, "", action, "error", f"{error} · 웹 휴지통에서 결과를 확인하세요."
                )
            else:
                step = StepResult(account.id, "", action, "ok")
            return OperationResult(action, (step,))

    def delete(self, remote):
        validate_remote_name(remote)
        with self._lock:
            if remote not in self.store.check_config():
                raise ValueError(
                    "config에서 remote를 찾을 수 없습니다. 앱을 다시 시작해 확인하세요."
                )
            self.set_enabled(remote, False)
            try:
                self.backend.rpc("config/delete", name=remote)
            finally:
                # config/delete does not propagate native config-save failures.
                remaining = self.store.check_config()
                if remote not in remaining:
                    self.refresh()
            if remote in remaining or any(a.remote == remote for a in self.settings.accounts):
                raise ValueError(
                    "계정 삭제를 확인하지 못했습니다. 계정은 사용 안 함으로 전환했습니다. "
                    "앱을 다시 시작해 설정을 확인하세요."
                )
