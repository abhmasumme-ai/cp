"""
relay_server.py — Cricket Control Panel relay (VPS-side)
v2 — bugfixes:
  [CRIT] xball + batsman_out passthrough in ball_event
  [CRIT] hash-only auth works (sha256 index)
  [HIGH] match_key validation (no path injection)
  [HIGH] session memory leak fixed
  [HIGH] token scrubbing in all logs
  [MED]  accept-then-close, stale panel re-check, api_match age check
"""
import asyncio
import gzip
import hashlib
import json
import logging
import os
import re
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import websockets
from fastapi import FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

try:
    import uvloop
    uvloop.install()
except ImportError:
    pass

try:
    import orjson as _orjson
except ImportError:
    _orjson = None


def _dumps(obj) -> str:
    if _orjson is not None:
        try:
            return _orjson.dumps(obj).decode("utf-8")
        except Exception:
            pass
    return json.dumps(obj, separators=(",", ":"), default=str)


# ═══════════════════════════════════════════════════════════
#  CONFIG
# ═══════════════════════════════════════════════════════════
log = logging.getLogger("relay")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

PUSH_TOKEN = os.environ.get("ES_PUSH_TOKEN", "").strip()
REST_TOKEN = os.environ.get("ES_REST_TOKEN", "").strip()
PUSH_URL   = os.environ.get("ES_PUSH_URL", "ws://webhook.entitysport.com:8087/connect")

_REST_RAW  = os.environ.get("ES_REST_BASE", "https://restapi.entitysport.com/v2").strip()
REST_BASE  = _REST_RAW.split("?")[0].rstrip("/")
REST_BASE  = re.sub(r"/matches/?$", "", REST_BASE).rstrip("/")

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8443"))

USERS_FILE = Path(os.environ.get("USERS_FILE", "users.json"))
ALLOWED_ORIGINS = [o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "*").split(",") if o.strip()]

STATUS_MAP = {1: "not_started", 2: "completed", 3: "started", 4: "cancelled"}

MAX_CACHE = 500
CACHE_FRESH_MS = 60_000
REST_REFRESH_S = 25
CONN_QUEUE_SIZE = 500
MAX_DISPATCH_TASKS = 1000


# ── token scrubbing ────────────────────────────────────
_TOKEN_RE = re.compile(r"(token=)[^&\s'\"<>]+", re.I)


def _scrub(obj) -> str:
    """Strip ES tokens from any exception/log string."""
    s = str(obj)
    s = _TOKEN_RE.sub(r"\1<redacted>", s)
    for v in (PUSH_TOKEN, REST_TOKEN):
        if v and len(v) > 8:
            s = s.replace(v, "<redacted>")
    return s


# ── match key validation ──────────────────────────────
_KEY_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")


def _valid_match_key(k: str) -> bool:
    return bool(k and _KEY_RE.match(k))


# ═══════════════════════════════════════════════════════════
#  USER DB  — hash-only + legacy plaintext + key_hash index
# ═══════════════════════════════════════════════════════════
USERS: dict[str, dict] = {}
_HASH_INDEX: dict[str, str] = {}     # sha256hex -> USERS key


def _rebuild_hash_index():
    global _HASH_INDEX
    idx: dict[str, str] = {}
    for uid, u in USERS.items():
        kh = u.get("key_hash")
        if not kh:
            continue
        algo, _, digest = kh.partition(":")
        if algo != "sha256" or not digest:
            continue
        idx[digest.strip().lower()] = uid
    _HASH_INDEX = idx


def _load_users():
    global USERS
    try:
        if USERS_FILE.exists():
            USERS = json.loads(USERS_FILE.read_text(encoding="utf-8"))
            log.info("loaded %d users from %s", len(USERS), USERS_FILE)
        else:
            USERS = {}
            log.warning("users file not found: %s (all auth will fail)", USERS_FILE)
    except Exception as e:
        USERS = {}
        log.error("users load error: %s", _scrub(e))
    _rebuild_hash_index()


