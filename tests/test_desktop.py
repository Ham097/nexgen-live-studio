import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from unittest import TestCase, mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QLabel
from studio import __main__ as launcher
from studio.capture import Capture
from studio.demo import populate
from studio.store import Store
from studio.window import Studio


class DesktopTests(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_demo_entrypoint_lock_and_window(self):
        original = QApplication.topLevelWidgets()
        shown = []

        def close_demo():
            for widget in QApplication.topLevelWidgets():
                if widget not in original and isinstance(widget, Studio):
                    shown.append(widget.windowTitle())
                    widget.close()
            self.app.quit()

        QTimer.singleShot(100, close_demo)
        with mock.patch.object(launcher, "QApplication", return_value=self.app):
            with mock.patch.object(sys, "argv", ["studio", "--demo"]):
                self.assertEqual(launcher.main(), 0)
        self.assertEqual(shown, ["NexGen Live Studio"])

    def test_presentation_is_scoped_to_current_student(self):
        with TemporaryDirectory() as directory:
            data = Path(directory)
            store = Store(data / "test.sqlite")
            lesson = populate(store)
            store.create({"name": "Other Student", "student_id": "other", "level": "C1"}, True)
            store.board_item(lesson, term="Teacher's private action")
            store.board_item(lesson, term="Public phrase", note="Internal note", shared=1)
            studio = Studio(store, Capture(store), data, data, demo=True)
            studio.open_lesson(lesson)
            studio.present()
            text = " ".join(label.text() for label in studio.presentation.findChildren(QLabel))
            self.assertIn("Maya Ellis", text)
            self.assertIn("Public phrase", text)
            self.assertNotIn("Other Student", text)
            self.assertNotIn("private action", text)
            self.assertNotIn("Internal note", text)
            studio.close()
