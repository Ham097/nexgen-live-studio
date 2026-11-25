"""Memory-only dual-channel acquisition with explicit recording epochs."""
from collections import deque
from dataclasses import dataclass, field
import json
from queue import Empty, Full, Queue
import re
import secrets
from struct import unpack_from
from threading import Event, RLock, Thread
from time import monotonic, sleep
import unicodedata
from urllib.parse import urlsplit
from uuid import uuid4

import numpy as np
import soxr
from websockets.exceptions import ConnectionClosed
from websockets.sync.server import serve

from .chrome import ORIGIN

RATE = 16000
PORT = 18763
FRAME_MS = 100
GATE = 0.008
MEET_CODE = re.compile(r"[a-z]{3}-[a-z]{4}-[a-z]{3}")


def physical_mic(name):
    name = "".join(char for char in unicodedata.normalize("NFKD", name.casefold().replace("ı", "i"))
                   if not unicodedata.combining(char))
    return not any(marker in name for marker in ("stereo mix", "stereo karisimi", "stereomix",
                                                  "cable output", "virtual", "what u hear", "loopback",
                                                  "wave out mix", "mezcla estereo", "mixage stereo"))


def microphone_order(default, choices, preferred):
    physical = [device for device in choices if physical_mic(device["name"])]
    selected = [device for device in choices if preferred and device["name"] == preferred]
    automatic = sorted(physical or choices, key=lambda device: device["index"] != default)
    return selected + [device for device in automatic if device not in selected]


@dataclass
class Buffer:
    signal: deque = field(default_factory=lambda: deque(maxlen=2))
    chunks: list = field(default_factory=list)
    started: int = 0
    silent: int = 0
    preview: bool = False