def check_user_key(key: str) -> bool:
    """
    Supports 3 formats in users.json:

      1) {"usr_xxx": {"expires": 123}}                          ← plaintext
      2) {"usr_xxx": {"key_hash": "sha256:<hex>", "expires": 123}}  ← legacy hash
      3) {"cust_001": {"key_hash": "sha256:<hex>", "expires": 123}} ← hash-only (best)

    Format 3 is the only one where the file never contains the actual key.
    """
    if not key or not key.startswith("usr_"):
        return False
    now = time.time()

    # 1) hash-index lookup (hash-only entries)
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    uid = _HASH_INDEX.get(digest)
    if uid:
        u = USERS.get(uid) or {}
        exp = u.get("expires")
        if exp and exp < now:
            return False
        return True

    # 2) legacy plaintext
    u = USERS.get(key)
    if not u:
        return False
    exp = u.get("expires")
    if exp and exp < now:
        return False
    kh = u.get("key_hash")
    if kh:
        algo, _, h = kh.partition(":")
        if algo != "sha256":
            return False
        if hashlib.sha256(key.encode("utf-8")).hexdigest() != h.strip().lower():
            return False
    return True


# ═══════════════════════════════════════════════════════════
#  STATE
# ═══════════════════════════════════════════════════════════
client = httpx.AsyncClient(timeout=15)

UP = {"connected": False, "msgs": 0, "last": 0.0}

LATEST_OBJ: "OrderedDict[str, dict]" = OrderedDict()
FIX: "OrderedDict[str, dict]" = OrderedDict()
_fix_pending: set[str] = set()
_dispatch_tasks: set[asyncio.Task] = set()

_rest = {
    "ts": 0.0, "rows": [], "last_error": "",
    "last_ok_ts": 0.0, "refreshing": False,
}
_rest_lock = asyncio.Lock()


def _lr_put(store: "OrderedDict[str, dict]", key: str, val: dict):
    store[key] = val
    store.move_to_end(key)
    while len(store) > MAX_CACHE:
        store.popitem(last=False)


def _lr_get(store: "OrderedDict[str, dict]", key: str):
    v = store.get(key)
    if v is not None:
        store.move_to_end(key)
    return v


def _schedule(coro_fn, *args):
    if len(_dispatch_tasks) >= MAX_DISPATCH_TASKS:
        log.debug("dispatch backlog full, dropping")
        return
    t = asyncio.create_task(coro_fn(*args))
    _dispatch_tasks.add(t)
    t.add_done_callback(_dispatch_tasks.discard)


# ═══════════════════════════════════════════════════════════
#  HELPERS
# ═══════════════════════════════════════════════════════════
def _i(v, d=0):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return d


def _decode(raw):
    if isinstance(raw, bytes):
        if raw[:2] == b"\x1f\x8b":
            raw = gzip.decompress(raw)
        raw = raw.decode("utf-8", errors="replace")
    return json.loads(raw)


_W_RE = re.compile(r"^\d*w$", re.I)


def fixture_row(mi):
    ta, tb = mi.get("teama") or {}, mi.get("teamb") or {}
    return {
        "key": str(mi.get("match_id")),
        "title": mi.get("title") or mi.get("short_title") or "",
        "format": mi.get("format_str") or "",
        "status": STATUS_MAP.get(_i(mi.get("status")), "not_started"),
        "start_at": mi.get("timestamp_start"),
        "teams": {"a": {"name": ta.get("name")}, "b": {"name": tb.get("name")}},
    }


