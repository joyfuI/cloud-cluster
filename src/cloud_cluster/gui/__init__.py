"""Native Qt Widgets interface."""

from PySide6.QtCore import QLibraryInfo, QTranslator


def install_korean_translation(app):
    translator = QTranslator(app)
    translator.load("qtbase_ko", QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath))
    app.installTranslator(translator)
    return translator
