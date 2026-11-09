import hashlib
import json
import sqlite3
from concurrent.futures.thread import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone

import pytest

from cloud_cluster.domain import BackendError, ErrorKind, Replica, Status
from cloud_cluster.verification import HASH_LENGTHS, VerificationStore
from cloud_cluster.vfs import VirtualFileSystem

DIGEST = hashlib.sha256(b"same").hexdigest()
REMOTE_HASH = hashlib.md5(b"same").hexdigest()


def test_different_remote_hashes_reuse_byte_proofs_after_restart(
    backend, accounts, tmp_path, monkeypatch
):
    a, b = accounts[:2]
    for account in (a, b):
        backend.put(account.fs, "x", b"same")
    original = backend.replica

    def remote_hash(fs, path, account_id):
        item = original(fs, path, account_id)
        kind = "md5" if account_id == a.id else "sha1"
        return replace(
            item,
            version="",
            hashes={kind: hashlib.new(kind, backend.nodes[fs, path].data).hexdigest()},
        )

    monkeypatch.setattr(backend, "replica", remote_hash)
    path = tmp_path / "content_hashes.db"
    vfs = VirtualFileSystem(backend, (a, b), verification_store=VerificationStore(path))
    try:
        entry = vfs.refresh("").entries[0]
        assert entry.assessment.status == Status.UNVERIFIED
        assert vfs.record_verification((r, DIGEST) for r in entry.replicas) == 2
        verified = vfs.cached("").entries[0]
        assert verified.assessment.status == Status.HEALTHY
        for raw, proof in zip(entry.replicas, verified.replicas):
            assert raw.hashes == proof.hashes and raw.fingerprint == proof.fingerprint
        assert vfs.refresh("").entries[0].assessment.status == Status.HEALTHY
    finally:
        vfs.close()

    restarted = VirtualFileSystem(backend, (a, b), verification_store=VerificationStore(path))
    try:
        assert restarted.scan("") == ((), True, 1)
        assert restarted.cached("").entries[0].assessment.verification == "정밀 검증 SHA-256"
        backend.put(b.fs, "x", b"diff")
        changed = restarted.refresh("").entries[0]
        assert changed.assessment.status == Status.UNVERIFIED
        assert not changed.replicas[1].verified_sha256
        assert restarted.record_verification([(changed.replicas[1], "f" * 64)]) == 1
        assert restarted.cached("").entries[0].assessment.status == Status.CONFLICT
        backend.failures["list", b.fs, ""] = BackendError(ErrorKind.NETWORK)
        assert restarted.refresh("").entries[0].assessment.status == Status.UNKNOWN
        assert all(call[0] == "list" for call in backend.calls)
    finally:
        restarted.close()


@pytest.fixture
def remote_replica():
    return Replica(
        "a",
        "한글.txt",
        size=4,
        modified=datetime(2026, 11, 4, tzinfo=UTC),
        hashes={"md5": REMOTE_HASH},
        object_id="object",
        version="content-v1",
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"path": "other/이동.TXT"},
        {"account_id": "other"},
        {"object_id": "new-object"},
        {"version": "content-v2"},
        {"size": 5},
        {"size": None},
        {"modified": None},
    ],
)
def test_content_hash_reuse_is_independent_of_file_metadata(
    accounts, tmp_path, remote_replica, changes
):
    store = VerificationStore(tmp_path / "content_hashes.db")
    assert store.record([(accounts[0], remote_replica, DIGEST)]) == 1
    changed = replace(remote_replica, **changes)
    restored = store.restore(accounts[0], (changed,))[0]
    assert restored.verified_sha256 == DIGEST
    assert restored.fingerprint == changed.fingerprint and restored.hashes == changed.hashes


@pytest.mark.parametrize("change", ["id", "root", "remote"])
def test_accounts_and_roots_share_provider_content_hashes(
    accounts, tmp_path, remote_replica, change
):
    path = tmp_path / "content_hashes.db"
    account = accounts[0]
    assert VerificationStore(path).record([(account, remote_replica, DIGEST)]) == 1
    other = replace(account, **{change: "different"})
    assert VerificationStore(path).restore(other, (remote_replica,))[0].verified_sha256 == DIGEST


