from dataclasses import replace

import pytest

from cloud_cluster.trash import GoogleTrashAdapter, TrashItem, capabilities


def test_unsupported_provider_has_web_only(accounts):
    account = replace(accounts[0], provider="onedrive")
    cap = capabilities(account)
    assert cap.web_url.startswith("https://") and not cap.list_items and not cap.restore_item


def test_google_restore_blocks_existing_case_variant(backend, accounts):
    adapter = GoogleTrashAdapter(backend, accounts[0])
    item = TrashItem("deleted", "X.txt", "root/X.txt", None, False, "parent")
    calls = []

    def request(suffix="", *, params=None, body=None, context=None):
        calls.append((suffix, body))
        if suffix:
            return {"id": "deleted", "name": "X.txt", "parents": ["parent"], "trashed": True}
        return {"files": [{"name": "x.TXT"}]}

    adapter._request = request
    with pytest.raises(ValueError):
        adapter.restore(item, approved=True)
    assert not any(body for _, body in calls)


def test_google_restore_requires_approval_and_known_path(backend, accounts):
    adapter = GoogleTrashAdapter(backend, accounts[0])
    item = TrashItem("deleted", "x", None, None, False)
    with pytest.raises(PermissionError):
        adapter.restore(item)
    with pytest.raises(ValueError):
        adapter.restore(item, approved=True)
