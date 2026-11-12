"""Explicit mutation plans using rclone's native file transfers."""

import ctypes
import os
from collections import deque
from concurrent.futures import as_completed
from concurrent.futures.thread import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from ctypes import wintypes
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from threading import RLock

from .diagnostics import diagnostic_ref, error_summary, logger
from .domain import (
    BackendError,
    CopyRejected,
    Endpoint,
    ErrorKind,
    OperationResult,
    QuotaSnapshot,
    Replica,
    Status,
    StepResult,
)
from .quota import QuotaCache
from .replicas import (
    assess,
    choose_accounts,
    compare,
    effective_capacity,
    is_excluded,
    path_key,
    path_keys_overlap,
    validate_path,
)

_DIRECTORY_EXISTS = object()


@dataclass(frozen=True)
class ExpectedItem:
    endpoint: Endpoint
    version: Replica


@dataclass(frozen=True)
class CopyIntent:
    source: ExpectedItem
    target: Endpoint
    previous: Replica | None

    @property
    def display_endpoint(self) -> Endpoint:
        return self.source.endpoint if self.target.account_id == "local" else self.target


@dataclass(frozen=True)
class RenameIntent:
    source: ExpectedItem
    target: Endpoint


@dataclass(frozen=True)
class MutationPlan:
    label: str
    copies: tuple[CopyIntent, ...] = ()
    deletions: tuple[ExpectedItem, ...] = ()
    mkdirs: tuple[Endpoint, ...] = ()
    rmdirs: tuple[ExpectedItem, ...] = ()
    local_sources: tuple[str, ...] = ()
    renames: tuple[RenameIntent, ...] = ()
    alternates: tuple[CopyIntent, ...] = ()
    precise: bool = False
    move_targets: tuple[ExpectedItem, ...] = ()
    move_directories: tuple[Endpoint, ...] = ()

    @property
    def write_endpoints(self):
        return (
            *self.mkdirs,
            *(copy.target for copy in (*self.copies, *self.alternates)),
            *(item.target for item in self.renames),
            *(item.endpoint for item in (*self.deletions, *self.rmdirs)),
        )

    @property
    def endpoints(self):
        return (
            *(copy.source.endpoint for copy in self.copies),
            *(copy.target for copy in (*self.copies, *self.alternates)),
            *(item.endpoint for item in (*self.deletions, *self.rmdirs)),
            *self.mkdirs,
            *(item.source.endpoint for item in self.renames),
            *(item.target for item in self.renames),
            *(item.endpoint for item in self.move_targets),
            *self.move_directories,
        )

    @property
    def summary(self):
        lines = [self.label]
        for item in self.renames:
            source = item.source.endpoint
            lines.append(f"이름 변경: {source.account_id}:{source.path} → {item.target.path}")
        for item in self.copies:
            target = item.target
            destination = (
                str(Path(target.fs) / target.path)
                if target.account_id == "local"
                else f"{target.account_id}:{target.path}"
            )
            suffix = " (기존 파일 덮어쓰기)" if item.previous else ""
            lines.append(
                f"복사: {item.source.endpoint.account_id}:{item.source.endpoint.path} → "
                f"{destination}{suffix}"
            )
        if self.alternates:
            lines.append("새 파일의 공간 예약이나 쓰기 전 전송이 거부되면 아래 대체 후보를 시도합니다.")
            for item in self.alternates:
                lines.append(f"대체 후보 · {item.target.account_id}:/{item.target.path}")
        if self.local_sources:
            lines.append("전송 응답이 불명확하면 원래 대상의 내용을 확인할 수 있습니다.")
        for item in (*self.deletions, *self.rmdirs):
            lines.append(f"삭제: {item.endpoint.account_id}:{item.endpoint.path}")
        for item in self.mkdirs:
            lines.append(f"폴더 생성: {item.account_id}:{item.path}")
        if self.local_sources or any(item.previous for item in self.copies) or self.deletions:
            lines.append("필요한 경우 실제 바이트를 읽어 검증합니다.")
            maximum_read = 2 * sum(item.source.version.size or 0 for item in self.copies)
            if maximum_read:
                lines.append(
                    f"추가 검증 읽기 상한(로컬 포함): {maximum_read:,}바이트. 서버 복사 미지원 시 전송량도 증가합니다."
                )
        return "\n".join(lines)


def local_endpoint(path: Path) -> Endpoint:
    path = path.absolute()
    validate_path(path.name)
    return Endpoint(path.parent.as_posix(), path.name)


@contextmanager
def locked_source(path):
    if os.name != "nt":
        with Path(path).open("rb"):
            yield
        return
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateFileW(str(Path(path).absolute()), 0x80000000, 1, None, 3, 0x80, None)
    if handle == wintypes.HANDLE(-1).value:
        raise OSError("원본 파일의 변경을 막는 잠금을 확보할 수 없습니다.")
    try:
        yield
    finally:
        kernel.CloseHandle(handle)


class _UploadPreparation:
    def __init__(self, pool, directories=None):
        self.pool = pool
        self.directories = {} if directories is None else directories
        self.capabilities = {}
        self.position = ""

    def read(self, accounts, lookup, context, action):
        def describe():
            if context:
                context.check()
                labels = ", ".join(account.label for account in pending.values())
                detail = " · ".join(filter(None, (self.position, action)))
                context.describe(f"{detail} · 응답 대기: {labels}" if labels else detail)

        if context:
            context.check()
        futures = {self.pool.submit(lookup, account): account for account in accounts}
        pending = futures.copy()
        try:
            describe()
            for future in as_completed(futures):
                pending.pop(future)
                if pending:
                    describe()
            if context:
                context.check()
            return [future.result() for future in futures]
        finally:
            for future in pending:
                future.cancel()


