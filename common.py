"""
_common.py — shared bootstrap for every clicker.
"""
import json
import os
import sys


def safe_input(prompt: str, default: str = "") -> str:
    """input() that NEVER hangs in subprocess mode."""
    if os.environ.get("CLICKER_NO_PROMPT") == "1":
        return default
    try:
        if not sys.stdin or not sys.stdin.isatty():
            return default
    except Exception:
        return default
    try:
        val = input(prompt).strip()
        return val or default
    except (EOFError, KeyboardInterrupt, OSError):
        return default


async def send_watch(ws, match_id: str):
    """Send proper JSON watch message to relay."""
    try:
        await ws.send(json.dumps({"type": "watch", "key": str(match_id)}))
        print(f"[WS] watch sent → {match_id}", flush=True)
    except Exception as e:
        print(f"[WS] watch send failed: {e!r}", flush=True)
