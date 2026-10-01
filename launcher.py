"""Start quietly, wait for a verified health endpoint, then open the local UI."""

import json
import subprocess
import time
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
URL = "http://127.0.0.1:18765"


def healthy():
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(URL + "/api/health", timeout=1) as response:
            return json.load(response).get("app") == "douyin-local"
    except Exception:
        return False


if not healthy():
    (ROOT / "data").mkdir(exist_ok=True)
    with (ROOT / "data" / "service.log").open("a", encoding="utf-8") as log:
        subprocess.Popen(
            [str(ROOT / ".venv" / "Scripts" / "python.exe"), "-X", "utf8", str(ROOT / "app.py")],
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    for _ in range(60):
        if healthy():
            break
        time.sleep(0.5)
if healthy():
    webbrowser.open(URL)
else:
    import ctypes

    ctypes.windll.user32.MessageBoxW(0, "本地服务启动失败，请查看 data/service.log。", "抖音本地下载器", 0)
