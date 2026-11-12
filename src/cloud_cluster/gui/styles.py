ROW_SELECTION_STYLE = """
QTableView, QTreeView { background-color: palette(base); }
QTableView::item:selected, QTreeView::item:selected {
    background-color: palette(highlight);
    color: palette(highlighted-text);
}
"""