class Capture:
    def __init__(self, store):
        self.store = store
        self.recognizer = None
        self.lock = RLock()
        self.jobs = Queue(maxsize=20)
        self.closed = Event()
        self.generation = 0
        self.token = secrets.token_urlsafe(24)
        self.connected = False
        self.meeting = None
        self.expected_meeting = None
        self.levels = {"student": 0., "teacher": 0.}
        self.latest = dict.fromkeys(self.levels, 0.)
        self.buffers = {key: Buffer() for key in self.levels}
        self.speech = deque()
        self.mode = "paused"
        self.sample_id = None
        self.epoch = None
        self.clock = monotonic()
        self.offset = 0
        self.last_ms = 0
        self.revision = 0
        self.failure = ""
        self.preview_text = ""
        self.pending = 0
        self.mic = None
        self.mic_error = ""
        self.server = None
        self.bridge_error = ""
        self.peer = None
        self.discoveries = {}
        self.focus = (0, None)
        self.speaker = None
        self.solo_text = deque(maxlen=40)
        self.solo_voice = False
        self.mic_task = None
        self.worker = Thread(target=self._decode, daemon=True, name="Studio inference")
        self.worker.start()

    def snapshot(self):
        with self.lock:
            tick = monotonic()
            visible = [rooms for updated, rooms in self.discoveries.values() if tick - updated < 6]
            return {"mode": self.mode, "tab": self.connected, "meeting": self.meeting,
                    "chrome": bool(visible), "rooms": sorted(set().union(*visible)),
                    "mic": self.mic is not None, "mic_error": self.mic_error,
                    "mic_name": self.mic, "speaker_name": self.speaker,
                    "solo_text": " ".join(self.solo_text), "solo_voice": self.solo_voice,
                    "bridge_error": self.bridge_error, "failure": self.failure, "pending": self.pending,
                    "preview": self.preview_text, "revision": self.revision,
                    **{key: level if tick - self.latest[key] < .3 else 0.
                       for key, level in self.levels.items()}}

    def server_start(self, port=PORT):
        self.server = serve(self._connection, "127.0.0.1", port, max_size=65536, max_queue=6,
                            origins=[re.compile(r"chrome-extension://[a-p]{32}")],
                            compression=None, close_timeout=1, ping_interval=10, ping_timeout=3)
        Thread(target=self.server.serve_forever, daemon=True, name="Meet audio bridge").start()

    def _connection(self, ws):
        route = urlsplit(ws.request.path).path
        if route == "/discover/v2":
            self._discover(ws)
            return
        if route != "/capture/v1":
            ws.close(1008, "Unknown route")
            return
        bridge_started = False
        try:
            request = json.loads(ws.recv(timeout=4))
            token = request.get("token")
            meeting = request.get("meet")
            if (not isinstance(token, str) or not secrets.compare_digest(token, self.token)
                    or not isinstance(meeting, str) or not MEET_CODE.fullmatch(meeting)):
                ws.close(1008, "Pairing rejected")
                return
            with self.lock:
                if self.peer is not None:
                    ws.close(1008, "One Meet tab at a time")
                    return
                if self.expected_meeting and self.expected_meeting != meeting:
                    ws.close(1008, "This tab differs from the selected reservation")
                    return
                self.meeting, self.peer = meeting, ws
                bridge_started = True
            previous = None
            last_sequence = None
            student_tick = None
            last_frame = monotonic()
            while not self.closed.is_set():
                with self.lock:
                    state = (self.generation, self.mode in ("assessment", "transcript"))
                if state != previous:
                    ws.send(json.dumps({"generation": state[0], "capture": state[1]}))
                    previous, last_sequence, student_tick = state, None, None
                    last_frame = monotonic()
                if state[1] and monotonic() - last_frame > 1.5:
                    self.pause("Meet audio frames stopped. Reconnect the tab and resume.")
                    continue
                try:
                    message = ws.recv(timeout=.12)
                except TimeoutError:
                    continue
                if isinstance(message, str) and message == "heartbeat":
                    with self.lock:
                        self.connected = self.peer is ws and (not self.expected_meeting
                                                               or self.expected_meeting == meeting)
                        self.latest["student"] = monotonic()
                    continue
                if not isinstance(message, bytes) or len(message) != 8 + 2 * RATE * FRAME_MS // 1000:
                    ws.close(1003, "Invalid audio frame")
                    break
                generation, sequence = unpack_from("<II", message)
                if generation != state[0] or not state[1]:
                    continue
                if last_sequence is not None and sequence != last_sequence + 1:
                    self.pause("Meet audio frames were lost. Resume after checking the tab.")
                    continue
                last_sequence = sequence
                arrived = monotonic()
                last_frame = arrived
                student_tick = arrived if student_tick is None else student_tick + FRAME_MS / 1000
                if abs(arrived - student_tick) > .5:
                    self.pause("Meet audio delivery stalled. Resume after checking the tab.")
                    continue
                self.feed("student", generation, np.frombuffer(message, dtype="<i2", offset=8)
                          .astype(np.float32) / 32768, student_tick)
        except (ConnectionClosed, TimeoutError, ValueError, TypeError, AttributeError):
            pass
        finally:
            if bridge_started:
                with self.lock:
                    self.connected, self.meeting, self.peer = False, None, None
                    if self.mode in ("assessment", "transcript"):
                        self.pause("Meet tab disconnected. Reconnect it before resuming.")

    def _discover(self, ws):
        if ws.request.headers.get("Origin") != ORIGIN:
            ws.close(1008, "Use the NexGen extension")
            return
        try:
            key = json.loads(ws.recv(timeout=4)).get("token")
            if not isinstance(key, str) or not secrets.compare_digest(key, self.token):
                ws.close(1008, "Local key rejected")
                return
            with self.lock:
                seen = self.focus[0]
            while not self.closed.is_set():
                try:
                    message = json.loads(ws.recv(timeout=.25))
                except TimeoutError:
                    message = None
                if message is not None:
                    rooms = message.get("rooms")
                    if (not isinstance(rooms, list) or len(rooms) > 60
                            or any(not isinstance(room, str) or not MEET_CODE.fullmatch(room) for room in rooms)):
                        ws.close(1008, "Invalid Meet list")
                        break
                    with self.lock:
                        self.discoveries[ws] = (monotonic(), set(rooms))
                with self.lock:
                    ticket, room = self.focus
                    attached = self.discoveries.get(ws)
                if ticket != seen:
                    seen = ticket
                    if attached and room in attached[1]:
                        ws.send(json.dumps({"focus": room}))
        except (ConnectionClosed, TimeoutError, ValueError, AttributeError, TypeError):
            pass
        finally:
            with self.lock:
                self.discoveries.pop(ws, None)

    def focus_meeting(self, room):
        with self.lock:
            self.focus = (self.focus[0] + 1, room)

    def select_meeting(self, code, reset=False):
        peer = None
        with self.lock:
            self.expected_meeting = code
            if self.meeting and (reset or (code and code != self.meeting)):
                peer = self.peer
                self.latest["student"] = 0.
                self.connected = False
        if peer is not None:
            Thread(target=peer.close, args=(1008, "Changed reservation"), daemon=True).start()

    def start(self, sample_id, mode, offset_ms=0):
        if mode not in ("assessment", "transcript", "solo"):
            raise ValueError("Choose a capture mode.")
        with self.lock:
            if not self.recognizer or not self.mic:
                raise ValueError("Wait for transcription and the microphone to be ready.")
            if mode == "solo":
                if self.mode != "paused" or self.pending:
                    raise ValueError("Finish the current recording before testing.")
                self.solo_text.clear()
                self.solo_voice = False
            else:
                if not self.connected or monotonic() - self.latest["student"] > 2:
                    raise ValueError("Allow audio for the Meet tab to continue.")
                if self.expected_meeting and self.expected_meeting != self.meeting:
                    raise ValueError("Allow audio for the Meet tab for this lesson.")
            self._flush()
            self.generation += 1
            self.offset = max(offset_ms, self.last_ms if self.sample_id == sample_id else 0)
            self.last_ms = self.offset
            self.clock = monotonic()
            self.sample_id = sample_id
            self.epoch = str(uuid4())
            self.mode = mode
            self.failure, self.preview_text = "", ""

    def pause(self, failure=""):
        with self.lock:
            self._flush()
            self.generation += 1
            self.mode = "paused"
            self.levels = dict.fromkeys(self.levels, 0.)
            self.preview_text = ""
            if failure:
                self.failure = failure

    def _flush(self):
        for speaker in self.buffers:
            if self.buffers[speaker].chunks:
                self._submit(speaker, False)
            self.buffers[speaker] = Buffer()

    def _submit(self, speaker, preview):
        state = self.buffers[speaker]
        if preview and self.jobs.qsize() > 1:
            return True
        job = (speaker, self.sample_id, self.epoch, self.mode, state.started,
               np.concatenate(state.chunks), preview)
        try:
            self.jobs.put_nowait(job)
        except Full:
            self.failure = "Local transcription fell behind. Audio capture paused. Review this sample."
            self.generation += 1
            self.mode = "paused"
            self.buffers = {key: Buffer() for key in self.levels}
            return False
        self.pending += 1
        return True

    def feed(self, speaker, generation, audio, end_tick):
        with self.lock:
            if (self.mode == "paused" or (self.mode == "solo" and speaker != "teacher")
                    or generation != self.generation or self.closed.is_set()):
                return
            stamp = self.offset + max(0, round((end_tick - self.clock) * 1000))
            self.last_ms = max(self.last_ms, stamp)
            level = float(np.sqrt(np.mean(audio * audio)))
            self.levels[speaker] = min(1., level * 5)
            self.latest[speaker] = monotonic()
            voiced = level > GATE
            if self.mode == "solo" and voiced:
                self.solo_voice = True
            if voiced:
                self.speech.append((self.epoch, speaker, stamp - FRAME_MS, stamp))
            while len(self.speech) > 2000:
                self.speech.popleft()
            state = self.buffers[speaker]
            if state.chunks:
                state.chunks.append(audio)
                state.silent = 0 if voiced else state.silent + FRAME_MS
                duration = len(state.chunks) * FRAME_MS
                if (not state.preview and (speaker == "student" or self.mode == "solo")
                        and duration >= 2200 and not state.silent):
                    state.preview = True
                    self._submit(speaker, True)
                if state.silent >= 600 or duration >= 10000:
                    self._submit(speaker, False)
                    self.buffers[speaker] = Buffer()
            elif voiced:
                state.started = max(0, stamp - (len(state.signal) + 1) * FRAME_MS)
                state.chunks = [*state.signal, audio]
                state.signal.clear()
            else:
                state.signal.append(audio)

    def _decode(self):
        while not self.closed.is_set():
            try:
                speaker, sample_id, epoch, mode, start, pcm, preview = self.jobs.get(timeout=.15)
            except Empty:
                continue
            try:
                if preview:
                    with self.lock:
                        if epoch != self.epoch or self.mode == "paused":
                            continue
                if not preview and speaker == "student":
                    # Give the second channel a short window to deliver its trailing audio frame.
                    sleep(.15)
                segments = self.recognizer.decode(pcm)
                if preview:
                    with self.lock:
                        if epoch == self.epoch and self.mode != "paused":
                            self.preview_text = " ".join(text for text, _, flags in segments if not flags)
                    continue
                if mode == "solo":
                    with self.lock:
                        self.solo_text.extend(text for text, _, _ in segments)
                        self.preview_text = ""
                    continue
                for text, words, flags in segments:
                    if self.closed.is_set():
                        break
                    for word in words:
                        word["start_ms"] += start
                        word["end_ms"] += start
                    first, last = min(w["start_ms"] for w in words), max(w["end_ms"] for w in words)
                    if speaker == "student":
                        with self.lock:
                            overlap = sum(max(0, min(last, end) - max(first, begin))
                                          for other_epoch, who, begin, end in self.speech
                                          if other_epoch == epoch and who == "teacher")
                        if overlap >= 200 and overlap >= .15 * (last - first):
                            flags.append("overlap or echo to review")
                    key = self.store.add_turn({"sample_id": sample_id, "epoch": epoch, "speaker": speaker,
                                         "mode": mode, "start_ms": first, "end_ms": last, "original": text,
                                         "words": words, "flags": flags})
                    if key:
                        with self.lock:
                            self.revision += 1
                with self.lock:
                    if speaker == "student" and epoch == self.epoch:
                        self.preview_text = ""
            except Exception as error:
                self.pause("Transcription stopped (" + type(error).__name__ + "). Check the model and review this sample.")
            finally:
                with self.lock:
                    self.pending -= 1
                self.jobs.task_done()

    @staticmethod
    def enumerate_mics():
        import pyaudiowpatch as pa
        with pa.PyAudio() as devices:
            wasapi = devices.get_host_api_info_by_type(pa.paWASAPI)
            choices = [devices.get_device_info_by_index(index) for index in range(devices.get_device_count())]
            return wasapi["defaultInputDevice"], [
                device for device in choices if device["hostApi"] == wasapi["index"]
                and device["maxInputChannels"] and not device["isLoopbackDevice"]]

    @classmethod
    def default_endpoints(cls, preferred):
        import pyaudiowpatch as pa
        default, choices = cls.enumerate_mics()
        ordered = microphone_order(default, choices, preferred)
        with pa.PyAudio() as devices:
            try:
                speaker = devices.get_default_wasapi_device(d_out=True)["name"]
            except (ValueError, OSError):
                speaker = None
        return (ordered[0]["name"] if ordered else None), speaker

    def microphone_worker(self, preferred=None):
        self.stop_mic()
        stop = Event()
        self.mic_stop = stop
        self.mic_task = Thread(target=self._microphone, args=(stop, preferred), daemon=True, name="WASAPI microphone")
        self.mic_task.start()

    def stop_mic(self):
        if getattr(self, "mic_stop", None):
            self.mic_stop.set()
        with self.lock:
            self.mic = None
            if self.mode != "paused":
                self.pause("Microphone changed. Resume when both channels are ready.")

    def _microphone(self, stop, preferred):
        import pyaudiowpatch as pa
        try:
            default, choices = self.enumerate_mics()
            with pa.PyAudio() as card:
                try:
                    speaker = card.get_default_wasapi_device(d_out=True)["name"]
                except (OSError, ValueError):
                    speaker = None
                with self.lock:
                    if not stop.is_set():
                        self.speaker = speaker
                for device in microphone_order(default, choices, preferred):
                    rate = int(device["defaultSampleRate"])
                    channels = min(2, int(device["maxInputChannels"]))
                    try:
                        stream = card.open(rate=rate, channels=channels, input=True, format=pa.paInt16,
                                           input_device_index=device["index"], frames_per_buffer=rate // 10, start=False)
                        break
                    except (OSError, ValueError):
                        continue
                else:
                    raise OSError("No usable Windows microphone. Check Windows microphone access.")
                try:
                    with self.lock:
                        if stop.is_set():
                            return
                        self.mic, self.mic_error = device["name"], ""
                    resampler = None
                    previous = None
                    remaining = np.empty(0, dtype=np.float32)
                    tick = None
                    streaming = False
                    while not stop.is_set() and not self.closed.is_set():
                        with self.lock:
                            generation = self.generation
                            active = self.mode != "paused"
                        if not active:
                            if streaming:
                                stream.stop_stream()
                                streaming = False
                            resampler, previous, tick = None, None, None
                            remaining = np.empty(0, dtype=np.float32)
                            stop.wait(.04)
                            continue
                        if not streaming:
                            stream.start_stream()
                            streaming = True
                        data = stream.read(rate // 10, exception_on_overflow=True)
                        arrived = monotonic()
                        with self.lock:
                            if stop.is_set() or generation != self.generation:
                                # Drop a frame if its acquisition crossed the recording boundary.
                                resampler, previous, tick = None, None, None
                                remaining = np.empty(0, dtype=np.float32)
                                del data
                                continue
                        if generation != previous:
                            resampler = soxr.ResampleStream(rate, RATE, 1, dtype="float32")
                            remaining = np.empty(0, dtype=np.float32)
                            tick = arrived - FRAME_MS / 1000
                            previous = generation
                        pcm = np.frombuffer(data, dtype="<i2").reshape(-1, channels).mean(axis=1, dtype=np.float32)
                        converted = resampler.resample_chunk(pcm / 32768)
                        remaining = np.concatenate((remaining, converted))
                        while len(remaining) >= RATE // 10:
                            tick += FRAME_MS / 1000
                            self.feed("teacher", generation, remaining[: RATE // 10], tick)
                            remaining = remaining[RATE // 10:]
                finally:
                    stream.close()
        except (OSError, ValueError, StopIteration) as error:
            with self.lock:
                if not stop.is_set():
                    self.mic_error = str(error)
                    if self.mode != "paused":
                        self.pause("Teacher microphone disconnected. Reconnect it in Settings.")
        finally:
            with self.lock:
                if self.mic_stop is stop:
                    self.mic = None

    def close(self):
        self.pause()
        self.closed.set()
        while True:
            try:
                self.jobs.get_nowait()
                self.jobs.task_done()
            except Empty:
                break
        self.stop_mic()
        if self.server:
            self.server.shutdown()
