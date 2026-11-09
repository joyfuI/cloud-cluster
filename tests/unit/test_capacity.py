from random import Random

import pytest

from cloud_cluster.domain import CloudAccount, QuotaSnapshot
from cloud_cluster.operations import OperationEngine
from cloud_cluster.replicas import choose_accounts, effective_capacity


@pytest.mark.parametrize(
    "values, total, free",
    [
        ([], 0, 0),
        ([(1000, 900)], 0, 0),
        ([(100, 100), (100, 100)], 100, 100),
        ([(1000, 1000), (100, 100)], 100, 100),
        ([(100, 100)] * 3, 150, 150),
        ([(100, 100)] * 4, 200, 200),
        ([(100, 0), (100, 50), (100, 50)], 150, 50),
        ([(100, 100), (100, 0), (100, 0)], 150, 0),
        ([(1000, 100), (100, 100), (100, 100)], 200, 110),
        ([(1000, 1000), (100, 10), (100, 10)], 200, 20),
        ([(100, 100, 30), (100, 80, 20)], 100, 60),
        ([(100, 20, 30), (100, 100)], 100, 0),
        ([(5, 5)] * 3, 7, 7),
        ([(0, 0), (100, 100)], 0, 0),
        ([(0, 0), (0, 0)], 0, 0),
        ([(100, None), (100, 80)], 100, None),
        ([(None, 70), (100, 80)], None, None),
        ([(-1, 1), (100, 80)], None, None),
        ([(100, -1), (100, 80)], 100, None),
        ([(100, 101), (100, 80)], 100, None),
        ([(True, 0), (100, 80)], None, None),
        ([(100, 10, True), (100, 80)], 100, None),
        ([(100, "10"), (100, 80)], 100, None),
    ],
)
def test_effective_capacity_accounts_for_ratio_policy_and_two_distinct_replicas(
    values, total, free
):
    quotas = tuple(QuotaSnapshot(*value) for value in values)
    before = tuple(quotas)
    assert effective_capacity(quotas) == QuotaSnapshot(total, free)
    assert quotas == before


def test_capacity_applies_active_reservations_without_modifying_remote_quotas():
    engine = OperationEngine(None, [])
    quotas = {name: QuotaSnapshot(100, 100) for name in ("a", "b", "c")}
    engine._reserved = {"a": 80, "b": 40}
    assert engine.capacity(quotas) == effective_capacity(
        [QuotaSnapshot(100, 20), QuotaSnapshot(100, 60), QuotaSnapshot(100, 100)]
    )
    assert quotas == {name: QuotaSnapshot(100, 100) for name in ("a", "b", "c")}
    assert engine._reserved == {"a": 80, "b": 40}


def test_continuous_estimate_matches_actual_small_file_placement_with_byte_rounding():
    random = Random(17)
    for _ in range(100):
        accounts = [CloudAccount(str(i), str(i), "drive") for i in range(random.randint(2, 6))]
        quotas = {
            account.id: QuotaSnapshot(total := random.randint(10, 200), random.randint(0, total))
            for account in accounts
        }
        estimated, stored = effective_capacity(quotas.values()).free, 0
        while True:
            try:
                targets = choose_accounts(accounts, quotas, 1)
            except ValueError:
                break
            stored += 1
            for target in targets:
                quota = quotas[target.id]
                quotas[target.id] = QuotaSnapshot(quota.total, quota.free - 1)
        # Byte-sized placements can fall on either side of the continuous estimate.
        assert abs(estimated - stored) <= 2 * len(accounts)
