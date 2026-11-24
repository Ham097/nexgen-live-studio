"""Render desktop screenshots using only the bundled fictional lesson."""
import os
from pathlib import Path
from tempfile import TemporaryDirectory

os.environ.setdefault("QT_QPA_PLATFORM", "windows")
os.environ.setdefault("QT_SCALE_FACTOR", "1")
from PySide6.QtWidgets import QApplication

from studio.capture import Capture
from studio.demo import populate
from studio.store import Store
from studio.theme import STYLE
from studio.window import Studio


def main():
    app = QApplication([])
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)
    output = Path(__file__).resolve().parents[1] / "docs" / "screens"
    output.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory() as directory:
        data = Path(directory)
        store = Store(data / "demo.sqlite")
        lesson = populate(store)
        window = Studio(store, Capture(store), data, output.parents[1], demo=True)
        available = app.primaryScreen().availableGeometry()
        window.resize(min(1440, available.width()), min(900, available.height()))
        window.show()
        window.open_lesson(lesson)
        for page, name in (("Home", "lesson-desk"), ("Live Studio", "live-studio"),
                           ("Review", "transcript-review"), ("Lesson Summary", "lesson-summary")):
            window.navigate(page)
            if page == "Review":
                window.review_turns.setCurrentRow(2)
            window.poll()
            app.processEvents()
            if not window.grab().save(str(output / (name + ".png"))):
                raise RuntimeError("Could not save " + name)
        store.revise(window.lesson["samples"][0]["turns"][1]["id"], teaching="I would take the train so I could work during the journey.")
        store.approve(lesson, "Passed", "Demo teacher", "")
        window.refresh()
        window.present(window.lesson["samples"][0]["turns"][1])
        window.presentation.resize(960, 780)
        app.processEvents()
        if not window.presentation.grab().save(str(output / "student-presentation.png")):
            raise RuntimeError("Could not save student presentation.")
        window.close()


if __name__ == "__main__":
    main()