def test_database_contains_only_provider_hash_mapping(accounts, tmp_path, remote_replica):
    store = VerificationStore(tmp_path / "content_hashes.db")
    assert store.record([(accounts[0], remote_replica, DIGEST)]) == 1
    with sqlite3.connect(store.path) as db:
        assert [row[1] for row in db.execute("PRAGMA table_info(content_hashes)")] == [
            "provider",
            "hash_type",
            "remote_hash",
            "sha256",
        ]
        assert db.execute("SELECT * FROM content_hashes").fetchall() == [
            (accounts[0].provider, "md5", REMOTE_HASH, DIGEST)
        ]


def test_provider_and_hash_type_are_distinct_keys(accounts, tmp_path, remote_replica):
    store = VerificationStore(tmp_path / "content_hashes.db")
    other_provider = replace(accounts[0], provider="other")
    other_hash = replace(remote_replica, hashes={"sha1": "a" * 40})
    assert store.record([(accounts[0], remote_replica, DIGEST)]) == 1
    assert not store.restore(other_provider, (remote_replica,))[0].verified_sha256
    assert not store.restore(accounts[0], (other_hash,))[0].verified_sha256
    for kind, digest in (("dropbox", DIGEST), ("blake3", "f" * 64)):
        replica = replace(remote_replica, hashes={kind: "a" * 64})
        assert store.record([(accounts[0], replica, digest)]) == 1
        assert store.restore(accounts[0], (replica,))[0].verified_sha256 == digest
    assert store.record([(other_provider, remote_replica, "f" * 64)]) == 1
    assert store.restore(accounts[0], (remote_replica,))[0].verified_sha256 == DIGEST
    assert store.restore(other_provider, (remote_replica,))[0].verified_sha256 == "f" * 64


@pytest.mark.parametrize("kind,length", HASH_LENGTHS.items())
def test_supported_hashes_normalize_hex_case(accounts, tmp_path, remote_replica, kind, length):
    store = VerificationStore(tmp_path / "content_hashes.db")
    value = DIGEST if kind == "sha256" else "a" * length
    replica = replace(remote_replica, hashes={kind: value.upper()})
    assert store.record([(accounts[0], replica, DIGEST.upper())]) == 1
    current = replace(replica, hashes={kind: value})
    assert store.restore(accounts[0], (current,))[0].verified_sha256 == DIGEST


@pytest.mark.parametrize(
    "hashes",
    [
        {"quickxor": "a" * 40},
        {"crc32": "a" * 8},
        {"md5": ""},
        {"md5": "short"},
        {"md5": "g" * 32},
        {"md5": 1},
        {"sha1": "a" * 39},
    ],
)
def test_unsupported_or_invalid_hashes_do_not_bind_proofs(
    accounts, tmp_path, remote_replica, hashes
):
    replica = replace(remote_replica, hashes=hashes)
    store = VerificationStore(tmp_path / "content_hashes.db")
    assert store.record([(accounts[0], replica, DIGEST)]) == 0
    assert store.restore(accounts[0], (replica,)) == (replica,)
    assert not store.path.exists()


def test_changed_hash_and_directories_do_not_restore(accounts, tmp_path, remote_replica):
    store = VerificationStore(tmp_path / "content_hashes.db")
    assert store.record([(accounts[0], remote_replica, DIGEST)]) == 1
    for replica in (
        replace(remote_replica, hashes={"md5": "f" * 32}),
        replace(remote_replica, is_dir=True),
    ):
        assert not store.restore(accounts[0], (replica,))[0].verified_sha256


def test_one_file_with_multiple_hashes_counts_once(accounts, tmp_path, remote_replica):
    store = VerificationStore(tmp_path / "content_hashes.db")
    replica = replace(remote_replica, hashes={"md5": REMOTE_HASH, "sha1": "a" * 40})
    assert store.record([(accounts[0], replica, DIGEST)]) == 1
    for kind, value in replica.hashes.items():
        assert (
            store.restore(accounts[0], (replace(replica, hashes={kind: value}),))[0].verified_sha256
            == DIGEST
        )
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT count(*) FROM content_hashes").fetchone()[0] == 2


