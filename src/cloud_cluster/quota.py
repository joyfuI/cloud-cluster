"""Shared, approximate cloud capacity with local transfer accounting."""

from dataclasses import replace
from threading import Lock, RLock
from time import monotonic

from .diagnostics import diagnostic_ref, error_summary, logger
from .domain import BackendError, ErrorKind, QuotaSnapshot


class QuotaCache:
    def __init__(self, backend):
        self.backend = backend
        self._items, self._locks, self._spent = {}, {}, {}
        self._guard = RLock()

    @staticmethod
    def _key(account):
        return account.id, account.provider, account.fs

    def get(self, account, context=None, *, force=False, capabilities=None):
        requested = monotonic()
        key = self._key(account)
        with self._guard:
            lock = self._locks.setdefault(key, Lock())
        while not lock.acquire(timeout=0.1):
            if context:
                context.check()
        try:
            if context:
                context.check()
            with self._guard:
                item = self._items.get(key)
                spent = self._spent.get(key, 0)
            if item and (
                (force and item[0] >= requested) or (not force and monotonic() - item[0] < 3600)
            ):
                value = item[1]
            else:
                try:
                    cap = (
                        capabilities
                        if capabilities is not None
                        else self.backend.features(account, context)
                    )
                    value = self.backend.about(account, context) if cap.quota else None
                except BackendError as error:
                    if error.kind == ErrorKind.CANCELLED:
                        raise
                    logger.warning(
                        "quota lookup failed trace=%s account=%s error=%s",
                        context.trace_id if context else "-",
                        diagnostic_ref(account.id),
                        error_summary(error),
                    )
                    value = BackendError(error.kind, str(error))
                with self._guard:
                    if isinstance(value, QuotaSnapshot) and value.free is not None:
                        # A concurrent transfer must survive a late capacity response.
                        value = replace(
                            value, free=max(0, value.free - self._spent.get(key, 0) + spent)
                        )
                    self._items[key] = monotonic(), value
            if isinstance(value, BackendError):
                raise BackendError(value.kind, str(value))
            return value
        finally:
            lock.release()

    def snapshot(self, accounts):
        with self._guard:
            result = {}
            for account in accounts:
                _, value = self._items.get(self._key(account), (0, None))
                result[account.id] = value if isinstance(value, QuotaSnapshot) else QuotaSnapshot()
            return result

    def consume(self, account, size):
        key = self._key(account)
        with self._guard:
            self._spent[key] = self._spent.get(key, 0) + size
            item = self._items.get(key)
            if item and isinstance(item[1], QuotaSnapshot) and item[1].free is not None:
                self._items[key] = item[0], replace(item[1], free=max(0, item[1].free - size))

    def clear(self):
        with self._guard:
            self._items.clear()
