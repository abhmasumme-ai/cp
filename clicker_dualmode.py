"""
clicker.py - v18 (v17 + post-click pre-warm + multi fast-dedup + fast constants) clickerdualmode delivery over/under
================================================================================
Modes:
  - Single-exact (default):
        python clicker.py <match_id>
        want = (over+1, xball|ball)  -- only this exact panel is clicked

  - Multi-candidate:
        python clicker.py <match_id> multi
        (or)  CLICKER_MULTI=1 python clicker.py <match_id>
        raw "3.2" -> click 4-2 OR 4-3
        raw "3.6" -> click 4-6 OR 5-1   <- over boundary
        raw "4.1" -> click 5-1 OR 5-2   <- over boundary (next over)

v18 changes over v17:
  1. Post-click pre-warm: refresh_panel_header() runs immediately after click
     so the NEXT event hits the cache fast-path (<20ms instead of 60-170ms).
  2. POST_CLICK_COOLDOWN 0.3 -> 0.1 (pre-warm removes need for long cooldown).
  3. EVENT_TIMEOUT_SEC 0.05 -> 0.01 (lower idle wake latency).
  4. Multi-mode fast pre-dedup: if ALL candidates already clicked, skip OCR.
  5. Idle-refresh skipped when recent WS activity (<1s) to avoid blocking loop.

v17 fixes retained:
  - Multi-mode panel-level dedup (3.6->4.1 no longer skipped)
  - Dedup key includes ball/xball
  - str(m.get("over", "")) -> over=0 works
  - max_boxes = len(final_targets)
  - Calibration loop crash-proof
  - _last_dedup_key cleared on RESET
  - _recent_clicked stores ACTUAL panel value in BOTH modes

PATched (app-only):
  - _common.safe_input (never blocks in subprocess mode)
  - _common.send_watch (proper JSON watch message to relay)

Env:
  CLICKER_WS_URL         default ws://127.0.0.1:5000/ws
  CLICKER_WS_TOKEN       optional WS auth token
  CLICKER_HEADLESS       1 = headless browser
  CLICKER_DEBUG          1 = verbose
  CLICKER_NO_IDLE        1 = disable idle cache refresh
  CLICKER_MULTI          1 = force multi-candidate mode
  CLICKER_SITE_URL       optional (else prompt)
  CLICKER_MATCH_URL      optional (else prompt)
  CLICKER_PROXY_URL      optional proxy URL
  CLICKER_DEDUP_WINDOW   default 30
  CLICKER_NO_PROMPT      1 = never prompt (set by clicker_manager)
  TESSERACT_CMD          custom tesseract path
"""
import asyncio
import collections
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

# ★ PATCH 1: shared safe_input + send_watch
try:
    from _common import safe_input, send_watch
except ImportError:
    from clickers._common import safe_input, send_watch

# ============================================================
#  Config
# ============================================================
_WS_URL_BASE = os.environ.get("CLICKER_WS_URL", "ws://127.0.0.1:5000/ws")
_WS_TOKEN = os.environ.get("CLICKER_WS_TOKEN", "").strip()
if _WS_TOKEN:
    _sep = "&" if "?" in _WS_URL_BASE else "?"
    WS_URL = f"{_WS_URL_BASE}{_sep}token={urllib.parse.quote(_WS_TOKEN)}"
else:
    WS_URL = _WS_URL_BASE

RECONNECT_SEC = 3
PING_SEC = 30
HEADLESS = os.environ.get("CLICKER_HEADLESS") == "1"
DEBUG = os.environ.get("CLICKER_DEBUG") == "1"
NO_IDLE = os.environ.get("CLICKER_NO_IDLE") == "1"
DEDUP_WINDOW = int(os.environ.get("CLICKER_DEDUP_WINDOW", "30"))
MULTI_MODE = os.environ.get("CLICKER_MULTI") == "1"   # overridable by CLI
TESSERACT_CMD = os.environ.get("TESSERACT_CMD") or (
    r"C:\Program Files\Tesseract-OCR\tesseract.exe"
    if os.path.exists(r"C:\Program Files\Tesseract-OCR\tesseract.exe") else None
)