@pytest.mark.parametrize("same_batch", [False, True])
def test_conflicting_hash_key_stays_disabled_after_restart(
    accounts, tmp_path, remote_replica, same_batch
):
    path = tmp_path / "content_hashes.db"
    store = VerificationStore(path)
    proofs = [(accounts[0], remote_replica, DIGEST), (accounts[0], remote_replica, "f" * 64)]
    if same_batch:
        assert store.record(proofs) == 0
    else:
        assert store.record(proofs[:1]) == 1
        assert store.record(proofs[1:]) == 0
    assert "서로 다른 SHA-256" in store.error
    store = VerificationStore(path)
    assert not store.restore(accounts[0], (remote_replica,))[0].verified_sha256
    assert store.record(proofs[:1]) == store.record(proofs[1:]) == 0
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT sha256 FROM content_hashes").fetchall() == [(None,)]


def test_disagreeing_cached_hashes_do_not_choose_an_arbitrary_digest(
    accounts, tmp_path, remote_replica
):
    store = VerificationStore(tmp_path / "content_hashes.db")
    a = remote_replica
    b = replace(a, hashes={"sha1": "a" * 40})
    assert store.record([(accounts[0], a, DIGEST), (accounts[0], b, "f" * 64)]) == 2
    current = replace(a, hashes=a.hashes | b.hashes)
    assert not store.restore(accounts[0], (current,))[0].verified_sha256


def test_proof_without_reusable_metadata_only_updates_current_memory(
    backend, accounts, tmp_path, monkeypatch
):
    a, b = accounts[:2]
    original = backend.replica
    monkeypatch.setattr(
        backend,
        "replica",
        lambda *args: replace(original(*args), hashes={}, object_id="", version="", modified=None),
    )
    for account in (a, b):
        backend.put(account.fs, "x", b"same")
    vfs = VirtualFileSystem(
        backend, (a, b), verification_store=VerificationStore(tmp_path / "content_hashes.db")
    )
    try:
        replicas = vfs.refresh("").entries[0].replicas
        assert vfs.record_verification((r, DIGEST) for r in replicas) == 0
        assert vfs.cached("").entries[0].assessment.status == Status.HEALTHY
        assert vfs.refresh("").entries[0].assessment.status == Status.UNVERIFIED
    finally:
        vfs.close()


@pytest.mark.parametrize("failure", ["corrupt", "directory", "locked", "schema"])
def test_database_failure_preserves_files_and_returns_no_proof(
    accounts, tmp_path, remote_replica, failure
):
    path = tmp_path / "content_hashes.db"
    store = VerificationStore(path)
    lock = None
    if failure == "corrupt":
        path.write_bytes(b"keep damaged database")
    elif failure == "directory":
        path.mkdir()
    elif failure == "schema":
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE content_hashes (unchanged TEXT)")
    else:
        assert store.record([(accounts[0], remote_replica, DIGEST)]) == 1
        lock = sqlite3.connect(path)
        lock.execute("BEGIN EXCLUSIVE")
    try:
        assert store.restore(accounts[0], (remote_replica,)) == (remote_replica,)
        assert store.error
        assert store.record([(accounts[0], remote_replica, DIGEST)]) == 0
        assert store.error
        if failure == "corrupt":
            assert path.read_bytes() == b"keep damaged database"
        elif failure == "directory":
            assert path.is_dir()
        elif failure == "schema":
            with sqlite3.connect(path) as db:
                assert [r[1] for r in db.execute("PRAGMA table_info(content_hashes)")] == [
                    "unchanged"
                ]
    finally:
        if lock:
            lock.close()


def test_invalid_sha256_is_not_saved_or_reused(accounts, tmp_path, remote_replica):
    store = VerificationStore(tmp_path / "content_hashes.db")
    assert store.record([(accounts[0], remote_replica, "invalid")]) == 0
    own_sha = replace(remote_replica, hashes={"sha256": "f" * 64})
    assert store.record([(accounts[0], own_sha, DIGEST)]) == 0
    assert store.record([(accounts[0], remote_replica, DIGEST)]) == 1
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE content_hashes SET sha256=?", ("z" * 64,))
    assert not store.restore(accounts[0], (remote_replica,))[0].verified_sha256


