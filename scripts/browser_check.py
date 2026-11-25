"""Verify the real Chrome connector against an isolated synthetic Meet page."""
from argparse import ArgumentParser
import base64
from contextlib import contextmanager
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
from time import monotonic, sleep
from urllib.request import urlopen
import winreg

import numpy as np
from playwright.sync_api import sync_playwright
from websockets.sync.client import connect
import json

from studio.analysis import tokens
from studio.asr import Recognizer
from studio.capture import Capture
from studio.chrome import HOST, EXTENSION_ID, publish_key, register
from studio.store import Store

ROOT = Path(__file__).resolve().parents[1]
MEET_URL = "https://meet.google.com/abc-defg-hij"
SYNTHETIC_TAB = """
<title>Local Meet audio fixture</title><button>Play a test tone</button>
<script>
const context = new AudioContext();
window.pitch = 440;
document.querySelector('button').onclick = async () => {
  const tone = context.createOscillator();
  const volume = context.createGain();
  volume.gain.value = 0.15;
  tone.frequency.value = window.pitch;
  tone.connect(volume).connect(context.destination);
  await context.resume();
  tone.start();
  window.generator = tone;
};
window.playSpeech = async (encoded) => {
  window.generator.stop();
  const wave = await context.decodeAudioData(Uint8Array.from(atob(encoded), ch => ch.charCodeAt(0)).buffer);
  const voice = context.createBufferSource();
  voice.buffer = wave;
  voice.connect(context.destination);
  voice.start();
};
</script>
"""


class QuietRecognizer:
    def decode(self, audio):
        return []


def wait_for(predicate, name, timeout=8):
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        result = predicate()
        if result:
            return result
        sleep(.05)
    raise AssertionError("Timed out: " + name)


@contextmanager
def test_host(data):
    browser = "Chromium"
    key_path = rf"Software\{browser}\NativeMessagingHosts\{HOST}"
    previous = None
    existed = True
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            try:
                previous = winreg.QueryValueEx(key, "")
            except FileNotFoundError:
                pass
    except FileNotFoundError:
        existed = False
    try:
        register(data, ROOT, (browser,))
        yield
    finally:
        if existed:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE) as key:
                if previous:
                    winreg.SetValueEx(key, "", 0, previous[1], previous[0])
                else:
                    winreg.DeleteValue(key, "")
        else:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key_path)


