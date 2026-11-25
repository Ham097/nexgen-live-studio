"""Native Qt teaching workspace."""
import ctypes
from ctypes import wintypes
from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
from threading import Thread
import webbrowser
from uuid import uuid4

from PySide6.QtCore import QEvent, QTimer, Qt, Signal
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import (QAbstractSpinBox, QApplication, QButtonGroup, QComboBox, QDialog, QFileDialog,
    QFormLayout, QFrame, QHBoxLayout, QInputDialog, QKeySequenceEdit, QLineEdit, QListWidget, QMainWindow, QMessageBox,
    QPlainTextEdit, QProgressBar, QPushButton, QSpinBox, QSplitter, QStackedWidget, QTextBrowser, QVBoxLayout, QWidget)

from .analysis import ACTIVITIES, LEVELS, OUTCOMES, report
from .asr import MODELS, Recognizer
from .chrome import register
from .integrations import get_reservations, https_endpoint, legacy_export, meet_code, reservation_rows, suggested, sync_due, upcoming
from .linguistics import warm as warm_language
from .store import doc, profile
from .widgets import Trend, button, card, checked, column, combo, form_dialog, heading, label, line, listings, plain, row, scroll, seconds, selected_data, turn_details, value


def spin(low, high, raw):
    widget = QSpinBox()
    widget.setRange(low, high)
    widget.setValue(raw)
    return widget


def clock(value):
    return datetime.fromisoformat(value).astimezone().strftime("%d %b · %H:%M")


PAGES = ("Home", "Live Studio", "Review", "Lesson Summary", "Settings", "Solo test")


