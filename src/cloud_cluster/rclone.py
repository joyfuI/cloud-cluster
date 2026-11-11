"""Only this adapter knows RC names and the pinned librclone ABI."""

import logging
import os
import re
from datetime import datetime
from pathlib import Path
from threading import RLock
from time import monotonic, sleep
from uuid import uuid4

from ._vendor.rclone import Rclone, RcloneException
from .diagnostics import logger
from .domain import (
    BackendCapabilities,
    BackendError,
    CopyRejected,
    ErrorKind,
    QuotaSnapshot,
    Replica,
)
from .replicas import validate_path

RCLONE_VERSION = "v1.76.0"
_PROGRESS_METHODS = {
    "operations/copyfile",
    "operations/hashsumfile",
    "operations/movefile",
    "cloudcluster/renamedir",
}
NATIVE_DIR = Path(__file__).resolve().parent / "_native"
_lock = RLock()
_runtime = None
_finalized = False
_HTTP_ERROR = re.compile(
    r"\b(?:googleapi:\s*error|http(?:/\d(?:\.\d)?)?(?:\s+status)?|status\s*code)"
    r"\s*[:=]?\s*([45]\d{2})\b",
    re.IGNORECASE,
)
_PROVIDER_REASON = re.compile(
    r"(?:\bapi error\s+|\breason[\"']?\s*[:=]\s*[\"']?|,\s*)"
    r"(AccessDenied|insufficientFilePermissions|insufficientPermissions|Forbidden"
    r"|userRateLimitExceeded|rateLimitExceeded|storageQuotaExceeded)\b",
    re.IGNORECASE,
)
_S3_UPLOAD = re.compile(
    r"\boperation error s3:\s*(?:PutObject|CreateMultipartUpload|UploadPart|CompleteMultipartUpload)\b",
    re.IGNORECASE,
)
_ERROR_URL = re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s<>\"']+", re.IGNORECASE)
_ERROR_SECRET = re.compile(
    r"(\b(?:bearer|basic)\s+|\b(?:access[_-]?token|refresh[_-]?token|client[_-]?secret"
    r"|password|authorization|token|api[_-]?key|secret[_-]?access[_-]?key|credential|signature)"
    r"[\"']?\s*[:=]\s*)(?:(?:bearer|basic)\s+)?(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,;]+)",
    re.IGNORECASE,
)


def classify(message: str) -> ErrorKind:
    message = message.lower()
    status = _HTTP_ERROR.search(message) or re.match(r"^\s*([45]\d{2})\b", message)
    status_kind = {
        "401": ErrorKind.AUTH,
        "429": ErrorKind.RATE_LIMIT,
        "403": ErrorKind.PERMISSION,
    }.get(status[1] if status else "")
    for words, kind in [
        (("invalid_grant", "unauthorized", "invalid token"), ErrorKind.AUTH),
        (("quota", "no space", "insufficient storage"), ErrorKind.QUOTA),
        (("rate limit", "too many requests"), ErrorKind.RATE_LIMIT),
        (
            ("permission denied", "access denied", "access is denied", "read-only"),
            ErrorKind.PERMISSION,
        ),
        (
            ("timeout", "timed out", "connection reset", "no such host", "network is unreachable"),
            ErrorKind.NETWORK,
        ),
        (("not supported", "doesn't support", "not implemented"), ErrorKind.UNSUPPORTED),
    ]:
        if kind == status_kind or any(word in message for word in words):
            return kind
    return ErrorKind.UNKNOWN


def _rc_error(method, message, *, rc_status=None, single_attempt=False):
    kind = classify(message)
    box_size_error = method == "operations/copyfile" and message.removeprefix(
        "pre-upload check: "
    ).startswith('Error "file_size_limit_exceeded" (403):')
    details = [f"rc_method={method}"]
    if rc_status is not None:
        details.append(f"rc_status={rc_status}")
    if box_size_error:
        kind = ErrorKind.FILE_SIZE
        details.extend(("provider_http=403", "provider_code=file_size_limit_exceeded"))
    elif status := _HTTP_ERROR.search(message):
        details.append(f"provider_http={status[1]}")
        if reason := _PROVIDER_REASON.search(message):
            details.append(f"provider_code={reason[1].lower()}")
    if method == "operations/copyfile":
        side = "unknown"
        if message.lower().startswith("failed to open source object:"):
            side = "source"
        elif box_size_error or _S3_UPLOAD.search(message):
            side = "target"
        details.append(f"failure_side={side}")
    summary = f"{method}: {kind.value}"
    if rc_status is not None:
        summary += f" (RC {rc_status})"
    preflight_rejected = box_size_error and message.startswith("pre-upload check: ")
    if preflight_rejected and single_attempt:
        details.append("write_result=not_attempted")
        return CopyRejected(kind, summary, diagnostic=" ".join(details))
    return BackendError(kind, summary, diagnostic=" ".join(details))


