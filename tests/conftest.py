import pytest

from cloud_cluster.domain import CloudAccount

from .fakes import FakeBackend


@pytest.fixture
def accounts():
    return [CloudAccount(name, name, "drive", "root") for name in ("a", "b", "c")]


@pytest.fixture
def backend():
    return FakeBackend()
