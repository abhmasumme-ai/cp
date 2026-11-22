"""
main.py (desktop) — thin proxy to relay server + clicker control.
"""
import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import websockets
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response

from clicker_manager import (
    start as cm_start,
    stop as cm_stop,
    reset as cm_reset,
    stop_all as cm_stop_all,
    status as cm_status,
)

log = logging.getLogger("proxy")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

BASE_DIR = Path(__file__).resolve().parent

# ── config.json from %APPDATA%\CricketPanel\ ────────────
_APPDATA = Path(os.environ.get("APPDATA") or Path.home())
_CFG = _APPDATA / "CricketPanel" / "config.json"


def _read_cfg():
    try:
        return json.loads(_CFG.read_text())
    except Exception:
        return {}


CFG = _read_cfg()
RELAY_HTTP = (CFG.get("relay_url") or "").replace("wss://", "https://").replace("ws://", "http://").rstrip("/")
RELAY_WS = (CFG.get("relay_url") or "").rstrip("/")
USER_KEY = CFG.get("user_key") or ""
LOCAL_PORT = int(CFG.get("local_port", 5000))

CONN_QUEUE_SIZE = 500
RECONNECT_MAX_S = 30
WS_PING_INTERVAL = 20
WS_PING_TIMEOUT = 20


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("local=%s relay=%s key_set=%s",
             LOCAL_PORT, RELAY_WS or "(none)", bool(USER_KEY))
    yield
    cm_stop_all()
    await _http.aclose()


app = FastAPI(lifespan=lifespan)
_http = httpx.AsyncClient(timeout=15)


# ═══════════════════════════════════════════════════════════
#  REST proxies (relay)
# ═══════════════════════════════════════════════════════════
def _hdr():
    return {"Authorization": f"Bearer {USER_KEY}"}


@app.get("/api/matches")
async def api_matches():
    if not RELAY_HTTP or not USER_KEY:
        return JSONResponse({"status": "ok", "data": {"matches": []}}, status_code=200)
    try:
        r = await _http.get(f"{RELAY_HTTP}/api/matches", headers=_hdr())
        return JSONResponse(r.json(), status_code=r.status_code)
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=200)


@app.get("/api/match/{key}")
async def api_match(key: str):
    if not RELAY_HTTP or not USER_KEY:
        return JSONResponse({"status": "error", "message": "not configured"}, status_code=503)
    try:
        r = await _http.get(f"{RELAY_HTTP}/api/match/{key}", headers=_hdr())
        return JSONResponse(r.json(), status_code=r.status_code)
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=503)


@app.post("/api/click")
async def api_click(req: Request):
    try:
        body = await req.json()
    except Exception:
        body = {}
    if not (RELAY_HTTP and USER_KEY):
        return JSONResponse({"status": "error", "message": "not configured"}, status_code=503)
    try:
        r = await _http.post(f"{RELAY_HTTP}/api/click", headers=_hdr(), json=body)
        return JSONResponse(r.json(), status_code=r.status_code)
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=502)


# ═══════════════════════════════════════════════════════════
#  Clicker control
# ═══════════════════════════════════════════════════════════
@app.post("/api/clicker/start")
async def api_clicker_start(req: Request):
    try:
        body = await req.json()
    except Exception:
        body = {}
    kind      = (body.get("type") or "").strip().lower()
    mode      = (body.get("mode") or "solo").strip().lower()
    match_key = str(body.get("match_key") or "").strip()
    site_url  = (body.get("site_url") or "").strip()
    match_url = (body.get("match_url") or "").strip()

    if not kind:
        raise HTTPException(400, "type required")
    if not match_key:
        raise HTTPException(400, "match_key required — activate a match first")
    if not site_url:
        raise HTTPException(400, "site_url required")
    if mode not in ("solo", "hybrid"):
        mode = "solo"

    try:
        info = cm_start(kind, match_key, site_url, match_url, mode,
                        local_port=LOCAL_PORT, user_key=USER_KEY)
        return {"status": "ok", **info}
    except Exception as e:
        raise HTTPException(500, f"start failed: {e!r}")


