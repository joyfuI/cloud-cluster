from dataclasses import dataclass, field
from pathlib import PurePosixPath

from PySide6.QtCore import (
    QAbstractItemModel,
    QAbstractTableModel,
    QModelIndex,
    QSortFilterProxyModel,
    Qt,
    Signal,
)
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import QApplication, QStyle

from ..domain import Status
from ..replicas import path_key


def size_text(size):
    if size is None:
        return "—"
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:,.0f} {unit}" if unit == "B" else f"{value:,.1f} {unit}"
        value /= 1024


class FileTableModel(QAbstractTableModel):
    COLUMNS = ("이름", "크기", "수정한 날짜", "파일 형식", "복제 상태", "저장된 클라우드")

    def __init__(self, accounts=(), parent=None):
        super().__init__(parent)
        self.entries, self.visible = (), 0
        self.labels = {a.id: a.label for a in accounts}
        style = QApplication.style()
        self.folder_icon = style.standardIcon(QStyle.StandardPixmap.SP_DirIcon)
        self.file_icon = style.standardIcon(QStyle.StandardPixmap.SP_FileIcon)
        self.warning_icon = style.standardIcon(QStyle.StandardPixmap.SP_MessageBoxWarning)

    def set_entries(self, entries):
        self.beginResetModel()
        self.entries = tuple(entries)
        self.visible = min(500, len(self.entries))
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else self.visible

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.COLUMNS)

    def canFetchMore(self, parent=QModelIndex()):
        return not parent.isValid() and self.visible < len(self.entries)

    def fetchMore(self, parent=QModelIndex()):
        if not self.canFetchMore(parent):
            return
        end = min(self.visible + 500, len(self.entries))
        self.beginInsertRows(QModelIndex(), self.visible, end - 1)
        self.visible = end
        self.endInsertRows()

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return self.COLUMNS[section]
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or index.row() >= self.visible:
            return None
        entry = self.entries[index.row()]
        column = index.column()
        if role == Qt.ItemDataRole.UserRole:
            return entry
        if role == Qt.ItemDataRole.ToolTipRole:
            return entry.path + "\n" + "\n".join(entry.assessment.issues)
        if role == Qt.ItemDataRole.DecorationRole:
            if column == 0:
                return self.folder_icon if entry.is_dir else self.file_icon
            if column == 4 and entry.assessment.status != Status.HEALTHY:
                return self.warning_icon
        if role == Qt.ItemDataRole.ForegroundRole and entry.assessment.status in {
            Status.UNKNOWN,
            Status.UNVERIFIED,
        }:
            return QBrush(QColor("#786447"))
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if column == 0:
            return entry.name
        if column == 1:
            sizes = {r.size for r in entry.replicas}
            return (
                "—"
                if entry.is_dir
                else (size_text(next(iter(sizes))) if len(sizes) == 1 else "복제본별 상이")
            )
        if column == 2:
            dates = {r.modified for r in entry.replicas}
            if len(dates) != 1:
                return "복제본별 상이"
            value = next(iter(dates))
            return value.astimezone().strftime("%Y-%m-%d %H:%M") if value else "—"
        if column == 3:
            return (
                "파일 폴더"
                if entry.is_dir
                else (PurePosixPath(entry.name).suffix.upper().lstrip(".") or "파일")
            )
        if column == 4:
            return f"{entry.assessment.status.value} · {entry.assessment.count}개 확인"
        if column == 5:
            return ", ".join(
                self.labels.get(a, a) for a in sorted({r.account_id for r in entry.replicas})
            )
        return None

    def flags(self, index):
        if not index.isValid():
            return Qt.ItemFlag.ItemIsDropEnabled
        return super().flags(index) | Qt.ItemFlag.ItemIsDragEnabled | Qt.ItemFlag.ItemIsDropEnabled