def slim_comm(live):
    out = []
    for e in (live.get("commentaries") or []):
        if not isinstance(e, dict):
            continue
        if e.get("event") not in ("ball", "wicket"):
            continue
        eid = str(e.get("event_id") or "")
        if not eid:
            continue
        out.append({
            "id": eid, "ov": _i(e.get("over")), "b": _i(e.get("ball")),
            "x": _i(e.get("xball")), "run": _i(e.get("run")), "bat": _i(e.get("bat_run")),
            "wd": _i(e.get("wide_run")), "nb": _i(e.get("noball_run")),
            "by": _i(e.get("bye_run")), "lb": _i(e.get("legbye_run")),
            "four": bool(e.get("four")), "six": bool(e.get("six")),
            "w": e.get("event") == "wicket" or bool(_W_RE.match(str(e.get("score", "")).strip())),
            "dis": str(e.get("dismissal") or ""), "fh": bool(e.get("freehit")),
        })
    return out[-30:]


def slim_match(msg, now):
    r = msg.get("response") or {}
    mi = r.get("match_info") or {}
    live = r.get("live") if isinstance(r.get("live"), dict) else {}
    ta, tb = mi.get("teama") or {}, mi.get("teamb") or {}
    li = live.get("live_inning") if isinstance(live.get("live_inning"), dict) else {}
    ls = live.get("live_score") if isinstance(live.get("live_score"), dict) else {}
    inn_no = _i(live.get("live_inning_number"))

    bat_id = str(li.get("batting_team_id", ""))
    bat = "a" if bat_id and bat_id == str(ta.get("team_id")) else (
        "b" if bat_id and bat_id == str(tb.get("team_id")) else None)

    mx = _i(li.get("max_over"))
    if not mx:
        for inn in ((r.get("scorecard") or {}).get("innings") or []):
            if _i(inn.get("number")) == inn_no or not mx:
                mx = _i(inn.get("max_over")) or mx

    score = None
    if ls and ls.get("runs") is not None:
        score = {
            "runs": _i(ls.get("runs")), "wk": _i(ls.get("wickets")),
            "ov": str(ls.get("overs", 0)), "target": _i(ls.get("target")),
            "rrr": ls.get("required_runrate"),
        }

    lw = li.get("last_wicket") if isinstance(li.get("last_wicket"), dict) else None
    status = STATUS_MAP.get(_i(mi.get("status")), "started")
    result = mi.get("result") or (mi.get("status_note") if status == "completed" else "") or ""

    def team(t):
        return {"id": t.get("team_id"), "name": t.get("name"),
                "short": t.get("short_name"), "sc": t.get("scores_full") or ""}

    ts = msg.get("timestamp")
    out = {
        "t": "match",
        "id": str(r.get("match_id") or mi.get("match_id") or live.get("mid") or ""),
        "ts": ts if isinstance(ts, (int, float)) else None,
        "_server_ts": now * 1000,
        "status": status,
        "game": str(mi.get("game_state_str") or live.get("game_state_str") or ""),
        "fmt": mi.get("format_str") or "",
        "title": mi.get("title") or "",
        "result": result,
        "toss": (mi.get("toss") or {}).get("text") or "",
        "day": mi.get("day"), "session": mi.get("session"),
        "start": mi.get("timestamp_start"),
        "a": team(ta), "b": team(tb),
        "inn": inn_no, "bat": bat, "max": mx,
        "score": score,
        "lw": ({"ov": str(lw.get("overs_at_dismissal", "")),
                "dis": str(lw.get("dismissal") or ""),
                "sc": _i(lw.get("score_at_dismissal"))} if lw else None),
        "comm": slim_comm(live),
    }
    if isinstance(ts, (int, float)):
        lag = round(now * 1000 - ts)
        if 0 <= lag < 60000:
            out["_lag_ms"] = lag
    return out


def _fresh(obj):
    d = dict(obj)
    d["_server_ts"] = time.time() * 1000
    d.pop("_lag_ms", None)
    return d


