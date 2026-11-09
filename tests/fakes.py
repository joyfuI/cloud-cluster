import hashlib
from dataclasses import dataclass, replace

from cloud_cluster.domain import (
    BackendCapabilities,
    BackendError,
    ErrorKind,
    QuotaSnapshot,
    Replica,
)


@dataclass
class Node:
    data: bytes | None
    version: int


class FakeBackend:
    def __init__(self):
        self.nodes, self.failures, self.quotas, self.caps = {}, {}, {}, {}
        self.calls = []
        self.clock = 0
        self.no_hashes = set()
        self.hash_types = {}

    def check(self, operation, endpoint, context=None):
        if context:
            context.check()
        self.calls.append((operation, endpoint.fs, endpoint.path))
        failure = self.failures.get((operation, endpoint.fs, endpoint.path))
        if failure:
            raise failure

    def put(self, fs, path, data):
        parts = path.split("/")
        for i in range(1, len(parts)):
            self.nodes.setdefault((fs, "/".join(parts[:i])), Node(None, 0))
        self.clock += 1
        self.nodes[fs, path] = Node(data, self.clock)

    def replica(self, fs, path, account_id):
        node = self.nodes[fs, path]
        kind = self.hash_types.get((fs, path), "sha256")
        hashes = (
            {}
            if node.data is None or fs in self.no_hashes
            else {kind: hashlib.new(kind, node.data).hexdigest()}
        )
        return Replica(
            account_id,
            path,
            node.data is None,
            None if node.data is None else len(node.data),
            hashes=hashes,
            object_id=fs + "/" + path,
            version=str(node.version),
        )

    def list_dir(self, account, path, context=None, *, show_hash=True):
        from cloud_cluster.domain import Endpoint

        self.check("list", Endpoint(account.fs, path), context)
        prefix = path + "/" if path else ""
        replicas = tuple(
            self.replica(fs, name, account.id)
            for fs, name in self.nodes
            if fs == account.fs and name.startswith(prefix) and "/" not in name[len(prefix) :]
        )
        return replicas if show_hash else tuple(replace(replica, hashes={}) for replica in replicas)

    def stat(self, endpoint, context=None):
        self.check("stat", endpoint, context)
        if endpoint.path == "":
            return Replica(endpoint.account_id, "", True)
        if (endpoint.fs, endpoint.path) not in self.nodes:
            return None
        return self.replica(endpoint.fs, endpoint.path, endpoint.account_id)

    def features(self, account, context=None):
        return self.caps.get(account.id, BackendCapabilities(("sha256",), quota=True))

    def about(self, account, context=None):
        return self.quotas.get(account.id, QuotaSnapshot(100_000, 90_000))

    def copy_file(self, source, target, context=None):
        self.check("copy", target, context)
        failure = self.failures.get(("copy-any", target.fs, ""))
        if failure:
            raise failure
        if (target.fs, target.path) in self.nodes and self.nodes[target.fs, target.path].data is None:
            raise BackendError(ErrorKind.CHANGED)
        self.put(target.fs, target.path, self.nodes[source.fs, source.path].data)

    def rename(self, source, target, context=None, *, is_dir):
        self.check("rename", target, context)
        paths = [
            path
            for fs, path in self.nodes
            if fs == source.fs
            and (path == source.path or (is_dir and path.startswith(source.path + "/")))
        ]
        for path in paths:
            new_path = target.path + path[len(source.path) :]
            self.nodes[target.fs, new_path] = self.nodes.pop((source.fs, path))

    def mkdir(self, endpoint, context=None):
        self.check("mkdir", endpoint, context)
        if (endpoint.fs, endpoint.path) not in self.nodes:
            self.put(endpoint.fs, endpoint.path, None)

    def delete_file(self, endpoint, context=None):
        self.check("delete", endpoint, context)
        del self.nodes[endpoint.fs, endpoint.path]

    def rmdir(self, endpoint, context=None):
        self.check("rmdir", endpoint, context)
        if any(
            fs == endpoint.fs and name.startswith(endpoint.path + "/") for fs, name in self.nodes
        ):
            raise BackendError(ErrorKind.CHANGED, "폴더가 비어 있지 않습니다.")
        del self.nodes[endpoint.fs, endpoint.path]

    def hash_file(self, endpoint, context=None, *, download=True):
        self.check("hash", endpoint, context)
        return hashlib.sha256(self.nodes[endpoint.fs, endpoint.path].data).hexdigest()
