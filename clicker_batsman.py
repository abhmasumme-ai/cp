"""
clicker_batsman.py - v25 (wicket-only, PREDICTIVE ROSTER + dual-mode)
Patched for subprocess (no-prompt, JSON watch).

Usage:
  python clicker_batsman.py <match_id>          -> STRICT mode
  python clicker_batsman.py <match_id> multi    -> MULTI mode
"""
import asyncio
import json
import os
import queue
import random
import re
import sys
import threading
import time
import urllib.parse

import websockets
from playwright.sync_api import sync_playwright
import numpy as np
import cv2
import pytesseract

try:
    from _common import safe_input, send_watch
except ImportError:
    from clickers._common import safe_input, send_watch

# ============================================================
#  Config
# ============================================================
_WS_URL_BASE = os.environ.get("CLICKER_WS_URL", "ws://127.0.0.1:5000/ws")
_WS_TOKEN = os.environ.get("CLICKER_WS_TOKEN", "").strip()
WS_URL = (_WS_URL_BASE + ("&" if "?" in _WS_URL_BASE else "?")
          + "token=" + urllib.parse.quote(_WS_TOKEN)) if _WS_TOKEN else _WS_URL_BASE

RECONNECT_SEC = 3
PING_SEC = 30
HEADLESS = os.environ.get("CLICKER_HEADLESS") == "1"
DEBUG = os.environ.get("CLICKER_DEBUG") == "1"
STRICT_MATCH = os.environ.get("CLICKER_STRICT_MATCH", "1") == "1"

TESSERACT_CMD = os.environ.get("TESSERACT_CMD") or (
    r"C:\Program Files\Tesseract-OCR\tesseract.exe"
    if os.path.exists(r"C:\Program Files\Tesseract-OCR\tesseract.exe") else None)

ANCHOR_TRUST_SEC = int(os.environ.get("CLICKER_ANCHOR_TRUST_SEC", "120"))
EVENT_TIMEOUT_SEC = 0.05
EVENT_Q_MAXSIZE = 500
DEDUP_WINDOW_S = 600
POST_CLICK_REFRESH_SEC = float(os.environ.get("CLICKER_POST_CLICK_REFRESH_SEC", "2.0"))
USE_ROSTER = os.environ.get("CLICKER_USE_ROSTER", "1") == "1"

try:
    RETRY_DELAYS_MS = [int(x.strip()) for x in
                       os.environ.get("CLICKER_RETRY_DELAYS", "300,500,800,1200").split(",")
                       if x.strip()]
except Exception:
    RETRY_DELAYS_MS = [300, 500, 800, 1200]
if not RETRY_DELAYS_MS:
    RETRY_DELAYS_MS = [300, 500, 800, 1200]

COMPARE_THRESHOLD = 0.70
CACHE_VERIFY_THRESHOLD = 0.82
OCR_PSM = os.environ.get("CLICKER_OCR_PSM", "7").strip()
if OCR_PSM not in ("6", "7", "11", "12", "13"):
    OCR_PSM = "7"
OCR_CFG_HEADER = f'--oem 1 --psm {OCR_PSM}'

VIEW_W, VIEW_H = 1080, 797
WIN_W, WIN_H = VIEW_W + 100, VIEW_H + 120
ANCHOR_LEFT, ANCHOR_TOP, ANCHOR_W, ANCHOR_H = 70, 400, 120, 34

NAME_STOPWORDS = {
    'over', 'under', 'ovr', 'undr', 'delivery', 'deliveries', 'del',
    'runs', 'run', 'wicket', 'out', 'live', 'match', 'next', 'ball',
    'the', 'and', 'vs'
}

ANCHOR_TEMPLATE = {}
MAPPED_TARGETS = []
CALIBRATED_ANCHOR_POS = None
last_anchor_pos = None
_anchor_trust_until = 0.0

_last_clicked = {"name": None, "at": 0.0}
_last_dedup_key = None
_roster_refresh_at = 0.0

_pending_reset = threading.Event()
_stop_event = threading.Event()

WS_STATE = {"match_filter": "", "up": False, "msg_count": 0, "last_ts": 0.0}
event_q = queue.Queue(maxsize=EVENT_Q_MAXSIZE)