# ═══════════════════════════════════════════════════════════
#  REST POLLING  (scrubbed errors)
# ═══════════════════════════════════════════════════════════
async def _fetch_status(status: int, per_page: int):
    try:
        r = await client.get(
            f"{REST_BASE}/matches/",
            params={"status": status, "per_page": per_page, "paged": 1, "token": REST_TOKEN},
        )
        if not r.is_success:
            return {"status": status, "error": f"HTTP {r.status_code}", "rows": []}
        resp = r.json().get("response")
        if not isinstance(resp, dict):
            return {"status": status, "error": "unexpected response type", "rows": []}
        items = resp.get("items") or []
        rows = [fixture_row(i) for i in items if isinstance(i, dict) and i.get("match_id")]
        return {"status": status, "error": "", "rows": rows}
    except Exception as e:
        return {"status": status, "error": f"exception: {_scrub(e)}", "rows": []}


async def _do_rest_fetch():
    if not REST_TOKEN:
        return
    async with _rest_lock:
        if _rest["refreshing"]:
            return
        _rest["refreshing"] = True
        try:
            parts = await asyncio.gather(
                _fetch_status(3, 50),
                _fetch_status(1, 50),
                _fetch_status(2, 50),
                _fetch_status(4, 20),
            )
            errors = [_scrub(p["error"]) for p in parts if p["error"]]
            merged: dict[str, dict] = {}
            for p in parts:
                for row in p["rows"]:
                    if row["key"] not in merged:
                        merged[row["key"]] = row
            rows = list(merged.values())
            now = time.time()
            _rest["rows"] = rows
            _rest["ts"] = now
            if not errors:
                _rest["last_error"] = ""
                _rest["last_ok_ts"] = now
            else:
                _rest["last_error"] = "; ".join(errors)
                log.warning("REST partial failure: %s", _rest["last_error"])
            log.info("REST refresh: %d fixtures", len(rows))
        finally:
            _rest["refreshing"] = False


async def _fixture_refresh_loop():
    if not REST_TOKEN:
        log.warning("ES_REST_TOKEN missing — fixture limited to push only")
        return
    try:
        await _do_rest_fetch()
    except Exception as e:
        log.warning("initial REST fetch failed: %s", _scrub(e))
    while True:
        await asyncio.sleep(REST_REFRESH_S)
        try:
            await _do_rest_fetch()
        except Exception as e:
            log.warning("periodic REST fetch failed: %s", _scrub(e))


# ═══════════════════════════════════════════════════════════
#  FIX (push fallback metadata)
# ═══════════════════════════════════════════════════════════
def _create_stub_fix(mid: str):
    if mid in FIX:
        return
    _lr_put(FIX, mid, {
        "key": mid, "title": f"Match {mid}", "format": "",
        "status": "started", "start_at": None,
        "teams": {"a": {}, "b": {}},
    })


async def _fetch_fix_metadata(mid: str):
    try:
        if not REST_TOKEN:
            _create_stub_fix(mid)
            return
        r = await client.get(f"{REST_BASE}/matches/{mid}/info", params={"token": REST_TOKEN})
        if not r.is_success:
            _create_stub_fix(mid)
            return
        resp = r.json().get("response") or {}
        mi = resp.get("match_info") if isinstance(resp.get("match_info"), dict) else resp
        if isinstance(mi, dict) and mi.get("match_id"):
            _lr_put(FIX, mid, fixture_row(mi))
            log.info("FIX populated via REST: %s → %s", mid, FIX[mid].get("title"))
        else:
            _create_stub_fix(mid)
    except Exception as e:
        log.warning("FIX fetch %s failed: %s", mid, _scrub(e))
        _create_stub_fix(mid)
    finally:
        _fix_pending.discard(mid)


def _ensure_fix(mid: str):
    if mid in FIX:
        return
    if REST_TOKEN:
        if mid not in _fix_pending:
            _fix_pending.add(mid)
            _schedule(_fetch_fix_metadata, mid)
        return
    _create_stub_fix(mid)