class OperationEngine:
    def __init__(self, backend, accounts, *, quotas=None, verification_store=None):
        self.backend, self.accounts = backend, list(accounts)
        self.quotas = quotas if quotas is not None else QuotaCache(backend)
        self.verification_store = verification_store
        self._guard = RLock()
        self._repair_planning = RLock()
        self._active = set()
        self._reserved = {}

    def account(self, account_id):
        return next(a for a in self.accounts if a.id == account_id)

    def quota(self, account, context=None, *, capabilities=None):
        quota = self.quotas.get(account, context, capabilities=capabilities) or QuotaSnapshot()
        with self._guard:
            return replace(quota, reserved=self._reserved.get(account.id, 0))

    def capacity(self, quotas):
        with self._guard:
            current = tuple(
                QuotaSnapshot(q.total, q.free, q.reserved + self._reserved.get(account_id, 0))
                for account_id, q in quotas.items()
            )
        return effective_capacity(current)

    def endpoint(self, replica):
        endpoint = Endpoint(self.account(replica.account_id).fs, replica.path, replica.account_id)
        self.validate_endpoint(endpoint)
        return endpoint

    def validate_endpoint(self, endpoint):
        validate_path(endpoint.path)
        account = next((a for a in self.accounts if a.id == endpoint.account_id), None)
        if account and is_excluded(account, endpoint.path):
            raise ValueError("제공업체 제외 경로는 사용할 수 없습니다.")
        return account

    @staticmethod
    def _index_listing(replicas):
        entries = {}
        for replica in replicas:
            entries.setdefault(path_key(replica.path), []).append(replica)
        return entries

    def remember_listings(self, snapshot, directories):
        for listing in snapshot.listings:
            if listing.state == "ok":
                directories[listing.account_id, snapshot.path, False] = self._index_listing(
                    listing.replicas
                )

    def _inspect_directory(self, account, path, context, *, show_hash, directories):
        key = (account.id, path, show_hash)
        if directories is not None and key in directories:
            entries = directories[key]
            if entries is not _DIRECTORY_EXISTS:
                return entries
        elif path and self.backend.stat(Endpoint(account.fs, path, account.id), context) is None:
            if directories is not None:
                directories[key] = None
            return None
        entries = self._index_listing(
            self.backend.list_dir(account, path, context, show_hash=show_hash)
        )
        if directories is not None:
            directories[key] = entries
        return entries

    def inspect(self, endpoint, context=None, *, from_listing=False, directories=None):
        account = self.validate_endpoint(endpoint)
        if account is not None:
            parent = str(PurePosixPath(endpoint.path).parent)
            parent = "" if parent == "." else parent
            entries = self._inspect_directory(
                account, parent, context, show_hash=from_listing, directories=directories
            )
            if entries is None:
                return None
            matches = entries.get(path_key(endpoint.path), ())
            if len(matches) > 1 or (matches and matches[0].path != endpoint.path):
                raise BackendError(
                    ErrorKind.CHANGED, "대소문자/Unicode 충돌 또는 중복 객체가 있습니다."
                )
            if from_listing:
                return matches[0] if matches else None
        return self.backend.stat(endpoint, context)

    def expected(self, endpoint, context=None, *, directories=None):
        item = self.inspect(endpoint, context, directories=directories)
        if item is None:
            raise BackendError(ErrorKind.CHANGED, "원본 항목이 없습니다.")
        return ExpectedItem(endpoint, item)

    def assert_expected(self, item, context=None, *, from_listing=False, directories=None):
        actual = self.inspect(
            item.endpoint, context, from_listing=from_listing, directories=directories
        )
        if actual is None or actual.fingerprint != item.version.fingerprint:
            raise BackendError(
                ErrorKind.CHANGED, "승인한 이후 항목이 달라졌습니다. 다시 확인하세요."
            )
        return actual

    def plan_upload(self, local_file, target_path, context=None, *, directories=None):
        with ThreadPoolExecutor(max_workers=4, thread_name_prefix="upload-plan") as pool:
            return self._plan_upload(
                local_file, target_path, context, _UploadPreparation(pool, directories)
            )

    def plan_uploads(self, paths, folder, context):
        context.set_phase("stage=upload_preparation")
        plans = []
        targets = set()
        label = "파일 업로드"

        def check_target(path):
            key = path_key(path)
            if key in targets:
                raise ValueError(f"업로드할 대상 경로가 중복됩니다. /{path}")
            targets.add(key)

        with ThreadPoolExecutor(max_workers=4, thread_name_prefix="upload-plan") as pool:
            preparation = _UploadPreparation(pool)
            for index, raw in enumerate(paths, 1):
                context.check()
                path = Path(raw)
                position = f"항목 {index:,}/{len(paths):,} · {path.name}"
                target = "/".join(filter(None, (folder, path.name)))
                if path.is_symlink() or path.is_junction():
                    raise ValueError("심볼릭 링크와 정션은 업로드할 수 없습니다.")
                if not path.is_dir():
                    check_target(target)
                    preparation.position = position
                    plans.append(self._plan_upload(path, target, context, preparation))
                    continue
                label = "폴더 업로드"
                context.describe(f"{position} · 로컬 폴더 확인")
                for parent, dirs, files in os.walk(path, followlinks=False):
                    context.check()
                    context.describe(f"{position} · 로컬 폴더 확인 · {Path(parent).name}")
                    suffix = Path(parent).relative_to(path.parent).as_posix()
                    target = "/".join(filter(None, (folder, suffix)))
                    check_target(target)
                    for name in dirs:
                        context.check()
                        child = Path(parent) / name
                        if child.is_symlink() or child.is_junction():
                            raise ValueError("폴더 안에 심볼릭 링크나 정션이 있습니다.")
                    preparation.position = f"{position} · /{target}"
                    plans.append(self.plan_mkdir(target, context, preparation=preparation))
                    for file_index, name in enumerate(sorted(files), 1):
                        file_target = f"{target}/{name}"
                        check_target(file_target)
                        preparation.position = (
                            f"/{target} · 파일 {file_index:,}/{len(files):,} · {name}"
                        )
                        plans.append(
                            self._plan_upload(
                                Path(parent) / name, file_target, context, preparation
                            )
                        )
        context.check()
        return MutationPlan(
            label,
            copies=tuple(copy for plan in plans for copy in plan.copies),
            mkdirs=tuple(dict.fromkeys(endpoint for plan in plans for endpoint in plan.mkdirs)),
            local_sources=tuple(source for plan in plans for source in plan.local_sources),
            alternates=tuple(copy for plan in plans for copy in plan.alternates),
        )

    def plan_upload_directories(self, paths, context):
        context.set_phase("stage=upload_preparation")
        with ThreadPoolExecutor(max_workers=4, thread_name_prefix="upload-plan") as pool:
            preparation = _UploadPreparation(pool)
            mkdirs = []
            for path in paths:
                context.check()
                preparation.position = f"/{path}"
                mkdirs.extend(self.plan_mkdir(path, context, preparation=preparation).mkdirs)
        return MutationPlan("폴더 업로드", mkdirs=tuple(mkdirs))

    def _plan_upload(self, local_file, target_path, context, preparation):
        if context:
            context.check()
            context.describe(f"{preparation.position or Path(local_file).name} · 로컬 파일 확인")
        validate_path(target_path)
        local_file = Path(local_file).absolute()
        if local_file.is_symlink() or not local_file.is_file():
            raise ValueError("일반 로컬 파일만 업로드할 수 있습니다.")
        for parent in local_file.parents:
            if parent.is_symlink() or parent.is_junction():
                raise ValueError("링크 또는 junction을 경유하는 원본은 업로드하지 않습니다.")
        source = self.expected(local_endpoint(local_file), context)
        directories = preparation.directories
        accounts = self._write_accounts(target_path)

        def lookup(account):
            try:
                if context:
                    context.check()
                if account.id not in preparation.capabilities:
                    preparation.capabilities[account.id] = self.backend.features(account, context)
                cap = preparation.capabilities[account.id]
                self._inspect_directory(
                    account, "", context, show_hash=False, directories=directories
                )
                old = self.inspect(
                    Endpoint(account.fs, target_path, account.id), context, directories=directories
                )
                quota = self.quota(account, context, capabilities=cap)
            except BackendError as error:
                if error.kind == ErrorKind.CANCELLED:
                    raise
                return None
            return account, cap, quota, old

        quotas, caps, available, previous = {}, {}, [], {}
        for result in preparation.read(accounts, lookup, context, "대상 확인"):
            if result is None:
                continue
            account, cap, quota, old = result
            available.append(account)
            caps[account.id] = cap
            quotas[account.id] = quota
            previous[account.id] = old
        existing = {account_id for account_id, item in previous.items() if item is not None}
        targets = choose_accounts(
            available,
            quotas,
            source.version.size or 0,
            capabilities=caps,
            existing=existing,
            include_alternates=True,
        )
        copies = tuple(
            CopyIntent(source, Endpoint(a.fs, target_path, a.id), previous[a.id]) for a in targets[:2]
        )
        if any(copy.previous and copy.previous.is_dir for copy in copies):
            raise ValueError("대상에 같은 이름의 폴더가 있습니다.")
        alternates = tuple(
            CopyIntent(source, Endpoint(a.fs, target_path, a.id), None)
            for a in targets[2:]
            if previous[a.id] is None
        )
        return MutationPlan(
            "파일 업로드", copies, local_sources=(str(local_file),), alternates=alternates
        )

    def plan_download(self, replica, local_file, context=None):
        target = local_endpoint(Path(local_file))
        source = self.expected(self.endpoint(replica), context)
        previous = self.inspect(target, context)
        if previous is not None and previous.is_dir:
            raise ValueError("대상에 같은 이름의 폴더가 있습니다.")
        return MutationPlan("다운로드", (CopyIntent(source, target, previous),))

    def plan_replicate(self, replica, target_account, context=None):
        if (
            target_account.id == replica.account_id
            or not target_account.enabled
            or target_account.readonly
        ):
            raise ValueError("다른 사용 가능한 쓰기 계정을 선택하세요.")
        source = self.expected(self.endpoint(replica), context)
        if source.version.is_dir:
            raise ValueError("파일 복제본을 선택하세요.")
        target = Endpoint(target_account.fs, replica.path, target_account.id)
        old = self.inspect(target, context)
        if old and old.is_dir:
            raise ValueError("대상에 같은 이름의 폴더가 있습니다.")
        return MutationPlan("복제", (CopyIntent(source, target, old),))

    def _write_accounts(self, path):
        return [
            account
            for account in self.accounts
            if account.enabled and not account.readonly and not is_excluded(account, path)
        ]

    def plan_mkdir(self, path, context=None, *, directories=None, preparation=None):
        validate_path(path)
        if preparation is not None:
            directories = preparation.directories

        def lookup(account):
            if context:
                context.check()
            endpoint = Endpoint(account.fs, path, account.id)
            old = self.inspect(endpoint, context, directories=directories)
            if old and not old.is_dir:
                raise ValueError("대상에 같은 이름의 파일이 있습니다.")
            if preparation is not None:
                key = (account.id, path, False)
                if old is None:
                    directories[key] = None
                else:
                    directories.setdefault(key, _DIRECTORY_EXISTS)
            return endpoint if old is None else None

        accounts = self._write_accounts(path)
        results = (
            preparation.read(accounts, lookup, context, "폴더 생성 위치 확인")
            if preparation is not None
            else map(lookup, accounts)
        )
        targets = tuple(endpoint for endpoint in results if endpoint is not None)
        if not targets and preparation is None:
            raise ValueError("생성할 폴더 또는 쓰기 가능한 계정이 없습니다.")
        return MutationPlan("새 폴더", mkdirs=targets)

    def _phase(self, context, action, endpoint, *, source=None, write_result=None):
        def fields(item, prefix=""):
            provider = (
                "local" if item.account_id == "local" else self.account(item.account_id).provider
            )
            return (
                f" {prefix}provider={provider!r} {prefix}account={diagnostic_ref(item.account_id)}"
                f" {prefix}path={diagnostic_ref(path_key(item.path))}"
            )

        phase = f"stage={action!r}" + fields(endpoint)
        if source is not None:
            phase += fields(source, "source_")
        if write_result is not None:
            phase += f" write_result={write_result}"
        context.set_phase(phase)

    def _describe(
        self, context, action, endpoint, size=None, *,
        source=None, write_result=None, note="", display=None,
    ):
        if context:
            self._phase(context, action, endpoint, source=source, write_result=write_result)
            display = endpoint if display is None else display
            label = (
                self.account(display.account_id).label
                if display.account_id != "local"
                else "로컬"
            )
            context.describe(" · ".join(filter(None, (action, label, note, f"/{display.path}"))), size)

    def plan_repair(self, entry, context=None):
        status = entry.assessment.status
        if status not in {Status.DIRECTORY, Status.UNDER}:
            raise ValueError("이 문제는 복제본 관리에서 확인하세요.")
        validate_path(entry.path)
        accounts = [a for a in self.accounts if a.enabled and not is_excluded(a, entry.path)]
        replicas, caps = {}, {}
        for account in accounts:
            endpoint = Endpoint(account.fs, entry.path, account.id)
            self._describe(context, "상태 조회", endpoint)
            caps[account.id] = self.backend.features(account, context)
            item = self.inspect(endpoint, context)
            if item is not None:
                replicas[account.id] = item
        if not replicas:
            raise ValueError("항목이 없어졌습니다. 다시 검사하세요.")
        current = assess(
            tuple(replicas.values()),
            complete=True,
            expected_dirs={a.id for a in accounts if caps[a.id].empty_directories},
        )
        if current.status != status:
            raise ValueError(f"상태가 {current.status.value}(으)로 달라졌습니다. 다시 검사하세요.")
        if status == Status.DIRECTORY:
            targets = tuple(
                Endpoint(a.fs, entry.path, a.id)
                for a in accounts
                if not a.readonly and caps[a.id].empty_directories and a.id not in replicas
            )
            if not targets:
                raise ValueError("누락된 폴더를 생성할 수 있는 쓰기 계정이 없습니다.")
            return MutationPlan("누락된 폴더 생성", mkdirs=targets)

        source = next(iter(replicas.values()))
        if source.size is None:
            raise ValueError("파일 크기가 확인되지 않아 복제 계정을 선택할 수 없습니다.")
        candidates, quotas = [], {}
        for account in accounts:
            if account.readonly or account.id in replicas:
                continue
            limit = caps[account.id].max_file_size
            if limit is not None and source.size > limit:
                continue
            try:
                self._describe(context, "용량 조회", Endpoint(account.fs, entry.path, account.id))
                quotas[account.id] = self.quota(account, context, capabilities=caps[account.id])
            except BackendError as error:
                if error.kind == ErrorKind.CANCELLED:
                    raise
                continue
            candidates.append(account)
        target = choose_accounts(candidates, quotas, source.size, required=1)[0]
        return MutationPlan(
            "추천 계정으로 복제",
            copies=(
                CopyIntent(
                    ExpectedItem(self.endpoint(source), source),
                    Endpoint(target.fs, entry.path, target.id),
                    None,
                ),
            ),
        )

    def _walk(self, replica, context, *, directories, confirmed=False):
        endpoint = self.endpoint(replica)
        if confirmed:
            self._describe(context, "삭제 대상 확인", endpoint)
            source = ExpectedItem(endpoint, replica)
            # Confirm folder identity from a fresh listing before reading its children.
            self.assert_expected(
                source,
                context,
                from_listing=replica.is_dir,
                directories=None if replica.is_dir else directories,
            )
        else:
            source = self.expected(endpoint, context, directories=directories)
        result = [source]
        if replica.is_dir:
            account = self.account(replica.account_id)
            if confirmed:
                self._describe(context, "하위 항목 조회", endpoint)
            for child in self.backend.list_dir(account, replica.path, context):
                result.extend(
                    self._walk(child, context, directories=directories, confirmed=confirmed)
                )
        return result

    def plan_delete(self, replicas, context=None, *, confirmed=False):
        items, directories = {}, {}
        for replica in replicas:
            if self.account(replica.account_id).readonly:
                raise ValueError("읽기 전용 계정의 파일은 삭제할 수 없습니다.")
            for item in self._walk(replica, context, directories=directories, confirmed=confirmed):
                key = (item.endpoint.fs, item.endpoint.path)
                previous = items.get(key)
                if (
                    confirmed
                    and previous
                    and previous.version.fingerprint != item.version.fingerprint
                ):
                    raise BackendError(ErrorKind.CHANGED, "조회 중 삭제 대상이 달라졌습니다.")
                items[key] = item
        files = tuple(item for item in items.values() if not item.version.is_dir)
        dirs = tuple(
            sorted(
                (item for item in items.values() if item.version.is_dir),
                key=lambda item: item.endpoint.path.count("/"),
                reverse=True,
            )
        )
        return MutationPlan("삭제", deletions=files, rmdirs=dirs)

    def execute_delete(self, replicas, context, *, approved=False):
        if not approved:
            raise PermissionError("삭제에 대한 사용자 승인이 필요합니다.")
        context.check()
        context.describe("삭제 대상 확인 중")
        replicas = tuple(replicas)
        plan = self.plan_delete(replicas, context, confirmed=True)
        for replica in replicas:
            if replica.is_dir:
                endpoint = self.endpoint(replica)
                self._describe(context, "하위 조회 후 폴더 확인", endpoint)
                self.assert_expected(ExpectedItem(endpoint, replica), context, from_listing=True)
        result = self.execute(plan, context, approved=approved)
        total = len(plan.deletions) + len(plan.rmdirs)
        deleted = sum(step.state == "ok" for step in result.steps)
        detail = f"삭제 {deleted:,}/{total:,}개 완료"
        if deleted < total:
            detail += f" · 중단 · {context.detail} · {result.steps[-1].message}"
        context.describe(detail)
        return replace(result, affected_paths=tuple(endpoint.path for endpoint in plan.endpoints))

    def plan_move(self, replicas, target_path, context=None, *, copy_only=False, directories=None):
        validate_path(target_path)
        replicas = tuple(replicas)
        if replicas and path_key(replicas[0].path) == path_key(target_path):
            if copy_only:
                raise ValueError("원본과 다른 대상 경로가 필요합니다.")
            renames = []
            name = PurePosixPath(target_path).name
            for replica in replicas:
                if path_key(replica.path) != path_key(target_path):
                    raise ValueError("같은 논리 경로의 항목만 함께 이름을 변경할 수 있습니다.")
                if replica.name == name:
                    continue
                source = self.endpoint(replica)
                target = replace(source, path=PurePosixPath(source.path).with_name(name).as_posix())
                self.validate_endpoint(target)
                renames.append(RenameIntent(ExpectedItem(source, replica), target))
            if not renames:
                raise ValueError("원본과 다른 대상 이름이 필요합니다.")
            return MutationPlan("대소문자 이름 변경", renames=tuple(renames))
        copies, files, dirs, mkdirs = [], [], [], []
        if directories is None:
            directories = {}
        for replica in replicas:
            if path_key(target_path).startswith(path_key(replica.path) + "/"):
                raise ValueError("폴더를 자신의 하위 경로로 이동할 수 없습니다.")
            if path_key(replica.path) == path_key(target_path):
                raise ValueError(
                    "대소문자만 변경하려면 먼저 다른 고유 이름으로 변경한 뒤 원하는 이름을 사용하세요."
                )
            for item in self._walk(replica, context, directories=directories):
                suffix = item.endpoint.path[len(replica.path) :]
                target = Endpoint(item.endpoint.fs, target_path + suffix, item.endpoint.account_id)
                previous = self.inspect(target, context, directories=directories)
                if previous is not None:
                    raise ValueError("대상 경로에 이미 항목이 있습니다. 다른 경로를 선택하세요.")
                if item.version.is_dir:
                    mkdirs.append(target)
                    dirs.append(item)
                else:
                    copies.append(CopyIntent(item, target, None))
                    files.append(item)
        return MutationPlan(
            "복사" if copy_only else "이동",
            tuple(copies),
            () if copy_only else tuple(files),
            tuple(mkdirs),
            ()
            if copy_only
            else tuple(sorted(dirs, key=lambda item: item.endpoint.path.count("/"), reverse=True)),
        )

    @contextmanager
    def _claim(self, plan):
        keys = {(e.fs.casefold(), path_key(e.path)) for e in plan.endpoints}
        with self._guard:
            if any(
                fs == other_fs and path_keys_overlap(path, other)
                for fs, path in keys
                for other_fs, other in self._active
            ):
                raise ValueError("같은 경로 또는 하위 경로의 다른 작업이 진행 중입니다.")
            self._active.update(keys)
        try:
            yield
        finally:
            with self._guard:
                self._active.difference_update(keys)

    def _verify(self, source, target, context, *, precise=False):
        right = self.backend.stat(target, context)
        if right is None:
            raise BackendError(ErrorKind.UNKNOWN, "전송 결과를 확인할 수 없습니다.")
        left = replace(source.version, verified_sha256="")
        comparison = compare(left, right)
        if comparison == "different":
            raise BackendError(
                ErrorKind.CHANGED, "복사한 내용이 원본과 다릅니다. 파일을 보존했습니다."
            )
        digest = ""
        if precise and comparison != "same":
            current = self.backend.stat(source.endpoint, context)
            if current is None or current.fingerprint != left.fingerprint:
                raise BackendError(ErrorKind.CHANGED, "내용 검증 도중 파일이 바뀌었습니다.")
            a = self.backend.hash_file(source.endpoint, context)
            b = self.backend.hash_file(target, context)
            if a != b:
                raise BackendError(ErrorKind.CHANGED, "SHA-256 내용 검증에 실패했습니다.")
            current = self.backend.stat(target, context)
            if current is None or current.fingerprint != right.fingerprint:
                raise BackendError(ErrorKind.CHANGED, "내용 검증 도중 파일이 바뀌었습니다.")
            digest = b
        self.assert_expected(source, context)
        return right, digest

    def _check_copy_target(self, copy, context, *, directories=None):
        current = self.inspect(copy.target, context, directories=directories)
        if (None if current is None else current.fingerprint) != (
            None if copy.previous is None else copy.previous.fingerprint
        ):
            raise BackendError(ErrorKind.CHANGED, "대상 항목이 승인 후 달라졌습니다.")

    def _copy(self, copy, context, *, upload=False, single_attempt=False):
        self._phase(context, "전송 직전 원본 확인", copy.source.endpoint, write_result="not_attempted")
        self.assert_expected(copy.source, context)
        self._phase(context, "전송 직전 대상 확인", copy.target, write_result="not_attempted")
        self._check_copy_target(copy, context)
        self._phase(context, "전송", copy.target, source=copy.source.endpoint)
        try:
            self.backend.copy_file(
                copy.source.endpoint,
                copy.target,
                replace(context, retries=0) if single_attempt else context,
            )
        except BackendError as error:
            if error.kind == ErrorKind.CANCELLED or (upload and isinstance(error, CopyRejected)):
                raise
            check_failure = ""
            if upload:
                self._describe(
                    context,
                    f"전송 결과 확인 · {error.kind.value}",
                    copy.target,
                    source=copy.source.endpoint,
                )
                try:
                    verified = self._verify(copy.source, copy.target, context, precise=True)
                    return error.kind.value, verified
                except BackendError as check_error:
                    if check_error.kind == ErrorKind.CANCELLED:
                        raise
                    check_failure = f" 재확인 실패 · {check_error}"
            # A failed response may follow a committed write; never blindly create again.
            raise BackendError(
                error.kind,
                f"{error.kind.value} · 전송 결과 확인 필요: {copy.target.path}. "
                f"현재 대상 상태를 다시 확인하세요.{check_failure}",
                diagnostic=f"{error.diagnostic} write_result=unconfirmed".strip(),
            ) from None
        return "", None

    def _settle_copy_space(self, copy, held, *, written):
        if copy.target.account_id == "local":
            return
        account_id, size = copy.target.account_id, copy.source.version.size
        with self._guard:
            if written:
                self.quotas.consume(self.account(account_id), size)
            self._reserved[account_id] -= size
            held[account_id] -= size

    def _execute_copy(self, copy, context, held, results, *, position, precise, alternates, reason):
        upload = alternates is not None
        reserved_copy = copy
        while True:
            context.check()
            target = copy.target
            display = copy.display_endpoint
            label = self.account(target.account_id).label if target.account_id != "local" else "로컬"
            with ExitStack() as stack:
                reservation = held
                try:
                    if copy is not reserved_copy:
                        try:
                            _, reservation, _ = stack.enter_context(
                                self._reserve(MutationPlan("파일 업로드", (copy,)), context, note=reason)
                            )
                        except BackendError as error:
                            if error.kind != ErrorKind.QUOTA:
                                raise
                            raise CopyRejected(error.kind) from None
                    action = "대체 업로드" if reason else "전송"
                    self._describe(
                        context, f"{action} · {position}", target, copy.source.version.size,
                        note=reason, display=display,
                    )
                    # A final preflight rejection proves no write only on the first attempt.
                    single_attempt = (
                        bool(alternates)
                        and copy.previous is None
                        and self.account(target.account_id).provider == "box"
                    )
                    recovered, verified = self._copy(
                        copy, context, upload=upload, single_attempt=single_attempt
                    )
                except CopyRejected as error:
                    if copy is reserved_copy:
                        self._settle_copy_space(copy, held, written=False)
                    rejection = f"{label} · {error.kind.value}"
                    if not alternates or copy.previous is not None:
                        raise CopyRejected(
                            error.kind, rejection, diagnostic=error.diagnostic
                        ) from None
                    results.append(
                        StepResult(target.account_id, target.path, "전송 거부", "skipped", error.kind.value)
                    )
                    reason = rejection
                    copy = alternates.popleft()
                    continue
                self._settle_copy_space(copy, reservation, written=True)
                if recovered:
                    note = " · ".join(filter(None, (reason, recovered)))
                    results.append(
                        StepResult(target.account_id, target.path, "전송 결과 확인", "ok", note)
                    )
                    return target, *verified
                results.append(
                    StepResult(display.account_id, display.path, action, "ok", reason)
                )
                self._describe(
                    context,
                    f"{'대체 업로드 검증' if reason else '전송 검증'} · {position}",
                    target,
                    write_result="reported_success",
                    note=reason,
                    display=display,
                )
                verified = self._verify(
                    copy.source, target, context, precise=bool(copy.previous or precise)
                )
                results.append(StepResult(display.account_id, display.path, "전송 검증", "ok"))
                return target, *verified

    def _cache_upload_verification(self, path, copies, context):
        store = self.verification_store
        source = copies[0][0]
        source_hashes = set(store.content_hashes(source.version))
        proven = [
            (target, replica, digest)
            for original, target, replica, digest in copies
            if original == source
            and (
                digest
                or (
                    source.version.size is not None
                    and source.version.size == replica.size
                    and source_hashes.intersection(store.content_hashes(replica))
                )
            )
        ]
        if not proven:
            return

        uploaded = {target.account_id: replica for _, target, replica, _ in copies}
        replicas = []
        for account in self.accounts:
            if not account.enabled or is_excluded(account, path):
                continue
            replica = uploaded.get(account.id)
            if replica is None:
                replica = self.inspect(Endpoint(account.fs, path, account.id), context)
            if replica is not None:
                replicas.append(replica)
        if assess(tuple(replicas), complete=True, expected_dirs=set()).status != Status.UNVERIFIED:
            return
        restored = tuple(
            store.restore(self.account(replica.account_id), (replica,))[0] for replica in replicas
        )
        if assess(restored, complete=True, expected_dirs=set()).status != Status.UNVERIFIED:
            return

        cached = {replica.account_id: replica.verified_sha256 for replica in restored}
        pending = [
            (target, replica, digest)
            for target, replica, digest in proven
            if not cached.get(target.account_id)
            or (digest and cached[target.account_id] != digest)
        ]
        if not pending:
            return
        digest = next((value for _, _, value in proven if value), "")
        if not digest and any(not value for _, _, value in pending):
            digest = dict(source_hashes).get("sha256") or self.backend.hash_file(
                source.endpoint, context
            )
        self.assert_expected(source, context)
        for _, target, replica, _ in copies:
            self.assert_expected(ExpectedItem(target, replica), context)
        context.check()
        store.record(
            (self.account(target.account_id), replica, value or digest)
            for target, replica, value in pending
        )

    def preflight(self, plan, context):
        context.check()
        self._validate_plan(plan)
        self._check_plan(plan, context, all_copies=True, delete_total=0)

    def _check_plan(self, plan, context, *, all_copies, delete_total):
        for item in plan.renames:
            self.assert_expected(item.source, context, from_listing=True)
        sources = {
            (copy.source.endpoint, copy.source.version.fingerprint): copy.source
            for copy in plan.copies
        }
        directories = {}
        originals, targets = tuple(sources.values()), plan.copies
        if not all_copies and not plan.mkdirs:
            # The first copy checks both endpoints before the first write.
            originals, targets = originals[1:], targets[1:]
        for index, source in enumerate(originals, 1):
            self._describe(
                context, f"실행 전 원본 확인 · {index}/{len(originals)}", source.endpoint
            )
            self.assert_expected(source, context, directories=directories)
        for index, copy in enumerate(targets, 1):
            self._describe(
                context, f"실행 전 대상 확인 · {index}/{len(targets)}", copy.target,
                display=copy.display_endpoint,
            )
            self._check_copy_target(copy, context, directories=directories)
        for index, item in enumerate(plan.deletions, 1):
            if (item.endpoint, item.version.fingerprint) in sources:
                continue
            if delete_total:
                self._describe(
                    context, f"삭제 전 확인 · {index:,}/{delete_total:,}", item.endpoint
                )
            self.assert_expected(item, context, directories=directories)
        for index, directory in enumerate(plan.mkdirs, 1):
            self._describe(context, f"폴더 생성 전 확인 · {index}/{len(plan.mkdirs)}", directory)
            current = self.inspect(directory, context, directories=directories)
            if current is not None and not current.is_dir:
                raise BackendError(ErrorKind.CHANGED, "폴더 생성 위치에 파일이 생겼습니다.")
        for item in plan.move_targets:
            self._describe(context, "이동 대상 재확인", item.endpoint)
            self.assert_expected(item, context, directories=directories)
        for directory in plan.move_directories:
            self._describe(context, "이동 대상 폴더 확인", directory)
            current = self.inspect(directory, context, from_listing=True, directories=directories)
            if current is None or not current.is_dir:
                raise BackendError(ErrorKind.CHANGED, "이동 대상 폴더가 바뀌었습니다.")

    def _execute(self, plan, context, held, replaced_targets):
        results, copied_targets = [], []
        uploads = {}
        delete_total = len(plan.deletions) + len(plan.rmdirs)
        if plan.copies or plan.mkdirs or plan.renames:
            delete_total = 0
        try:
            for copy in replaced_targets.values():
                target = copy.target
                results.append(
                    StepResult(target.account_id, target.path, "공간 예약", "skipped", ErrorKind.QUOTA.value)
                )
                self._describe(context, "대체 전환 전 기존 대상 확인", target)
                self._check_copy_target(copy, context)
            self._check_plan(plan, context, all_copies=False, delete_total=delete_total)
            for index, directory in enumerate(plan.mkdirs, 1):
                try:
                    self._describe(context, f"폴더 생성 · {index}/{len(plan.mkdirs)}", directory)
                    self.backend.mkdir(directory, context)
                    results.append(
                        StepResult(directory.account_id, directory.path, "폴더 생성", "ok")
                    )
                except BackendError as error:
                    if error.kind == ErrorKind.CANCELLED:
                        raise
                    context.record_failure(error)
                    logger.warning(
                        "mkdir failed trace=%s account=%s path=%s error=%s",
                        context.trace_id,
                        diagnostic_ref(directory.account_id),
                        diagnostic_ref(path_key(directory.path)),
                        error_summary(error),
                    )
                    results.append(
                        StepResult(
                            directory.account_id, directory.path, "폴더 생성", "error", str(error)
                        )
                    )
            if any(item.state == "error" for item in results):
                return OperationResult(plan.label, tuple(results))
            alternatives = {}
            for item in plan.alternates:
                alternatives.setdefault((item.source.endpoint, item.target.path), deque()).append(item)
            for index, copy in enumerate(plan.copies, 1):
                replaced = replaced_targets.get(copy.target)
                target, replica, digest = self._execute_copy(
                    copy, context, held, results,
                    position=f"{index}/{len(plan.copies)}",
                    precise=plan.precise or bool(plan.deletions),
                    alternates=(
                        alternatives.get((copy.source.endpoint, copy.target.path), deque())
                        if plan.local_sources else None
                    ),
                    reason=(
                        f"{self.account(replaced.target.account_id).label} · {ErrorKind.QUOTA.value}"
                        if replaced else ""
                    ),
                )
                if plan.precise:
                    copied_targets.append((target, replica))
                if plan.local_sources and self.verification_store:
                    uploads.setdefault(target.path, []).append((copy.source, target, replica, digest))
            # A move deletes nothing until every destination has passed verification.
            for index, item in enumerate(plan.deletions, 1):
                if delete_total:
                    self._describe(
                        context, f"삭제 직전 확인 · {index:,}/{delete_total:,}", item.endpoint
                    )
                self.assert_expected(item, context)
                if delete_total:
                    self._describe(
                        context, f"파일 삭제 · {index:,}/{delete_total:,}", item.endpoint
                    )
                self.backend.delete_file(item.endpoint, context)
                results.append(
                    StepResult(item.endpoint.account_id, item.endpoint.path, "파일 삭제", "ok")
                )
            for index, item in enumerate(plan.rmdirs, len(plan.deletions) + 1):
                if delete_total:
                    self._describe(
                        context, f"빈 폴더 삭제 · {index:,}/{delete_total:,}", item.endpoint
                    )
                self.backend.rmdir(item.endpoint, context)
                results.append(
                    StepResult(item.endpoint.account_id, item.endpoint.path, "빈 폴더 삭제", "ok")
                )
            for item in plan.renames:
                self._describe(context, "이름 변경", item.target)
                self.assert_expected(item.source, context, from_listing=True)
                self.backend.rename(
                    item.source.endpoint, item.target, context, is_dir=item.source.version.is_dir
                )
                actual = self.inspect(item.target, context, from_listing=True)
                if actual is None or compare(item.source.version, actual) == "different":
                    raise BackendError(ErrorKind.CHANGED, "이름 변경 결과를 확인할 수 없습니다.")
                results.append(
                    StepResult(item.target.account_id, item.target.path, "이름 변경", "ok")
                )
            for path, copies in uploads.items():
                try:
                    context.check()
                    self._describe(context, "업로드 검증 결과 확인", copies[0][1])
                    self._cache_upload_verification(path, copies, context)
                except BackendError as error:
                    if error.kind != ErrorKind.CANCELLED:
                        logger.warning(
                            "upload verification cache skipped trace=%s error=%s",
                            context.trace_id,
                            error_summary(error),
                        )
        except Exception as error:
            cancelled = isinstance(error, BackendError) and error.kind == ErrorKind.CANCELLED
            state = "rejected" if isinstance(error, CopyRejected) else "error"
            context.record_failure(error)
            results.append(StepResult("", "", "중단", "cancelled" if cancelled else state, str(error)))
        return OperationResult(plan.label, tuple(results), copied_targets=tuple(copied_targets))

    def _validate_plan(self, plan):
        if not any((plan.copies, plan.deletions, plan.mkdirs, plan.rmdirs, plan.renames)):
            raise ValueError("실행할 항목이 없습니다.")
        if plan.alternates and (
            not plan.local_sources or any(copy.previous is not None for copy in plan.alternates)
        ):
            raise ValueError("대체 대상은 새 파일 업로드에만 사용할 수 있습니다.")
        changes = [copy.target for copy in (*plan.copies, *plan.alternates)] + list(plan.mkdirs)
        changes += [item.endpoint for item in (*plan.deletions, *plan.rmdirs)]
        changes += [item.source.endpoint for item in plan.renames]
        changes += [item.target for item in plan.renames]
        for endpoint in changes:
            self.validate_endpoint(endpoint)
            if endpoint.account_id != "local":
                account = self.account(endpoint.account_id)
                if not account.enabled or account.readonly:
                    raise ValueError("비활성 또는 읽기 전용 계정은 변경할 수 없습니다.")
        for copy in plan.copies:
            self.validate_endpoint(copy.source.endpoint)
            if copy.source.endpoint.account_id != "local":
                if not self.account(copy.source.endpoint.account_id).enabled:
                    raise ValueError("원본 계정이 비활성 상태입니다. 다시 계획하세요.")

    def execute_repair(self, entry, context, *, approved=False):
        if not approved:
            raise PermissionError("문제 해결 실행 요청이 필요합니다.")
        context.check()
        context.set_phase("stage=repair_plan")
        context.describe(f"계획 조회 · /{entry.path}")
        with ExitStack() as locks:
            while not self._repair_planning.acquire(timeout=0.1):
                context.check()
            try:
                context.check()
                plan = self.plan_repair(entry, context)
                self._validate_plan(plan)
                locks.enter_context(self._claim(plan))
                plan, held, replaced_targets = locks.enter_context(self._reserve(plan, context))
            finally:
                self._repair_planning.release()
            return self._execute(plan, context, held, replaced_targets)

    def execute(self, plan, context, *, approved=False):
        if not approved:
            raise PermissionError("변경 범위에 대한 사용자 승인이 필요합니다.")
        context.set_phase("stage=validate_plan")
        self._validate_plan(plan)
        context.set_phase("stage=claim_paths")
        with self._claim(plan), self._reserve(plan, context) as (selected, held, replaced_targets):
            if plan.local_sources:
                with ExitStack() as locks:
                    context.set_phase("stage=local_source_lock provider=local")
                    for source in dict.fromkeys(plan.local_sources):
                        locks.enter_context(locked_source(source))
                    return self._execute(selected, context, held, replaced_targets)
            return self._execute(selected, context, held, replaced_targets)

    def _reserve_upload(self, plan, context, held):
        copies, candidates = list(plan.copies), list(plan.alternates)
        replaced_targets = {}
        ordered = sorted(enumerate(copies), key=lambda item: item[1].target.account_id)
        for position, (index, original) in enumerate(ordered, 1):
            copy, reason = original, ""
            while True:
                context.check()
                target = copy.target
                action = "대체 대상 공간 확인" if reason else f"공간 예약 · {position}/{len(copies)}개 계정"
                self._describe(context, action, target, note=reason)
                try:
                    self._reserve_space(target, original.source.version.size, held)
                except BackendError as error:
                    if error.kind != ErrorKind.QUOTA or original.previous is not None:
                        raise
                    alternate = next(
                        (
                            item for item in candidates
                            if item.source == original.source and item.target.path == target.path
                        ),
                        None,
                    )
                    if alternate is None:
                        raise
                    if copy is original:
                        reason = f"{self.account(target.account_id).label} · {error.kind.value}"
                    candidates.remove(alternate)
                    copy = alternate
                else:
                    if copy is not original:
                        copies[index] = copy
                        replaced_targets[target] = original
                    break
        if replaced_targets:
            plan = replace(plan, copies=tuple(copies), alternates=tuple(candidates))
        return plan, replaced_targets

    def _reserve_space(self, target, size, held):
        account_id = target.account_id
        if account_id == "local":
            import shutil

            if shutil.disk_usage(target.fs).free < size:
                raise BackendError(ErrorKind.QUOTA)
            return
        account = self.account(account_id)
        with self._guard:
            quota = self.quotas.snapshot((account,))[account_id]
            reserved = self._reserved.get(account_id, 0)
            if quota.free is not None and quota.free - reserved < size:
                raise BackendError(ErrorKind.QUOTA)
            self._reserved[account_id] = reserved + size
            held[account_id] = held.get(account_id, 0) + size

    @contextmanager
    def _reserve(self, plan, context, *, note=""):
        amounts = {}
        for copy in plan.copies:
            if copy.source.version.size is None:
                raise ValueError("파일 크기가 확인되지 않아 저장 공간을 예약할 수 없습니다.")
            size = copy.source.version.size
            amounts[copy.target.account_id] = amounts.get(copy.target.account_id, 0) + size
        held = {}
        try:
            if plan.alternates:
                plan, replaced_targets = self._reserve_upload(plan, context, held)
            else:
                replaced_targets = {}
                for index, (account_id, size) in enumerate(sorted(amounts.items()), 1):
                    copy = next(c for c in plan.copies if c.target.account_id == account_id)
                    action = "대체 대상 공간 확인" if note else f"공간 예약 · {index}/{len(amounts)}개 계정"
                    self._describe(context, action, copy.target, note=note, display=copy.display_endpoint)
                    self._reserve_space(copy.target, size, held)
            yield plan, held, replaced_targets
        finally:
            with self._guard:
                for account_id, size in held.items():
                    self._reserved[account_id] -= size