def test_failed_batch_rolls_back_all_hash_mappings(accounts, tmp_path, remote_replica):
    store = VerificationStore(tmp_path / "content_hashes.db")
    store.restore(accounts[0], (remote_replica,))
    with sqlite3.connect(store.path) as db:
        db.execute(
            "CREATE TRIGGER fail_write BEFORE INSERT ON content_hashes "
            "WHEN NEW.remote_hash='" + "f" * 32 + "' BEGIN SELECT RAISE(ABORT, 'test'); END"
        )
    other = replace(remote_replica, hashes={"md5": "f" * 32})
    assert store.record([(accounts[0], remote_replica, DIGEST), (accounts[0], other, DIGEST)]) == 0
    assert store.error
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT count(*) FROM content_hashes").fetchone()[0] == 0


def test_concurrent_paths_deduplicate_one_content_mapping(accounts, tmp_path, remote_replica):
    store = VerificationStore(tmp_path / "content_hashes.db")

    def verify(index):
        replica = replace(remote_replica, path=f"folder/{index}", version=str(index))
        assert store.record([(accounts[0], replica, DIGEST)]) == 1
        assert store.restore(accounts[0], (replica,))[0].verified_sha256 == DIGEST

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(verify, range(12)))
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT count(*) FROM content_hashes").fetchone()[0] == 1


def test_object_version_uses_scoped_structured_key_and_reuses_after_move(
    accounts, tmp_path, remote_replica
):
    account = accounts[0]
    replica = replace(remote_replica, hashes={}, object_id='物件",[1]', version="v1:2")
    store = VerificationStore(tmp_path / "content_hashes.db")
    assert store.record([(account, replica, DIGEST)]) == 1
    moved = replace(replica, path="new/name", size=9, modified=None)
    assert VerificationStore(store.path).restore(account, (moved,))[0].verified_sha256 == DIGEST
    with sqlite3.connect(store.path) as db:
        provider, kind, value, digest = db.execute("SELECT * FROM content_hashes").fetchone()
        assert (provider, kind, digest) == (account.provider, "object-version", DIGEST)
        assert json.loads(value) == [
            account.id,
            account.fs,
            replica.object_id,
            replica.version,
        ]
    assert store.record([(account, replica, "f" * 64)]) == 0
    assert store.record([(account, replica, DIGEST)]) == 0
    assert not store.restore(account, (replica,))[0].verified_sha256


@pytest.mark.parametrize("field", ["id", "root", "remote", "provider", "object_id", "version"])
def test_object_versions_do_not_cross_account_or_revision_boundaries(
    accounts, tmp_path, remote_replica, field
):
    account = accounts[0]
    replica = replace(remote_replica, hashes={})
    store = VerificationStore(tmp_path / "content_hashes.db")
    assert store.record([(account, replica, DIGEST)]) == 1
    if field in {"object_id", "version"}:
        replica = replace(replica, **{field: "changed"})
    else:
        account = replace(account, **{field: "changed"})
    assert not store.restore(account, (replica,))[0].verified_sha256


@pytest.mark.parametrize(
    "changes",
    [
        {"version": None},
        {"version": 1},
        {"object_id": None},
        {"is_dir": True},
        {"hashes": {"quickxor": "abc"}},
        {"hashes": {"md5": "invalid"}},
    ],
)
def test_object_fallback_rejects_invalid_identity_or_hashes(
    accounts, tmp_path, remote_replica, changes
):
    store = VerificationStore(tmp_path / "content_hashes.db")
    account = accounts[0]
    replica = replace(
        remote_replica, hashes={}, **{k: v for k, v in changes.items() if k != "hashes"}
    )
    if "hashes" in changes:
        replica = replace(replica, hashes=changes["hashes"])
    assert store.record([(account, replica, DIGEST)]) == 0
    assert store.restore(account, (replica,)) == (replica,)
    assert not store.path.exists()


def test_object_and_hash_cache_work_without_configuration_scope(accounts, tmp_path, remote_replica):
    store = VerificationStore(tmp_path / "content_hashes.db")
    no_hash = replace(remote_replica, hashes={})
    assert store.record([(accounts[0], no_hash, DIGEST)]) == 1
    assert store.record([(accounts[0], remote_replica, DIGEST)]) == 1
    with sqlite3.connect(store.path) as db:
        assert set(db.execute("SELECT hash_type FROM content_hashes")) == {
            ("md5",),
            ("object-version",),
        }