# ═══════════════════════════════════════════════════════════
#  PER-USER SESSIONS  (with cleanup)
# ═══════════════════════════════════════════════════════════
class UserSession:
    def __init__(self, user_key: str):
        self.user_key = user_key
        self.panels: dict[WebSocket, str] = {}
        self.clicker: WebSocket | None = None
        self.clicker_watch: str = ""

    def panel_targets(self, mid: str) -> list[WebSocket]:
        return [ws for ws, w in self.panels.items() if w == mid]


_sessions: dict[str, UserSession] = {}


def _session(user_key: str) -> UserSession:
    s = _sessions.get(user_key)
    if s is None:
        s = UserSession(user_key)
        _sessions[user_key] = s
    return s


def _maybe_cleanup_session(token: str):
    sess = _sessions.get(token)
    if sess is not None and not sess.panels and sess.clicker is None:
        _sessions.pop(token, None)


# ═══════════════════════════════════════════════════════════
#  DISPATCH  (per-panel stale re-check)
# ═══════════════════════════════════════════════════════════
async def _dispatch_event(mid: str, out: dict):
    txt = _dumps(out)
    for sess in list(_sessions.values()):
        if sess.clicker is not None and sess.clicker_watch == mid:
            try:
                await sess.clicker.send_text(txt)
            except Exception:
                pass
        targets = sess.panel_targets(mid)
        dead = []
        for p in targets:
            if sess.panels.get(p) != mid:
                continue
            try:
                await p.send_text(txt)
            except Exception:
                dead.append(p)
        for p in dead:
            sess.panels.pop(p, None)


async def _dispatch_match(mid: str, out: dict):
    txt = _dumps(out)
    for sess in list(_sessions.values()):
        targets = sess.panel_targets(mid)
        dead = []
        for p in targets:
            if sess.panels.get(p) != mid:
                continue
            try:
                await p.send_text(txt)
            except Exception:
                dead.append(p)
        for p in dead:
            sess.panels.pop(p, None)


# ═══════════════════════════════════════════════════════════
#  UPSTREAM  — xball + batsman_out passthrough
# ═══════════════════════════════════════════════════════════
def handle_upstream(raw):
    try:
        msg = _decode(raw)
    except Exception:
        return
    if not isinstance(msg, dict):
        return
    r = msg.get("response")
    if not isinstance(r, dict):
        return

    mi0 = r.get("match_info") if isinstance(r.get("match_info"), dict) else {}
    live0 = r.get("live") if isinstance(r.get("live"), dict) else {}
    mid = str(r.get("match_id") or mi0.get("match_id") or live0.get("mid") or "")
    if not mid or not _valid_match_key(mid):
        return

    if isinstance(mi0, dict) and mi0.get("match_id"):
        _lr_put(FIX, mid, fixture_row(mi0))
        _fix_pending.discard(mid)
    else:
        _ensure_fix(mid)

    # ── ball_event ─────────────────────────────────────
    if r.get("ball_event"):
        d = r.get("data") if isinstance(r.get("data"), dict) else {}
        out = {
            "t": "ev", "id": mid,
            "ev": str(r.get("ball_event", "")),
            "code": str(r.get("code", "")),
            "over": d.get("over"), "ball": d.get("ball"),
            "xball": d.get("xball"),                                 # ★ FIX #1
            "inn": _i(d.get("inn_num")), "iid": d.get("iid"),
            "striker": d.get("striker_name") or d.get("striker"),
            "bowler": d.get("bowler_name") or d.get("bowler"),
            "batsman_out": (d.get("batsman_out") or                   # ★ FIX #2
                            d.get("batsman_out_name") or
                            d.get("dismissed_batsman") or
                            d.get("out_batsman") or
                            d.get("dismissed_player") or
                            r.get("batsman_out")),
            "_server_ts": time.time() * 1000,
        }
        _schedule(_dispatch_event, mid, out)
        return

    # ── full match ─────────────────────────────────────
    if "match_info" in r or "live" in r:
        out = slim_match(msg, time.time())
        if not out["id"] or out["id"] != mid:
            return
        _lr_put(LATEST_OBJ, mid, out)
        _schedule(_dispatch_match, mid, out)


