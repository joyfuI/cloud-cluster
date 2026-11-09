"""Provider-specific recycle bins. No empty-trash or permanent-delete API."""

import json
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .domain import BackendError, Endpoint, ErrorKind
from .replicas import path_key, validate_path


@dataclass(frozen=True)
class TrashItem:
    id: str
    name: str
    original_path: str | None
    deleted_at: str | None
    is_dir: bool
    parent_id: str = ""


@dataclass(frozen=True)
class TrashCapabilities:
    list_items: bool
    restore_item: bool
    web_url: str


def capabilities(account):
    if account.provider == "drive":
        return TrashCapabilities(True, not account.readonly, "https://drive.google.com/drive/trash")
    if account.provider == "onedrive":
        return TrashCapabilities(False, False, "https://onedrive.live.com/?qt=recyclebin")
    if account.provider == "dropbox":
        return TrashCapabilities(False, False, "https://www.dropbox.com/deleted_files")
    return TrashCapabilities(False, False, "")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        return None


class GoogleTrashAdapter:
    API = "https://www.googleapis.com/drive/v3/files"

    def __init__(self, backend, account):
        if account.provider != "drive":
            raise ValueError("Google Drive 계정이 필요합니다.")
        self.backend, self.account = backend, account
        self._opener = build_opener(NoRedirect())
        self._parents = {}

    def _request(self, suffix="", *, params=None, body=None, context=None):
        if context:
            context.check()
        # A native read lets rclone refresh its token; Python never owns a refresh-token flow.
        self.backend.stat(Endpoint(self.account.fs, "", self.account.id), context)
        config = self.backend.rpc("config/get", name=self.account.remote)
        try:
            token = json.loads(config["token"])["access_token"]
        except KeyError, ValueError, TypeError:
            raise BackendError(ErrorKind.AUTH) from None
        url = self.API + suffix + ("?" + urlencode(params) if params else "")
        request = Request(
            url,
            data=None if body is None else json.dumps(body).encode(),
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
            method="GET" if body is None else "PATCH",
        )
        try:
            with self._opener.open(request, timeout=30) as response:
                result = json.load(response)
        except HTTPError as error:
            kind = {401: ErrorKind.AUTH, 403: ErrorKind.PERMISSION, 429: ErrorKind.RATE_LIMIT}.get(
                error.code, ErrorKind.UNKNOWN
            )
            raise BackendError(
                kind, f"Google Drive 휴지통: {kind.value} (HTTP {error.code})"
            ) from None
        except URLError, TimeoutError:
            raise BackendError(ErrorKind.NETWORK) from None
        if context:
            context.check()
        return result

    def _parent_path(self, item_id, context, visited=None):
        visited = set() if visited is None else visited
        if item_id in visited or len(visited) > 128:
            return None
        if item_id not in self._parents:
            self._parents[item_id] = self._request(
                "/" + quote(item_id, safe=""),
                params={"fields": "id,name,parents", "supportsAllDrives": "true"},
                context=context,
            )
        item = self._parents[item_id]
        if item_id == self._root_id:
            return ""
        parents = item.get("parents", [])
        if len(parents) != 1:
            return None
        visited.add(item_id)
        parent = self._parent_path(parents[0], context, visited)
        return None if parent is None else "/".join(filter(None, [parent, item["name"]]))

    def list_items(self, context=None):
        self._parents.clear()
        config = self.backend.rpc("config/get", name=self.account.remote)
        root = config.get("root_folder_id") or config.get("team_drive") or "root"
        self._root_id = self._request(
            "/" + quote(root, safe=""),
            params={"fields": "id", "supportsAllDrives": "true"},
            context=context,
        )["id"]
        items, page = [], ""
        while True:
            params = {
                "q": "trashed=true",
                "pageSize": 1000,
                "fields": "nextPageToken,incompleteSearch,files(id,name,mimeType,parents,trashedTime)",
                "supportsAllDrives": "true",
                "includeItemsFromAllDrives": "true",
            }
            if page:
                params["pageToken"] = page
            result = self._request(params=params, context=context)
            if result.get("incompleteSearch"):
                raise BackendError(ErrorKind.UNKNOWN, "휴지통 검색이 완전하지 않습니다.")
            for item in result.get("files", []):
                parents = item.get("parents", [])
                parent = None
                if len(parents) == 1:
                    try:
                        parent = self._parent_path(parents[0], context)
                    except BackendError:
                        pass
                original = (
                    None if parent is None else "/".join(filter(None, [parent, item["name"]]))
                )
                items.append(
                    TrashItem(
                        item["id"],
                        item["name"],
                        original,
                        item.get("trashedTime"),
                        item.get("mimeType") == "application/vnd.google-apps.folder",
                        parents[0] if len(parents) == 1 else "",
                    )
                )
            page = result.get("nextPageToken", "")
            if not page:
                return tuple(items)

    def restore(self, item, context=None, *, approved=False):
        if not approved or self.account.readonly:
            raise PermissionError("선택 복원 승인이 필요합니다.")
        if not item.original_path or not item.parent_id:
            raise ValueError("원래 경로를 확인할 수 없습니다. 웹 휴지통을 이용하세요.")
        validate_path(item.original_path)
        root = path_key(self.account.root)
        if root and not path_key(item.original_path).startswith(root + "/"):
            raise ValueError("앱 관리 루트 밖의 항목입니다. 웹 휴지통을 이용하세요.")
        current = self._request(
            "/" + quote(item.id, safe=""),
            params={"fields": "id,name,parents,trashed", "supportsAllDrives": "true"},
            context=context,
        )
        if (
            not current.get("trashed")
            or current.get("name") != item.name
            or current.get("parents") != [item.parent_id]
        ):
            raise BackendError(ErrorKind.CHANGED)
        parent = item.parent_id.replace("\\", "\\\\").replace("'", "\\'")
        listing = self._request(
            params={
                "q": f"'{parent}' in parents and trashed=false",
                "pageSize": 1000,
                "fields": "files(name),nextPageToken,incompleteSearch",
            },
            context=context,
        )
        if listing.get("nextPageToken") or listing.get("incompleteSearch"):
            raise ValueError("충돌 검사가 완전하지 않습니다. 웹에서 확인하세요.")
        if any(
            path_key(entry["name"]) == path_key(item.name) for entry in listing.get("files", [])
        ):
            raise ValueError("대상 경로에 같은 이름이 있습니다. 덮어쓰지 않았습니다.")
        return self._request(
            "/" + quote(item.id, safe=""),
            params={"fields": "id,name,trashed", "supportsAllDrives": "true"},
            body={"trashed": False},
            context=context,
        )
