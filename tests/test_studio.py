from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
from struct import pack
import tempfile
from time import monotonic, sleep
from unittest import TestCase, mock
from urllib.error import URLError

import numpy as np
from websockets.exceptions import ConnectionClosed, InvalidStatus
from websockets.sync.client import connect

from studio.analysis import tokens
from studio.capture import Capture, RATE, microphone_order
from studio.chrome import ORIGIN
from studio.integrations import legacy_export, reservation_rows, suggested, sync_due, upcoming
from studio.store import Store


def wait_for(check, seconds=4):
    end = monotonic() + seconds
    while monotonic() < end:
        if check():
            return True
        sleep(.02)
    return False


def turn(sample, text, start=100, width=300, speaker="student", mode="assessment", epoch="a", flags=()):
    words = [{"text": word, "start_ms": start + index * width,
              "end_ms": start + (index + 1) * width, "probability": .9}
             for index, word in enumerate(tokens(text))]
    return {"sample_id": sample, "epoch": epoch, "speaker": speaker, "mode": mode, "start_ms": start,
            "end_ms": words[-1]["end_ms"], "original": text, "words": words, "flags": list(flags)}


class Database(TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name, "test.sqlite")
        self.store = Store(self.path)
        self.identity = {"student_id": "synthetic-learner", "name": "Fictional Student", "level": "B1"}
        self.session = self.store.create(self.identity, True)

    def test_consent_and_unique_reservation(self):
        with self.assertRaises(ValueError):
            self.store.create(self.identity, False)
        identity = {**self.identity, "reservation_id": "r"}
        self.store.create(identity, True)
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.create(identity, True)

    def test_weighted_combined_metrics_skip_unusable_turns(self):
        first = self.store.sample(self.session, "Finish & Expand")
        second = self.store.sample(self.session, "Scenario & Role-Play")
        for sample, text, width in ((first, "I can give you one good example today", 500),
                                    (second, "I have learned important lessons and I could teach them in another session today", 1000)):
            self.store.add_turn(turn(sample, text, width=width))
        self.store.add_turn(turn(first, "These are my notes", start=5000, speaker="teacher"))
        self.store.add_turn(turn(first, "I also went there", start=6500, mode="transcript"))
        assisted = self.store.add_turn(turn(second, "This was not independent", start=20000))
        self.store.revise(assisted, evidence="assisted")
        self.store.add_turn(turn(second, "Uncertain overlapping words", start=22000, flags=["overlap"]))
        metrics = self.store.preview_metrics(self.store.load(self.session))
        self.assertEqual((metrics["words"], metrics["response_ms"], metrics["wpm"]), (22, 18000, 73.3))
        self.store.sample_action(first, excluded=1)
        self.assertEqual(self.store.preview_metrics(self.store.load(self.session))["words"], 14)

    def test_corrections_preserve_original_and_undo(self):
        sample = self.store.sample(self.session, "Compare & Choose")
        entry = turn(sample, "She could fly")
        key = self.store.add_turn(entry)
        self.assertIsNone(self.store.add_turn(entry))
        self.store.revise(key, error_count=0, evidence="independent", confirmed_grammar=["modality.could"])
        self.store.revise(key, teaching="She could take a flight rather than walk")
        lesson = self.store.load(self.session)
        metrics = self.store.preview_metrics(lesson)
        self.assertIn("modality.could", metrics["confirmed_grammar"])
        self.assertNotIn("flight", metrics["vocabulary"])
        self.store.revise(key, verified="She went there yesterday")
        revised = self.store.load(self.session)["samples"][0]["turns"][0]
        self.assertEqual(revised["original"], "She could fly")
        self.assertEqual(revised["words"], [])
        self.assertIsNone(revised["error_count"])
        self.assertEqual(revised["confirmed_grammar"], [])
        self.assertIn("go", revised["features"]["vocabulary"])
        self.assertTrue(self.store.revise(key, undo=True))
        self.assertEqual(len(self.store.load(self.session)["samples"][0]["turns"][0]["words"]), 3)

    def test_previous_response_is_not_a_pause_and_epochs_are_independent(self):
        sample = self.store.sample(self.session, "Finish & Expand")
        for text, start, who, epoch in (("Choose one please", 100, "teacher", "a"),
                                       ("I would choose the red one", 2500, "student", "a"),
                                       ("I can give one example", 4900, "student", "a"),
                                       ("The color looks great", 7400, "student", "b")):
            self.store.add_turn(turn(sample, text, start=start, speaker=who, epoch=epoch))
        result = self.store.preview_metrics(self.store.load(self.session))
        self.assertEqual(result["thinking_median_ms"], 1500)
        self.assertEqual(result["pause_count"], 1)
        self.assertEqual(result["pause_median_ms"], 600)

    def test_draft_recovery_approval_and_export(self):
        sample = self.store.sample(self.session, "Unlabelled")
        entry = turn(sample, "I went to school")
        self.store.add_turn(entry)
        self.store.board_item(self.session, term="board only vocabulary", shared=1, note="private")
        with self.assertRaises(ValueError):
            self.store.approve(self.session, "Passed", "Coach", "")
        reopened = Store(self.path)
        self.assertEqual(reopened.load(self.session)["samples"][0]["state"], "interrupted")
        payload = reopened.approve(self.session, "Passed", "Coach", "Good effort")
        self.assertNotIn("board only vocabulary", payload["metrics"]["vocabulary"])
        self.assertNotIn("private", json.dumps(payload))
        exported = legacy_export(reopened.load(self.session))
        self.assertEqual(exported["preferred_transcript"]["segments"][0]["speaker"], "student")
        self.assertTrue(exported["session"]["consent_confirmed"])
        with self.assertRaises(ValueError):
            reopened.approve(self.session, "Passed", "Coach", "")
        with self.assertRaises(ValueError):
            reopened.add_turn(entry)
        reopened.delete(self.session)
        self.assertIsNone(reopened.outbox(self.session))

    def test_authoritative_outbox_retry_and_host_binding(self):
        api = "https://nexgen.example/api"
        key = self.store.create({**self.identity, "source": "nexgen", "reservation_id": "booking",
                                 "adapter_url": api}, True)
        self.store.approve(key, "Insufficient evidence", "Coach", "")
        with mock.patch("studio.integrations.build_opener") as opener:
            instance = opener.return_value
            instance.open.side_effect = URLError("offline")
            self.assertEqual(sync_due(self.store, "https://other.example/api", "synthetic"), 0)
            instance.open.assert_not_called()
            self.assertEqual(sync_due(self.store, api, "synthetic"), 0)
            self.assertEqual(self.store.outbox(key)["attempts"], 1)
            instance.open.reset_mock()
            self.assertEqual(sync_due(self.store, api, "synthetic"), 0)
            instance.open.assert_not_called()
            instance.open.side_effect = None
            instance.open.return_value.__enter__.return_value.read.return_value = json.dumps(
                {"status": "duplicate", "session_id": key}).encode()
            self.assertEqual(sync_due(self.store, api, "synthetic", force=True), 1)
            self.assertEqual(instance.open.call_args[0][0].get_header("Idempotency-key"), key)
            self.assertIsNotNone(self.store.outbox(key)["delivered_at"])

    def test_reservations_cancel_reschedule_and_ambiguous_candidates(self):
        now = datetime(2030, 1, 1, tzinfo=timezone.utc)
        row = {**self.identity, "reservation_id": "r", "status": "confirmed", "updated_at": now.isoformat(),
               "start_at": (now - timedelta(minutes=2)).isoformat(),
               "end_at": (now + timedelta(minutes=25)).isoformat()}
        second = {**row, "reservation_id": "r2"}
        cancelled = {"reservation_id": "r", "status": "cancelled",
                     "updated_at": (now + timedelta(minutes=1)).isoformat()}
        payload = {"schema": "nexgen.live_studio.reservations.v1", "reservations": [row, second]}
        self.assertIsNone(suggested(upcoming(reservation_rows(payload), now), now))
        payload["reservations"].append(cancelled)
        self.assertEqual(suggested(upcoming(reservation_rows(payload), now), now)["reservation_id"], "r2")