def _debug_rc_error(method, trace, job, message):
    if (
        logger.isEnabledFor(logging.DEBUG)
        and method.startswith(("operations/", "cloudcluster/"))
        and method != "operations/cleanup"
        and "oauth2:" not in message.lower()
    ):
        # shortcut: masks recognizable credential formats, extend when a backend adds another one
        safe = _ERROR_SECRET.sub(r"\1<redacted>", _ERROR_URL.sub("<url>", message))
        if len(safe) > 4096:
            safe = safe[:4096] + "…"
        logger.debug("rc error detail trace=%s method=%s job=%s message=%r", trace, method, job, safe)


class RcloneAdapter:
    def __init__(self, dll_path=None):
        global _runtime
        path = Path(dll_path or NATIVE_DIR / "librclone.dll").resolve(strict=True)
        with _lock:
            if _finalized:
                raise RuntimeError("종료한 DLL은 다시 초기화하지 않습니다. 앱을 다시 실행하세요.")
            if _runtime is None:
                self._dll_directory = (
                    os.add_dll_directory(str(path.parent)) if os.name == "nt" else None
                )
                _runtime = Rclone(str(path))
            self._runtime = _runtime
        self._jobs = set()
        self._jobs_lock = RLock()
        self.rpc("options/set", main={"AskPassword": False, "LogLevel": "ERROR"})
        version = self.rpc("core/version").get("version", "")
        if version != RCLONE_VERSION:
            raise RuntimeError(f"DLL 버전 불일치: {RCLONE_VERSION}이 필요합니다.")

    def rpc(self, method, *, _trace="-", **parameters):
        with _lock:
            try:
                return self._runtime.rpc(method, **parameters)
            except RcloneException as error:
                message = str(error)
                failure = _rc_error(method, message, rc_status=error.status)
                logger.warning(
                    "rc call failed method=%s status=%d kind=%s trace=%s %s",
                    method,
                    error.status,
                    failure.kind.name,
                    _trace,
                    failure.diagnostic,
                )
                _debug_rc_error(method, _trace, "-", message)
                raise failure from None

    def run(self, method, context=None, **parameters):
        if context:
            context.check()
        trace = context.trace_id if context else "-"
        started = monotonic()
        logger.debug("rc submitting trace=%s method=%s", trace, method)
        group = "cloud-cluster/" + uuid4().hex
        result = self.rpc(method, **parameters, _async=True, _group=group, _trace=trace)
        job_id = result["jobid"]
        logger.debug("rc submitted trace=%s method=%s job=%d", trace, method, job_id)
        with self._jobs_lock:
            self._jobs.add(job_id)
        cancelled_at = None
        last_stats = 0.0
        while True:
            if context and context.cancelled.is_set() and cancelled_at is None:
                logger.info("rc cancel requested trace=%s method=%s job=%d", trace, method, job_id)
                self.rpc("job/stop", jobid=job_id)
                cancelled_at = monotonic()
            status = self.rpc("job/status", jobid=job_id, _trace=trace)
            if status.get("finished"):
                with self._jobs_lock:
                    self._jobs.discard(job_id)
                failure = None
                if cancelled_at is None and not status.get("success"):
                    message = status.get("error") or ""
                    failure = _rc_error(
                        method,
                        message,
                        single_attempt=(parameters.get("_config") or {}).get("LowLevelRetries") == 1,
                    )
                    logger.warning(
                        "rc job failed trace=%s method=%s job=%d kind=%s elapsed_ms=%.0f %s",
                        trace,
                        method,
                        job_id,
                        failure.kind.name,
                        (monotonic() - started) * 1000,
                        failure.diagnostic,
                    )
                    _debug_rc_error(method, trace, job_id, message)
                try:
                    if context and method in _PROGRESS_METHODS:
                        context.progress(
                            {**self.rpc("core/stats", group=group), "detail": context.detail}
                        )
                    self.rpc("core/stats-delete", group=group)
                except BackendError:
                    if failure is None:
                        raise
                if failure is not None:
                    raise failure from None
                if cancelled_at is not None:
                    raise BackendError(ErrorKind.CANCELLED)
                logger.debug(
                    "rc finished trace=%s method=%s job=%d elapsed_ms=%.0f",
                    trace,
                    method,
                    job_id,
                    (monotonic() - started) * 1000,
                )
                return status.get("output", {})
            if cancelled_at is not None and monotonic() - cancelled_at > 15:
                logger.warning(
                    "rc cancel unconfirmed trace=%s method=%s job=%d", trace, method, job_id
                )
                raise BackendError(
                    ErrorKind.UNKNOWN,
                    "취소 완료를 확인하지 못했습니다. 원격 상태를 다시 확인하세요.",
                )
            if context and method in _PROGRESS_METHODS and monotonic() - last_stats >= 0.5:
                context.progress({**self.rpc("core/stats", group=group), "detail": context.detail})
                last_stats = monotonic()
            sleep(0.1)

    @staticmethod
    def replica(account_id, item, path=None):
        value = item.get("ModTime")
        modified = (
            datetime.fromisoformat(value.replace("Z", "+00:00"))
            if value and not value.startswith("0001-")
            else None
        )
        size = item.get("Size")
        return Replica(
            account_id,
            item.get("Path", "") if path is None else path,
            item.get("IsDir", False),
            size if size is not None and size >= 0 else None,
            modified,
            {k.lower(): v for k, v in (item.get("Hashes") or {}).items() if v},
            item.get("ID", ""),
            # Generic metadata is user editable, not a provider content revision.
            "",
            item.get("MimeType", ""),
        )

    def list_dir(self, account, path, context=None, *, show_hash=True):
        validate_path(path, allow_root=True, names=False)
        result = self.run(
            "operations/list",
            context,
            fs=account.fs,
            remote=path,
            opt={"recurse": False, "showHash": show_hash, "showOrigIDs": True},
        )
        items = []
        for item in result.get("list", []):
            raw = item["Path"]
            # RC paths are relative to the fs root; preserve them rather than joining twice.
            items.append(self.replica(account.id, item, raw))
        return tuple(items)

    def stat(self, endpoint, context=None):
        validate_path(endpoint.path, allow_root=True, names=False)
        result = self.run(
            "operations/stat",
            context,
            fs=endpoint.fs,
            remote=endpoint.path,
            opt={"showHash": True, "showOrigIDs": True},
        )
        item = result.get("item")
        return None if item is None else self.replica(endpoint.account_id, item, endpoint.path)

    def features(self, account, context=None):
        result = self.run("operations/fsinfo", context, fs=account.fs)
        features = result.get("Features", {})
        return BackendCapabilities(
            tuple(result.get("Hashes", [])),
            features.get("CanHaveEmptyDirectories", False),
            features.get("Copy", False),
            features.get("Move", False),
            features.get("About", False),
            trash_cleanup=features.get("CleanUp") is True,
        )

    def empty_trash(self, account, context):
        self.run("operations/cleanup", context, fs=account.fs)

    def about(self, account, context=None):
        result = self.run("operations/about", context, fs=account.fs)
        return QuotaSnapshot(result.get("total"), result.get("free"))

    def copy_file(self, source, target, context=None):
        validate_path(source.path)
        validate_path(target.path)
        self.run(
            "operations/copyfile",
            context,
            srcFs=source.fs,
            srcRemote=source.path,
            dstFs=target.fs,
            dstRemote=target.path,
            _config={
                "IgnoreTimes": True,
                "LowLevelRetries": (context.retries + 1) if context else 3,
                "Retries": 1,
                "Inplace": False,
            },
        )

    def rename(self, source, target, context=None, *, is_dir):
        validate_path(source.path)
        validate_path(target.path)
        if source.fs != target.fs:
            raise ValueError("같은 리모트 안에서만 이름을 변경할 수 있습니다.")
        if is_dir:
            self.run(
                "cloudcluster/renamedir",
                context,
                fs=source.fs,
                remote=source.path,
                target=target.path,
            )
        else:
            self.run(
                "operations/movefile",
                context,
                srcFs=source.fs,
                srcRemote=source.path,
                dstFs=target.fs,
                dstRemote=target.path,
            )

    def mkdir(self, endpoint, context=None):
        validate_path(endpoint.path)
        self.run("operations/mkdir", context, fs=endpoint.fs, remote=endpoint.path)

    def delete_file(self, endpoint, context=None):
        validate_path(endpoint.path)
        self.run("operations/deletefile", context, fs=endpoint.fs, remote=endpoint.path)

    def rmdir(self, endpoint, context=None):
        validate_path(endpoint.path)
        self.run("operations/rmdir", context, fs=endpoint.fs, remote=endpoint.path)

    def hash_file(self, endpoint, context=None, *, download=True):
        validate_path(endpoint.path)
        result = self.run(
            "operations/hashsumfile",
            context,
            fs=endpoint.fs,
            remote=endpoint.path,
            hashType="SHA-256",
            download=download,
        )
        value = result.get("hash", "")
        if len(value) != 64:
            raise BackendError(ErrorKind.UNKNOWN, "SHA-256 검증 결과가 없습니다.")
        return value.lower()

    def shutdown(self, timeout=15):
        global _finalized
        with self._jobs_lock:
            jobs = tuple(self._jobs)
        for job_id in jobs:
            status = self.rpc("job/status", jobid=job_id)
            if not status.get("finished"):
                self.rpc("job/stop", jobid=job_id)
        deadline = monotonic() + timeout
        while jobs:
            jobs = tuple(j for j in jobs if not self.rpc("job/status", jobid=j).get("finished"))
            if not jobs:
                break
            if monotonic() >= deadline:
                return False
            sleep(0.1)
        with _lock:
            if not _finalized:
                self._runtime.close()
                _finalized = True
        return True
