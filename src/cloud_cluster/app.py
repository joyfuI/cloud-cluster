import argparse
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from PySide6.QtCore import QLockFile, QTimer, qVersion
from PySide6.QtGui import QFont, QFontDatabase, QIcon
from PySide6.QtWidgets import QApplication, QMessageBox

from .accounts import AccountManager
from .diagnostics import error_summary, logger
from .gui import install_korean_translation
from .gui.window import MainWindow
from .rclone import RCLONE_VERSION, RcloneAdapter
from .settings import SettingsStore


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Cloud Cluster — Windows 클라우드 통합 파일 관리자"
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        help="데이터 폴더 (기본: 실행 파일 옆, 소스 실행은 프로젝트 루트)",
    )
    parser.add_argument("--smoke-test", type=Path, help="창 렌더링을 저장하고 정상 종료")
    args = parser.parse_args(argv)
    app = QApplication.instance() or QApplication([])
    # Qt does not take ownership of the translator; retain it through the event loop.
    _translator = install_korean_translation(app)
    app.setApplicationName("Cloud Cluster")
    app.setOrganizationName("CloudCluster")
    app.setWindowIcon(QIcon(str(Path(__file__).with_name("cloud-cluster.ico"))))
    font = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "malgun.ttf"
    if font.exists():
        QFontDatabase.addApplicationFont(str(font))
        app.setFont(QFont("Malgun Gothic", 9))
    try:
        store = SettingsStore(args.data_dir)
        lock = QLockFile(str(store.directory / "instance.lock"))
        lock.setStaleLockTime(0)
        if not lock.tryLock(0):
            QMessageBox.information(
                None, "Cloud Cluster", "이 설정을 사용하는 Cloud Cluster가 이미 실행 중입니다."
            )
            return 1
    except (ValueError, OSError, TypeError) as error:
        QMessageBox.critical(
            None, "설정을 읽을 수 없습니다", str(error) + "\n기존 설정 파일을 보존합니다."
        )
        return 1
    logger.setLevel(logging.INFO)
    handler = RotatingFileHandler(
        store.directory / "cloud-cluster.log",
        maxBytes=2 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s [%(process)d:%(threadName)s] %(message)s")
    )
    logger.addHandler(handler)
    previous_hook = sys.excepthook

    def report_exception(exc_type, error, traceback):
        logger.error("unhandled callback error=%s", error_summary(error, traceback))
        previous_hook(exc_type, error, traceback)

    sys.excepthook = report_exception
    try:
        try:
            settings = store.load()
        except (ValueError, OSError, TypeError) as error:
            logger.error("settings startup failed error=%s", error_summary(error))
            QMessageBox.critical(
                None, "설정을 읽을 수 없습니다", str(error) + "\n기존 설정 파일을 보존합니다."
            )
            return 1
        logger.setLevel(settings.log_level)
        logger.info(
            "app started mode=%s python=%s qt=%s log_level=%s",
            "packaged" if "__compiled__" in globals() else "source",
            sys.version.split()[0],
            qVersion(),
            settings.log_level,
        )
        backend, manager, startup_error = None, None, ""
        try:
            store.select_config(settings.rclone_config)
            backend = RcloneAdapter()
            manager = AccountManager(backend, store, settings)
            manager.open()
        except Exception as error:
            logger.error("backend initialization failed error=%s", error_summary(error))
            startup_error = str(error)
            manager = None
            if backend:
                backend.shutdown()
            backend = None
        else:
            logger.info(
                "backend ready rclone=%s accounts=%d enabled=%d",
                RCLONE_VERSION,
                len(settings.accounts),
                sum(a.enabled for a in settings.accounts),
            )
        window = MainWindow(backend, settings, store, manager, startup_error)
        window.showMaximized()
        if args.smoke_test:
            attempts = 0

            def capture():
                nonlocal attempts
                attempts += 1
                if window.workers.pending or window.transfers.busy:
                    if attempts >= 300:
                        app.exit(2)
                        return
                    QTimer.singleShot(100, capture)
                    return
                args.smoke_test.parent.mkdir(parents=True, exist_ok=True)
                window.grab().save(str(args.smoke_test))
                window.close()

            QTimer.singleShot(800, capture)
        result = app.exec()
        logger.info("app exited code=%d", result)
        return result
    except Exception as error:
        logger.error("app stopped error=%s", error_summary(error))
        raise
    finally:
        sys.excepthook = previous_hook
        lock.unlock()
        logger.removeHandler(handler)
        handler.close()


if __name__ == "__main__":
    raise SystemExit(main())