INTERMEDIATE_PATTERNS = [
    r'^ball\b.*\b(chalu|start|chalo|shuru|shuro)',
    r'^ball in ?air', r'^boundary ?check',
    r'^(third ?umpire|3rd ?umpire)',
    r'^(confirm(ing)?|checking)', r'^(review|drs)',
    r'^(batsman|bowler|new ?batter|misfield|dropped|overthrow)',
]


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def dbg(msg):
    if DEBUG:
        print(f"[{time.strftime('%H:%M:%S')}] [DBG] {msg}", flush=True)


def is_intermediate(ev):
    return any(re.search(p, ev.lower().strip()) for p in INTERMEDIATE_PATTERNS)


def is_ball_chalu(ev):
    return bool(re.search(r'^ball\b.*\b(chalu|start|chalo|shuru|shuro)',
                          ev.lower().strip()))


def handle_reset(from_id, to_id):
    _pending_reset.set()
    log(f"[RESET] {from_id} -> {to_id}")


def check_pending_reset():
    global _last_clicked, _last_dedup_key
    if _pending_reset.is_set():
        _pending_reset.clear()
        _last_clicked = {"name": None, "at": 0.0}
        _last_dedup_key = None
        _panel_roster.clear()
        log("[RESET] dedup + roster cleared")


def _clean(s):
    return re.sub(r'[^a-z\s]', ' ', (s or "").lower()).strip()


def _levenshtein(a, b):
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    if len(a) > len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a):
        cur = [i + 1]
        for j, cb in enumerate(b):
            cur.append(min(prev[j + 1] + 1, cur[j] + 1, prev[j] + (ca != cb)))
        prev = cur
    return prev[-1]


def extract_name_from_text(text):
    if not text:
        return None
    t = text
    for kw in ('over', 'under', 'ovr', 'undr', 'deliveries', 'delivery', 'del'):
        t = re.sub(rf'\b{kw}\b', ' ', t, flags=re.I)
    t = re.sub(r'\b(runs?|wicket|out|live|match|next|ball)\b', ' ', t, flags=re.I)
    t = re.sub(r'\d+(\.\d+)?', ' ', t)
    t = re.sub(r'[^A-Za-z\s]', ' ', t)
    words = [w for w in t.split() if len(w) >= 2 and w.lower() not in NAME_STOPWORDS]
    return ' '.join(words[:4]).strip() if words else None


def extract_run_from_text(text):
    if not text:
        return 0.0
    m = re.search(r'\(?(\d+\.\d+)\)?', text)
    if m:
        try:
            return float(m.group(1))
        except Exception:
            pass
    return 0.0


def is_name_match(ws_name, ocr_name):
    if not ws_name or not ocr_name:
        return False
    a, b = _clean(ws_name), _clean(ocr_name)
    if not a or not b:
        return False
    a_words, b_words = a.split(), b.split()
    a_set, b_set = set(a_words), set(b_words)
    if a_set == b_set:
        return True
    if STRICT_MATCH:
        return False
    if a_set and b_set and (a_set.issubset(b_set) or b_set.issubset(a_set)):
        return True
    for wa in a_words[:3]:
        if len(wa) < 3:
            continue
        for wb in b_words:
            if len(wb) < 3:
                continue
            if wa == wb:
                return True
            if len(wa) >= 5 and len(wb) >= 5:
                if wa.startswith(wb) or wb.startswith(wa):
                    return True
                if abs(len(wa) - len(wb)) <= 1 and _levenshtein(wa, wb) <= 1:
                    return True
    return False