class Studio(QMainWindow):
    background = Signal(str, object, object)

    def __init__(self, store, capture, data, root, demo=False):
        super().__init__()
        self.setWindowTitle("NexGen Live Studio")
        self.resize(1180, 795)
        self.setMinimumSize(850, 640)
        self.store, self.capture, self.data, self.root = store, capture, data, root
        self.demo = demo
        self.session_id = self.sample_id = self.lesson = None
        self.reservations = store.settings("reservations", [])
        self.model_status = "Speech model is starting…"
        self.jobs = set()
        self.last_poll = None
        self.token = os.environ.get("NEXGEN_STUDIO_TOKEN", "")
        self.api = self.store.settings("api_url", "")
        self.presentation = None
        self.toolbar = None
        self.hotkeys = []
        self.shortcut_alert = ""
        self.bound_meet = None
        self.bridge_status = ""
        self.solo_requested = False
        self.pending_meet = None
        self.detected_mic = None
        self.background.connect(self.background_done)
        self._layout()
        self.lesson_navigation()
        self.home_refresh()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll)
        self.timer.start(260)
        self.sync_timer = QTimer(self)
        self.sync_timer.timeout.connect(self.sync)
        self.sync_timer.start(30000)
        self.schedule_timer = QTimer(self)
        self.schedule_timer.timeout.connect(self.fetch_reservations)
        self.schedule_timer.start(60000)
        self.capture.mic_error = ""
        if demo:
            self.model_status = "Audio disabled for fictional demo"
        else:
            self.register_hotkeys()
            self.background_job("chrome", lambda: register(self.data, self.root))
            self.background_job("language", warm_language)
            self.apply_mic()
            self.load_model(automatic=True)
            self.mic_retry = QTimer(self)
            self.mic_retry.timeout.connect(self.recover_mic)
            self.mic_retry.start(3500)
            QTimer.singleShot(1000, self.fetch_reservations)
        QTimer.singleShot(2000, self.sync)

    def _layout(self):
        container, body = column(QHBoxLayout(), spacing=0)
        self.setCentralWidget(container)
        rail = QFrame()
        rail.setObjectName("rail")
        rail.setFixedWidth(198)
        left = QVBoxLayout(rail)
        left.setContentsMargins(17, 26, 17, 21)
        left.setSpacing(8)
        left.addWidget(label("NEXGEN", "eyebrow"))
        left.addWidget(label("Live Studio", "brand"))
        left.addSpacing(24)
        self.nav = QButtonGroup(self)
        self.stack = QStackedWidget()
        for name, build in (("Home", self.build_home), ("Live Studio", self.build_studio),
                            ("Review", self.build_review), ("Lesson Summary", self.build_summary),
                            ("Settings", self.build_settings), ("Solo test", self.build_solo)):
            entry = button(name, lambda _, page=name: self.navigate(page))
            entry.setCheckable(True)
            entry.setProperty("nav", True)
            self.nav.addButton(entry, self.stack.count())
            self.stack.addWidget(build())
            if name != "Solo test":
                left.addWidget(entry)
            else:
                entry.hide()
        self.nav.button(0).setChecked(True)
        left.addStretch()
        left.addWidget(label("CAPTURE STATUS", "eyebrow"))
        self.rail_status = label("Paused", "muted", True)
        left.addWidget(self.rail_status)
        self.toolbar_shortcut = button("Floating toolbar", self.show_toolbar)
        left.addWidget(self.toolbar_shortcut)
        left.addWidget(label("Fictional demo" if self.demo else "Private · stored on this PC", "muted", True))
        body.addWidget(rail)
        body.addWidget(self.stack, 1)
        QApplication.instance().installEventFilter(self)

    def eventFilter(self, widget, event):
        if (event.type() == QEvent.Type.KeyPress and isinstance(widget, QWidget)
                and widget.window() == self and self.session_id and not event.isAutoRepeat()
                and event.modifiers() == Qt.KeyboardModifier.NoModifier
                and not isinstance(widget, (QLineEdit, QPlainTextEdit, QTextBrowser, QComboBox,
                                            QAbstractSpinBox, QKeySequenceEdit))):
            if event.key() == Qt.Key.Key_V and self.draft():
                self.quick_vocab()
                return True
            if event.key() == Qt.Key.Key_Space and not isinstance(widget, QPushButton):
                if self.trigger.isEnabled():
                    self.toggle()
                    return True
        return super().eventFilter(widget, event)

    def navigate(self, page):
        if page != "Solo test" and self.capture.snapshot()["mode"] == "solo":
            self.capture.pause()
        index = PAGES.index(page)
        self.nav.button(index).setChecked(True)
        self.stack.setCurrentIndex(index)
        if index == 0:
            self.home_refresh()
        elif index == 3:
            self.refresh_summary()
        elif index == 4:
            self.update_diagnostics()

    def lesson_navigation(self):
        for index in (1, 2, 3):
            self.nav.button(index).setVisible(bool(self.lesson))
        self.toolbar_shortcut.setVisible(bool(self.lesson))

    @staticmethod
    def page(kicker, title, note):
        panel, contents = column(margins=26, spacing=14)
        contents.addWidget(heading(kicker, title, note))
        return panel, contents

    def build_home(self):
        panel, layout = self.page("", "Ready to teach?", "Connect to a Meet lesson or check your sound on your own.")
        pair, actions = column(QHBoxLayout(), spacing=14)
        self.meet_home_state = label("Looking for Google Meet…", "muted", True)
        self.meet_action = button("Find Google Meet", self.meet_flow, True)
        meet = card(label("Google Meet lesson", "section"), self.meet_home_state, row(self.meet_action, None))
        meet.setObjectName("meetHome")
        self.solo_home_state = label("Use your microphone or play a short sample.", "muted", True)
        solo = card(label("Test on my own", "section"), self.solo_home_state,
                    row(button("Open solo test", lambda: self.navigate("Solo test")), None))
        actions.addWidget(meet, 1)
        actions.addWidget(solo, 1)
        layout.addWidget(pair)
        self.audio_home = label("", "muted", True)
        self.model_home = label("", "muted", True)
        self.model_retry = button("Retry transcription setup", lambda: self.load_model(automatic=True))
        self.model_retry.hide()
        layout.addWidget(card(label("Readiness", "section"), self.audio_home,
                              row(self.model_home, self.model_retry, None)))
        self.schedule_switch = button("Show saved lessons and schedule", self.show_history)
        self.schedule_switch.setCheckable(True)
        layout.addWidget(row(self.schedule_switch, None))
        self.home_history, history = column()
        self.suggestion = label("Checking upcoming lessons…", "section", True)
        self.suggestion_detail = label("", "muted", True)
        self.prepare = button("Prepare lesson", self.prepare_suggested, True)
        history.addWidget(card(self.suggestion, self.suggestion_detail,
                               row(self.prepare, button("Prepare unscheduled lesson", self.unscheduled), None)))
        lists = QSplitter()
        self.upcoming_list = QListWidget()
        self.upcoming_list.setAccessibleName("Upcoming reservations")
        self.upcoming_list.itemDoubleClicked.connect(lambda _: self.prepare_selected())
        self.recent = QListWidget()
        self.recent.setAccessibleName("Recent local lessons")
        self.recent.itemDoubleClicked.connect(lambda _: self.open_recent())
        lists.addWidget(card(label("Upcoming lessons", "section"), self.upcoming_list,
                            row(button("Open selected", self.prepare_selected),
                                button("Import schedule", self.import_schedule), None)))
        lists.addWidget(card(label("Recent lessons", "section"), self.recent,
                            row(button("Open selected", self.open_recent),
                                button("Delete selected", self.delete_recent), None)))
        lists.setSizes([500, 500])
        lists.setMinimumHeight(250)
        history.addWidget(lists)
        self.home_history.hide()
        layout.addWidget(self.home_history)
        layout.addStretch()
        return scroll(panel)

    def show_history(self, visible):
        self.home_history.setVisible(visible)
        self.schedule_switch.setText("Hide saved lessons and schedule" if visible else "Show saved lessons and schedule")

    def build_solo(self):
        panel, layout = self.page("", "Test on my own", "Check your microphone, speakers and transcription before a lesson.")
        self.solo_mic_status = label("Finding your microphone…", "muted", True)
        self.solo_meter = QProgressBar()
        self.solo_meter.setRange(0, 100)
        self.solo_meter.setTextVisible(False)
        self.solo_meter.setAccessibleName("Microphone level")
        self.solo_trigger = button("Start microphone test", self.toggle_solo, True)
        self.reference_trigger = button("Play a test sentence", self.test_speakers)
        layout.addWidget(card(label("Microphone", "section"), self.solo_mic_status, self.solo_meter,
                              row(self.solo_trigger, None)))
        self.solo_feedback = label("Try saying “I can hear you clearly today.”", "muted", True)
        self.solo_transcript = label("Your words will appear here.", "value", True)
        layout.addWidget(card(label("What Studio heard", "section"), self.solo_transcript, self.solo_feedback))
        self.reference_status = label("Hear a short computer-generated sentence through your speakers.", "muted", True)
        layout.addWidget(card(label("Speakers and transcription", "section"), self.reference_status,
                              row(self.reference_trigger, None)))
        layout.addWidget(row(button("Back to Home", lambda: self.navigate("Home")), None))
        layout.addStretch()
        return scroll(panel)

    def toggle_solo(self):
        state = self.capture.snapshot()
        if state["mode"] == "solo":
            self.capture.pause()
        elif not self.demo and not self.sample_id and state["mode"] == "paused" and not state["pending"]:
            try:
                self.capture.start(None, "solo")
                self.solo_requested = True
            except ValueError as error:
                self.solo_feedback.setText(str(error))
        self.poll()

    def test_speakers(self):
        from .solo import reference
        engine = self.capture.recognizer
        if (engine and self.capture.snapshot()["mode"] == "paused" and not self.capture.snapshot()["pending"]
                and self.background_job("reference", lambda: reference(self.root, engine))):
            self.reference_status.setText("Playing and transcribing the test sentence…")
            self.poll()

    def session_heading(self):
        self.student_header = label("Open a lesson from Home", "section", True)
        self.session_info = label("", "muted", True)
        return card(self.student_header, self.session_info,
                    row(button("Find / open Meet", self.meet_flow),
                        button("Student presentation", self.present), None))

    def build_studio(self):
        panel, layout = self.page("LESSON IN PROGRESS", "Live Studio", "Capture speaking samples and save teaching points as you go.")
        layout.addWidget(self.session_heading())
        self.capture_state = label("PAUSED", "eyebrow")
        self.capture_help = label("", "muted", True)
        self.activity = combo(ACTIVITIES)
        self.activity.setAccessibleName("Activity")
        self.mode = combo(["Active assessment", "Transcript only"])
        self.mode.setAccessibleName("Capture mode")
        self.mode.currentIndexChanged.connect(self.change_mode)
        self.trigger = button("Start recording", self.toggle, True)
        self.finish_button = button("Finish", self.finish)
        self.record_options = row(label("Activity"), self.activity, label("Capture mode"), self.mode, None)
        options = button("Options", self.record_options.setVisible)
        options.setCheckable(True)
        self.record_options.hide()
        layout.addWidget(card(row(self.capture_state, None, self.trigger, self.finish_button, options),
                              self.capture_help, self.record_options))
        metrics_panel, numbers = column(QHBoxLayout(), spacing=10)
        self.metrics = []
        for title in ("PACE · WPM", "FLOW · WORDS/RUN", "VOCABULARY", "GRAMMAR · CONFIRMED", "ERRORS / 100"):
            number = label("—", "value")
            self.metrics.append(number)
            numbers.addWidget(card(label(title, "eyebrow"), number))
        layout.addWidget(metrics_panel)
        split = QSplitter()
        self.live_turns = QListWidget()
        self.live_turns.setAccessibleName("Live transcript")
        self.live_turns.itemDoubleClicked.connect(lambda _: self.open_live_turn())
        self.live_teacher = checked("Show my microphone")
        self.live_teacher.toggled.connect(self.refresh_transcript)
        self.provisional = label("", "muted", True)
        self.provisional.hide()
        split.addWidget(card(row(label("Transcript", "section"), None, self.live_teacher), self.live_turns, self.provisional,
                             row(button("Review selected", self.open_live_turn), button("Share selected", self.share_live_turn),
                                 button("Add selected to board", self.term_from_live), None)))
        self.board_items = QListWidget()
        self.board_items.setAccessibleName("Teaching board items")
        self.board_items.itemDoubleClicked.connect(lambda _: self.edit_board())
        self.vocab = line("Type a word, phrase or correction")
        self.vocab.returnPressed.connect(self.add_vocab)
        split.addWidget(card(label("Teaching board", "section"), self.vocab, self.board_items,
                             row(button("Add", self.add_vocab, True), button("Edit", self.edit_board),
                                 button("Delete", self.delete_board), None)))
        split.setSizes([690, 330])
        layout.addWidget(split, 1)
        levels, horizontal = column(QHBoxLayout(), spacing=9)
        self.meters = []
        for name in ("Meet tab", "Teacher mic"):
            meter = QProgressBar()
            meter.setTextVisible(False)
            meter.setRange(0, 100)
            meter.setAccessibleName(name + " audio level")
            self.meters.append(meter)
            horizontal.addWidget(label(name, "muted"))
            horizontal.addWidget(meter, 1)
        layout.addWidget(levels)
        return panel

    def build_review(self):
        panel, layout = self.page("EVIDENCE WORKSPACE", "Transcript review",
                                  "Verify what was said, check uncertain evidence and add teaching corrections.")
        self.review_heading = label("Open a lesson from Home", "section")
        self.search = line("Search transcript")
        self.search.textChanged.connect(self.refresh_transcript)
        self.filter = combo(["All samples"])
        self.filter.setAccessibleName("Sample filter")
        self.filter.currentIndexChanged.connect(self.refresh_transcript)
        self.activity_filter = combo(["All activities", *ACTIVITIES])
        self.activity_filter.setAccessibleName("Activity filter")
        self.activity_filter.currentIndexChanged.connect(self.refresh_transcript)
        self.review_teacher = checked("Teacher speech")
        self.review_teacher.toggled.connect(self.refresh_transcript)
        self.bookmarks = checked("Bookmarks")
        self.bookmarks.toggled.connect(self.refresh_transcript)
        layout.addWidget(card(self.review_heading, row(self.search, self.filter, self.activity_filter,
                                                        self.review_teacher, self.bookmarks)))
        self.review_turns = QListWidget()
        self.review_turns.setAccessibleName("Search results")
        self.review_turns.currentItemChanged.connect(lambda *_: self.refresh_detail())
        self.review_turns.itemDoubleClicked.connect(lambda _: self.edit_turn())
        self.detail = QTextBrowser()
        self.detail.setOpenLinks(False)
        self.detail.setAccessibleName("Transcript evidence details")
        split = QSplitter()
        split.addWidget(self.review_turns)
        split.addWidget(self.detail)
        split.setSizes([480, 550])
        layout.addWidget(split, 1)
        layout.addWidget(row(button("Edit / verify", self.edit_turn, True), button("Bookmark", self.bookmark_turn),
                             button("Undo last change", self.undo_turn), button("Share selected", self.share_review_turn),
                             button("Add selected words to board", self.term_from_review), None))
        return panel

    def build_summary(self):
        panel, layout = self.page("RESULTS & APPROVAL", "Lesson summary",
                                  "Compare samples, check evidence and choose the assessment outcome.")
        self.summary_heading = label("Open a lesson from Home", "section")
        self.combined = label("", "muted", True)
        self.summary_status = label("", "eyebrow", True)
        layout.addWidget(card(self.summary_heading, self.combined, self.summary_status))
        self.sample_list = QListWidget()
        self.sample_list.setAccessibleName("Speaking samples")
        self.history = QListWidget()
        self.history.setAccessibleName("Vocabulary and grammar passport")
        self.trend = Trend()
        self.change_text = label("", "muted", True)
        split = QSplitter()
        split.addWidget(card(label("Speaking samples", "section"), self.sample_list,
                             row(button("Resume sample", self.resume_sample), button("Label / exclude", self.edit_sample),
                                 button("Delete sample", self.delete_sample), None)))
        split.addWidget(card(label("Learner passport", "section"), self.change_text,
                             label("Comparable speaking pace · WPM", "eyebrow"), self.trend, self.history))
        split.setSizes([565, 470])
        layout.addWidget(split, 1)
        layout.addWidget(row(button("Approve lesson", self.approve, True), button("Export report", self.export),
                             button("Export for Review Studio", lambda: self.export(legacy=True)), None))
        return panel

    def build_settings(self):
        panel, layout = self.page("LOCAL SETUP", "Settings",
                                  "Choose your microphone and model, connect Chrome and manage local data.")
        settings, items = column(spacing=12)
        self.diagnostics = label("", "muted", True)
        self.model_choice = combo(tuple(MODELS))
        self.model_choice.setCurrentText(self.store.settings("model", self.default_model()))
        self.compute = combo(["auto", "cpu", "cuda"])
        self.compute.setCurrentText(self.store.settings("device", "auto"))
        self.mic_choice = combo(["Windows default"])
        self.mic_choice.setAccessibleName("Teacher microphone")
        items.addWidget(card(label("Audio & local model", "section"), self.diagnostics,
                             row(self.mic_choice, button("Detect microphones", self.detect_mics),
                                 button("Apply microphone", self.apply_mic), None),
                             row(self.model_choice, self.compute,
                                 button("Download / load model", lambda: self.load_model(True)), None)))
        items.addWidget(card(label("Google Meet connection", "section"),
                             label("Load the bundled connector once. When Chrome asks for access to a Meet tab, click the Live Studio icon.", "muted", True),
                             row(button("Set up Chrome", self.chrome_setup), None)))
        self.hot_toggle = QKeySequenceEdit(QKeySequence(self.store.settings("hot_toggle", "F8")))
        self.hot_finish = QKeySequenceEdit(QKeySequence(self.store.settings("hot_finish", "F9")))
        for widget in (self.hot_toggle, self.hot_finish):
            widget.setMaximumSequenceLength(1)
        self.retention = spin(0, 3650, self.store.settings("retention", 0))
        items.addWidget(card(label("Shortcuts & retention", "section"),
                             label("Space and V work in the focused Studio when you are outside editable fields. A retention value of 0 keeps lessons until you delete them.", "muted", True),
                             row(label("Start / pause"), self.hot_toggle, label("Finish"), self.hot_finish,
                                 label("Retention days"), self.retention, button("Save", self.save_preferences), None)))
        self.integration_state = label("", "muted", True)
        self.api_input = line("HTTPS adapter base URL", self.api)
        self.token_input = line("Adapter token for this launch", self.token)
        self.token_input.setEchoMode(QLineEdit.EchoMode.Password)
        items.addWidget(card(label("NexGen connection", "section"), self.integration_state, self.api_input,
                             self.token_input, row(button("Save connection", self.configure_adapter),
                                                   button("Refresh schedule", self.fetch_reservations),
                                                   button("Retry queued results", lambda: self.sync(True)),
                                                   button("Assessment profiles", self.edit_profile), None)))
        items.addStretch()
        layout.addWidget(scroll(settings), 1)
        return panel

    def default_model(self):
        return "large-v3-turbo" if self.existing_models() else "base.en"

    def existing_models(self):
        path = self.root.parent / "nexgen-analyzer" / "models"
        return path if path.is_dir() else None

    def home_refresh(self):
        offers = upcoming(self.reservations)
        self.recommended = suggested(offers)
        self.prepare.setEnabled(self.recommended is not None)
        if self.recommended:
            item = self.recommended
            self.suggestion.setText(f'{item["name"]}  ·  {item["level"]}')
            self.suggestion_detail.setText(f'Suggested lesson · {clock(item["start_at"])} · Confirm identity before capture.')
        else:
            self.suggestion.setText("Choose a lesson" if offers else "Ready when you are")
            self.suggestion_detail.setText("Select a reservation to confirm the learner." if offers else
                                           "Import reservations or start an unscheduled lesson with a student ID.")
        listings(self.upcoming_list, [(f'{clock(item["start_at"])}   ·   {item["name"]}   ·   {item["level"]}', item)
                                      for item in offers])
        listings(self.recent, [(f'{item["name"]}   ·   {item["level"]}   ·   {clock(item["created_at"])}'
                                f'   ·   {"Approved" if item["approval"] else "Draft"}', item["id"])
                               for item in self.store.sessions()])

    def prepare_suggested(self):
        if self.recommended:
            self.identity(self.recommended)

    def prepare_selected(self):
        item = selected_data(self.upcoming_list)
        if item:
            self.identity(item)

    def unscheduled(self):
        self.identity({})

    def identity(self, item, room=None, start=False):
        reservation = bool(item)
        adapter = item.get("_adapter")
        source = "demo" if self.demo else "nexgen" if adapter else "local"
        prior = next((existing for existing in self.store.sessions() if reservation
                      and existing["reservation_id"] == item["reservation_id"]
                      and existing["source"] == source and existing["adapter_url"] == (adapter or None)), None)
        if prior and (prior["student_id"] != item["student_id"] or prior["meet_url"] != item.get("meet_url")
                      or prior["mission_id"] != item.get("mission_id")):
            return self.notice("This reservation changed after a local lesson was created. Check the old draft before capturing.")
        if prior and prior["approval"]:
            return self.open_lesson(prior["id"])
        if self.sample_id or self.capture.snapshot()["pending"]:
            return self.notice("Finish the current sample before preparing another lesson.")
        name = line("Student name", item.get("name", ""))
        identity = line("Stable student ID", item.get("student_id", ""))
        level = combo(LEVELS)
        level.setCurrentText(item.get("level", "B1"))
        meet = line("Optional Google Meet link", item.get("meet_url") or (f"https://meet.google.com/{room}" if room else ""))
        mission = line("Optional mission ID", item.get("mission_id", "") or "")
        for field in (name, identity, level, meet, mission):
            field.setEnabled(not reservation)
        consent = checked("My student agreed to recording.")
        fields = [("Student", name), ("Level", level)]
        learner = None
        if not reservation and start:
            learner = combo(["New student"])
            known = {}
            for old in self.store.sessions():
                known.setdefault(old["student_id"], old)
            names = [value["name"] for value in known.values()]
            for key, old in sorted(known.items(), key=lambda item: item[1]["name"].casefold()):
                display = old["name"]
                learner.addItem(display if names.count(display) == 1 else f"{display} · {key[-6:]}", (key, display))

            def pick_learner(_):
                selected = learner.currentData()
                name.setEnabled(selected is None)
                if selected:
                    level.setCurrentText(known[selected[0]]["level"])

            learner.currentIndexChanged.connect(pick_learner)
            fields.insert(0, ("Learner", learner))
        elif not reservation:
            fields.extend([("Student ID", identity), ("Meet", meet), ("Mission", mission)])
        if room:
            fields.append(("Meet", label(room)))
        fields.append(("", consent))
        action = "Start recording" if start and self.capture.recognizer and self.capture.mic else "Open lesson"
        dialog = form_dialog(self, "Confirm your lesson", fields, "Resume draft" if prior and not start else action)
        if not dialog.exec():
            return
        if not consent.isChecked() or meet.text().strip() and not meet_code(meet.text().strip()):
            return self.notice("Confirm consent and use a standard Google Meet meeting link.")
        try:
            selected = learner.currentData() if learner else None
            student_id, display = selected if selected else (
                identity.text() or ("local-" + str(uuid4()) if start else ""), name.text())
            lesson_id = prior["id"] if prior else self.store.create({
                "student_id": student_id, "name": display, "level": level.currentText(),
                "reservation_id": item.get("reservation_id"), "mission_id": mission.text() or None,
                "meet_url": item.get("meet_url") if reservation else meet.text().strip() or None,
                "source": source, "adapter_url": adapter or None}, True)
            self.open_lesson(lesson_id, room)
            if room and self.capture.snapshot()["meeting"] == room:
                self.bound_meet = room
                self.capture.select_meeting(room)
            if action == "Start recording" and start:
                self.toggle()
        except ValueError as error:
            self.notice(str(error))

    def open_recent(self):
        item = selected_data(self.recent)
        if item:
            self.open_lesson(item)

    def delete_recent(self):
        item = selected_data(self.recent)
        if not item or not self.confirm("Permanently delete this local lesson and its evidence?"):
            return
        if item == self.session_id:
            if self.capture.snapshot()["pending"] or self.sample_id:
                return self.notice("Finish the sample and wait for transcription before deleting.")
            self.session_id = self.lesson = None
            self.capture.select_meeting(None, reset=True)
            self.capture.stop_mic()
            if self.presentation:
                self.presentation.close()
        self.store.delete(item)
        self.refresh()
        self.home_refresh()

    def open_lesson(self, key, room=None):
        if self.sample_id or self.capture.snapshot()["pending"] or "transcript" in self.jobs:
            return self.notice("Finish the current sample and wait for transcription before changing lessons.")
        if self.session_id != key and self.presentation:
            self.presentation.close()
            self.presentation = None
        changed = self.session_id != key
        self.session_id = key
        self.refresh()
        if changed:
            self.bound_meet = meet_code(self.lesson.get("meet_url")) or room
        self.capture.select_meeting(self.bound_meet, reset=changed and not self.bound_meet)
        if not self.demo and not self.lesson["approval"] and not self.capture.mic:
            self.apply_mic(preferred=self.store.settings("microphone"))
        self.navigate("Lesson Summary" if self.lesson["approval"] else "Live Studio")

    def draft(self):
        return self.lesson is not None and not self.lesson["approval"]

    def reservation_valid(self):
        if not self.lesson["reservation_id"]:
            return True
        reservation = next((row for row in self.reservations
                            if row["reservation_id"] == self.lesson["reservation_id"]
                            and (row.get("_adapter") or None) == self.lesson["adapter_url"]), None)
        return bool(reservation and reservation["status"] == "confirmed"
                    and reservation["student_id"] == self.lesson["student_id"]
                    and reservation.get("meet_url") == self.lesson["meet_url"]
                    and reservation.get("mission_id") == self.lesson["mission_id"])

    def toggle(self):
        if self.capture.snapshot()["mode"] != "paused":
            self.capture.pause()
            self.poll()
            return
        if not self.draft() or self.demo:
            return
        if "reference" in self.jobs:
            return self.notice("Wait for the sound check to finish.")
        if not self.reservation_valid():
            return self.notice("The current schedule no longer matches this reservation.")
        state = self.capture.snapshot()
        if not self.capture.recognizer or not state["tab"] or not state["mic"]:
            return self.notice("Wait for your mic and transcription, and allow audio on the Meet tab.")
        if not self.bound_meet:
            if not self.confirm(f'Confirm that Meet {state["meeting"]} contains only you and {self.lesson["name"]}.'):
                return
            self.bound_meet = state["meeting"]
            self.capture.select_meeting(self.bound_meet)
        try:
            if not self.sample_id:
                self.sample_id = self.store.sample(self.session_id, self.activity.currentText())
            latest = max((turn["end_ms"] for sample in self.lesson["samples"]
                          if sample["id"] == self.sample_id for turn in sample["turns"]), default=0)
            self.capture.start(self.sample_id, self.current_mode(), latest + 100 if latest else 0)
            self.refresh()
            self.poll()
        except ValueError as error:
            self.notice(str(error))

    def current_mode(self):
        return "assessment" if self.mode.currentIndex() == 0 else "transcript"

    def change_mode(self):
        if self.sample_id and self.capture.snapshot()["mode"] != "paused":
            try:
                self.capture.start(self.sample_id, self.current_mode())
            except ValueError as error:
                self.capture.pause(str(error))

    def finish(self):
        if not self.sample_id:
            return
        self.capture.pause()
        self.store.sample_action(self.sample_id, state="finished")
        self.sample_id = None
        self.refresh()
        self.poll()

    def poll(self):
        state = self.capture.snapshot()
        active = state["mode"] in ("assessment", "transcript")
        solo = state["mode"] == "solo"
        just_connected = state["tab"] and (not self.last_poll or not self.last_poll["tab"])
        if self.last_poll and (state["pending"], state["revision"]) != (self.last_poll["pending"], self.last_poll["revision"]):
            self.refresh()
        self.last_poll = state
        draft = self.draft() and not self.demo
        self.trigger.setEnabled(draft and not solo and "reference" not in self.jobs
                                and bool(self.capture.recognizer) and state["tab"] and state["mic"])
        self.finish_button.setEnabled(bool(self.sample_id))
        self.activity.setEnabled(not self.sample_id)
        status = {"paused": "PAUSED", "solo": "● MIC TEST",
                  "transcript": "●  RECORDING · TRANSCRIPT ONLY",
                  "assessment": "●  RECORDING · ASSESSMENT"}[state["mode"]]
        self.rail_status.setText(status)
        self.capture_state.setText(status)
        if self.trigger.property("recording") != active:
            self.trigger.setProperty("recording", active)
            self.trigger.style().unpolish(self.trigger)
            self.trigger.style().polish(self.trigger)
        self.trigger.setText("Pause" if active else "Resume recording" if self.sample_id else "Start recording")
        help_text = ("Recording. The student's words will appear after a brief pause." if active else
                     "Allow Meet audio on the browser tab to start." if not state["tab"] else
                     "Meet audio is connected. In a call alone, use the solo test or show your microphone.")
        if state["pending"] and not active:
            help_text = "Finishing the transcript…"
        self.capture_help.setText(state["failure"] or help_text)
        self.provisional.setText("Live draft · " + state["preview"])
        self.provisional.setVisible(bool(state["preview"]))
        self.ready_home(state)
        self.ready_solo(state, solo, active)
        if just_connected and (state["meeting"] == self.pending_meet or self.stack.currentIndex() == 0):
            room, self.pending_meet = state["meeting"], None
            QTimer.singleShot(0, lambda: self.meet_ready(room))
        for meter, key in zip(self.meters, ("student", "teacher")):
            meter.setValue(round(state[key] * 100))
        self.update_diagnostics()
        if self.toolbar and self.toolbar.isVisible():
            self.toolbar_state.setText(status)
            self.toolbar_trigger.setText(self.trigger.text())
            self.toolbar_trigger.setEnabled(self.trigger.isEnabled())
            self.toolbar_finish.setEnabled(self.finish_button.isEnabled())

    def ready_home(self, state):
        if self.demo:
            self.meet_home_state.setText("Fictional demonstration")
            self.meet_action.setEnabled(False)
        elif state["tab"]:
            self.meet_home_state.setText("Meet audio is connected.")
            self.meet_action.setText("Continue to lesson")
        elif self.pending_meet and self.pending_meet in state["rooms"]:
            self.meet_home_state.setText("In Chrome, click the Live Studio icon once to allow Meet audio.")
            self.meet_action.setText("Show Meet tab")
        elif state["rooms"]:
            self.meet_home_state.setText("Meet tab found." if len(state["rooms"]) == 1
                                         else f'{len(state["rooms"])} Meet tabs found.')
            self.meet_action.setText("Connect Meet")
        elif state["chrome"]:
            self.meet_home_state.setText("Chrome is connected. Waiting for a Meet tab.")
            self.meet_action.setText("Open Google Meet")
        else:
            self.meet_home_state.setText(state["bridge_error"] or self.bridge_status or
                                         "Looking for the Chrome connector…")
            self.meet_action.setText("Set up Chrome")
        mic = state["mic_name"] or ("Unavailable" if state["mic_error"] else "Checking…")
        speaker = state["speaker_name"] or "Windows default"
        self.audio_home.setText(f"Microphone   {mic}\nSpeakers   {speaker}")
        self.model_home.setText(self.model_status)

    def ready_solo(self, state, solo, active):
        self.solo_mic_status.setText(state["mic_name"] or state["mic_error"] or "Finding your microphone…")
        busy = active or self.sample_id is not None or "reference" in self.jobs
        self.solo_trigger.setEnabled(not self.demo and (solo or (
            not busy and bool(self.capture.recognizer) and state["mic"] and not state["pending"])))
        self.reference_trigger.setEnabled(not self.demo and not busy and not solo and
                                          bool(self.capture.recognizer) and not state["pending"])
        self.solo_trigger.setText("Stop mic test" if solo else "Start microphone test")
        if self.solo_trigger.property("recording") != solo:
            self.solo_trigger.setProperty("recording", solo)
            self.solo_trigger.style().unpolish(self.solo_trigger)
            self.solo_trigger.style().polish(self.solo_trigger)
        self.solo_meter.setValue(round(state["teacher"] * 100) if solo else 0)
        if not self.capture.recognizer:
            self.solo_feedback.setText(self.model_status)
        elif solo or self.solo_requested:
            transcript = state["solo_text"]
            if solo and state["preview"]:
                transcript = (transcript + " " + state["preview"]).strip()
            self.solo_transcript.setText(transcript or "Listening…" if solo else transcript or "No words heard.")
            if state["failure"]:
                hint = state["failure"]
            elif state["pending"]:
                hint = "Transcribing…"
            elif transcript:
                hint = "Your microphone and transcription are working."
            elif solo and state["solo_voice"]:
                hint = "Sound detected. Say a short English sentence, then pause."
            elif solo:
                hint = "Try saying “I can hear you clearly today.”"
            else:
                hint = "Try again and check that the microphone meter moves when you speak."
            self.solo_feedback.setText(hint)
        else:
            self.solo_feedback.setText("Try saying “I can hear you clearly today.”")

    def refresh(self):
        if not self.session_id:
            self.lesson = None
        else:
            self.lesson = self.store.load(self.session_id)
        lesson = self.lesson
        self.lesson_navigation()
        self.student_header.setText(f'{lesson["name"]}  ·  {lesson["level"]}' if lesson else "Open a lesson from Home")
        self.session_info.setText(("Approved" if lesson["approval"] else "Draft") +
                                  f'  ·  {lesson["mission_id"] or "Open speaking practice"}'
                                  if lesson else "Confirm a learner and consent to begin.")
        self.review_heading.setText(f'{lesson["name"]} · Transcript evidence' if lesson else "Open a lesson from Home")
        metrics = self.store.preview_metrics(lesson) if lesson else None
        values = (metrics["wpm"], metrics["flow"], len(metrics["vocabulary"]), len(metrics["confirmed_grammar"]),
                  metrics["errors_per_100"]) if metrics and metrics["words"] else [None] * 5
        for widget, number in zip(self.metrics, values):
            widget.setText(value(number))
        self.refresh_filters()
        self.refresh_transcript()
        self.vocab.setEnabled(self.draft())
        listings(self.board_items, [(f'{item["term"]}{"  ·  shared" if item["shared"] else ""}'
                                    f'{"  ·  revise" if item["revision"] else ""}', item["id"])
                                    for item in lesson["board"]] if lesson else [])
        if self.stack.currentIndex() == 3:
            self.refresh_summary()

    def refresh_filters(self):
        items = self.lesson["samples"] if self.lesson else []
        current = self.filter.currentData()
        self.filter.blockSignals(True)
        self.filter.clear()
        self.filter.addItem("All samples", None)
        for sample in items:
            self.filter.addItem(sample["label"] or f'Sample {sample["position"]}', sample["id"])
        self.filter.setCurrentIndex(max(0, self.filter.findData(current)))
        self.filter.blockSignals(False)

    def refresh_transcript(self):
        if not self.lesson:
            listings(self.live_turns, [])
            listings(self.review_turns, [])
            self.detail.clear()
            return
        live, reviewed = [], []
        search = self.search.text().casefold()
        for sample in self.lesson["samples"]:
            for turn in sample["turns"]:
                prefix = "★ " if turn["bookmarked"] else ""
                warning = "  ·  Check audio" if turn["flags"] else ""
                if turn["excluded"] or sample["excluded"]:
                    warning = "  ·  Excluded"
                title = (f'{prefix}{seconds(turn["start_ms"])}  ·  {turn["speaker"].title()}{warning}\n'
                         f'{turn["text"]}')
                item = (title, turn["id"])
                student = turn["speaker"] == "student"
                if (student or self.live_teacher.isChecked()) and not sample["excluded"]:
                    live.append(item)
                if (student or self.review_teacher.isChecked()) and (not self.bookmarks.isChecked() or turn["bookmarked"]):
                    if self.filter.currentData() not in (None, sample["id"]):
                        continue
                    if self.activity_filter.currentIndex() and self.activity_filter.currentText() != sample["activity"]:
                        continue
                    if search in (turn["text"] + " " + turn["teaching"]).casefold():
                        reviewed.append(item)
        listings(self.live_turns, live, follow=True)
        listings(self.review_turns, reviewed)
        self.refresh_detail()

    def get_turn(self, key):
        if self.lesson and key:
            for sample in self.lesson["samples"]:
                for turn in sample["turns"]:
                    if turn["id"] == key:
                        return turn

    def refresh_detail(self):
        turn = self.get_turn(selected_data(self.review_turns))
        self.detail.setHtml(turn_details(turn) if turn else "<p>Select a turn to inspect its evidence.</p>")

    def open_live_turn(self):
        key = selected_data(self.live_turns)
        if key:
            self.navigate("Review")
            for index in range(self.review_turns.count()):
                item = self.review_turns.item(index)
                if item.data(Qt.ItemDataRole.UserRole) == key:
                    self.review_turns.setCurrentItem(item)
                    break

    def edit_turn(self):
        turn = self.get_turn(selected_data(self.review_turns))
        if not turn or not self.draft():
            return
        original = plain(turn["original"])
        original.setReadOnly(True)
        verified = plain(turn["verified"] if turn["verified"] is not None else turn["original"])
        teaching = plain(turn["teaching"], "Optional wording to teach")
        status = combo(["observed", "independent", "assisted"])
        status.setCurrentText(turn["evidence"])
        excluded = checked("Exclude this turn", turn["excluded"])
        cleared = checked("I reviewed and cleared the audio flag")
        cleared.setEnabled(bool(turn["flags"]))
        errors = spin(-1, 300, -1 if turn["error_count"] is None else turn["error_count"])
        errors.setSpecialValueText("Unreviewed")
        constructions = [(key, checked(key, key in turn["confirmed_grammar"]))
                         for key in turn["features"].get("grammar", {})]
        dialog = form_dialog(self, "Verify a transcript turn", [
            ("Recognition", original), ("What was said", verified), ("Teaching correction", teaching),
            ("Evidence", status), ("Confirmed errors", errors), ("", excluded), ("", cleared)]
            + [("Confirm construction", choice) for _, choice in constructions])
        if dialog.exec():
            edited = verified.toPlainText().strip()
            fields = {"verified": None if edited == turn["original"] else edited,
                      "teaching": teaching.toPlainText().strip(), "evidence": status.currentText(),
                      "excluded": int(excluded.isChecked()), "flags": [] if cleared.isChecked() else turn["flags"]}
            if fields["verified"] == turn["verified"] or errors.value() != (
                    -1 if turn["error_count"] is None else turn["error_count"]):
                fields["error_count"] = errors.value() if errors.value() >= 0 else None
            if fields["verified"] == turn["verified"]:
                fields["confirmed_grammar"] = [key for key, choice in constructions if choice.isChecked()]
            if not self.background_job("transcript", lambda: self.store.revise(turn["id"], **fields)):
                self.notice("The previous transcript edit is still saving.")

    def bookmark_turn(self):
        turn = self.get_turn(selected_data(self.review_turns))
        if turn and self.draft():
            self.store.revise(turn["id"], bookmarked=int(not turn["bookmarked"]))
            self.refresh()

    def undo_turn(self):
        key = selected_data(self.review_turns)
        if key and self.draft():
            self.store.revise(key, undo=True)
            self.refresh()

    def quick_vocab(self):
        self.navigate("Live Studio")
        self.vocab.setFocus()

    def add_vocab(self):
        text = self.vocab.text().strip()
        if text and self.draft():
            self.store.board_item(self.session_id, term=text)
            self.vocab.clear()
            self.refresh()

    def term_from_live(self):
        turn = self.get_turn(selected_data(self.live_turns))
        if turn and turn["speaker"] == "student" and self.draft():
            self.vocab.setText(turn["teaching"] or turn["text"])
            self.vocab.selectAll()
            self.vocab.setFocus()

    def term_from_review(self):
        turn = self.get_turn(selected_data(self.review_turns))
        if not turn or turn["speaker"] != "student" or not self.draft():
            return
        phrase = self.detail.textCursor().selectedText().strip()
        self.quick_vocab()
        self.vocab.setText(phrase or turn["text"])
        self.vocab.selectAll()

    def edit_board(self):
        key = selected_data(self.board_items)
        if not key or not self.draft():
            return
        item = next(item for item in self.lesson["board"] if item["id"] == key)
        text_fields = [(field, line(title, item[field])) for field, title in (
            ("term", "Word or phrase"), ("meaning", "Turkish meaning"), ("definition", "English definition"),
            ("example", "Example"), ("note", "Private note"))]
        revision = checked("Mark for revision", item["revision"])
        shared = checked("Include on the student board and in exports", item["shared"])
        dialog = form_dialog(self, "Edit teaching item", [(widget.placeholderText(), widget) for _, widget in text_fields]
                             + [("", revision), ("", shared)])
        if dialog.exec():
            try:
                self.store.board_item(self.session_id, key, **{field: widget.text().strip() for field, widget in text_fields},
                                      revision=int(revision.isChecked()), shared=int(shared.isChecked()))
                self.refresh()
            except ValueError as error:
                self.notice(str(error))

    def delete_board(self):
        key = selected_data(self.board_items)
        if key and self.draft() and self.confirm("Delete this item from the teaching board?"):
            self.store.board_item(self.session_id, key)
            self.refresh()

    def refresh_summary(self):
        if not self.lesson:
            listings(self.sample_list, [])
            listings(self.history, [])
            self.summary_heading.setText("Open a lesson from Home")
            self.combined.clear()
            self.summary_status.clear()
            self.change_text.clear()
            self.trend.set_values([])
            return
        lesson = self.lesson
        metrics = self.store.preview_metrics(lesson)
        self.summary_heading.setText(f'{lesson["name"]} · {lesson["level"]} · Combined evidence')
        self.combined.setText(
            f'{metrics["words"]} eligible words  ·  {seconds(metrics["response_ms"])} measured response time  ·  '
            f'{value(metrics["wpm"])} WPM  ·  {value(metrics["flow"])} words/run  ·  {metrics["pause_count"]} pauses\n'
            f'{len(metrics["vocabulary"])} observed vocabulary  ·  {len(metrics["independent_vocabulary"])} independent  ·  '
            f'{len(metrics["grammar"])} grammar candidates  ·  {value(metrics["errors_per_100"])} errors / 100'
            f'  ·  {metrics["validated_words"]} reviewed words')
        queued = self.store.outbox(lesson["id"])
        self.summary_status.setText(
            f'APPROVED · {lesson["approval"]["outcome"]} · ' + (
                "Synced" if queued and queued["delivered_at"] else
                queued["last_error"] if queued and queued["last_error"] else
                "Pending adapter delivery" if lesson["source"] == "nexgen" else "Local report ready for export")
            if lesson["approval"] else "DRAFT · Teacher decision required")
        samples = []
        for sample in lesson["samples"]:
            result = report([sample], lesson["profile"])
            samples.append((f'{sample["label"] or "Sample " + str(sample["position"])}  ·  {sample["activity"]}\n'
                            f'{result["words"]} eligible words  ·  {value(result["wpm"])} WPM  ·  '
                            f'{"excluded" if sample["excluded"] else sample["state"]}', sample["id"]))
        listings(self.sample_list, samples)
        history = self.store.passport(lesson["student_id"], omit=lesson["id"])
        recorded = [entry for entry in history["lessons"] if entry["comparable"] and entry["profile_match"]
                    and entry["activities"] == metrics["activities"]]
        matching = [entry["wpm"] for entry in recorded]
        current = metrics["wpm"] if metrics["comparable"] else None
        self.trend.set_values([*matching, current] if current is not None and lesson["approval"] else matching)
        newly = set(metrics["vocabulary"]) - set(history["vocabulary"])
        reused = {word for word in metrics["independent_vocabulary"] if history["vocabulary"].get(word, {}).get("introduced")}
        self.change_text.setText(f'{len(newly)} newly observed · {len(reused)} previously taught and independently reused'
                                 + (f' · Pace {matching[-1]} → {current} WPM' if matching and current is not None else ''))
        items = [(f'New · {word}', word) for word in sorted(newly)]
        for group in ("vocabulary", "grammar"):
            items += [(f'{key}  ·  {entry["state"]}  ·  {entry["lessons"]} lesson(s)'
                       f'\n{entry["frequency"]} use(s) · first {entry["first"][:10]}'
                       + (f' · last {entry["last"][:10]}' if entry["last"] else "")
                       + (f' · {entry["confirmed"]} confirmed' if group == "grammar" else ""), group + "." + key)
                      for key, entry in sorted(history[group].items())]
        listings(self.history, items)

    def selected_sample(self):
        key = selected_data(self.sample_list)
        if self.lesson and key:
            return next((sample for sample in self.lesson["samples"] if sample["id"] == key), None)

    def edit_sample(self):
        sample = self.selected_sample()
        if not sample or not self.draft():
            return
        name = line("Sample name", sample["label"])
        activity = combo(ACTIVITIES)
        activity.setCurrentText(sample["activity"])
        excluded = checked("Exclude from combined evidence", sample["excluded"])
        dialog = form_dialog(self, "Edit speaking sample", [("Name", name), ("Activity", activity), ("", excluded)])
        if dialog.exec():
            self.store.sample_action(sample["id"], label=name.text().strip(), activity=activity.currentText(),
                                     excluded=int(excluded.isChecked()))
            self.refresh()

    def resume_sample(self):
        sample = self.selected_sample()
        if (not sample or self.demo or not self.draft() or self.sample_id or sample["excluded"]
                or self.capture.snapshot()["pending"]):
            return
        self.sample_id = sample["id"]
        self.store.sample_action(self.sample_id, state="open")
        self.activity.setCurrentText(sample["activity"])
        self.refresh()
        self.navigate("Live Studio")
        self.poll()

    def delete_sample(self):
        sample = self.selected_sample()
        if (sample and self.draft() and sample["id"] != self.sample_id and not self.capture.snapshot()["pending"]
                and self.confirm("Permanently delete this speaking sample and all its turns?")):
            self.store.sample_action(sample["id"])
            self.refresh()

    def approve(self):
        if not self.draft():
            return
        if self.sample_id or self.capture.snapshot()["pending"] or "transcript" in self.jobs:
            return self.notice("Finish the sample and wait for transcription before approving.")
        decision = combo(OUTCOMES)
        decision.setCurrentText("Review required")
        teacher = line("Teacher name", self.store.settings("teacher", ""))
        note = plain(placeholder="Optional feedback for the learner")
        acknowledged = checked("I reviewed the result and approve it for export.")
        dialog = form_dialog(self, "Approve lesson results", [("Outcome", decision), ("Teacher", teacher),
                                                              ("Feedback", note), ("", acknowledged)], "Approve")
        if not dialog.exec() or not acknowledged.isChecked():
            return
        try:
            self.store.approve(self.session_id, decision.currentText(), teacher.text(), note.toPlainText())
            self.store.configure("teacher", teacher.text().strip())
            self.refresh()
            self.sync()
        except ValueError as error:
            self.notice(str(error))

    def export(self, legacy=False):
        if not self.lesson:
            return
        try:
            queued = self.store.outbox(self.session_id)
            payload = legacy_export(self.lesson) if legacy else (
                json.loads(queued["payload"]) if queued else {"schema": "nexgen.live_studio.draft.v1", "lesson": self.lesson})
            name = "nexgen-review-studio" if legacy else "nexgen-live-studio-" + ("approved" if queued else "draft")
            path, _ = QFileDialog.getSaveFileName(self, "Export lesson", f"{name}-{self.session_id[:8]}.json", "JSON (*.json)")
            if path:
                target = Path(path)
                temporary = target.with_name(target.name + ".part")
                temporary.write_text(doc(payload) + "\n", encoding="utf-8")
                temporary.replace(target)
                self.statusBar().showMessage("Lesson exported.", 4000)
        except (ValueError, OSError) as error:
            self.notice(str(error))

    def present(self, turn=None):
        if not self.lesson:
            return
        if not isinstance(turn, dict):
            turn = None
        if self.presentation:
            self.presentation.close()
        window = QWidget(None, Qt.WindowType.Window)
        window.setWindowTitle("NexGen · Student Presentation")
        window.resize(940, 650)
        shell, layout = column(margins=32)
        window.setLayout(QHBoxLayout())
        window.layout().addWidget(shell)
        lesson = self.lesson
        metrics = self.store.preview_metrics(lesson)
        layout.addWidget(heading("NEXGEN COURSES", f'{lesson["name"]} · Speaking lesson', "Language from your lesson"))
        if turn and turn["speaker"] == "student" and not turn["flags"] and not turn["excluded"]:
            layout.addWidget(card(label("Your answer", "section"), label(turn["text"], wrap=True),
                                  label(turn["teaching"], "muted", True)))
        board = [item for item in lesson["board"] if item["shared"]]
        body, items = column()
        for item in board:
            detail = " · ".join(item[key] for key in ("meaning", "definition", "example") if item[key])
            items.addWidget(card(label(item["term"], "section", True), label(detail, "muted", True)))
        if not board and not turn:
            items.addWidget(card(label("No items shared yet", "muted")))
        if lesson["approval"]:
            history = self.store.passport(lesson["student_id"], omit=lesson["id"])
            passport = self.store.passport(lesson["student_id"])
            comparable = [entry for entry in history["lessons"] if entry["comparable"] and entry["profile_match"]
                          and entry["activities"] == metrics["activities"]]
            pace = Trend()
            current = metrics["wpm"]
            pace.set_values([entry["wpm"] for entry in comparable] + ([current] if current is not None else []))
            result = (f'{len(metrics["independent_vocabulary"])} independently demonstrated vocabulary items · '
                      f'{len(metrics["independent_grammar"])} independently demonstrated grammar patterns')
            comparisons = ""
            if comparable and metrics["comparable"]:
                old = comparable[-1]
                comparisons = (f'Pace {old["wpm"]} → {current} WPM · '
                               f'Flow {old["flow"]} → {metrics["flow"]} words/run')
                if old["errors_per_100"] is not None and metrics["errors_per_100"] is not None:
                    comparisons += f' · Reviewed errors {old["errors_per_100"]} → {metrics["errors_per_100"]} per 100 words'
            new_words = sorted(set(metrics["independent_vocabulary"]) - set(history["vocabulary"]))
            items.addWidget(card(label("Speaking evidence", "section"), label(result, wrap=True),
                                 label("Comparable speaking pace · WPM", "eyebrow"), pace,
                                 label(comparisons, "muted", True),
                                 label("Newly demonstrated · " + ", ".join(new_words), "muted", True) if new_words
                                 else label("")))
            for key, title in (("vocabulary", "Vocabulary passport"), ("grammar", "Grammar passport")):
                new_total = [item for item, entry in passport[key].items() if entry["independent_lessons"]]
                retained = [item for item in new_total if passport[key][item]["state"] == "retained"]
                if new_total:
                    examples = ", ".join(item.split(".")[-1].replace("_", " ") for item in sorted(new_total)[:12])
                    items.addWidget(card(label(title, "section"),
                                         label(f"{len(new_total)} demonstrated · {len(retained)} demonstrated on separate days", wrap=True),
                                         label(examples, "muted", True)))
        items.addStretch()
        layout.addWidget(scroll(body), 1)
        self.presentation = window
        window.show()

    def share_live_turn(self):
        self.share_turn(self.live_turns)

    def share_review_turn(self):
        self.share_turn(self.review_turns)

    def share_turn(self, widget):
        turn = self.get_turn(selected_data(widget))
        if turn and turn["speaker"] == "student" and not turn["flags"] and not turn["excluded"]:
            if self.confirm("Show this learner answer and its teaching correction in the presentation window?"):
                self.present(turn)

    def show_toolbar(self):
        if not self.toolbar:
            self.toolbar = QWidget(None, Qt.WindowType.Tool | Qt.WindowType.WindowStaysOnTopHint)
            self.toolbar.setWindowTitle("NexGen capture controls")
            self.toolbar_state = label("PAUSED", "eyebrow")
            self.toolbar_trigger = button("Start sample", self.toggle, True)
            self.toolbar_finish = button("Finish", self.finish)
            body = row(self.toolbar_state, self.toolbar_trigger, self.toolbar_finish, button("Studio", self.show_studio))
            layout = QHBoxLayout(self.toolbar)
            layout.addWidget(body)
        self.toolbar.show()
        self.poll()

    def show_studio(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()
        self.navigate("Live Studio")

    @staticmethod
    def launch_chrome(url):
        for directory in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"), os.environ.get("LOCALAPPDATA")):
            if directory:
                executable = Path(directory) / "Google" / "Chrome" / "Application" / "chrome.exe"
                if executable.is_file():
                    subprocess.Popen([str(executable), url])
                    return
        webbrowser.open(url)

    def chrome_setup(self):
        folder = str(self.root / "extension")
        QApplication.clipboard().setText(folder)
        dialog = QMessageBox(self)
        dialog.setWindowTitle("Connect Chrome once")
        dialog.setText("Set up the Live Studio connector in Chrome")
        dialog.setInformativeText("1. Open Chrome Extensions and turn on Developer mode.\n"
                                  "2. Choose Load unpacked and paste the copied folder path.\n"
                                  "3. Pin Live Studio from the Chrome Extensions menu.")
        launch = dialog.addButton("Open Chrome Extensions", QMessageBox.ButtonRole.AcceptRole)
        dialog.addButton("Close", QMessageBox.ButtonRole.RejectRole)
        dialog.exec()
        if dialog.clickedButton() == launch:
            self.launch_chrome("chrome://extensions/")

    def meet_flow(self):
        if self.demo:
            return
        if self.capture.snapshot()["mode"] == "solo":
            self.capture.pause()
        state = self.capture.snapshot()
        if state["tab"]:
            self.meet_ready(state["meeting"])
        elif state["rooms"]:
            recommended = meet_code(self.recommended.get("meet_url")) if self.recommended else None
            preferred = self.bound_meet or recommended
            if preferred in state["rooms"]:
                room = preferred
            elif len(state["rooms"]) == 1:
                room = state["rooms"][0]
            else:
                room, chosen = QInputDialog.getItem(self, "Choose a Meet tab", "Meeting code", state["rooms"], editable=False)
                if not chosen:
                    return
            self.pending_meet = room
            self.capture.focus_meeting(room)
            self.poll()
        elif state["chrome"]:
            url = self.lesson.get("meet_url") if self.draft() else None
            self.launch_chrome(url or "https://meet.google.com/")
        else:
            self.chrome_setup()

    def meet_ready(self, room):
        if self.capture.snapshot()["meeting"] != room:
            return
        if self.draft() and self.bound_meet == room:
            self.navigate("Live Studio")
            return
        if self.sample_id or self.capture.snapshot()["pending"]:
            return
        self.raise_()
        self.activateWindow()
        matched = [item for item in upcoming(self.reservations) if meet_code(item.get("meet_url")) == room]
        self.identity(matched[0] if len(matched) == 1 else {}, room=room, start=True)

    def background_job(self, name, operation):
        if name in self.jobs:
            return False
        self.jobs.add(name)

        def perform():
            try:
                result = operation()
            except Exception as error:
                self.background.emit(name, None, error)
            else:
                self.background.emit(name, result, None)

        Thread(target=perform, daemon=True, name=f"Studio {name}").start()
        return True

    def load_model(self, download=False, automatic=False):
        if self.demo:
            return
        if self.sample_id or self.capture.snapshot()["mode"] != "paused" or self.capture.snapshot()["pending"] or "reference" in self.jobs:
            return self.notice("Finish the sample before changing the model.")
        model, device = self.model_choice.currentText(), self.compute.currentText()
        def load():
            cache, existing = str(self.data / "models"), self.existing_models()
            if not automatic:
                return Recognizer(model, device, cache, existing, download)
            try:
                return Recognizer(model, device, cache, existing)
            except (OSError, RuntimeError, ValueError):
                return Recognizer("base.en", "cpu", cache, existing, download=True)

        if self.background_job("model", load):
            self.capture.recognizer = None
            self.model_retry.hide()
            self.model_status = "Getting transcription ready. First use may download the speech model…"
            self.store.configure("model", model)
            self.store.configure("device", device)

    def detect_mics(self):
        if not self.demo:
            self.background_job("microphones", self.capture.enumerate_mics)

    def apply_mic(self, _=False, preferred=None):
        if self.demo:
            return
        selected = preferred or (self.store.settings("microphone") if self.mic_choice.count() == 1 else None) or self.mic_choice.currentText()
        if selected == "Windows default":
            selected = None
        self.store.configure("microphone", selected)
        self.capture.microphone_worker(selected)

    def recover_mic(self):
        preferred = self.store.settings("microphone")
        if self.capture.snapshot()["mode"] == "paused":
            if not self.capture.mic and (not self.capture.mic_task or not self.capture.mic_task.is_alive()):
                self.capture.microphone_worker(preferred)
            self.background_job("endpoints", lambda: self.capture.default_endpoints(preferred))

    def import_schedule(self):
        path, _ = QFileDialog.getOpenFileName(self, "Import reservations", "", "JSON (*.json)")
        if not path:
            return
        try:
            rows = reservation_rows(json.loads(Path(path).read_text(encoding="utf-8-sig")))
            self.set_schedule([{**row, "_adapter": False} for row in rows])
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            self.notice("The reservation file is invalid. See the reservation example in the guide.")

    def set_schedule(self, rows):
        self.reservations = rows
        self.store.configure("reservations", rows)
        if self.draft() and self.sample_id and not self.reservation_valid():
            self.capture.pause("This reservation changed. Check the learner and meeting before continuing.")
        self.home_refresh()

    def configure_adapter(self):
        api, token = self.api_input.text().strip().rstrip("/"), self.token_input.text().strip()
        if api:
            try:
                https_endpoint(api, "reservations")
            except ValueError as error:
                return self.notice(str(error))
        self.api, self.token = api, token
        self.store.configure("api_url", api)
        self.integration_state.setText(self.integration_label())
        self.fetch_reservations()
        self.sync()

    def integration_label(self):
        if not self.api or not self.token:
            return "Local mode · NexGen adapter disconnected"
        return "NexGen adapter configured"

    def fetch_reservations(self):
        if self.api and self.token and not self.demo:
            api, token = self.api, self.token
            self.background_job("reservations", lambda: (api, get_reservations(api, token)))

    def sync(self, force=False):
        if self.api and self.token and not self.demo:
            api, token = self.api, self.token
            self.background_job("sync", lambda: sync_due(self.store, api, token, bool(force)))

    def background_done(self, name, result, error):
        self.jobs.discard(name)
        if name == "model":
            if error:
                self.model_status = "Transcription unavailable. Retry with an internet connection."
                self.model_retry.show()
                self.model_detail = str(error).splitlines()[0][:240]
            else:
                self.capture.recognizer = result
                self.model_status = "Transcription ready"
                self.model_detail = f"{result.model} · {result.device.upper()} · {result.load_seconds:.2f} s warmup"
        elif name == "chrome":
            self.bridge_status = ("Chrome connection setup failed. Check the Windows connector." if error else
                                  "Open Chrome with the Live Studio connector enabled.")
        elif name == "endpoints" and not error:
            expected, speaker = result
            self.capture.speaker = speaker
            if self.detected_mic and expected and expected != self.detected_mic and self.capture.snapshot()["mode"] == "paused":
                self.capture.microphone_worker(self.store.settings("microphone"))
            self.detected_mic = expected
        elif name == "reference":
            self.reference_status.setText(f'Heard on the test recording · {result}' if result else
                                          "The test sentence could not be played or transcribed. Check the speaker volume.")
        elif name == "microphones":
            if error:
                self.notice("Windows audio devices could not be enumerated.")
            else:
                _, choices = result
                self.mic_choice.clear()
                self.mic_choice.addItems(["Windows default", *(item["name"] for item in choices)])
                self.mic_choice.setCurrentText(self.store.settings("microphone") or "Windows default")
        elif name == "reservations":
            if error:
                self.integration_state.setText("Reservation refresh failed. The last saved schedule is available offline.")
            else:
                api, rows = result
                if api == self.api:
                    self.set_schedule([{**row, "_adapter": api} for row in rows])
                    self.integration_state.setText("NexGen schedule refreshed.")
        elif name == "sync":
            if error:
                self.integration_state.setText("Could not run synchronization.")
            if self.stack.currentIndex() == 3:
                self.refresh_summary()
        elif name == "transcript":
            if error:
                self.notice("Could not save the transcript edit.")
            self.refresh()

    def update_diagnostics(self):
        snapshot = self.capture.snapshot()
        mic = self.capture.mic or self.capture.mic_error or "Opens after you confirm learner and consent"
        bridge = snapshot["bridge_error"] or ("Connected" if snapshot["tab"] else "Disconnected")
        self.diagnostics.setText(f'{self.model_status}\nMicrophone · {mic}\nMeet audio · {bridge}\n'
                                 + getattr(self, "model_detail", ""))
        if self.api_input.text() == "" and self.token_input.text() == "":
            self.integration_state.setText(self.integration_label())

    def save_preferences(self):
        values = [self.hot_toggle.keySequence().toString(), self.hot_finish.keySequence().toString()]
        if values[0] == values[1] and values[0]:
            return self.notice("Choose two different shortcuts.")
        for value_ in values:
            if value_ and (len(value_.split("+")[-1]) < 2 and "+" not in value_):
                return self.notice("Use a function key or a shortcut with a modifier for global controls.")
        for key, value_ in zip(("hot_toggle", "hot_finish"), values):
            self.store.configure(key, value_)
        self.store.configure("retention", self.retention.value())
        self.register_hotkeys()
        self.statusBar().showMessage(self.shortcut_alert or "Preferences saved. Retention runs at next launch.", 5000)

    def edit_profile(self):
        selected = combo(LEVELS)
        if not form_dialog(self, "Choose assessment level", [("Level", selected)], "Continue").exec():
            return
        level = selected.currentText()
        data = self.store.settings(f"profile.{level}", profile(level))
        pause = spin(200, 2000, data["pause_ms"])
        gap = spin(2000, 10000, data["response_gap_ms"])
        words = spin(1, 500, data["min_timed_words"])
        duration = spin(1, 600, data["min_response_ms"] // 1000)
        activities = [checked(activity, activity in data["required_activities"]) for activity in ACTIVITIES[1:]]
        if form_dialog(self, f"{level} evidence profile", [("Meaningful pause (ms)", pause), ("Response gap (ms)", gap),
                ("Minimum timed words", words), ("Minimum response (seconds)", duration)]
                + [("Required activity", item) for item in activities]).exec():
            self.store.configure(f"profile.{level}", {"level": level, "pause_ms": pause.value(),
                "response_gap_ms": gap.value(), "min_timed_words": words.value(),
                "min_response_ms": duration.value() * 1000,
                "required_activities": [item.text() for item in activities if item.isChecked()]})

    def hotkey_parts(self, text):
        pieces = text.split("+")
        main = pieces[-1].upper()
        mods = {"ALT": 1, "CTRL": 2, "SHIFT": 4, "META": 8, "WIN": 8}
        code = 0
        if main.startswith("F") and main[1:].isdigit() and 1 <= int(main[1:]) <= 24:
            code = 0x70 + int(main[1:]) - 1
        elif len(main) == 1 and main.isascii() and main.isalnum():
            code = ord(main)
        return 0x4000 | sum(mods[piece.upper()] for piece in pieces[:-1]), code

    def register_hotkeys(self):
        if os.name != "nt" or self.demo:
            return
        user = ctypes.windll.user32
        for index in self.hotkeys:
            user.UnregisterHotKey(int(self.winId()), index)
        self.hotkeys.clear()
        self.shortcut_alert = ""
        for index, key, default in ((191, "hot_toggle", "F8"), (192, "hot_finish", "F9")):
            text = self.store.settings(key, default)
            if not text:
                continue
            try:
                mods, code = self.hotkey_parts(text)
                if not code or not user.RegisterHotKey(int(self.winId()), index, mods, code):
                    self.shortcut_alert = f"Could not register {text}. Choose another global shortcut."
                else:
                    self.hotkeys.append(index)
            except KeyError:
                self.shortcut_alert = f"{text} is unsupported. Choose a function key or a modifier with a letter."
        if self.shortcut_alert:
            self.statusBar().showMessage(self.shortcut_alert, 6000)

    def nativeEvent(self, event_type, message):
        if os.name == "nt":
            native = wintypes.MSG.from_address(int(message))
            if native.message == 0x0312 and native.wParam in self.hotkeys:
                action = self.toggle if native.wParam == 191 else self.finish
                if not QApplication.activeModalWidget() and (action == self.finish or self.capture.snapshot()["mode"] != "paused"
                        or self.trigger.isEnabled()):
                    QTimer.singleShot(0, action)
                return True, 0
        return super().nativeEvent(event_type, message)

    def confirm(self, message):
        return QMessageBox.question(self, "NexGen Live Studio", message,
                                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                                    QMessageBox.StandardButton.Cancel) == QMessageBox.StandardButton.Yes

    def notice(self, message):
        QMessageBox.information(self, "NexGen Live Studio", message)

    def closeEvent(self, event):
        if self.sample_id:
            self.finish()
        if self.capture.snapshot()["pending"] or "transcript" in self.jobs:
            self.statusBar().showMessage("Finishing transcription. Live Studio will close when the draft is saved.", 4000)
            QTimer.singleShot(500, self.close)
            event.ignore()
            return
        self.capture.close()
        if self.presentation:
            self.presentation.close()
        if self.toolbar:
            self.toolbar.close()
        if os.name == "nt":
            for index in self.hotkeys:
                ctypes.windll.user32.UnregisterHotKey(int(self.winId()), index)
        super().closeEvent(event)