class TestRecognition:
    def decode(self, audio):
        if np.sqrt(np.mean(audio * audio)) < .01:
            return []
        text = "Student speech" if max(audio) > .17 else "Teacher speech"
        words = [{"text": word, "start_ms": 30 + index * 125, "end_ms": 150 + index * 125, "probability": .9}
                 for index, word in enumerate(text.split())]
        return [(text, words, [])]


class CaptureTests(TestCase):
    def setUp(self):
        Database.setUp(self)
        self.capture = Capture(self.store)
        self.addCleanup(self.capture.close)
        self.capture.recognizer = TestRecognition()
        self.capture.mic = "Synthetic teacher input"
        self.capture.server_start(0)
        self.url = f"ws://127.0.0.1:{self.capture.server.socket.getsockname()[1]}/capture/v1"
        self.origin = "chrome-extension://" + "a" * 32
        self.sample = self.store.sample(self.session, "Compare & Choose")

    def test_bridge_auth_pause_speaker_channels_and_disconnect(self):
        with self.assertRaises(InvalidStatus):
            connect(self.url, origin="https://example.org")
        with connect(self.url, origin=self.origin) as rejected:
            rejected.send(json.dumps({"token": "bad", "meet": "abc-defg-hij"}))
            with self.assertRaises(ConnectionClosed):
                rejected.recv(timeout=2)
        self.capture.select_meeting("abc-defg-hij")
        with connect(self.url, origin=self.origin) as bridge:
            bridge.send(json.dumps({"token": self.capture.token, "meet": "abc-defg-hij"}))
            paused = json.loads(bridge.recv(timeout=2))
            self.assertFalse(paused["capture"])
            bridge.send("heartbeat")
            self.assertTrue(wait_for(lambda: self.capture.snapshot()["tab"]))
            self.capture.start(self.sample, "assessment")
            active = json.loads(bridge.recv(timeout=2))
            speech = np.full(RATE // 10, 10000, dtype="<i2").tobytes()
            quiet = bytes(RATE // 5)
            tick = monotonic()
            for index in range(12):
                bridge.send(pack("<II", active["generation"], index) + (speech if index < 4 else quiet))
                if index >= 5:
                    self.capture.feed("teacher", active["generation"], np.full(RATE // 10, .10), tick + index / 10)
                sleep(.1)
            self.capture.pause()
            new_pause = json.loads(bridge.recv(timeout=2))
            self.assertFalse(new_pause["capture"])
            bridge.send(pack("<II", active["generation"], 12) + speech)
            self.capture.feed("teacher", new_pause["generation"], np.full(RATE // 10, .10), monotonic())
            self.assertTrue(wait_for(lambda: self.capture.snapshot()["pending"] == 0))
            self.assertEqual({item["speaker"]: item["text"] for item in self.store.load(self.session)["samples"][0]["turns"]},
                             {"student": "Student speech", "teacher": "Teacher speech"})
            self.capture.start(self.sample, "transcript")
            self.assertTrue(json.loads(bridge.recv(timeout=2))["capture"])
        self.assertTrue(wait_for(lambda: self.capture.snapshot()["mode"] == "paused"))
        self.assertIn("disconnected", self.capture.snapshot()["failure"])

    def test_missing_tab_frames_fail_to_paused(self):
        with connect(self.url, origin=self.origin) as bridge:
            bridge.send(json.dumps({"token": self.capture.token, "meet": "abc-defg-hij"}))
            bridge.recv(timeout=2)
            bridge.send("heartbeat")
            self.assertTrue(wait_for(lambda: self.capture.snapshot()["tab"]))
            self.capture.start(self.sample, "assessment")
            self.assertTrue(json.loads(bridge.recv(timeout=2))["capture"])
            self.assertTrue(wait_for(lambda: self.capture.snapshot()["mode"] == "paused"))
            self.assertIn("stopped", self.capture.snapshot()["failure"])

    def test_automatic_audio_prefers_a_real_microphone(self):
        devices = [{"index": 1, "name": "Stereo Karışımı (Realtek Audio)"},
                   {"index": 2, "name": "CABLE Output (VB-Audio Virtual Cable)"},
                   {"index": 3, "name": "Intel Microphone Array"}]
        self.assertEqual(microphone_order(1, devices, None), [devices[2]])
        self.assertEqual(microphone_order(1, devices, devices[1]["name"]), [devices[1], devices[2]])

    def test_solo_works_without_meet_and_never_writes_to_a_lesson(self):
        self.capture.start(None, "solo")
        generation = self.capture.generation
        for _ in range(5):
            self.capture.feed("student", generation, np.full(RATE // 10, .5), monotonic())
            self.capture.feed("teacher", generation, np.full(RATE // 10, .10), monotonic())
        self.capture.pause()
        self.assertTrue(wait_for(lambda: self.capture.snapshot()["pending"] == 0))
        self.assertEqual(self.capture.snapshot()["solo_text"], "Teacher speech")
        self.assertTrue(self.capture.snapshot()["solo_voice"])
        self.assertEqual(self.store.load(self.session)["samples"][0]["turns"], [])

    def test_discovery_authentication_and_focus_do_not_start_capture(self):
        endpoint = self.url.replace("/capture/v1", "/discover/v2")
        with connect(endpoint, origin=self.origin) as rejected:
            with self.assertRaises(ConnectionClosed):
                rejected.recv(timeout=2)
        with connect(endpoint, origin=ORIGIN) as browser:
            browser.send(json.dumps({"token": self.capture.token}))
            browser.send(json.dumps({"rooms": ["abc-defg-hij"]}))
            self.assertTrue(wait_for(lambda: self.capture.snapshot()["rooms"] == ["abc-defg-hij"]))
            self.assertEqual(self.capture.snapshot()["mode"], "paused")
            self.assertFalse(self.capture.snapshot()["tab"])
            self.capture.focus_meeting("abc-defg-hij")
            self.assertEqual(json.loads(browser.recv(timeout=2)), {"focus": "abc-defg-hij"})
