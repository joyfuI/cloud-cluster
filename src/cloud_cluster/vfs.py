"""On-demand listings; failures are never an empty directory."""

from collections import OrderedDict
from concurrent.futures import as_completed

# shortcut: Nuitka 4.3 misses Python 3.15's lazy re-export; restore it after compiler upgrade.
from concurrent.futures.thread import ThreadPoolExecutor
from dataclasses import replace
from threading import RLock
from time import monotonic

from .diagnostics import diagnostic_ref, error_summary, logger
from .domain import DirectorySnapshot, Endpoint, Listing, Status
from .replicas import is_excluded, merge, path_key, validate_path

_WRAPPER_PROVIDERS = {
    "alias",
    "cache",
    "chunker",
    "combine",
    "compress",
    "crypt",
    "hasher",
    "union",
}


class FolderCache:
    def __init__(self, ttl=300, max_folders=200, max_entries=100_000):
        self.ttl, self.max_folders, self.max_entries = ttl, max_folders, max_entries
        self._items = OrderedDict()
        self._reading = {}
        self._lock = RLock()

    def get(self, key):
        with self._lock:
            value = self._items.get(key)
            if value is None:
                return None
            self._items.move_to_end(key)
            return replace(value, stale=value.stale or monotonic() - value.checked_at >= self.ttl)

    def put(self, key, value):
        with self._lock:
            self._items[key] = value
            self._items.move_to_end(key)
            while len(self._items) > 1 and (
                len(self._items) > self.max_folders
                or sum(
                    len(item.replicas) for snap in self._items.values() for item in snap.listings
                )
                > self.max_entries
            ):
                self._items.popitem(last=False)

    def start_read(self, key):
        with self._lock:
            token = object()
            self._reading[key] = token
            return token

    def finish_read(self, key, token, snapshot):
        with self._lock:
            if self._reading.get(key) is token:
                del self._reading[key]
                if snapshot is not None:
                    self.put(key, snapshot)

    def mark_stale(self, paths=(), *, current=None):
        changed = {path_key(path) for path in paths}
        parents = {""}
        for path in changed:
            while "/" in path:
                path = path.rpartition("/")[0]
                parents.add(path)

        def affected(path):
            if not changed or "" in changed or path in parents:
                return True
            while path:
                if path in changed:
                    return True
                path = path.rpartition("/")[0]
            return False

        with self._lock:
            for key in self._items.keys() | self._reading.keys():
                if affected(key[-1]):
                    self._reading.pop(key, None)
                    if key in self._items and not self._items[key].stale:
                        self._items[key] = replace(self._items[key], stale=True)
        return current is not None and affected(path_key(current))

    def invalidate(self, *paths):
        with self._lock:
            for key in self._items.keys() | self._reading.keys():
                cached_path = key[-1]
                if not paths or any(
                    cached_path == path_key(p) or cached_path.startswith(path_key(p) + "/")
                    for p in paths
                ):
                    self._items.pop(key, None)
                    self._reading.pop(key, None)


