"""Map provider hashes or scoped object/path identities to byte-read SHA-256."""

import json
import re
import sqlite3
from contextlib import closing, contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from threading import RLock

from .diagnostics import error_summary, logger
from .replicas import STRONG_HASHES, validate_path

HASH_LENGTHS = {"md5": 32, "sha1": 40, "sha256": 64, "dropbox": 64, "blake3": 64, "whirlpool": 128}


class VerificationStore:
    def __init__(self, path):
        self.path = path
        self.error = ""
        self._lock = RLock()

    @staticmethod
    def _hashes(replica):
        if replica.is_dir:
            return ()
        return tuple(
            (kind, value.lower())
            for kind, value in replica.hashes.items()
            if kind in STRONG_HASHES
            and isinstance(value, str)
            and re.fullmatch(f"[0-9a-fA-F]{{{HASH_LENGTHS[kind]}}}", value)
        )

    @classmethod
    def _keys(cls, account, replica):
        hashes = cls._hashes(replica)
        if hashes:
            return hashes
        if (
            replica.is_dir
            or replica.hashes
            or not isinstance(replica.object_id, str)
            or not isinstance(replica.version, str)
        ):
            return ()
        if not replica.object_id:
            if not isinstance(replica.path, str):
                return ()
            try:
                validate_path(replica.path)
            except ValueError, UnicodeError:
                return ()
        identity = [account.id, account.fs, replica.object_id or replica.path]
        if replica.object_id and replica.version:
            kind = "object-version"
            identity.append(replica.version)
        elif (
            type(replica.size) is int
            and replica.size >= 0
            and isinstance(replica.modified, datetime)
            and replica.modified.utcoffset() is not None
        ):
            # shortcut: size/time can miss preserved-metadata edits; prefer content revisions.
            kind = "object-metadata" if replica.object_id else "path-metadata"
            identity.extend(
                [
                    replica.size,
                    replica.modified.astimezone(UTC).isoformat(timespec="microseconds"),
                ]
            )
        else:
            return ()
        return (
            (
                kind,
                json.dumps(
                    identity,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            ),
        )

    @staticmethod
    def _lookup(db, provider, hashes):
        digests = set()
        for kind, value in hashes:
            row = db.execute(
                "SELECT sha256 FROM content_hashes WHERE provider=? AND hash_type=? AND remote_hash=?",
                (provider, kind, value),
            ).fetchone()
            if row is not None:
                if not isinstance(row[0], str) or not re.fullmatch(r"[0-9a-f]{64}", row[0]):
                    return ""
                digests.add(row[0])
        return digests.pop() if len(digests) == 1 else ""

    @contextmanager
    def _database(self):
        with self._lock, closing(sqlite3.connect(self.path, timeout=1)) as db, db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS content_hashes ("
                "provider TEXT NOT NULL, hash_type TEXT NOT NULL, remote_hash TEXT NOT NULL, "
                "sha256 TEXT, PRIMARY KEY (provider, hash_type, remote_hash))"
            )
            yield db

    def _failed(self, error):
        if not self.error:
            logger.warning("verification cache unavailable error=%s", error_summary(error))
        self.error = "검증 결과 저장소를 읽거나 쓸 수 없습니다."

    def restore(self, account, replicas):
        replicas = tuple(replicas)
        candidates = [(i, keys) for i, r in enumerate(replicas) if (keys := self._keys(account, r))]
        if not candidates:
            return replicas
        restored = list(replicas)
        try:
            with self._database() as db:
                for index, hashes in candidates:
                    if digest := self._lookup(db, account.provider, hashes):
                        restored[index] = replace(replicas[index], verified_sha256=digest)
            self.error = ""
        except (sqlite3.Error, OSError) as error:
            self._failed(error)
            return replicas
        return tuple(restored)

    def record(self, proofs):
        proofs = [
            (account.provider, hashes, digest.lower())
            for account, replica, digest in proofs
            if isinstance(digest, str)
            and re.fullmatch(r"[0-9a-fA-F]{64}", digest)
            and (hashes := self._keys(account, replica))
            and all(kind != "sha256" or value == digest.lower() for kind, value in hashes)
        ]
        if not proofs:
            self.error = ""
            return 0
        try:
            with self._database() as db:
                # NULL permanently disables a key observed with different SHA-256 digests.
                db.executemany(
                    "INSERT INTO content_hashes VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(provider, hash_type, remote_hash) DO UPDATE SET sha256="
                    "CASE WHEN content_hashes.sha256=excluded.sha256 "
                    "THEN content_hashes.sha256 ELSE NULL END",
                    (
                        (provider, kind, value, digest)
                        for provider, hashes, digest in proofs
                        for kind, value in hashes
                    ),
                )
                stored = sum(
                    self._lookup(db, provider, hashes) == digest
                    for provider, hashes, digest in proofs
                )
            self.error = (
                "동일한 검증 식별자에 서로 다른 SHA-256이 확인되어 검증 결과 재사용을 중단했습니다."
                if stored < len(proofs)
                else ""
            )
            return stored
        except (sqlite3.Error, OSError) as error:
            self._failed(error)
            return 0
