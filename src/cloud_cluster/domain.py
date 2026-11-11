"""Cloud-independent data. No Qt objects or credentials belong here."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Protocol


class Status(StrEnum):
    HEALTHY = "정상"
    UNDER = "복제 부족"
    CONFLICT = "충돌"
    OVER = "복제 초과"
    UNKNOWN = "확인 불가"
    UNVERIFIED = "검증 불가"
    DIRECTORY = "디렉터리 불일치"


class ErrorKind(StrEnum):
    AUTH = "재인증 필요"
    PERMISSION = "권한 부족"
    QUOTA = "용량 부족"
    FILE_SIZE = "파일 크기 제한 초과"
    RATE_LIMIT = "요청 제한"
    NETWORK = "연결 오류"
    UNSUPPORTED = "지원 안 함"
    UNKNOWN = "결과 불명"
    CANCELLED = "취소됨"
    CHANGED = "조회 후 변경됨"


class BackendError(Exception):
    def __init__(self, kind: ErrorKind, message: str = "", *, diagnostic: str = ""):
        self.kind = kind
        self.diagnostic = diagnostic
        super().__init__(message or kind.value)


class CopyRejected(BackendError):
    """The backend rejected the copy before writing the destination."""


@dataclass(frozen=True)
class CloudAccount:
    id: str
    remote: str
    provider: str
    root: str = ""
    enabled: bool = True
    readonly: bool = False

    @property
    def fs(self) -> str:
        if self.provider == "drive":
            return f"{self.remote},use_trash=true:{self.root}"
        separator = "" if self.remote.endswith(":") else ":"
        return f"{self.remote}{separator}{self.root}"

    @property
    def label(self) -> str:
        return self.remote.rstrip(":")


@dataclass(frozen=True)
class Endpoint:
    fs: str
    path: str
    account_id: str = "local"


@dataclass(frozen=True)
class BackendCapabilities:
    hashes: tuple[str, ...] = ()
    empty_directories: bool = True
    server_copy: bool = False
    server_move: bool = False
    quota: bool = False
    max_file_size: int | None = None
    trash_cleanup: bool = False


@dataclass(frozen=True)
class QuotaSnapshot:
    total: int | None = None
    free: int | None = None
    reserved: int = 0


@dataclass(frozen=True)
class Replica:
    account_id: str
    path: str
    is_dir: bool = False
    size: int | None = None
    modified: datetime | None = None
    hashes: Mapping[str, str] = field(default_factory=dict)
    object_id: str = ""
    version: str = ""
    mime: str = ""
    verified_sha256: str = ""

    @property
    def name(self) -> str:
        return PurePosixPath(self.path).name

    @property
    def fingerprint(self) -> tuple:
        return (
            self.account_id,
            self.path,
            self.object_id,
            self.version,
            self.is_dir,
            self.size,
            self.modified,
            tuple(sorted(self.hashes.items())),
        )


@dataclass(frozen=True)
class EntryAssessment:
    status: Status
    issues: tuple[str, ...]
    count: int
    complete: bool
    verification: str = "메타데이터"


@dataclass(frozen=True)
class LogicalEntry:
    key: str
    path: str
    replicas: tuple[Replica, ...]
    assessment: EntryAssessment

    @property
    def name(self) -> str:
        return self.replicas[0].name

    @property
    def is_dir(self) -> bool:
        return all(replica.is_dir for replica in self.replicas)


@dataclass(frozen=True)
class Listing:
    account_id: str
    replicas: tuple[Replica, ...] = ()
    state: str = "ok"  # ok, absent, error, excluded
    error: str = ""


@dataclass(frozen=True)
class DirectorySnapshot:
    path: str
    listings: tuple[Listing, ...]
    entries: tuple[LogicalEntry, ...]
    checked_at: float
    stale: bool = False

    @property
    def complete(self) -> bool:
        return all(item.state in {"ok", "absent"} for item in self.listings)


@dataclass(frozen=True)
class StepResult:
    account_id: str
    path: str
    action: str
    state: str
    message: str = ""


@dataclass(frozen=True)
class OperationResult:
    label: str
    steps: tuple[StepResult, ...]
    verification: tuple[tuple[Replica, str], ...] = ()
    affected_paths: tuple[str, ...] = ()
    copied_targets: tuple[tuple[Endpoint, Replica], ...] = ()

    @property
    def outcome(self) -> str:
        states = {step.state for step in self.steps if step.state != "skipped"}
        if states == {"ok"}:
            return "완료"
        if "ok" in states:
            return "일부 성공"
        if states == {"rejected"}:
            return "실패"
        return "취소됨" if "cancelled" in states else "실패 / 결과 확인 필요"


class CloudBackend(Protocol):
    def list_dir(
        self, account: CloudAccount, path: str, context=None, *, show_hash: bool = True
    ) -> tuple[Replica, ...]: ...
    def stat(self, endpoint: Endpoint, context=None) -> Replica | None: ...
    def features(self, account: CloudAccount, context=None) -> BackendCapabilities: ...
    def about(self, account: CloudAccount, context=None) -> QuotaSnapshot: ...
    def copy_file(self, source: Endpoint, target: Endpoint, context=None) -> None: ...
    def rename(self, source: Endpoint, target: Endpoint, context=None, *, is_dir: bool) -> None: ...
    def mkdir(self, endpoint: Endpoint, context=None) -> None: ...
    def delete_file(self, endpoint: Endpoint, context=None) -> None: ...
    def rmdir(self, endpoint: Endpoint, context=None) -> None: ...
    def hash_file(self, endpoint: Endpoint, context=None, *, download: bool = True) -> str: ...