async def upstream_loop():
    if not PUSH_TOKEN:
        log.warning("ES_PUSH_TOKEN missing — push disabled")
        return
    url = f"{PUSH_URL}?token={PUSH_TOKEN}"
    delay = 3
    while True:
        t0 = time.time()
        msgs0 = UP["msgs"]
        try:
            async with websockets.connect(url, ping_interval=None, max_size=None) as ws:
                UP["connected"] = True
                log.info("Entity push connected")
                async for raw in ws:
                    UP["msgs"] += 1
                    UP["last"] = time.time()
                    try:
                        handle_upstream(raw)
                    except Exception as e:
                        log.warning("push message skip: %s", _scrub(e))
                    await asyncio.sleep(0)
                log.warning("Entity push server close (code=%s)", ws.close_code)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("Entity push disconnected: %s", _scrub(e))
        UP["connected"] = False
        if time.time() - t0 > 30 or UP["msgs"] > msgs0:
            delay = 3
        await asyncio.sleep(delay)
        delay = min(delay * 2, 60)


# ═══════════════════════════════════════════════════════════
#  APP + LIFESPAN
# ═══════════════════════════════════════════════════════════
@asynccontextmanager
async def lifespan(app: FastAPI):
    _load_users()
    if ALLOWED_ORIGINS == ["*"]:
        log.warning("CORS allow_origins=['*'] — set ALLOWED_ORIGINS in production")
    tasks = []
    if PUSH_TOKEN:
        tasks.append(asyncio.create_task(upstream_loop()))
    if REST_TOKEN:
        tasks.append(asyncio.create_task(_fixture_refresh_loop()))
    log.info("relay up | REST=%s | push=%s | users=%d | hashed=%d",
             bool(REST_TOKEN), bool(PUSH_TOKEN), len(USERS), len(_HASH_INDEX))
    yield
    for t in tasks:
        t.cancel()
        try:
            await t
        except BaseException:
            pass
    for t in list(_dispatch_tasks):
        t.cancel()
    if _dispatch_tasks:
        await asyncio.gather(*_dispatch_tasks, return_exceptions=True)
    await client.aclose()