# Timing - fastest pipeline settings (v18 tightened)
CACHE_FRESH_SEC = 0.5
IDLE_REFRESH_SEC = 0.4
ANCHOR_TRUST_SEC = 60
EVENT_TIMEOUT_SEC = 0.01         # v18: was 0.05
POST_CLICK_COOLDOWN = 0.1        # v18: was 0.3
EVENT_Q_MAXSIZE = 500
IDLE_SKIP_WS_RECENT_SEC = 1.0    # v18: skip idle OCR if WS active within 1s

# OCR
COMPARE_THRESHOLD = 0.70
CACHE_VERIFY_THRESHOLD = 0.82
HEADER_CROP_W = 240
OCR_RESIZE = 3
OCR_CFG_HEADER = '--oem 1 --psm 6'
OCR_CFG_HEADER_FALLBACK = '--oem 1 --psm 7'
OCR_CFG_SETUP = '--oem 1 --psm 6'

# Viewport
VIEW_W = 1080
VIEW_H = 797
WIN_W = VIEW_W + 100
WIN_H = VIEW_H + 120

ANCHOR_LEFT = 70
ANCHOR_TOP = 400
ANCHOR_W = 100
ANCHOR_H = 34

# State
ANCHOR_TEMPLATE = {}
MAPPED_TARGETS = []
CALIBRATED_ANCHOR_POS = None
last_anchor_pos = None
_anchor_trust_until = 0.0
_last_click_at = 0.0

panel_cache = {
    "over": None, "del": None, "ts": 0.0, "ax": None, "ay": None,
}

# Dedup state (both modes use _recent_clicked with ACTUAL panel value)
_recent_clicked = collections.deque(maxlen=DEDUP_WINDOW)

_pending_reset = threading.Event()
_xball_missing_logged = False
_last_dedup_key = None      # module-level so RESET can clear it

WS_STATE = {"match_filter": "", "up": False, "msg_count": 0, "last_ts": 0.0}
event_q = queue.Queue(maxsize=EVENT_Q_MAXSIZE)
_stop_event = threading.Event()

INTERMEDIATE_PATTERNS = [
    r'^ball\b.*\b(chalu|start|chalo|shuru|shuro)',
    r'^ball in ?air', r'^boundary ?check',
    r'^(third ?umpire|3rd ?umpire)',
    r'^(confirm(ing)?|checking)', r'^(review|drs)',
    r'^(batsman|bowler|new ?batter|misfield|dropped|overthrow)',
]


# ============================================================
#  Utilities
# ============================================================
def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def dbg(msg):
    if DEBUG:
        print(f"[{time.strftime('%H:%M:%S')}] [DBG] {msg}", flush=True)


def is_intermediate(ev):
    ev_low = ev.lower().strip()
    return any(re.search(p, ev_low) for p in INTERMEDIATE_PATTERNS)


def is_ball_chalu(ev):
    return bool(re.search(r'^ball\b.*\b(chalu|start|chalo|shuru|shuro)',
                          ev.lower().strip()))


def handle_reset(from_id, to_id):
    """Called from WS thread. Sets flag only (thread-safe)."""
    _pending_reset.set()
    log(f"[RESET] {from_id} -> {to_id}")


def check_pending_reset():
    """Called from main thread. Clears dedup + dedup key safely."""
    global _last_dedup_key
    if _pending_reset.is_set():
        _pending_reset.clear()
        _recent_clicked.clear()
        _last_dedup_key = None
        log("[RESET] dedup window + dedup key cleared")


