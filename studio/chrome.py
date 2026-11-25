"""Register the per-user Chrome host that hands the extension an ephemeral local key."""
import hashlib
import json
import os
from pathlib import Path
import subprocess

EXTENSION_ID = "hheadkjenhbpkhnabjfnkffbpanjbmgj"
ORIGIN = f"chrome-extension://{EXTENSION_ID}"
HOST = "com.nexgen.live_studio"
PAIRING_FILE = "chrome-session.json"


def publish_key(data, token):
    write_json(data / PAIRING_FILE, {"token": token})


def remove_key(data, token):
    path = data / PAIRING_FILE
    try:
        if json.loads(path.read_text(encoding="utf-8"))["token"] == token:
            path.unlink()
    except (OSError, ValueError, KeyError):
        pass


def write_json(path, payload):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload), encoding="utf-8")
    temporary.replace(path)


def register(data, root, browsers=(r"Google\Chrome", r"Microsoft\Edge", "Chromium")):
    import winreg
    source = root / "native" / "FindStudio.cs"
    digest = hashlib.sha256(source.read_bytes()).hexdigest()[:14]
    binary = data / f"ChromeBridge-{digest}.exe"
    if not binary.is_file():
        frameworks = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Microsoft.NET"
        compiler = next((path for family in ("Framework64", "Framework")
                         if (path := frameworks / family / "v4.0.30319" / "csc.exe").is_file()), None)
        if compiler is None:
            raise OSError("Windows .NET Framework is required for the Chrome connector.")
        result = subprocess.run([str(compiler), "/nologo", "/target:winexe", f"/out:{binary}", str(source)],
                                capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW,
                                timeout=20)
        if result.returncode:
            raise OSError("Windows could not install the Chrome connector.")
    manifest = data / f"{HOST}.json"
    write_json(manifest, {"name": HOST, "description": "NexGen Live Studio on this Windows account",
                          "path": str(binary), "type": "stdio", "allowed_origins": [ORIGIN + "/"]})
    for browser in browsers:
        key_path = rf"Software\{browser}\NativeMessagingHosts\{HOST}"
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            winreg.SetValueEx(key, "", 0, winreg.REG_SZ, str(manifest))