def test_versionless_object_reuses_metadata_after_restart_and_timezone_change(
    accounts, tmp_path, remote_replica
):
    account = accounts[0]
    replica = replace(remote_replica, hashes={}, version="", size=0)
    store = VerificationStore(tmp_path / "content_hashes.db")
    assert store.record([(account, replica, DIGEST)]) == 1
    moved = replace(
        replica, path="moved", modified=replica.modified.astimezone(timezone(timedelta(hours=9)))
    )
    restored = VerificationStore(store.path).restore(account, (moved,))[0]
    assert restored.verified_sha256 == DIGEST and restored.fingerprint == moved.fingerprint
    with sqlite3.connect(store.path) as db:
        kind, value = db.execute("SELECT hash_type, remote_hash FROM content_hashes").fetchone()
        assert kind == "object-metadata"
        assert json.loads(value) == [
            account.id,
            account.fs,
            replica.object_id,
            0,
            "2026-11-04T00:00:00.000000+00:00",
        ]
    assert store.record([(account, replica, "f" * 64)]) == 0
    assert store.record([(account, replica, DIGEST)]) == 0
    assert not VerificationStore(store.path).restore(account, (replica,))[0].verified_sha256


@pytest.mark.parametrize("change", ["size", "modified", "object_id", "version", "account"])
def test_metadata_proof_misses_when_identity_or_metadata_changes(
    accounts, tmp_path, remote_replica, change
):
    account = accounts[0]
    replica = replace(remote_replica, hashes={}, version="")
    store = VerificationStore(tmp_path / "content_hashes.db")
    assert store.record([(account, replica, DIGEST)]) == 1
    if change == "account":
        account = accounts[1]
    else:
        replica = replace(
            replica,
            **{
                change: {
                    "size": 5,
                    "modified": replica.modified + timedelta(microseconds=1),
                    "object_id": "new-object",
                    "version": "content-v2",
                }[change]
            },
        )
    assert not store.restore(account, (replica,))[0].verified_sha256


@pytest.mark.parametrize(
    "changes",
    [
        {"size": None},
        {"size": -1},
        {"size": True},
        {"size": 4.0},
        {"modified": None},
        {"modified": "2026-11-04"},
        {"modified": datetime(2026, 11, 4)},
    ],
)
@pytest.mark.parametrize("object_id", ["object", ""])
def test_metadata_cache_requires_valid_size_and_aware_modified_time(
    accounts, tmp_path, remote_replica, changes, object_id
):
    account = accounts[0]
    replica = replace(remote_replica, hashes={}, version="", object_id=object_id, **changes)
    store = VerificationStore(tmp_path / "content_hashes.db")
    assert store.record([(account, replica, DIGEST)]) == 0
    assert store.restore(account, (replica,)) == (replica,)
    assert not store.path.exists()


def test_hash_and_content_version_take_priority_over_metadata_keys(
    accounts, tmp_path, remote_replica
):
    account = accounts[0]
    store = VerificationStore(tmp_path / "content_hashes.db")
    assert store.record([(account, remote_replica, DIGEST)]) == 1
    versioned = replace(remote_replica, hashes={})
    assert store.record([(account, versioned, DIGEST)]) == 1
    with sqlite3.connect(store.path) as db:
        assert set(db.execute("SELECT hash_type FROM content_hashes")) == {
            ("md5",),
            ("object-version",),
        }
    without_version = replace(versioned, version="")
    assert not store.restore(account, (without_version,))[0].verified_sha256