class PanelRoster:
    def __init__(self):
        self.rows = []
        self.out_names = set()
        self.last_refresh = 0.0
        self.lock = threading.Lock()

    def clear(self):
        with self.lock:
            self.rows = []
            self.out_names = set()
            self.last_refresh = 0.0

    def refresh(self, page, under_boxes, ax, ay):
        results = ocr_all_under_boxes(page, under_boxes, ax, ay)
        new_rows = []
        for i, (box, text) in enumerate(results):
            new_rows.append({
                "i": i,
                "box": box,
                "name": extract_name_from_text(text),
                "run": extract_run_from_text(text),
                "text": text,
            })
        with self.lock:
            self.rows = new_rows
            self.last_refresh = time.time()

    def find(self, striker):
        key = _clean(striker)
        if not key:
            return None
        with self.lock:
            if key in self.out_names:
                dbg(f"[ROSTER] '{striker}' already marked OUT - skip fast")
                return None
            for row in self.rows:
                if row["name"] and is_name_match(striker, row["name"]):
                    return row
        return None

    def mark_out(self, striker):
        key = _clean(striker)
        with self.lock:
            self.out_names.add(key)
            for row in self.rows:
                if row["name"] and is_name_match(striker, row["name"]):
                    dbg(f"[ROSTER] mark_out '{striker}' (was row {row['i']}: '{row['name']}')")
                    row["name"] = None
                    row["box"] = None
                    break

    def active_names(self):
        with self.lock:
            return [r["name"] for r in self.rows if r["name"]]

    def snapshot(self):
        with self.lock:
            return [{"i": r["i"], "name": r["name"], "run": r["run"]}
                    for r in self.rows]


_panel_roster = PanelRoster()


def _do_roster_refresh(page, reason="periodic"):
    if not USE_ROSTER:
        return False
    ax, ay = find_anchor_fast(page)
    if ax is None:
        dbg(f"[ROSTER] refresh({reason}): no anchor")
        return False
    under_boxes = [t for t in MAPPED_TARGETS if t["side"] == "under"]
    if not under_boxes:
        return False
    _panel_roster.refresh(page, under_boxes, ax, ay)
    names = _panel_roster.active_names()
    dbg(f"[ROSTER] refresh({reason}) -> {len(names)} active: {names}")
    return True


SHOW_TARGETS_JS = """
() => {
    let box = document.getElementById('__pw_target_guide');
    if (!box) {
        box = document.createElement('div');
        box.id = '__pw_target_guide';
        box.style.cssText = 'position:fixed;inset:0;pointer-events:none;z-index:999999;background:rgba(0,0,0,0.2);';
        document.body.appendChild(box);
    }
    box.innerHTML = '';
    let rowCount = window.__row_count || 3;
    let baseY = 400, anchorW = 120, anchorH = 34, leftX = 70, rightX = 425, boxW = 240, boxH = 30, rowGap = 3.34;

    const blueBox = document.createElement('div');
    blueBox.style.cssText = `position:absolute;left:${leftX}px;top:${baseY}px;width:${anchorW}px;height:${anchorH}px;border:2px solid #00aaff;background:rgba(0,170,255,0.4);box-sizing:border-box;display:flex;align-items:center;justify-content:center;color:white;font-weight:bold;font-size:10px;`;
    blueBox.textContent = "Anchor";
    box.appendChild(blueBox);

    let firstRowY = baseY + anchorH;
    let finalTargets = [];

    for (let i = 0; i < rowCount; i++) {
        let y = firstRowY + i * (boxH + rowGap);
        const leftBox = document.createElement('div');
        leftBox.style.cssText = `position:absolute;left:${leftX}px;top:${y}px;width:${boxW}px;height:${boxH}px;border:2px solid red;background:transparent;box-sizing:border-box;`;
        box.appendChild(leftBox);
        const rightBox = document.createElement('div');
        rightBox.style.cssText = `position:absolute;left:${rightX}px;top:${y}px;width:${boxW}px;height:${boxH}px;border:2px solid red;background:transparent;box-sizing:border-box;`;
        box.appendChild(rightBox);
        finalTargets.push({side: 'over', x: leftX, y: y, w: boxW, h: boxH});
        finalTargets.push({side: 'under', x: rightX, y: y, w: boxW, h: boxH});
    }

    let btnTop = firstRowY + rowCount * (boxH + rowGap) + 15;
    const btnContainer = document.createElement('div');
    btnContainer.style.cssText = `position:absolute;left:${leftX}px;top:${btnTop}px;pointer-events:auto;display:flex;gap:10px;`;

    const doneBtn = document.createElement('button');
    doneBtn.textContent = 'DONE';
    doneBtn.style.cssText = 'padding:10px 30px;font-size:16px;font-weight:bold;background:#00c853;color:white;border:none;border-radius:6px;cursor:pointer;';
    doneBtn.onclick = () => { window.__pw_final_targets = finalTargets; window.__pw_action = 'done'; };

    const plusBtn = document.createElement('button');
    plusBtn.textContent = '+ 2 Rows';
    plusBtn.style.cssText = 'padding:10px 20px;font-size:16px;font-weight:bold;background:#2196f3;color:white;border:none;border-radius:6px;cursor:pointer;';
    plusBtn.onclick = () => { window.__row_count = Math.min(10, rowCount + 2); window.__pw_action = 'update'; };

    const minusBtn = document.createElement('button');
    minusBtn.textContent = '- 2 Rows';
    minusBtn.style.cssText = 'padding:10px 20px;font-size:16px;font-weight:bold;background:#ff9800;color:white;border:none;border-radius:6px;cursor:pointer;';
    minusBtn.onclick = () => { window.__row_count = Math.max(2, rowCount - 2); window.__pw_action = 'update'; };

    btnContainer.appendChild(doneBtn);
    btnContainer.appendChild(plusBtn);
    btnContainer.appendChild(minusBtn);
    box.appendChild(btnContainer);
}
"""


