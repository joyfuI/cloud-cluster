from dataclasses import replace

import pytest

from cloud_cluster.domain import Listing, QuotaSnapshot, Replica, Status
from cloud_cluster.replicas import choose_accounts, merge, path_key, validate_path


def file(account, content="same", name="x.txt", hashes=True):
    return Replica(account, name, size=4, hashes={"sha256": content} if hashes else {})


@pytest.mark.parametrize(
    "replicas,status",
    [
        ((file("a"), file("b")), Status.HEALTHY),
        ((file("a"),), Status.UNDER),
        ((file("a"), file("b", "diff")), Status.CONFLICT),
        ((file("a"), file("b"), file("c")), Status.OVER),
        ((file("a", hashes=False), file("b", hashes=False)), Status.UNVERIFIED),
        ((file("a"), file("b", name="X.txt")), Status.CONFLICT),
        ((file("a"), file("a")), Status.CONFLICT),
        ((file("a"), Replica("b", "x.txt", is_dir=True)), Status.CONFLICT),
    ],
)
def test_statuses(replicas, status):
    listings = [
        Listing(a, tuple(r for r in replicas if r.account_id == a))
        for a in {r.account_id for r in replicas}
    ]
    assert merge(listings, {})[0].assessment.status == status


def test_failure_is_not_absence():
    result = merge([Listing("a", (file("a"),)), Listing("b", state="error")], {})
    assert result[0].assessment.status == Status.UNKNOWN
    assert result[0].assessment.count == 1


def test_conflict_has_priority_over_incomplete():
    result = merge(
        [
            Listing("a", (file("a"),)),
            Listing("b", (file("b", "diff"),)),
            Listing("c", state="error"),
        ],
        {},
    )
    assert result[0].assessment.status == Status.CONFLICT


def test_weak_checksums_do_not_prove_equality():
    copies = [Replica(a, "x", size=4, hashes={"quickxor": "same"}) for a in ("a", "b")]
    assert (
        merge([Listing(r.account_id, (r,)) for r in copies], {})[0].assessment.status
        == Status.UNVERIFIED
    )


def test_verified_sha256_compares_to_native_hash_without_hiding_remote_conflicts():
    from cloud_cluster.replicas import compare

    proof = Replica("a", "x", size=4, hashes={"md5": "cloud"}, verified_sha256="a" * 64)
    native = Replica("b", "x", size=4, hashes={"sha256": "a" * 64})
    assert compare(proof, native) == "same"
    assert compare(proof, replace(native, hashes={"sha256": "b" * 64})) == "different"
    assert compare(proof, replace(proof, hashes={"md5": "changed"})) == "different"


@pytest.mark.parametrize(
    "name",
    [
        "CON",
        "NUL.txt",
        "COM¹.png",
        "a.",
        "a ",
        "a?.txt",
        "../x",
        "C:/x",
        "/x",
        "a//b",
        "a\\b",
        "a\x00b",
    ],
)
def test_invalid_names(name):
    with pytest.raises(ValueError):
        validate_path(name)


def test_unicode_normalization_is_comparison_only():
    original = "e\u0301.txt"
    assert path_key(original) == path_key("é.TXT")
    assert validate_path(original) == original


def test_placement_balances_ratio_and_is_deterministic(accounts):
    quotas = {
        "a": QuotaSnapshot(1000, 500),
        "b": QuotaSnapshot(100, 90),
        "c": QuotaSnapshot(200, 180),
    }
    assert [a.id for a in choose_accounts(accounts, quotas, 10)] == ["c", "b"]
    assert [a.id for a in choose_accounts(list(reversed(accounts)), {}, 10)] == ["a", "b"]
    assert choose_accounts(accounts, quotas, 10, existing=("a",))[0].id == "a"


def test_capacity_unknown_and_insufficient(accounts):
    assert [a.id for a in choose_accounts(accounts, {"a": QuotaSnapshot(10, 1)}, 20)] == ["b", "c"]
    with pytest.raises(ValueError):
        choose_accounts([replace(a, readonly=True) for a in accounts], {}, 1)


def test_directory_mismatch_and_no_empty_directory_support():
    from cloud_cluster.domain import BackendCapabilities

    rows = [Listing("a", (Replica("a", "folder", True),)), Listing("b")]
    assert merge(rows, {})[0].assessment.status == Status.DIRECTORY
    assert (
        merge(rows, {"b": BackendCapabilities(empty_directories=False)})[0].assessment.status
        == Status.HEALTHY
    )