def test_path_metadata_coexists_with_existing_database_keys_without_conversion(
    accounts, tmp_path, remote_replica
):
    account = accounts[0]
    store = VerificationStore(tmp_path / "content_hashes.db")
    versioned = replace(remote_replica, hashes={}, object_id=remote_replica.path)
    metadata = replace(versioned, version="")
    old = [(account, r, DIGEST) for r in (remote_replica, versioned, metadata)]
    assert store.record(old) == 3
    with sqlite3.connect(store.path) as db:
        previous = db.execute("SELECT * FROM content_hashes ORDER BY hash_type").fetchall()
        schema = db.execute("SELECT sql FROM sqlite_master WHERE type='table'").fetchall()
    path_only = replace(metadata, object_id="")
    assert store.record([(account, path_only, DIGEST)]) == 1
    reopened = VerificationStore(store.path)
    assert reopened.restore(account, (path_only,))[0].verified_sha256 == DIGEST
    for _, replica, _ in old:
        assert reopened.restore(account, (replica,))[0].verified_sha256 == DIGEST
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT sql FROM sqlite_master WHERE type='table'").fetchall() == schema
        assert (
            db.execute(
                "SELECT * FROM content_hashes WHERE hash_type!='path-metadata' ORDER BY hash_type"
            ).fetchall()
            == previous
        )
        value, digest = db.execute(
            "SELECT remote_hash, sha256 FROM content_hashes WHERE hash_type='path-metadata'"
        ).fetchone()
        assert digest == DIGEST
        assert json.loads(value) == [
            account.id,
            account.fs,
            path_only.path,
            path_only.size,
            "2026-11-04T00:00:00.000000+00:00",
        ]
    assert reopened.record([(account, path_only, "f" * 64)]) == 0
    assert reopened.record([(account, path_only, DIGEST)]) == 0
    assert not VerificationStore(store.path).restore(account, (path_only,))[0].verified_sha256
    assert reopened.restore(account, (metadata,))[0].verified_sha256 == DIGEST


@pytest.mark.parametrize(
    "change",
    ["path", "case", "size", "modified", "object_id", "account", "root", "provider"],
)
def test_path_metadata_does_not_cross_path_metadata_or_account_boundaries(
    accounts, tmp_path, remote_replica, change
):
    account = accounts[0]
    replica = replace(remote_replica, hashes={}, object_id="", version="")
    store = VerificationStore(tmp_path / "content_hashes.db")
    assert store.record([(account, replica, DIGEST)]) == 1
    if change in {"account", "root", "provider"}:
        field, value = {
            "account": ("id", "other"),
            "root": ("root", "elsewhere"),
            "provider": ("provider", "other"),
        }[change]
        account = replace(account, **{field: value})
    else:
        field, value = {
            "path": ("path", "moved/한글.txt"),
            "case": ("path", "한글.TXT"),
            "size": ("size", 5),
            "modified": ("modified", replica.modified + timedelta(microseconds=1)),
            "object_id": ("object_id", "new-id"),
        }[change]
        replica = replace(replica, **{field: value})
    assert not store.restore(account, (replica,))[0].verified_sha256


@pytest.mark.parametrize("path", [None, "", "../x", "a/../x", "/x", "a//x", "a\\x"])
def test_path_cache_rejects_missing_or_unsafe_relative_paths(
    accounts, tmp_path, remote_replica, path
):
    account = accounts[0]
    replica = replace(remote_replica, hashes={}, object_id="", version="", path=path)
    store = VerificationStore(tmp_path / "content_hashes.db")
    assert store.record([(account, replica, DIGEST)]) == 0
    assert store.restore(account, (replica,)) == (replica,)
    assert not store.path.exists()


@pytest.mark.parametrize("kind", ["object-version", "object-metadata", "path-metadata"])
def test_previous_configuration_scoped_keys_are_left_untouched_and_not_reused(
    accounts, tmp_path, remote_replica, kind
):
    account = accounts[0]
    replica = replace(
        remote_replica,
        hashes={},
        object_id="" if kind == "path-metadata" else "object",
        version="v1" if kind == "object-version" else "",
    )
    identity = [account.id, account.fs, replica.object_id or replica.path]
    identity += ["v1"] if kind == "object-version" else [4, "2026-11-04T00:00:00.000000+00:00"]
    old_value = json.dumps(["a" * 64, *identity], ensure_ascii=False, separators=(",", ":"))
    store = VerificationStore(tmp_path / "content_hashes.db")
    store.restore(account, (replica,))
    with sqlite3.connect(store.path) as db:
        db.execute(
            "INSERT INTO content_hashes VALUES (?, ?, ?, ?)",
            (account.provider, kind, old_value, "f" * 64),
        )
    assert not store.restore(account, (replica,))[0].verified_sha256
    assert store.record([(account, replica, DIGEST)]) == 1
    assert VerificationStore(store.path).restore(account, (replica,))[0].verified_sha256 == DIGEST
    with sqlite3.connect(store.path) as db:
        assert set(db.execute("SELECT remote_hash, sha256 FROM content_hashes")) == {
            (old_value, "f" * 64),
            (json.dumps(identity, ensure_ascii=False, separators=(",", ":")), DIGEST),
        }