app = FastAPI(lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _auth_header(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "missing bearer token")
    key = authorization[7:].strip()
    if not check_user_key(key):
        raise HTTPException(401, "invalid or expired user key")
    return key


# ═══════════════════════════════════════════════════════════
#  WS /relay  — accept-then-close + match_key validation
# ═══════════════════════════════════════════════════════════
@app.websocket("/relay")
async def relay_ws(ws: WebSocket):
    token = ws.query_params.get("token", "")
    role = ws.query_params.get("role", "panel").lower()

    await ws.accept()                                    # ★ accept first

    if not check_user_key(token):
        await ws.close(code=4401, reason="invalid user key")
        return

    sess = _session(token)

    # ── CLICKER ─────────────────────────────────────────
    if role == "clicker":
        old = sess.clicker
        sess.clicker = ws
        if old is not None and old is not ws:
            try:
                await old.close(code=4000, reason="replaced by new clicker")
            except Exception:
                pass
        log.info("clicker connected: user=%s...", token[:10])

        try:
            while True:
                raw = await ws.receive_text()
                if raw == "ping":
                    await ws.send_text("pong")
                    continue
                try:
                    m = json.loads(raw)
                except Exception:
                    continue
                t = m.get("type")
                if t == "watch":
                    new_key = (m.get("key") or "").strip()
                    if new_key and not _valid_match_key(new_key):
                        log.warning("clicker watch: invalid key %r", new_key)
                        continue
                    sess.clicker_watch = new_key
                    log.info("clicker watch: user=%s... key=%s",
                             token[:10], new_key or "(none)")
                    if new_key:
                        obj = _lr_get(LATEST_OBJ, new_key)
                        if obj:
                            try:
                                await ws.send_text(_dumps(_fresh(obj)))
                            except Exception:
                                pass
                elif t == "ping":
                    await ws.send_text("pong")
        except WebSocketDisconnect:
            pass
        except Exception as e:
            log.warning("clicker err: %s", _scrub(e))
        finally:
            if sess.clicker is ws:
                sess.clicker = None
            _maybe_cleanup_session(token)
            log.info("clicker disconnected: user=%s...", token[:10])
        return

    # ── PANEL ───────────────────────────────────────────
    sess.panels[ws] = ""
    log.info("panel connected: user=%s... (%d panels)", token[:10], len(sess.panels))
    try:
        while True:
            raw = await ws.receive_text()
            if raw == "ping":
                await ws.send_text("pong")
                continue
            if raw.startswith("watch:"):
                new_key = raw[6:].strip()
                if new_key and not _valid_match_key(new_key):
                    log.warning("panel watch: invalid key %r", new_key)
                    continue
                sess.panels[ws] = new_key
                log.info("watch: user=%s... key=%s", token[:10], new_key or "(none)")

                sess.clicker_watch = new_key
                if sess.clicker:
                    try:
                        await sess.clicker.send_text(
                            _dumps({"type": "watch", "key": new_key})
                        )
                    except Exception:
                        pass

                if new_key:
                    obj = _lr_get(LATEST_OBJ, new_key)
                    if obj:
                        await ws.send_text(_dumps(_fresh(obj)))
                    else:
                        _schedule(_send_baseline, ws, sess, new_key)
                continue
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        sess.panels.pop(ws, None)
        _maybe_cleanup_session(token)
        log.info("panel disconnected: user=%s... (%d panels left)",
                 token[:10], len(sess.panels))


async def _send_baseline(ws: WebSocket, sess: UserSession, mid: str):
    if not REST_TOKEN:
        return
    if not _valid_match_key(mid):
        return
    if sess.panels.get(ws) != mid:
        return                                       # panel already moved on

    obj = _lr_get(LATEST_OBJ, mid)
    if obj is None:
        try:
            r = await client.get(f"{REST_BASE}/matches/{mid}/info",
                                 params={"token": REST_TOKEN})
            if not r.is_success:
                return
            resp = r.json().get("response") or {}
            mi = resp.get("match_info") if isinstance(resp.get("match_info"), dict) else resp
            if not isinstance(mi, dict) or not mi.get("match_id"):
                return
            obj = slim_match({"response": {"match_id": mid, "match_info": mi}}, time.time())
            obj["id"] = str(mid)
            _lr_put(LATEST_OBJ, mid, obj)
        except Exception as e:
            log.warning("baseline fetch failed %s: %s", mid, _scrub(e))
            return

    if sess.panels.get(ws) != mid:
        return
    try:
        await ws.send_text(_dumps(_fresh(obj)))
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════
#  REST API
# ═══════════════════════════════════════════════════════════
@app.get("/api/matches")
async def api_matches(authorization: str | None = Header(None)):
    _auth_header(authorization)
    merged: dict[str, dict] = {}

    for row in _rest["rows"]:
        merged[row["key"]] = dict(row)

    for k, row in FIX.items():
        is_stub = str(row.get("title", "")).startswith("Match ") and not row.get("format")
        if is_stub and k in merged:
            continue
        if k in merged:
            base = merged[k]
            for kk, vv in row.items():
                if vv not in (None, "", {}):
                    base[kk] = vv
        else:
            merged[k] = dict(row)

    order = {"started": 0, "not_started": 1, "completed": 2, "cancelled": 3}
    matches = sorted(merged.values(), key=lambda r: order.get(r.get("status"), 9))
    return {"status": "ok", "data": {"matches": matches}}


@app.get("/api/match/{match_key}")
async def api_match(match_key: str, authorization: str | None = Header(None)):
    _auth_header(authorization)
    if not _valid_match_key(match_key):
        raise HTTPException(400, "invalid match key")

    # 1. fresh cache
    obj = _lr_get(LATEST_OBJ, match_key)
    if obj:
        age_ms = time.time() * 1000 - obj.get("_server_ts", 0)
        if age_ms < CACHE_FRESH_MS:
            return {"status": "ok", "data": _fresh(obj)}

    # 2. REST
    if REST_TOKEN:
        try:
            r = await client.get(f"{REST_BASE}/matches/{match_key}/info",
                                 params={"token": REST_TOKEN})
            if r.is_success:
                try:
                    resp = r.json().get("response") or {}
                except Exception:
                    resp = {}
                if not (isinstance(resp, dict) and resp.get("error")):
                    mi = resp.get("match_info") if isinstance(resp.get("match_info"), dict) else resp
                    if isinstance(mi, dict) and mi.get("match_id"):
                        obj = slim_match(
                            {"response": {"match_id": match_key, "match_info": mi}},
                            time.time(),
                        )
                        obj["id"] = str(match_key)
                        _lr_put(LATEST_OBJ, match_key, obj)
                        return {"status": "ok", "data": _fresh(obj)}
        except Exception as e:
            log.warning("baseline %s err: %s", match_key, _scrub(e))

    # 3. stale cache fallback
    if obj:
        return {"status": "ok", "data": _fresh(obj), "stale": True}

    # 4. FIX fallback
    fix = _lr_get(FIX, match_key)
    if fix:
        return {
            "status": "ok",
            "data": {
                "t": "match", "id": str(match_key),
                "_server_ts": time.time() * 1000,
                "status": fix.get("status", "started"),
                "fmt": fix.get("format", ""),
                "title": fix.get("title", ""),
                "a": {"name": fix.get("teams", {}).get("a", {}).get("name", "")},
                "b": {"name": fix.get("teams", {}).get("b", {}).get("name", "")},
                "score": None, "comm": [],
            },
        }

    return JSONResponse(
        {"status": "error", "message": "match info not available"},
        status_code=404,
    )


@app.post("/api/click")
async def api_click(req: Request, authorization: str | None = Header(None)):
    user_key = _auth_header(authorization)
    try:
        body = await req.json()
    except Exception:
        body = {}

    sess = _sessions.get(user_key)
    delivered = False
    if sess and sess.clicker:
        try:
            await sess.clicker.send_text(_dumps({"type": "manual_click", "payload": body}))
            delivered = True
        except Exception:
            pass
    return {"status": "ok", "delivered": delivered}


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "push_connected": UP["connected"],
        "push_msgs": UP["msgs"],
        "push_last_age_s": round(time.time() - UP["last"], 1) if UP["last"] else None,
        "matches_cached": len(LATEST_OBJ),
        "fixtures_cached": len(FIX),
        "rest_token_set": bool(REST_TOKEN),
        "rest_cache_age_s": round(time.time() - _rest["ts"], 1) if _rest["ts"] else None,
        "rest_rows": len(_rest["rows"]),
        "sessions": len(_sessions),
        "users_online": len([s for s in _sessions.values() if s.panels or s.clicker]),
        "panels_total": sum(len(s.panels) for s in _sessions.values()),
        "clickers_total": sum(1 for s in _sessions.values() if s.clicker),
        "dispatch_backlog": len(_dispatch_tasks),
        "users_loaded": len(USERS),
        "hashed_users": len(_HASH_INDEX),
    }


# ═══════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "relay_server:app",
        host=HOST,
        port=PORT,
        workers=1,
        access_log=False,
        log_level="warning",
        ws_per_message_deflate=False,
    )