class EntryProxy(QSortFilterProxyModel):
    sort_requested = Signal(int, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.query, self.status, self.scope, self.mode = "", "", "", "browse"
        self.setDynamicSortFilter(False)

    def update_filter(self, *, query=None, status=None, scope=None, mode=None):
        self.beginFilterChange()
        if query is not None:
            self.query = query.casefold()
        if status is not None:
            self.status = status
        if scope is not None:
            self.scope = scope
        if mode is not None:
            self.mode = mode
        self.endFilterChange(QSortFilterProxyModel.Direction.Rows)

    def filterAcceptsRow(self, row, parent):
        entry = self.sourceModel().entries[row]
        if self.mode == "issues" and entry.assessment.status == Status.HEALTHY:
            return False
        return (
            self.query in entry.name.casefold()
            and (not self.status or entry.assessment.status.value == self.status)
            and (not self.scope or any(r.account_id == self.scope for r in entry.replicas))
        )

    def sort(self, column, order=Qt.SortOrder.AscendingOrder):
        self.sort_requested.emit(column, order)


@dataclass(eq=False)
class FolderNode:
    label: str
    path: str = ""
    kind: str = "dir"
    scope: str = ""
    parent: object = None
    children: list = field(default_factory=list)
    loaded: bool = False
    loading: bool = False


class FolderTreeModel(QAbstractItemModel):
    load_requested = Signal(object)

    def __init__(self, accounts=(), parent=None):
        super().__init__(parent)
        self.generation = 0
        self.root = FolderNode("root", loaded=True)
        self.reset_accounts(accounts)

    def reset_accounts(self, accounts):
        self.beginResetModel()
        self.generation += 1
        root = FolderNode("root", loaded=True)
        unified = FolderNode("통합 클라우드", kind="unified", parent=root)
        cloud_group = FolderNode("연결된 계정", kind="group", parent=root, loaded=True)
        trash_group = FolderNode("클라우드 휴지통", kind="group", parent=root, loaded=True)
        for account in accounts:
            suffix = " (사용 안 함)" if not account.enabled else ""
            cloud_group.children.append(
                FolderNode(
                    account.label + suffix, kind="account", scope=account.id, parent=cloud_group
                )
            )
            trash_group.children.append(
                FolderNode(
                    account.label, kind="trash", scope=account.id, parent=trash_group, loaded=True
                )
            )
        root.children = [
            unified,
            cloud_group,
            FolderNode("문제 항목", kind="issues", parent=root, loaded=True),
            trash_group,
        ]
        self.root = root
        self.endResetModel()

    def node(self, index):
        return index.internalPointer() if index.isValid() else self.root

    def index(self, row, column, parent=QModelIndex()):
        if not self.hasIndex(row, column, parent):
            return QModelIndex()
        return self.createIndex(row, column, self.node(parent).children[row])

    def parent(self, index):
        if not index.isValid():
            return QModelIndex()
        parent = self.node(index).parent
        if parent is None or parent is self.root:
            return QModelIndex()
        return self.createIndex(parent.parent.children.index(parent), 0, parent)

    def index_for(self, node):
        if node is self.root or node.parent is None:
            return QModelIndex()
        return self.createIndex(node.parent.children.index(node), 0, node)

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.column() > 0 else len(self.node(parent).children)

    def columnCount(self, parent=QModelIndex()):
        return 1

    def flags(self, index):
        flags = super().flags(index)
        if not index.isValid():
            return flags
        node = self.node(index)
        if node.kind == "unified":
            return flags | Qt.ItemFlag.ItemIsDropEnabled
        if node.kind == "dir" and not node.scope:
            return flags | Qt.ItemFlag.ItemIsDragEnabled | Qt.ItemFlag.ItemIsDropEnabled
        return flags

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        node = self.node(index)
        if role == Qt.ItemDataRole.DisplayRole:
            return node.label
        if role == Qt.ItemDataRole.UserRole:
            return node
        if role == Qt.ItemDataRole.DecorationRole:
            return QApplication.style().standardIcon(
                QStyle.StandardPixmap.SP_TrashIcon
                if node.kind == "trash"
                else QStyle.StandardPixmap.SP_DirIcon
            )
        return None

    def canFetchMore(self, parent):
        node = self.node(parent)
        return node.kind in {"unified", "account", "dir"} and not node.loaded and not node.loading

    def hasChildren(self, parent=QModelIndex()):
        node = self.node(parent)
        return bool(node.children) or (
            node.kind in {"unified", "account", "dir"} and not node.loaded
        )

    def fetchMore(self, parent):
        if self.canFetchMore(parent):
            node = self.node(parent)
            node.loading = True
            self.load_requested.emit(node)

    def set_children(self, node, entries, *, required_child=None):
        children = [
            FolderNode(e.name, e.path, scope=node.scope, parent=node)
            for e in entries
            if e.is_dir and (not node.scope or any(r.account_id == node.scope for r in e.replicas))
        ]
        if required_child is not None and not any(
            path_key(child.label) == required_child for child in children
        ):
            return
        parent = self.index_for(node)
        if node.children:
            self.beginRemoveRows(parent, 0, len(node.children) - 1)
            node.children = []
            self.endRemoveRows()
        if children:
            self.beginInsertRows(parent, 0, len(children) - 1)
            node.children = children
            self.endInsertRows()
        node.loading, node.loaded = False, True