def setup_from_done(page, final_targets):
    global CALIBRATED_ANCHOR_POS, last_anchor_pos, _anchor_trust_until
    log("Saving anchor + target box positions...")
    page.evaluate("() => { let b = document.getElementById('__pw_target_guide'); if(b) b.remove(); }")
    page.wait_for_timeout(1000)
    raw = page.screenshot(type="png", scale="css")
    src_img = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_GRAYSCALE)
    anchor_img = src_img[ANCHOR_TOP:ANCHOR_TOP + ANCHOR_H, ANCHOR_LEFT:ANCHOR_LEFT + ANCHOR_W]
    ANCHOR_TEMPLATE.update({"img": anchor_img, "w": ANCHOR_W, "h": ANCHOR_H})
    res = cv2.matchTemplate(src_img, anchor_img, cv2.TM_CCOEFF_NORMED)
    _, max_val, _, max_loc = cv2.minMaxLoc(res)
    if max_val >= COMPARE_THRESHOLD:
        CALIBRATED_ANCHOR_POS = max_loc
        last_anchor_pos = max_loc
        _anchor_trust_until = time.time() + ANCHOR_TRUST_SEC
    log(f"   Anchor saved (conf={max_val:.2f})")
    MAPPED_TARGETS.clear()
    for t in final_targets:
        MAPPED_TARGETS.append({
            "side": t.get('side'),
            "x": int(t['x']), "y": int(t['y']),
            "w": int(t['w']), "h": int(t['h']),
        })
    log(f"   Mapped {len(MAPPED_TARGETS)} boxes total")


def find_anchor_fast(page):
    global CALIBRATED_ANCHOR_POS, last_anchor_pos, _anchor_trust_until
    now = time.time()
    cached = CALIBRATED_ANCHOR_POS or last_anchor_pos
    if cached and now < _anchor_trust_until:
        return cached
    if cached:
        ax, ay = cached
        try:
            raw = page.screenshot(
                clip={"x": ax, "y": ay, "width": ANCHOR_W, "height": ANCHOR_H},
                type="png", scale="css")
            crop = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_GRAYSCALE)
            if crop is not None and crop.shape == ANCHOR_TEMPLATE["img"].shape:
                res = cv2.matchTemplate(crop, ANCHOR_TEMPLATE["img"], cv2.TM_CCOEFF_NORMED)
                _, max_val, _, _ = cv2.minMaxLoc(res)
                if max_val >= CACHE_VERIFY_THRESHOLD:
                    _anchor_trust_until = now + ANCHOR_TRUST_SEC
                    return ax, ay
        except Exception:
            pass
    try:
        raw = page.screenshot(type="png", scale="css")
        src_img = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_GRAYSCALE)
        res = cv2.matchTemplate(src_img, ANCHOR_TEMPLATE["img"], cv2.TM_CCOEFF_NORMED)
        _, max_val, _, max_loc = cv2.minMaxLoc(res)
        if max_val >= COMPARE_THRESHOLD:
            CALIBRATED_ANCHOR_POS = max_loc
            last_anchor_pos = max_loc
            _anchor_trust_until = now + ANCHOR_TRUST_SEC
            return max_loc[0], max_loc[1]
    except Exception:
        pass
    return None, None