# ============================================================
#  Calibration UI (JS)
# ============================================================
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
    let baseY = 400, anchorW = 100, anchorH = 34, leftX = 70, rightX = 425, boxW = 220, boxH = 30, rowGap = 3.34;
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
    plusBtn.textContent = '+ 3 Rows';
    plusBtn.style.cssText = 'padding:10px 20px;font-size:16px;font-weight:bold;background:#2196f3;color:white;border:none;border-radius:6px;cursor:pointer;';
    plusBtn.onclick = () => { window.__row_count = Math.min(9, rowCount + 3); window.__pw_action = 'update'; };
    const minusBtn = document.createElement('button');
    minusBtn.textContent = '- 3 Rows';
    minusBtn.style.cssText = 'padding:10px 20px;font-size:16px;font-weight:bold;background:#ff9800;color:white;border:none;border-radius:6px;cursor:pointer;';
    minusBtn.onclick = () => { window.__row_count = Math.max(3, rowCount - 3); window.__pw_action = 'update'; };
    const resetBtn = document.createElement('button');
    resetBtn.textContent = 'RESET';
    resetBtn.style.cssText = 'padding:10px 20px;font-size:16px;font-weight:bold;background:#ff3d00;color:white;border:none;border-radius:6px;cursor:pointer;';
    resetBtn.onclick = () => { window.__pw_action = 'reset'; };
    btnContainer.appendChild(doneBtn);
    btnContainer.appendChild(plusBtn);
    btnContainer.appendChild(minusBtn);
    btnContainer.appendChild(resetBtn);
    box.appendChild(btnContainer);
}
"""


# ============================================================
#  Calibration
# ============================================================
def setup_from_done(page, final_targets):
    global CALIBRATED_ANCHOR_POS, last_anchor_pos, _anchor_trust_until
    log("Saving anchor and target box text from screen...")

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
    KNOWN_LINES = [0.5, 1.5, 2.5, 3.5, 4.5, 5.5, 6.5, 7.5]
    max_boxes = len(final_targets)

    for i, t in enumerate(final_targets[:max_boxes]):
        x, y, w, h = int(t['x']), int(t['y']), int(t['w']), int(t['h'])
        side_hint = (t.get('side') or '').lower()
        crop = src_img[y:y + h, x:x + w]
        if crop.size == 0:
            continue
        crop_r = cv2.resize(crop, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
        text = pytesseract.image_to_string(crop_r, config=OCR_CFG_SETUP).lower()
        log(f"      [BOX {i}] side={side_hint or '?'} OCR={text!r}")

        side = side_hint if side_hint in ('over', 'under') else ('over' if i % 2 == 0 else 'under')
        line = None
        m_ou = re.search(r'\b(over|under|ovr|undr)\b\s*(\d+\.?\d*)', text)
        if not m_ou:
            m_ou = re.search(r'(\d+\.?\d*)\s*\b(over|under|ovr|undr)\b', text)
        if m_ou:
            for g in m_ou.groups():
                if g and re.fullmatch(r'\d+\.?\d*', g):
                    line = float(g)
                elif g and g[0] == 'o':
                    side = 'over'
                elif g and g[0] == 'u':
                    side = 'under'
        if line is None:
            floats = [float(n) for n in re.findall(r'(\d+\.\d+)', text)]
            for f in floats:
                if f in KNOWN_LINES:
                    line = f
                    break
            if line is None and floats:
                line = floats[0]
        if line is None:
            line = KNOWN_LINES[i // 2] if (i // 2) < len(KNOWN_LINES) else 0.5

        MAPPED_TARGETS.append({"side": side, "line": line, "x": x, "y": y, "w": w, "h": h})
        log(f"      Mapping: {side.upper()} {line}")

    log(f"   Mapped {len(MAPPED_TARGETS)}/{max_boxes} targets")


# ============================================================
#  Target decision
# ============================================================
def _has_mapped(side, line):
    return any(t["side"] == side and abs(t["line"] - line) < 0.01
               for t in MAPPED_TARGETS)


def find_target(side, line):
    for t in MAPPED_TARGETS:
        if t["side"] == side and abs(t["line"] - line) < 0.01:
            return t
    return None


def decide_side_line(run, kind):
    if run == 0:
        if _has_mapped("under", 0.5):
            return ("under", 0.5)
        return (None, None)
    for ln in [7.5, 6.5, 5.5, 4.5, 3.5, 2.5, 1.5, 0.5]:
        if ln < run and _has_mapped("over", ln):
            return ("over", ln)
    return (None, None)


# ============================================================
#  Anchor detection
# ============================================================
def find_anchor_fast(page):
    global CALIBRATED_ANCHOR_POS, last_anchor_pos, _anchor_trust_until
    now = time.time()
    cached = CALIBRATED_ANCHOR_POS or last_anchor_pos
    if cached and now < _anchor_trust_until:
        return cached
    if cached:
        ax, ay = cached
        if 0 <= ax <= VIEW_W - ANCHOR_W and 0 <= ay <= VIEW_H - ANCHOR_H:
            try:
                raw = page.screenshot(
                    clip={"x": ax, "y": ay, "width": ANCHOR_W, "height": ANCHOR_H},
                    type="png", scale="css")
                crop = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_GRAYSCALE)
                if crop is not None and crop.shape == ANCHOR_TEMPLATE["img"].shape:
                    res = cv2.matchTemplate(crop, ANCHOR_TEMPLATE["img"],
                                            cv2.TM_CCOEFF_NORMED)
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


# ============================================================
#  Panel header OCR
# ============================================================
_OCR_DIAG = {"fail_count": 0}


def preprocess_ocr(crop):
    return cv2.resize(crop, None, fx=OCR_RESIZE, fy=OCR_RESIZE,
                      interpolation=cv2.INTER_CUBIC)


def refresh_panel_header(page, ax, ay, probe_x, probe_y, probe_h):
    if ax is None:
        return False
    try:
        raw = page.screenshot(
            clip={"x": probe_x, "y": probe_y,
                  "width": HEADER_CROP_W, "height": probe_h},
            type="png", scale="css")
        crop = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_GRAYSCALE)
        if crop is None or crop.size == 0:
            return False
        crop_big = preprocess_ocr(crop)

        text = ""
        for cfg in (OCR_CFG_HEADER, OCR_CFG_HEADER_FALLBACK):
            try:
                text = pytesseract.image_to_string(crop_big, config=cfg).lower().strip()
            except Exception:
                text = ""
            if text:
                break

        if not text and _OCR_DIAG["fail_count"] < 5:
            _OCR_DIAG["fail_count"] += 1
            n = _OCR_DIAG["fail_count"]
            log(f"   [OCR-DIAG #{n}] clip=({probe_x},{probe_y},w={HEADER_CROP_W}) text=''")

        if not text:
            return False

        m = (re.search(r'(\d+)\D{0,4}over\D{0,8}(\d+)\D{0,4}del', text) or
             re.search(r'(\d+)\D{0,4}over\D{0,8}(\d+)\D{0,4}de[l1]', text) or
             re.search(r'(\d+)\D{0,4}ov[er]{1,2}\D{0,8}(\d+)\D{0,4}d[e1]', text))
        if not m:
            if DEBUG:
                dbg(f"OCR no-match: {text!r}")
            return False

        po = int(m.group(1))
        pd = int(m.group(2))
        if not (0 <= po <= 50 and 1 <= pd <= 50):
            return False

        old = (panel_cache["over"], panel_cache["del"])
        panel_cache["over"] = po
        panel_cache["del"] = pd
        panel_cache["ts"] = time.time()
        panel_cache["ax"] = ax
        panel_cache["ay"] = ay
        if DEBUG and old != (po, pd):
            dbg(f"cache {old}->({po},{pd})")
        return True
    except Exception as e:
        dbg(f"OCR err: {e}")
        return False


# ============================================================
#  Click action
# ============================================================
def do_click(page, target_box, ax, ay):
    x = ax + (target_box["x"] - ANCHOR_LEFT)
    y = ay + (target_box["y"] - ANCHOR_TOP)
    cx = x + target_box["w"] / 2
    cy = y + target_box["h"] / 2
    try:
        page.mouse.click(cx, cy, delay=random.randint(2, 4))
    except Exception as e:
        log(f"   Click error: {e}")
        return None, None
    return cx, cy


# ============================================================
#  Over/ball parsing
# ============================================================
def parse_over_ball(d):
    ov = d.get("over")
    if ov is None:
        return None, None, None, None

    over_num = None
    over_dec = 0
    ball = 1

    if isinstance(ov, (int, float)) and not isinstance(ov, bool):
        over_num = int(ov)
        frac = ov - over_num
        if frac > 0.01:
            try:
                over_dec = int(round(frac * 10))
                ball = over_dec
            except (ValueError, TypeError):
                over_dec = 0
                ball = 1
        else:
            over_dec = 0
            ball = 1
    else:
        ov_str = str(ov).strip()
        try:
            if "." in ov_str:
                parts = ov_str.split(".")
                over_num = int(parts[0])
                over_dec = int(parts[1]) if parts[1] else 1
                ball = over_dec
            else:
                over_num = int(ov_str)
                over_dec = 0
                ball = 1
        except (ValueError, IndexError):
            return None, None, None, None

    b = d.get("ball")
    if b is not None:
        try:
            ball = int(b)
        except (ValueError, TypeError):
            pass

    xball = d.get("xball")
    if xball is not None:
        try:
            xball = int(xball)
        except (ValueError, TypeError):
            xball = None

    return over_num, over_dec, ball, xball


# ============================================================
#  Want candidates (used by BOTH modes)
#  - cands[0] == single-exact want
#  - cands[0..1] == multi-candidate
# ============================================================
def compute_want_candidates(over_num, over_dec, ball, xball):
    global _xball_missing_logged

    base_over = over_num + 1

    if xball is not None and xball > 0:
        delivery = xball
    else:
        delivery = ball
        if xball is None and not _xball_missing_logged:
            _xball_missing_logged = True
            log("[INFO] Server payload missing 'xball'. Using 'ball' fallback. "
                "Fix: add 'xball': d.get('xball') to ball_event output in main.py.")

    if over_dec >= 6:
        cands = [(base_over, delivery), (base_over + 1, 1)]
    else:
        cands = [(base_over, delivery), (base_over, delivery + 1)]

    out = []
    seen = set()
    for c in cands:
        if c in seen:
            continue
        if 0 <= c[0] <= 50 and 1 <= c[1] <= 50:
            seen.add(c)
            out.append(c)
    return out


# ============================================================
#  Event processing (unified single + multi)
# ============================================================
def process_event(page, ball_key, run, kind, over_num, over_dec, ball, xball):
    global _last_click_at

    t0 = time.perf_counter()
    side_want, line_want = decide_side_line(run, kind)
    all_cands = compute_want_candidates(over_num, over_dec, ball, xball)

    if MULTI_MODE:
        cands = all_cands
        cand_str = " | ".join(f"{o}-{d}" for o, d in cands) if cands else "?"
        log(f"[TARGET] {ball_key} run={run}({kind}) -> "
            f"{side_want} {line_want} | want: {cand_str} "
            f"(over_dec={over_dec} ball={ball})")
    else:
        cands = all_cands[:1]
        want = cands[0] if cands else None
        cand_str = f"{want[0]}-{want[1]}" if want else "?"
        log(f"[TARGET] {ball_key} run={run}({kind}) -> "
            f"{side_want} {line_want} | want={cand_str}")

    if not cands:
        log(f"[NO-CANDIDATE] over_num={over_num} over_dec={over_dec}")
        return False

    if side_want is None:
        log(f"[NO-MAPPED-LINE] run={run} - skip")
        return False

    # Single-mode fast pre-dedup: if exact want already clicked, skip entirely.
    if not MULTI_MODE and cands[0] in _recent_clicked:
        log(f"[ALREADY-CLICKED] ({cand_str})")
        return False

    # v18: Multi-mode fast pre-dedup — if ALL candidates already clicked, skip.
    if MULTI_MODE and all(c in _recent_clicked for c in cands):
        log(f"[ALREADY-CLICKED-ALL] {cand_str}")
        return False

    target_box = find_target(side_want, line_want)
    if not target_box:
        log(f"[NO-TARGET] {side_want} {line_want}")
        return False

    def check_and_click(source):
        """Read current panel; click only if it's in cands AND not already clicked."""
        global _last_click_at
        po, pd = panel_cache["over"], panel_cache["del"]
        if po is None or pd is None:
            return False
        if (po, pd) not in cands:
            return False
        if (po, pd) in _recent_clicked:
            log(f"[ALREADY-CLICKED] panel {po}-{pd} (in dedup window)")
            return False
        cx, cy = do_click(page, target_box,
                          panel_cache["ax"], panel_cache["ay"])
        if cx is None:
            return False
        ms = round((time.perf_counter() - t0) * 1000)
        log(f"[CLICK-{source}] panel={po}-{pd} "
            f"target={side_want} {line_want} "
            f"@({cx:.0f},{cy:.0f}) [+{ms}ms]")
        _recent_clicked.append((po, pd))
        _last_click_at = time.time()
        panel_cache["ts"] = 0    # invalidate — panel has advanced

        # v18: post-click pre-warm — re-read panel immediately so the NEXT
        # event hits the cache fast-path instead of paying full OCR latency.
        # Best-effort: if it reads stale, the next event's slow path corrects it.
        try:
            axw = panel_cache["ax"]
            ayw = panel_cache["ay"]
            if axw is not None and ayw is not None:
                txw = axw + (target_box["x"] - ANCHOR_LEFT)
                tyw = ayw + (target_box["y"] - ANCHOR_TOP)
                refresh_panel_header(page, axw, ayw, txw, tyw, target_box["h"])
        except Exception:
            pass
        return True

    # Fast path: cache hit (cache populated by a recent idle-refresh OCR)
    cache_age = time.time() - panel_cache["ts"]
    if cache_age < CACHE_FRESH_SEC and panel_cache["ax"] is not None:
        if check_and_click(f"CACHE age={cache_age*1000:.0f}ms"):
            return True

    # Slow path: OCR refresh
    ax, ay = find_anchor_fast(page)
    if ax is None:
        log("[NO-ANCHOR]")
        return False

    tx = ax + (target_box["x"] - ANCHOR_LEFT)
    ty = ay + (target_box["y"] - ANCHOR_TOP)
    refresh_panel_header(page, ax, ay, tx, ty, target_box["h"])

    if check_and_click("OCR"):
        return True

    ms = round((time.perf_counter() - t0) * 1000)
    log(f"[MISS] panel={panel_cache['over']}-{panel_cache['del']} "
        f"not in {{{cand_str}}} [+{ms}ms]")
    return False