@app.post("/api/clicker/stop")
async def api_clicker_stop(req: Request):
    try:
        body = await req.json()
    except Exception:
        body = {}
    kind = (body.get("type") or "").strip().lower()
    do_reset = bool(body.get("reset"))

    if not kind:
        cm_stop_all()
        return {"status": "ok", "stopped": "all"}

    if do_reset:
        ok = cm_reset(kind, local_port=LOCAL_PORT, user_key=USER_KEY)
        return {"status": "ok", "reset": ok}
    return {"status": "ok", "stopped": cm_stop(kind)}


@app.get("/api/clicker/status")
async def api_clicker_status():
    return {"status": "ok", "clickers": cm_status()}


# ═══════════════════════════════════════════════════════════
#  WS proxy (panel + clicker) with auto-reconnect
# ═══════════════════════════════════════════════════════════
@app.websocket("/ws")
async def ws_proxy(ws: WebSocket):
    await ws.accept()
    role = ws.query_params.get("role", "panel")
    if not RELAY_WS or not USER_KEY:
        await ws.close(code=1011, reason="relay not configured")
        return

    url = f"{RELAY_WS}?token={USER_KEY}&role={role}"
    to_relay: asyncio.Queue[str] = asyncio.Queue(maxsize=CONN_QUEUE_SIZE)
    client_gone = asyncio.Event()
    last_watch = {"msg": None}

    async def client_reader():
        try:
            async for msg in ws.iter_text():
                if msg.startswith("watch:") or '"type":"watch"' in msg.replace(" ", ""):
                    last_watch["msg"] = msg
                await to_relay.put(msg)
        except Exception:
            pass
        finally:
            client_gone.set()

    async def relay_loop():
        backoff = 1
        while not client_gone.is_set():
            try:
                async with websockets.connect(
                    url, ping_interval=WS_PING_INTERVAL,
                    ping_timeout=WS_PING_TIMEOUT, max_size=None,
                ) as up:
                    backoff = 1
                    log.info("relay connected (role=%s)", role)

                    if to_relay.empty() and last_watch["msg"]:
                        try:
                            await up.send(last_watch["msg"])
                        except Exception:
                            pass

                    async def sender():
                        while True:
                            msg = await to_relay.get()
                            try:
                                await up.send(msg)
                            except Exception:
                                try:
                                    to_relay.put_nowait(msg)
                                except asyncio.QueueFull:
                                    pass
                                raise

                    async def receiver():
                        async for msg in up:
                            txt = msg if isinstance(msg, str) else msg.decode()
                            try:
                                await ws.send_text(txt)
                            except Exception:
                                return

                    tasks = [
                        asyncio.create_task(sender()),
                        asyncio.create_task(receiver()),
                        asyncio.create_task(client_gone.wait()),
                    ]
                    done, pending = await asyncio.wait(
                        tasks, return_when=asyncio.FIRST_COMPLETED
                    )
                    for p in pending:
                        p.cancel()
                    await asyncio.gather(*pending, return_exceptions=True)
                    if client_gone.is_set():
                        return
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("relay conn error: %r", e)

            if client_gone.is_set():
                return
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, RECONNECT_MAX_S)

    try:
        await asyncio.gather(client_reader(), relay_loop())
    except Exception:
        pass
    finally:
        try:
            await ws.close()
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════
#  Static files
# ═══════════════════════════════════════════════════════════
def _file(name: str):
    f = BASE_DIR / name
    if not f.exists():
        return JSONResponse({"detail": f"{name} not found"}, status_code=404)
    return FileResponse(f)


@app.get("/")
async def index():
    return _file("index.html")


@app.get("/index.css")
async def css():
    return _file("index.css")


@app.get("/script.js")
async def js():
    return _file("script.js")


@app.get("/favicon.ico")
async def favicon():
    return Response(status_code=204)


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "relay_configured": bool(RELAY_HTTP and USER_KEY),
        "relay_url": RELAY_HTTP or None,
        "user_key_set": bool(USER_KEY),
        "local_port": LOCAL_PORT,
        "clickers": cm_status(),
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=LOCAL_PORT,
                access_log=False, log_level="warning")
