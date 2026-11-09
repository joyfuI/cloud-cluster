from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QTreeView

from .file_table import accept_file_drag, emit_file_drop, has_file_mime, start_path_drag


class FolderTreeView(QTreeView):
    files_dropped = Signal(object, object, object, bool)

    def startDrag(self, supported_actions):
        index = self.currentIndex()
        if index.flags() & Qt.ItemFlag.ItemIsDragEnabled:
            start_path_drag(self, [index.data(Qt.ItemDataRole.UserRole).path])

    def dragEnterEvent(self, event):
        if has_file_mime(event):
            accept_file_drag(event)
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        index = self.indexAt(event.position().toPoint())
        if has_file_mime(event) and index.flags() & Qt.ItemFlag.ItemIsDropEnabled:
            accept_file_drag(event)
        else:
            event.ignore()

    def dropEvent(self, event):
        index = self.indexAt(event.position().toPoint())
        if has_file_mime(event) and index.flags() & Qt.ItemFlag.ItemIsDropEnabled:
            emit_file_drop(self.files_dropped, event, index.data(Qt.ItemDataRole.UserRole).path)
        else:
            event.ignore()