# ============================================================
#  Ball classification
# ============================================================
def ball_info(d):
    ev = str(d.get("ev") or "").strip()
    ev_low = ev.lower()
    if not ev or is_intermediate(ev):
        return (ev, "intermediate", 0)
    if re.match(r'^(wicket|out|w)[.!]*$', ev_low):
        return (ev, "wicket", 0)
    if re.match(r'^(bowled|lbw|caught|c ?& ?b|run ?out|stumped?|hit ?wicket|retired|handled|obstruct)', ev_low):
        return (ev, "wicket", 0)
    if re.match(r'^(no[\s_-]?ball|nb)\b', ev_low):
        m = re.search(r'(\d+)', ev_low)
        return (ev, "noball", int(m.group(1)) + 1 if m else 1)
    if re.match(r'^wide\b', ev_low):
        m = re.search(r'(\d+)', ev_low)
        return (ev, "wide", int(m.group(1)) + 1 if m else 1)
    if re.match(r'^(six|6)\b', ev_low):
        return ("6", "six", 6)
    if re.match(r'^(four|4)\b', ev_low):
        return ("4", "four", 4)
    if re.match(r'^(dot|0)\b', ev_low):
        return ("0", "dot", 0)
    m = re.match(r'^(\d+)', ev_low)
    if m:
        n = int(m.group(1))
        return (str(n), "run", n)
    return (ev, "intermediate", 0)


