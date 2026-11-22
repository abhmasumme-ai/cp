"""
desktop.py — starts main.py in a background thread and opens a native window.
"""
import json
import os
import threading
import time
from pathlib import Path

import httpx
import uvicorn
import webview


def _read_cfg():
    p = Path(os.environ.get("APPDATA") or Path.home()) / "CricketPanel" / "config.json"
    try:
        return json.loads(p.read_text())
    except Exception:
        return {}


def wait_until_up(port: int, timeout: float = 20.0) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            r = httpx.get(f"http://127.0.0.1:{port}/health", timeout=1.0)
            if r.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(0.3)
    return False


def main():
    cfg = _read_cfg()
    port = int(cfg.get("local_port", 5000))

    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    threading.Thread(
        target=uvicorn.run,
        args=("main:app",),
        kwargs={"host": "127.0.0.1", "port": port,
                "access_log": False, "log_level": "warning"},
        daemon=True,
    ).start()

    if not wait_until_up(port):
        raise SystemExit("main.py failed to start")

    webview.create_window(
        "Cricket Control Panel",
        f"http://127.0.0.1:{port}/",
        width=1400, height=900, min_size=(1100, 700),
    )
    webview.start()


if __name__ == "__main__":
    main()
