from argparse import ArgumentParser
import os
from pathlib import Path
import sys
import tempfile

from PySide6.QtCore import QLockFile
from PySide6.QtWidgets import QApplication, QMessageBox

from .capture import Capture
from .chrome import publish_key, remove_key
from .store import Store
from .theme import STYLE
from .window import Studio

ROOT = Path(__file__).resolve().parent.parent


def main():
    parser = ArgumentParser(description="NexGen Live Studio")
    parser.add_argument("--demo", action="store_true", help="Open a fictional lesson in an isolated temporary database.")
    args = parser.parse_args()
    app = QApplication(sys.argv[:1])
    app.setApplicationName("NexGen Live Studio")
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)
    temporary = tempfile.TemporaryDirectory(prefix="nexgen-demo-") if args.demo else None
    data = Path(temporary.name) if temporary else Path(os.environ.get("LOCALAPPDATA", Path.home())) / "NexGen" / "Live Studio"
    data.mkdir(parents=True, exist_ok=True)
    lock = QLockFile(str(data / "studio.lock"))
    if not lock.tryLock(0):
        QMessageBox.information(None, "NexGen Live Studio", "Live Studio is already running.")
        return 0
    store = Store(data / "lessons.sqlite3")
    store.prune(store.settings("retention", 0))
    capture = Capture(store)
    if not args.demo:
        try:
            capture.server_start()
            publish_key(data, capture.token)
        except OSError:
            capture.bridge_error = ("Close the other Studio process and restart." if not capture.server else
                                    "Windows could not save the Chrome connection key.")
    window = Studio(store, capture, data, ROOT, args.demo)
    if args.demo:
        from .demo import populate
        lesson = populate(store)
        window.open_lesson(lesson)
    window.show()
    try:
        return app.exec()
    finally:
        if not args.demo:
            remove_key(data, capture.token)
        lock.unlock()
        if temporary:
            temporary.cleanup()


if __name__ == "__main__":
    sys.exit(main())
