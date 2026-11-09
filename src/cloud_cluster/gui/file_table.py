import json

from PySide6.QtCore import (
    QItemSelection,
    QItemSelectionModel,
    QMimeData,
    QPoint,
    QRect,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import QDrag
from PySide6.QtWidgets import QApplication, QRubberBand, QStyleOptionViewItem, QTableView

INTERNAL_MIME = "application/x-cloud-cluster-paths"


def start_path_drag(view, paths):
    mime = QMimeData()
    mime.setData(INTERNAL_MIME, json.dumps(paths).encode())
    drag = QDrag(view)
    drag.setMimeData(mime)
    drag.exec(Qt.DropAction.CopyAction | Qt.DropAction.MoveAction, Qt.DropAction.MoveAction)


def has_file_mime(event):
    mime = event.mimeData()
    return mime.hasUrls() or mime.hasFormat(INTERNAL_MIME)


def accept_file_drag(event):
    if event.mimeData().hasFormat(INTERNAL_MIME):
        action = (
            Qt.DropAction.CopyAction
            if event.modifiers() & Qt.KeyboardModifier.ControlModifier
            else Qt.DropAction.MoveAction
        )
        if event.possibleActions() & action:
            event.setDropAction(action)
            event.accept()
            return
    event.acceptProposedAction()


def emit_file_drop(signal, event, folder):
    mime = event.mimeData()
    urls = [url.toLocalFile() for url in mime.urls() if url.isLocalFile()]
    internal = bytes(mime.data(INTERNAL_MIME)) if mime.hasFormat(INTERNAL_MIME) else b""
    copy_only = bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier)
    signal.emit(urls, internal, folder, copy_only)
    accept_file_drag(event)


class FileTableView(QTableView):
    files_dropped = Signal(object, object, object, bool)

    def __init__(self, export_files=None):
        super().__init__()
        self._export_files = export_files
        self._selection_start = self._selection_end = None
        self._selection_before_drag = QItemSelection()
        self._selection_band = QRubberBand(QRubberBand.Shape.Rectangle, self.viewport())
        self._selection_scroll = QTimer(self, interval=50)
        self._selection_scroll.timeout.connect(self._scroll_selection_band)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._finish_selection_drag()
            point = event.position().toPoint()
            index = self.indexAt(point)
            blank = not index.isValid()
            if index.isValid() and index.column() == 0:
                option = QStyleOptionViewItem()
                self.initViewItemOption(option)
                option.rect = self.visualRect(index)
                width = self.itemDelegateForIndex(index).sizeHint(option, index).width()
                blank = point.x() - option.rect.left() >= width
            if blank and self.model() is not None:
                self._selection_start = point + QPoint(
                    self.horizontalOffset(), self.verticalOffset()
                )
                if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
                    self._selection_before_drag = self.selectionModel().selection()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._selection_start is None or not event.buttons() & Qt.MouseButton.LeftButton:
            self._finish_selection_drag()
            super().mouseMoveEvent(event)
            return
        point = event.position().toPoint()
        if self._selection_end is None:
            start = self._selection_start - QPoint(self.horizontalOffset(), self.verticalOffset())
            if (point - start).manhattanLength() < QApplication.startDragDistance():
                return
            if self.hasAutoScroll():
                self._selection_scroll.start()
        self._selection_end = point
        self._update_selection_band()
        event.accept()

    def _update_selection_band(self):
        start = self._selection_start - QPoint(self.horizontalOffset(), self.verticalOffset())
        left, right = sorted((start.x(), self._selection_end.x()))
        top, bottom = sorted((start.y(), self._selection_end.y()))
        rect = QRect(QPoint(left, top), QPoint(right, bottom))
        self._selection_band.setGeometry(rect.intersected(self.viewport().rect()))
        self._selection_band.show()

        model = self.model()
        first = self.rowAt(max(top, self.rowViewportPosition(0)))
        selection = QItemSelection(self._selection_before_drag)
        flags = QItemSelectionModel.SelectionFlag
        if first >= 0:
            last = self.rowAt(bottom)
            if last < 0:
                last = model.rowCount() - 1
            covered = QItemSelection(
                model.index(first, 0), model.index(last, model.columnCount() - 1)
            )
            selection.merge(covered, flags.Toggle)
        self.selectionModel().select(selection, flags.ClearAndSelect)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            dragged = self._selection_end is not None
            if dragged and self._selection_end != event.position().toPoint():
                self._selection_end = event.position().toPoint()
                self._update_selection_band()
            self._finish_selection_drag()
            if dragged:
                event.accept()
                return
        super().mouseReleaseEvent(event)

    def _scroll_selection_band(self):
        y = self._selection_end.y()
        margin = self.autoScrollMargin()
        direction = (y >= self.viewport().height() - margin) - (y < margin)
        if direction:
            bar = self.verticalScrollBar()
            bar.setValue(bar.value() + direction * bar.singleStep())

    def scrollContentsBy(self, dx, dy):
        super().scrollContentsBy(dx, dy)
        if self._selection_end is not None:
            self._update_selection_band()

    def focusOutEvent(self, event):
        self._finish_selection_drag()
        super().focusOutEvent(event)

    def reset(self):
        self._finish_selection_drag()
        super().reset()

    def _finish_selection_drag(self):
        if self._selection_start is not None:
            self._selection_start = self._selection_end = None
            self._selection_before_drag = QItemSelection()
            self._selection_band.hide()
            self._selection_scroll.stop()

    def startDrag(self, supported_actions):
        indexes = {
            index.row(): index.siblingAtColumn(0)
            for index in self.selectionModel().selectedIndexes()
        }
        entries = [index.data(Qt.ItemDataRole.UserRole) for _, index in sorted(indexes.items())]
        if entries and not (self._export_files and self._export_files(entries)):
            start_path_drag(self, [entry.path for entry in entries])

    def dragEnterEvent(self, event):
        if has_file_mime(event):
            accept_file_drag(event)

    def dragMoveEvent(self, event):
        self.dragEnterEvent(event)

    def dropEvent(self, event):
        index = self.indexAt(event.position().toPoint())
        entry = index.siblingAtColumn(0).data(Qt.ItemDataRole.UserRole) if index.isValid() else None
        folder = entry.path if entry and entry.is_dir else None
        emit_file_drop(self.files_dropped, event, folder)