# ============================================================
#  WebSocket client
# ============================================================
async def ws_loop():
    while not _stop_event.is_set():
        mf = WS_STATE.get("match_filter") or ""
        try:
            async with websockets.connect(WS_URL, ping_interval=None,
                                          compression=None, max_size=None) as ws:
                WS_STATE["up"] = True
                log(f"[WS] connected: {WS_URL}")
                # ★ PATCH 2: JSON watch via shared helper
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
                            handle_reset(m.get('from'), m.get('to'))
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


# ============================================================
#  Main
# ============================================================
def monitor_and_click():
    global MULTI_MODE, _last_dedup_key

    if len(sys.argv) < 2:
        print("Usage: python clicker.py <match_key> [multi|single]")
        return
    match_filter = sys.argv[1].strip()

    # CLI overrides env
    if len(sys.argv) >= 3:
        arg2 = sys.argv[2].strip().lower()
        if arg2 == "multi":
            MULTI_MODE = True
        elif arg2 in ("single", "v15"):
            MULTI_MODE = False

    WS_STATE["match_filter"] = match_filter

    if TESSERACT_CMD:
        pytesseract.pytesseract.tesseract_cmd = TESSERACT_CMD

    # ★ PATCH 3: safe_input for site URL
    site_url = (os.environ.get("CLICKER_SITE_URL", "").strip()
                or safe_input("\nSite URL: ", "https://example.com"))

    # ★ PATCH 4: safe_input for proxy
    proxy_config = None
    proxy_url = (os.environ.get("CLICKER_PROXY_URL", "").strip()
                 or safe_input("Proxy URL [Enter to skip]: ", ""))
    if proxy_url:
        try:
            if "@" in proxy_url:
                proto, rest = proxy_url.split("://") if "://" in proxy_url else ("http://", proxy_url)
                auth, host_port = rest.split("@")
                user, pwd = auth.split(":")
                proxy_config = {"server": f"{proto}://{host_port}",
                                "username": user, "password": pwd}
            else:
                proxy_config = {"server": proxy_url}
            log(f"Proxy: {proxy_config['server']}")
        except Exception as e:
            log(f"Proxy error: {e}")
    else:
        log("Proxy skipped.")

    try:
        pytesseract.image_to_string(np.zeros((30, 100), dtype=np.uint8),
                                    config=OCR_CFG_HEADER)
        log("Tesseract warmed up")
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

        # ★ PATCH 5: safe_input for match URL
        match_url = (os.environ.get("CLICKER_MATCH_URL", "").strip()
                     or safe_input("Match URL: ", ""))
        if not match_url:
            log("[!] no match URL — abort")
            browser.close()
            return
        page.goto(match_url)
        log(f"Match page: {page.url}")

        print("\n" + "=" * 65)
        print("Go to browser and select 'Quick Events'")
        print("Adjust red boxes below the blue anchor box")
        print("Click 'DONE' when ready")
        print("=" * 65 + "\n")

        while not _stop_event.is_set():
            try:
                page.evaluate("() => { window.__pw_action = null; }")
                page.evaluate(SHOW_TARGETS_JS)
            except Exception as e:
                log(f"Calibration setup error: {e}")
                break
            print("Waiting for calibration (DONE / + / - / RESET)...")
            action = None
            while not _stop_event.is_set():
                try:
                    action = page.evaluate("() => window.__pw_action")
                except Exception as e:
                    log(f"Calibration poll error: {e}")
                    break
                if action in ('done', 'update', 'reset'):
                    break
                page.wait_for_timeout(200)
            if action == 'done':
                try:
                    final_targets = page.evaluate("() => window.__pw_final_targets")
                    setup_from_done(page, final_targets)
                except Exception as e:
                    log(f"Calibration save error: {e}")
                break
            elif action == 'reset':
                try:
                    page.evaluate("() => { window.__row_count = 3; }")
                except Exception:
                    break
                continue
            elif action == 'update':
                continue
            else:
                break

        if not MAPPED_TARGETS:
            log("No targets mapped - aborting.")
            browser.close()
            return

        start_ws_thread()
        log(f"[WS] waiting... (match_key={match_filter})")
        if MULTI_MODE:
            log("   Mode: MULTI-candidate")
            log("   raw 3.2 -> click 4-2 OR 4-3")
            log("   raw 3.6 -> click 4-6 OR 5-1  (over boundary)")
            log("   raw 4.1 -> click 5-1 OR 5-2  (over boundary)")
        else:
            log("   Mode: SINGLE-exact")
            log("   want = (over+1, xball|ball) - exact only")
        log(f"   dedup window {DEDUP_WINDOW}")
        log(f"   cache fresh <{CACHE_FRESH_SEC*1000:.0f}ms")
        log(f"   idle refresh every {IDLE_REFRESH_SEC*1000:.0f}ms"
            f"{' [DISABLED]' if NO_IDLE else ''}")
        log(f"   post-click cooldown {POST_CLICK_COOLDOWN*1000:.0f}ms\n")

        _last_dedup_key = None

        try:
            while not _stop_event.is_set():
                check_pending_reset()

                try:
                    m = event_q.get(timeout=EVENT_TIMEOUT_SEC)
                except queue.Empty:
                    if NO_IDLE:
                        continue
                    if event_q.qsize() > 0:
                        continue
                    if time.time() - _last_click_at < POST_CLICK_COOLDOWN:
                        continue
                    # v18: skip idle OCR if recent WS activity (avoid blocking
                    # the main loop right after events were just processed).
                    if time.time() - WS_STATE["last_ts"] < IDLE_SKIP_WS_RECENT_SEC:
                        continue
                    age = time.time() - panel_cache["ts"]
                    if age < IDLE_REFRESH_SEC:
                        continue
                    box = MAPPED_TARGETS[0]
                    ax, ay = find_anchor_fast(page)
                    if ax is not None:
                        px = ax + (box["x"] - ANCHOR_LEFT)
                        py = ay + (box["y"] - ANCHOR_TOP)
                        refresh_panel_header(page, ax, ay, px, py, box["h"])
                    continue

                if m.get("t") != "ev":
                    continue

                ev = str(m.get("ev") or "").strip()
                if not ev:
                    continue
                if is_ball_chalu(ev):
                    _last_dedup_key = None
                    continue

                # dedup key includes ball/xball; safe over stringify
                over_str = str(m.get("over", ""))
                bkey = m.get("xball") or m.get("ball") or ""
                dedup_key = f"{over_str}|{bkey}|{ev}"
                if dedup_key == _last_dedup_key:
                    log(f"[DUP] {dedup_key}")
                    continue
                _last_dedup_key = dedup_key

                text, kind, run = ball_info(m)
                if kind == "intermediate":
                    continue

                over_num, over_dec, main_ball, xball = parse_over_ball(m)
                if over_num is None:
                    continue

                lag_str = ""
                srv_ts = m.get("_server_ts")
                if srv_ts:
                    lag_str = f" | lag={round(time.time()*1000 - srv_ts, 1)}ms"

                xb_str = f" xball={xball}" if xball is not None else ""
                log(f"[EV] ev={text} kind={kind} run={run} | "
                    f"over={m.get('over')} over_dec={over_dec} "
                    f"ball={m.get('ball')}{xb_str}{lag_str}")

                process_event(
                    page, f"{over_str}_{ev}",
                    run, kind, over_num, over_dec, main_ball, xball
                )

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