def browser_test(executable, directory, capture, check, voice, cache):
    extension = directory / "extension"
    shutil.copytree(ROOT / "extension", extension)
    port = capture.server.socket.getsockname()[1]
    for path in extension.glob("*.js"):
        path.write_text(path.read_text(encoding="utf-8").replace("127.0.0.1:18763", f"127.0.0.1:{port}"), encoding="utf-8")
    profile = directory / "chrome"
    profile.mkdir()
    command = [str(executable), "--headless=new", "--no-first-run", "--disable-background-networking",
               "--no-default-browser-check", "--remote-debugging-port=0", f"--user-data-dir={profile}",
               f"--disable-extensions-except={extension}", f"--load-extension={extension}",
               "--enable-unsafe-extension-debugging", "about:blank"]
    process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        with sync_playwright() as playwright:
            port_file = profile / "DevToolsActivePort"
            wait_for(port_file.exists, "browser startup")
            endpoint = "http://127.0.0.1:" + port_file.read_text().splitlines()[0]
            browser = playwright.chromium.connect_over_cdp(endpoint)
            context = browser.contexts[0]
            context.route("https://meet.google.com/**", lambda route: route.fulfill(content_type="text/html", body=SYNTHETIC_TAB))
            context.route("https://outside.test/**", lambda route: route.fulfill(body="Outside the meeting"))

            def tone(url, pitch):
                tab = context.new_page()
                tab.goto(url)
                tab.evaluate("pitch => window.pitch = pitch", pitch)
                tab.get_by_role("button", name="Play a test tone").click()
                return tab

            chosen = tone(MEET_URL, 440)
            tone("https://meet.google.com/xxx-yyyy-zzz", 997)
            wait_for(lambda: capture.snapshot()["rooms"] == ["abc-defg-hij", "xxx-yyyy-zzz"], "automatic Meet discovery", 15)
            assert not capture.snapshot()["tab"]
            capture.focus_meeting("abc-defg-hij")
            wait_for(lambda: chosen.evaluate("document.visibilityState") == "visible", "desktop requested tab focus")
            chosen.bring_to_front()
            cdp = browser.new_browser_cdp_session()

            def allow_audio():
                tabs = cdp.send("Target.getTargets", {"filter": [{"type": "tab"}, {"exclude": True}]})["targetInfos"]
                target = next(item["targetId"] for item in tabs if item["url"] == MEET_URL)
                cdp.send("Extensions.triggerAction", {"id": EXTENSION_ID, "targetId": target})
                def find_popup():
                    with urlopen(endpoint + "/json/list", timeout=3) as response:
                        return next((item["webSocketDebuggerUrl"] for item in json.load(response)
                                     if item["url"] == f"chrome-extension://{EXTENSION_ID}/connect.html"), None)
                popup_url = wait_for(find_popup, "Chrome action popup")
                with connect(popup_url) as popup:
                    popup.send(json.dumps({"id": 2, "method": "Runtime.evaluate",
                        "params": {"expression": "allow()", "awaitPromise": True, "userGesture": True}}))
                    popup.recv(timeout=5)
                    try:
                        wait_for(lambda: capture.snapshot()["tab"], "single-click Chrome authorization")
                    except AssertionError as error:
                        popup.send(json.dumps({"id": 1, "method": "Runtime.evaluate",
                            "params": {"expression": "(async () => ({status: document.querySelector('#status').textContent, "
                                "script: typeof allow, active: (await chrome.tabs.query({active:true,currentWindow:true}))[0]?.url}))()",
                                "awaitPromise": True, "returnByValue": True}}))
                        popup_snapshot = json.loads(popup.recv(timeout=4))
                        raise AssertionError(f"{error}. Popup: {popup_snapshot}") from error

            allow_audio()
            session, sample = check
            assert capture.snapshot()["student"] == 0
            capture.start(sample, "assessment")
            wait_for(lambda: capture.snapshot()["student"] > .05, "live Meet frames")
            with capture.lock:
                block = np.concatenate(capture.buffers["student"].chunks)
                spectrum = abs(np.fft.rfft(block))
                hz = float(np.fft.rfftfreq(len(block), 1 / 16000)[spectrum.argmax()])
                assert abs(hz - 440) < 12, hz
            capture.pause()
            wait_for(lambda: capture.snapshot()["pending"] == 0, "transcript queue drains while paused")
            if voice:
                capture.recognizer = Recognizer("base.en", "cpu", str(cache), download=False)
                capture.start(sample, "transcript")
                chosen.evaluate("encoded => window.playSpeech(encoded)", base64.b64encode(voice.read_bytes()).decode())
                wait_for(lambda: capture.snapshot()["revision"] > 0, "saved Whisper transcript", 13)
                turns = session.load()["samples"][0]["turns"]
                assert any(item["speaker"] == "student" and tokens(item["text"]) == ["yes", "i", "can"]
                           for item in turns), turns
            else:
                capture.start(sample, "transcript")
                wait_for(lambda: capture.snapshot()["student"] > .05, "resume with no new permission")
            chosen.goto("https://outside.test/after-meet")
            wait_for(lambda: not capture.snapshot()["tab"] and capture.snapshot()["mode"] == "paused", "disconnect on navigation")
            wait_for(lambda: capture.snapshot()["rooms"] == ["xxx-yyyy-zzz"], "abandoned Meet removed")
            print(f"Meet discovery and tab focus worked before authorization. One Chrome action enabled {hz:.1f} Hz tab audio.")
            print("Excluded 997 Hz from the other tab. Pause, resume and navigation away passed.")
            if voice:
                print("Synthetic speech reached SQLite through Chrome tab capture, AudioWorklet and Whisper.")
    finally:
        process.terminate()
        process.wait(timeout=10)


def main():
    parser = ArgumentParser()
    parser.add_argument("chrome", type=Path, help="Chromium executable with extension development support")
    parser.add_argument("--voice", type=Path, help="Windows SAPI beginner.wav generated by synthetic.ps1")
    parser.add_argument("--cache", type=Path, default=Path("private/models"))
    args = parser.parse_args()
    with TemporaryDirectory(ignore_cleanup_errors=True) as folder:
        directory = Path(folder)
        store = Store(directory / "sessions.sqlite")
        capture = Capture(store)
        capture.recognizer = QuietRecognizer()
        capture.mic = "Synthetic test source"
        capture.select_meeting("abc-defg-hij")
        capture.server_start(0)
        lesson = store.create({"student_id": "fixture", "name": "Fictional", "level": "B1"}, True)
        sample = store.sample(lesson, "Unlabelled")

        from functools import partial
        from types import SimpleNamespace
        test_lesson = SimpleNamespace(load=partial(store.load, lesson))

        try:
            with test_host(directory):
                publish_key(directory, capture.token)
                browser_test(args.chrome, directory, capture, (test_lesson, sample), args.voice, args.cache)
        finally:
            capture.close()


if __name__ == "__main__":
    main()