def _preprocess_for_ocr(crop):
    up = cv2.resize(crop, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
    _, thresh = cv2.threshold(up, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return thresh


def ocr_all_under_boxes(page, under_boxes, ax, ay):
    if not under_boxes:
        return []
    coords = []
    for b in under_boxes:
        x = ax + (b["x"] - ANCHOR_LEFT)
        y = ay + (b["y"] - ANCHOR_TOP)
        coords.append((int(x), int(y), int(b["w"]), int(b["h"])))
    x_min = max(0, min(c[0] for c in coords))
    y_min = max(0, min(c[1] for c in coords))
    x_max = min(VIEW_W, max(c[0] + c[2] for c in coords))
    y_max = min(VIEW_H, max(c[1] + c[3] for c in coords))
    clip_w, clip_h = x_max - x_min, y_max - y_min
    if clip_w <= 0 or clip_h <= 0:
        return [(b, "") for b in under_boxes]
    try:
        raw = page.screenshot(
            clip={"x": x_min, "y": y_min, "width": clip_w, "height": clip_h},
            type="png", scale="css")
        big = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_GRAYSCALE)
    except Exception as e:
        dbg(f"Batch screenshot error: {e}")
        return [(b, "") for b in under_boxes]

    results = []
    for box, (x, y, w, h) in zip(under_boxes, coords):
        cx0 = max(0, x - x_min)
        cy0 = max(0, y - y_min)
        crop = big[cy0:cy0 + h, cx0:cx0 + w]
        if crop.size == 0:
            results.append((box, ""))
            continue
        try:
            text = pytesseract.image_to_string(
                _preprocess_for_ocr(crop), config=OCR_CFG_HEADER).strip()
        except Exception as e:
            dbg(f"OCR error: {e}")
            text = ""
        results.append((box, text.lower()))
    return results


def do_click(page, target_box, ax, ay):
    x = ax + (target_box["x"] - ANCHOR_LEFT)
    y = ay + (target_box["y"] - ANCHOR_TOP)
    cx = x + target_box["w"] / 2
    cy = y + target_box["h"] / 2
    try:
        page.mouse.click(cx, cy, delay=random.randint(2, 5))
    except Exception as e:
        log(f"   Click error: {e}")
        return None, None
    return cx, cy


def get_striker(m):
    for key in ("batsman_out", "dismissed_batsman", "out_batsman",
                "batsman_out_name", "dismissed_player"):
        v = m.get(key)
        if v and isinstance(v, str) and len(v.strip()) >= 2:
            return v.strip(), key
    for key in ("striker", "striker_name", "batsman", "batsman_name",
                "player", "player_name", "batsmanName", "strikerName"):
        v = m.get(key)
        if v and isinstance(v, str) and len(v.strip()) >= 2:
            return v.strip(), key
    return None, None


def ball_info(d):
    ev = str(d.get("ev") or "").strip()
    ev_low = ev.lower()
    if not ev:
        return (ev, "intermediate", 0)
    if re.match(r'^(wicket|out|w|batsman (out|dismissed))[.!]*$', ev_low):
        return (ev, "wicket", 0)
    if re.match(r'^(bowled|lbw|caught|c ?& ?b|run ?out|stumped?|hit ?wicket|'
                r'retired|handled|obstruct)', ev_low):
        return (ev, "wicket", 0)
    return (ev, "intermediate", 0)


def handle_wicket(page, striker):
    global _last_clicked, _roster_refresh_at

    if not striker:
        return False
    now = time.time()
    if (_last_clicked["name"] == striker.lower()
            and now - _last_clicked["at"] < DEDUP_WINDOW_S):
        log(f"[SKIP-DUP] '{striker}' clicked {(now - _last_clicked['at']):.0f}s ago")
        return False

    if USE_ROSTER:
        row = _panel_roster.find(striker)
        if row and row.get("box"):
            ax, ay = find_anchor_fast(page)
            if ax is not None:
                cx, cy = do_click(page, row["box"], ax, ay)
                if cx is not None:
                    log(f"[FAST-CLICK] '{striker}' row {row['i']} "
                        f"(run={row['run']}) @({cx:.0f},{cy:.0f})")
                    _panel_roster.mark_out(striker)
                    _last_clicked = {"name": striker.lower(), "at": time.time()}
                    _roster_refresh_at = time.time() + POST_CLICK_REFRESH_SEC
                    return True
            else:
                log(f"[FAST-MISS] '{striker}' found in roster but no anchor")
        else:
            dbg(f"[FAST-MISS] '{striker}' not in roster -> slow path")

    ok = _handle_wicket_retry(page, striker)
    if ok:
        if USE_ROSTER:
            _panel_roster.mark_out(striker)
            _roster_refresh_at = time.time() + POST_CLICK_REFRESH_SEC
    return ok


def _handle_wicket_retry(page, striker):
    global _last_clicked, CALIBRATED_ANCHOR_POS, _anchor_trust_until

    under_boxes = [t for t in MAPPED_TARGETS if t["side"] == "under"]
    if not under_boxes:
        log("[NO-BOXES] no under boxes mapped")
        return False

    total = len(RETRY_DELAYS_MS) + 1
    t_start = time.perf_counter()
    matched_options = []
    last_ocr = []

    for attempt in range(total):
        ax, ay = find_anchor_fast(page)
        if ax is None:
            log(f"[NO-ANCHOR] attempt {attempt+1}/{total}")
            if attempt < total - 1:
                page.wait_for_timeout(RETRY_DELAYS_MS[attempt])
                continue
            return False

        ocr_results = ocr_all_under_boxes(page, under_boxes, ax, ay)
        last_ocr = ocr_results
        matched_options = []
        for i, (box, text) in enumerate(ocr_results):
            if DEBUG and text:
                dbg(f"   [A{attempt}][BOX {i}] '{text[:80]}'")
            if not text:
                continue
            live_name = extract_name_from_text(text)
            live_run = extract_run_from_text(text)
            if live_name and is_name_match(striker, live_name):
                matched_options.append((live_run, box, live_name, text))
                log(f"   [MATCH-A{attempt}] '{striker}' -> '{live_name}' (run={live_run})")

        if matched_options:
            break

        valid_names = [extract_name_from_text(t) for _, t in ocr_results]
        valid_names = [n for n in valid_names if n]
        all_empty = len(valid_names) == 0
        if attempt < total - 1:
            delay = RETRY_DELAYS_MS[attempt]
            tag = "PANEL-EMPTY" if all_empty else "RETRY"
            log(f"[{tag} {attempt+1}/{total-1}] '{striker}' not found "
                f"({len(valid_names)} names visible), wait {delay}ms")
            page.wait_for_timeout(delay)
            if all_empty and attempt == 0:
                CALIBRATED_ANCHOR_POS = None
                _anchor_trust_until = 0.0

    if not matched_options:
        sample = [t[:50].replace('\n', ' ') for _, t in last_ocr if t.strip()][:5]
        log(f"[NO-MATCH] '{striker}' after {total} attempts")
        if sample:
            log(f"   Visible OCR: {sample}")
        return False

    matched_options.sort(key=lambda x: x[0], reverse=True)
    best_run, best_box, best_name, _ = matched_options[0]
    if len(matched_options) > 1:
        log(f"[MULTI-MATCH] {len(matched_options)} candidates, picking run={best_run}")
    if best_run == 0:
        log(f"[WARN] '{striker}' name-only click (no run extracted)")

    ax, ay = find_anchor_fast(page)
    if ax is None:
        log("[NO-ANCHOR] cannot click")
        return False
    cx, cy = do_click(page, best_box, ax, ay)
    if cx is None:
        return False

    elapsed_ms = round((time.perf_counter() - t_start) * 1000)
    log(f"[SLOW-CLICK] UNDER {best_run} for '{striker}' "
        f"@({cx:.0f},{cy:.0f}) [+{elapsed_ms}ms]")
    _last_clicked = {"name": striker.lower(), "at": time.time()}
    return True


async def ws_loop():
    while not _stop_event.is_set():
        mf = WS_STATE.get("match_filter") or ""
        try:
            async with websockets.connect(WS_URL, ping_interval=None,
                                          compression=None, max_size=None) as ws:
                WS_STATE["up"] = True
                log(f"[WS] connected: {WS_URL}")
                if mf:
                    await send_watch(ws, mf)

                async def pinger():
                    while True:
                        await asyncio.sleep(PING_SEC)
                        try:
                            await ws.send("ping")
                        except Exception:
                            break

                ptask = asyncio.create_task(pinger())
                try:
                    async for raw in ws:
                        if raw == "pong" or raw == b"pong":
                            continue
                        if isinstance(raw, (bytes, bytearray)):
                            try:
                                raw = raw.decode("utf-8", errors="replace")
                            except Exception:
                                continue
                        try:
                            m = json.loads(raw)
                        except (ValueError, TypeError):
                            continue
                        mid_msg = m.get("id")
                        if mf and mid_msg and str(mid_msg) != str(mf):
                            continue
                        if m.get("t") == "reset":
                            handle_reset(m.get("from"), m.get("to"))
                            continue
                        WS_STATE["msg_count"] += 1
                        WS_STATE["last_ts"] = time.time()
                        try:
                            event_q.put_nowait(m)
                        except queue.Full:
                            dbg("event_q full - dropping message")
                finally:
                    ptask.cancel()
        except asyncio.CancelledError:
            return
        except Exception as e:
            log(f"[WS] disconnected: {type(e).__name__}: {e}")
        finally:
            WS_STATE["up"] = False
        if _stop_event.is_set():
            return
        await asyncio.sleep(RECONNECT_SEC)


def start_ws_thread():
    def _run():
        try:
            asyncio.run(ws_loop())
        except Exception as e:
            log(f"[WS] thread crashed: {e}")
    threading.Thread(target=_run, daemon=True).start()


def monitor_and_click():
    global STRICT_MATCH, _last_dedup_key, _roster_refresh_at

    if len(sys.argv) < 2:
        print("Usage: python clicker_batsman.py <match_key> [multi]")
        return

    match_filter = sys.argv[1].strip()
    if len(sys.argv) >= 3:
        arg2 = sys.argv[2].strip().lower()
        if arg2 == "multi":
            STRICT_MATCH = False
        elif arg2 in ("strict", "full"):
            STRICT_MATCH = True

    WS_STATE["match_filter"] = match_filter
    if TESSERACT_CMD:
        pytesseract.pytesseract.tesseract_cmd = TESSERACT_CMD

    site_url = (os.environ.get("CLICKER_SITE_URL", "").strip()
                or safe_input("\nSite URL: ", "https://example.com"))

    proxy_config = None
    proxy_url = (os.environ.get("CLICKER_PROXY_URL", "").strip()
                 or safe_input("Proxy URL [Enter to skip]: ", ""))
    if proxy_url:
        try:
            if "@" in proxy_url:
                proto, rest = (proxy_url.split("://") if "://" in proxy_url
                               else ("http://", proxy_url))
                auth, host_port = rest.split("@")
                user, pwd = auth.split(":")
                proxy_config = {"server": f"{proto}://{host_port}",
                                "username": user, "password": pwd}
            else:
                proxy_config = {"server": proxy_url}
            log(f"Proxy: {proxy_config['server']}")
        except Exception as e:
            log(f"Proxy error: {e}")

    try:
        pytesseract.image_to_string(np.zeros((30, 100), dtype=np.uint8),
                                    config=OCR_CFG_HEADER)
        log(f"Tesseract warmed up (psm={OCR_PSM})")
    except Exception:
        pass

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=HEADLESS,
            args=[f"--window-size={WIN_W},{WIN_H}", "--window-position=0,0"],
        )
        context = browser.new_context(
            viewport={"width": VIEW_W, "height": VIEW_H},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/122.0.0.0 Safari/537.36",
            proxy=proxy_config,
        )
        page = context.new_page()
        page.goto(site_url)

        match_url = (os.environ.get("CLICKER_MATCH_URL", "").strip()
                     or safe_input("Match URL: ", ""))
        if not match_url:
            log("[!] no match URL — abort")
            browser.close()
            return
        page.goto(match_url)
        log(f"Match page ready: {page.url}")

        print("\n" + "=" * 65)
        print("Go to browser and select 'Players, Total Runs' market")
        print("Adjust red boxes. Use '+ 2 Rows' if more players.")
        print("Click 'DONE' when ready")
        print("=" * 65 + "\n")

        while not _stop_event.is_set():
            try:
                page.evaluate("() => { window.__pw_action = null; }")
                page.evaluate(SHOW_TARGETS_JS)
            except Exception as e:
                log(f"Calibration setup error: {e}")
                break
            print("Waiting for calibration (DONE / + / -)...")
            action = None
            while not _stop_event.is_set():
                try:
                    action = page.evaluate("() => window.__pw_action")
                except Exception as e:
                    log(f"Calibration poll error: {e}")
                    break
                if action in ('done', 'update'):
                    break
                page.wait_for_timeout(200)
            if action == 'update':
                continue
            if action == 'done':
                try:
                    final_targets = page.evaluate("() => window.__pw_final_targets")
                    setup_from_done(page, final_targets)
                except Exception as e:
                    log(f"Calibration save error: {e}")
                break
            break

        if not MAPPED_TARGETS:
            log("No targets mapped - aborting.")
            browser.close()
            return

        if USE_ROSTER:
            log("[ROSTER] building initial prediction...")
            ok = _do_roster_refresh(page, "initial")
            if ok:
                snap = _panel_roster.snapshot()
                log(f"[ROSTER] {len(snap)} rows cached:")
                for r in snap:
                    log(f"     row {r['i']:2d}: '{r['name']}' (run={r['run']})")
            else:
                log("[ROSTER] initial refresh failed - will rely on ball_chalu")

        start_ws_thread()
        log(f"[WS] waiting... (match_key={match_filter})")
        log(f"   v25 - wicket-only  |  FAST(roster) + SLOW(OCR retry)")
        log(f"   Mode: {'STRICT' if STRICT_MATCH else 'MULTI'}")
        log(f"   Roster: {'ON' if USE_ROSTER else 'OFF'}  |  OCR psm={OCR_PSM}")

        _last_dedup_key = None
        _roster_refresh_at = 0.0

        try:
            while not _stop_event.is_set():
                check_pending_reset()

                if USE_ROSTER and _roster_refresh_at and time.time() >= _roster_refresh_at:
                    _roster_refresh_at = 0.0
                    _do_roster_refresh(page, "post-wicket")
                    names = _panel_roster.active_names()
                    log(f"[ROSTER] post-wicket refresh: {len(names)} active = {names}")

                try:
                    m = event_q.get(timeout=EVENT_TIMEOUT_SEC)
                except queue.Empty:
                    continue

                if m.get("t") != "ev":
                    continue
                ev = str(m.get("ev") or "").strip()
                if not ev:
                    continue

                if is_ball_chalu(ev):
                    _last_dedup_key = None
                    if USE_ROSTER:
                        _do_roster_refresh(page, "ball_chalu")
                    continue

                text, kind, _ = ball_info(m)
                if kind != "wicket":
                    continue

                over_str = str(m.get("over", ""))
                bkey = m.get("xball") or m.get("ball") or ""
                dedup_key = f"{over_str}|{bkey}|{text}"
                if dedup_key == _last_dedup_key:
                    dbg(f"[DUP] {dedup_key}")
                    continue
                _last_dedup_key = dedup_key

                striker, src = get_striker(m)
                if not striker:
                    log("[WICKET] no batsman name in payload - skip")
                    continue

                log(f"[WICKET] '{striker}' (from '{src}') | "
                    f"over={m.get('over')} ball={m.get('ball')} ev='{text}'")
                handle_wicket(page, striker)

        except KeyboardInterrupt:
            print("\nClosing...")
        finally:
            _stop_event.set()
            try:
                context.close()
            except Exception:
                pass
            try:
                browser.close()
            except Exception:
                pass


if __name__ == "__main__":
    monitor_and_click()
