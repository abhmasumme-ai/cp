"""
clicker_manager.py — spawn / stop / reset clicker subprocesses.
"""
import logging
import os
import subprocess
import sys
from pathlib import Path

log = logging.getLogger("clickers")
BASE = Path(__file__).resolve().parent
CLICKERS_DIR = BASE / "clickers"

CLICKERS = {
    "delivery": "clicker_dualmode.py",
    "wicket":   "clicker_wicket.py",
    "batsman":  "clicker_batsman.py",
    "custom":   "clicker_custom.py",
}

PROCS: dict[str, subprocess.Popen] = {}
LAST:  dict[str, tuple] = {}


def _kill_tree(pid: int):
    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                           capture_output=True, timeout=5)
        except Exception as e:
            log.warning("taskkill %s: %r", pid, e)
    else:
        import signal
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except OSError:
            pass


def _python() -> str:
    if getattr(sys, "frozen", False):
        return os.environ.get("CLICKER_PYTHON", "python")
    return sys.executable


def start(kind: str, match_key: str, site_url: str = "",
          match_url: str = "", mode: str = "solo",
          *, local_port: int = 5000, user_key: str = "") -> dict:
    if kind not in CLICKERS:
        raise ValueError(f"unknown clicker kind: {kind!r}")
    script = CLICKERS_DIR / CLICKERS[kind]
    if not script.exists():
        raise FileNotFoundError(f"clicker script missing: {script}")

    stop(kind)

    cmd = [_python(), "-u", str(script), str(match_key)]
    if mode == "hybrid":
        cmd.append("multi")

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"]  = "1"
    env["CLICKER_NO_PROMPT"] = "1"
    env["CLICKER_SITE_URL"]  = site_url or ""
    env["CLICKER_MATCH_URL"] = match_url or ""
    env["CLICKER_MATCH_ID"]  = str(match_key)
    env["CLICKER_WS_URL"]    = f"ws://127.0.0.1:{local_port}/ws?role=clicker"
    env["CLICKER_WS_TOKEN"]  = user_key or ""
    if mode == "hybrid":
        env["CLICKER_MULTI"] = "1"
    else:
        env.pop("CLICKER_MULTI", None)

    show = os.environ.get("CLICKER_SHOW_OUTPUT") == "1"
    stdout = None if show else subprocess.DEVNULL
    stderr = None if show else subprocess.DEVNULL

    flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    p = subprocess.Popen(
        cmd, env=env, cwd=str(BASE),
        creationflags=flags,
        stdout=stdout, stderr=stderr,
    )
    PROCS[kind] = p
    LAST[kind]  = (match_key, site_url, match_url, mode)
    log.info("started %s pid=%s mode=%s key=%s", kind, p.pid, mode, match_key)
    return {"pid": p.pid, "cmd": " ".join(cmd), "mode": mode}


def stop(kind: str) -> bool:
    p = PROCS.pop(kind, None)
    if p is None:
        return False
    if p.poll() is None:
        _kill_tree(p.pid)
        try:
            p.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass
    log.info("stopped %s (pid=%s rc=%s)", kind, p.pid, p.returncode)
    return True


def reset(kind: str, *, local_port: int = 5000, user_key: str = "") -> bool:
    params = LAST.get(kind)
    stop(kind)
    if not params:
        return False
    match_key, site_url, match_url, mode = params
    try:
        start(kind, match_key, site_url, match_url, mode,
              local_port=local_port, user_key=user_key)
        return True
    except Exception as e:
        log.warning("reset %s failed: %r", kind, e)
        return False


def stop_all():
    for k in list(PROCS):
        stop(k)


def status() -> dict:
    out = {}
    for kind in CLICKERS:
        p = PROCS.get(kind)
        if p is None:
            out[kind] = {"running": False}
        else:
            rc = p.poll()
            out[kind] = {
                "running": rc is None,
                "pid": p.pid,
                "returncode": rc,
                "last_params": LAST.get(kind),
            }
    return out