class VirtualFileSystem:
    def __init__(self, backend, accounts, *, ttl=300, verification_store=None):
        self.backend, self.accounts = backend, list(accounts)
        self.verification_store = verification_store
        self.cache = FolderCache(ttl)
        self.capabilities = {}
        self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="listing")

    def cache_key(self, path):
        return (tuple((a.id, a.fs, a.enabled) for a in self.accounts), path_key(path))

    def cached(self, path):
        return self.cache.get(self.cache_key(path))

    def mark_changed(self, paths, *, current=None):
        accounts = [account for account in self.accounts if account.enabled]
        # Wrapped or differently rooted remotes can expose a write at another logical path.
        if (
            any(a.provider in _WRAPPER_PROVIDERS for a in accounts)
            or len({a.root for a in accounts}) > 1
        ):
            paths = ()
        return self.cache.mark_stale(paths, current=current)

    def _list(self, account, path, context):
        phase = "features"
        try:
            if account.id not in self.capabilities:
                self.capabilities[account.id] = self.backend.features(account, context)
            if path:
                phase = "stat"
                item = self.backend.stat(Endpoint(account.fs, path, account.id), context)
                if item is None:
                    return Listing(account.id, state="absent")
                if not item.is_dir:
                    return Listing(account.id, state="error", error="해당 경로가 파일입니다.")
            phase = "list"
            replicas = self.backend.list_dir(account, path, context)
            if self.verification_store:
                phase = "cache"
                replicas = self.verification_store.restore(account, replicas)
            return Listing(account.id, replicas)
        except Exception as error:
            log = logger.debug if context and context.cancelled.is_set() else logger.warning
            log(
                "listing failed trace=%s phase=%s account=%s path=%s error=%s",
                context.trace_id if context else "-",
                phase,
                diagnostic_ref(account.id),
                diagnostic_ref(path_key(path)),
                error_summary(error),
            )
            return Listing(account.id, state="error", error=str(error))

    def refresh(self, path, context=None, on_partial=None, *, on_progress=None):
        validate_path(path, allow_root=True, names=False)
        key = self.cache_key(path)
        token = self.cache.start_read(key)
        snapshot = None
        try:
            snapshot = self._refresh(path, context, on_partial, on_progress)
            return snapshot
        finally:
            self.cache.finish_read(key, token, snapshot)

    def _refresh(self, path, context, on_partial, on_progress):
        previous = self.cached(path)
        older = {listing.account_id: listing for listing in previous.listings} if previous else {}
        accounts = [
            account
            for account in self.accounts
            if account.enabled and not is_excluded(account, path)
        ]
        futures = {
            self._pool.submit(self._list, account, path, context): account.id
            for account in accounts
        }
        results = {
            account.id: replace(
                older.get(account.id) or Listing(account.id), state="excluded", error="조회 중"
            )
            for account in accounts
        }
        pending = set(futures.values())
        if on_progress:
            on_progress(pending)
        for future in as_completed(futures):
            listing = future.result()
            if listing.state in {"error", "excluded"} and listing.account_id in older:
                listing = replace(listing, replicas=older[listing.account_id].replicas)
            results[futures[future]] = listing
            pending.discard(futures[future])
            if on_progress:
                on_progress(pending)
            if on_partial:
                listings = tuple(results.values())
                # Preserve cached states for display while tracking actual response states.
                display_listings = tuple(
                    older.get(item.account_id, item) if item.account_id in pending else item
                    for item in listings
                )
                on_partial(
                    DirectorySnapshot(
                        path,
                        listings,
                        merge(display_listings, self.capabilities, self.accounts),
                        monotonic(),
                    )
                )
        listings = tuple(results.values())
        return DirectorySnapshot(
            path, listings, merge(listings, self.capabilities, self.accounts), monotonic()
        )

    def scan_folder(self, path, context=None):
        snapshot = self.refresh(path, context)
        issues = tuple(entry for entry in snapshot.entries if entry.assessment.status != Status.HEALTHY)
        children = tuple(
            entry.path for entry in snapshot.entries
            if entry.is_dir and entry.assessment.status != Status.CONFLICT
        )
        return issues, snapshot.complete, len(snapshot.entries), children

    def scan(self, path, context=None, on_progress=None):
        pending, visited, issues = [path], set(), {}
        complete, count = True, 0
        while pending:
            if context:
                context.check()
            path = pending.pop()
            if path_key(path) in visited:
                continue
            visited.add(path_key(path))
            found, folder_complete, checked, children = self.scan_folder(path, context)
            complete &= folder_complete
            count += checked
            issues.update((entry.key, entry) for entry in found)
            pending.extend(children)
            if on_progress:
                on_progress(count, len(issues), path)
        return tuple(issues.values()), complete, count

    def close(self):
        self._pool.shutdown(wait=True, cancel_futures=True)

    def recheck(self, entries, context=None, *, proofs=(), load_folder=None):
        load_folder = self.refresh if load_folder is None else load_folder
        verified = {replica.fingerprint: digest for replica, digest in proofs}
        folders = {}
        for entry in entries:
            parent = entry.path.rpartition("/")[0]
            folders.setdefault(parent, []).append(entry)
        updates, complete = {}, True
        for index, (parent, selected) in enumerate(folders.items(), 1):

            def progress(pending):
                labels = ", ".join(a.label for a in self.accounts if a.id in pending)
                context.describe(
                    f"{index}/{len(folders)}개 폴더 · /{parent} · "
                    + (f"응답 대기: {labels}" if labels else "조회 결과 병합 중")
                )

            if context:
                context.check()
            snapshot = load_folder(parent, context, on_progress=progress if context else None)
            if verified:
                raw = snapshot
                snapshot = self._verified_snapshot(raw, verified)
                key = self.cache_key(parent)
                with self.cache._lock:
                    cached = self.cache._items.get(key)
                    if cached is raw:
                        self.cache.put(key, snapshot)
                    elif cached and not cached.stale and cached.checked_at == raw.checked_at:
                        self.cache.put(key, self._verified_snapshot(cached, verified))
            complete &= snapshot.complete
            current = {entry.key: entry for entry in snapshot.entries}
            for entry in selected:
                if entry.key in current:
                    updates[entry.key] = current[entry.key]
                elif snapshot.complete and snapshot.listings:
                    updates[entry.key] = None
                else:
                    complete = False
                    updates[entry.key] = replace(
                        entry,
                        assessment=replace(
                            entry.assessment,
                            status=Status.UNKNOWN,
                            issues=("일부 계정 미확인",),
                            complete=False,
                        ),
                    )
        if context:
            context.check()
        return updates, complete

    def _verified_snapshot(self, snapshot, verified):
        listings = tuple(
            replace(
                listing,
                replicas=tuple(
                    replace(r, verified_sha256=verified[r.fingerprint])
                    if r.fingerprint in verified
                    else r
                    for r in listing.replicas
                ),
            )
            for listing in snapshot.listings
        )
        return replace(
            snapshot,
            listings=listings,
            entries=merge(listings, self.capabilities, self.accounts),
        )

    def record_verification(self, proofs):
        proofs = tuple(proofs)
        stored = 0
        if self.verification_store:
            accounts = {a.id: a for a in self.accounts}
            stored = self.verification_store.record(
                (accounts[r.account_id], r, digest)
                for r, digest in proofs
                if r.account_id in accounts and not is_excluded(accounts[r.account_id], r.path)
            )
        verified = {replica.fingerprint: digest for replica, digest in proofs}
        with self.cache._lock:
            for key, snapshot in list(self.cache._items.items()):
                self.cache._items[key] = self._verified_snapshot(snapshot, verified)
        return stored
