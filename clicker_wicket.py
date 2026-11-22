"""
clicker_wicket.py - v27 (FASTEST wicket click) — patched for subprocess.
"""
import asyncio
import difflib
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

TEMPLATE_MATCH_THRESHOLD = float(os.environ.get("CLICKER_TEMPLATE_THRESHOLD", "0.75"))
OCR_UPSCALE = int(os.environ.get("CLICKER_OCR_UPSCALE", "2"))
WICKET_THROTTLE_S = float(os.environ.get("CLICKER_WICKET_THROTTLE_S", "0.5"))
VERIFY_EVERY = int(os.environ.get("CLICKER_VERIFY_EVERY", "10"))
EVENT_Q_MAXSIZE = int(os.environ.get("CLICKER_EVENT_Q_MAXSIZE", "2000"))
ANCHOR_TRUST_SEC = int(os.environ.get("CLICKER_ANCHOR_TRUST_SEC", "600"))

TESSERACT_CMD = os.environ.get("TESSERACT_CMD") or (
    r"C:\Program Files\Tesseract-OCR\tesseract.exe"
    if os.path.exists(r"C:\Program Files\Tesseract-OCR\tesseract.exe") else None
)

EVENT_TIMEOUT_SEC = 0.05
COMPARE_THRESHOLD = 0.70
CACHE_VERIFY_THRESHOLD = 0.82
OCR_CFG_MULTI = '--oem 1 --psm 6'
OCR_CFG_HEADER = '--oem 1 --psm 7'

VIEW_W, VIEW_H = 1080, 797
WIN_W, WIN_H = VIEW_W + 100, VIEW_H + 120

ANCHOR_LEFT, ANCHOR_TOP, ANCHOR_W, ANCHOR_H = 70, 400, 240, 34

ANCHOR_TEMPLATE = {}
MAPPED_TARGETS = []
UNDER_TEMPLATE = {}
UNDER_REL_POS = None
CALIBRATED_ANCHOR_POS = None
last_anchor_pos = None
_anchor_trust_until = 0.0
_last_wicket_time = 0.0
_last_dedup_key = None
_wicket_click_count = 0

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
    global _last_dedup_key, _last_wicket_time, UNDER_REL_POS, _wicket_click_count
    if _pending_reset.is_set():
        _pending_reset.clear()
        UNDER_TEMPLATE.clear()
        UNDER_REL_POS = None
        _last_dedup_key = None
        _last_wicket_time = 0.0
        _wicket_click_count = 0
        log("[RESET] under cache + dedup + throttle cleared")


def _is_under_word(w):
    w = (w or "").lower().strip()
    if not w:
        return False
    if 'under' in w:
        return True
    if 4 <= len(w) <= 7:
        return difflib.SequenceMatcher(None, w, 'under').ratio() >= 0.7
    return False


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
    let baseY = 400, anchorW = 240, anchorH = 34, leftX = 70, boxW = 420, boxH = 30, rowGap = 4;

    const blueBox = document.createElement('div');
    blueBox.style.cssText = `position:absolute;left:${leftX}px;top:${baseY}px;width:${anchorW}px;height:${anchorH}px;border:2px solid #00aaff;background:rgba(0,170,255,0.4);box-sizing:border-box;display:flex;align-items:center;justify-content:center;color:white;font-weight:bold;font-size:10px;`;
    blueBox.textContent = "Anchor";
    box.appendChild(blueBox);

    let y1 = baseY + anchorH;
    let y2 = y1 + boxH + rowGap;

    const legend1 = document.createElement('div');
    legend1.textContent = "OVER";
    legend1.style.cssText = `position:absolute;left:${leftX}px;top:${y1 - 16}px;color:red;font-size:11px;font-weight:bold;`;
    box.appendChild(legend1);

    const box1 = document.createElement('div');
    box1.style.cssText = `position:absolute;left:${leftX}px;top:${y1}px;width:${boxW}px;height:${boxH}px;border:2px solid red;background:transparent;box-sizing:border-box;`;
    box.appendChild(box1);

    const legend2 = document.createElement('div');
    legend2.textContent = "UNDER";
    legend2.style.cssText = `position:absolute;left:${leftX}px;top:${y2 - 16}px;color:red;font-size:11px;font-weight:bold;`;
    box.appendChild(legend2);

    const box2 = document.createElement('div');
    box2.style.cssText = `position:absolute;left:${leftX}px;top:${y2}px;width:${boxW}px;height:${boxH}px;border:2px solid red;background:transparent;box-sizing:border-box;`;
    box.appendChild(box2);

    let finalTargets = [
        {side: 'over', x: leftX, y: y1, w: boxW, h: boxH},
        {side: 'under', x: leftX, y: y2, w: boxW, h: boxH}
    ];

    let btnTop = y2 + boxH + 15;
    const btnContainer = document.createElement('div');
    btnContainer.style.cssText = `position:absolute;left:${leftX}px;top:${btnTop}px;pointer-events:auto;display:flex;gap:10px;`;

    const doneBtn = document.createElement('button');
    doneBtn.textContent = 'DONE';
    doneBtn.style.cssText = 'padding:10px 30px;font-size:16px;font-weight:bold;background:#00c853;color:white;border:none;border-radius:6px;cursor:pointer;';
    doneBtn.onclick = () => { window.__pw_final_targets = finalTargets; window.__pw_action = 'done'; };

    btnContainer.appendChild(doneBtn);
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
    anchor_img = src_img[ANCHOR_TOP:ANCHOR_TOP + ANCHOR_H,
                         ANCHOR_LEFT:ANCHOR_LEFT + ANCHOR_W]
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
    log(f"   Mapped {len(MAPPED_TARGETS)} boxes")


def prewarm_under_position(page, retries=3):
    global UNDER_REL_POS
    for attempt in range(retries):
        ax, ay = find_anchor_fast(page)
        if ax is None:
            if attempt < retries - 1:
                page.wait_for_timeout(400)
                continue
            log("[PREWARM] no anchor")
            return False
        pos = _find_under_once(page, ax, ay)
        if pos is None:
            if attempt < retries - 1:
                page.wait_for_timeout(400)
                continue
            log("[PREWARM] Under not found")
            return False
        UNDER_REL_POS = (pos[0] - ax, pos[1] - ay)
        log(f"[PREWARM] ✓ Under cached: rel=({UNDER_REL_POS[0]:.0f},"
            f"{UNDER_REL_POS[1]:.0f}) attempt={attempt+1}")
        return True
    return False


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


def _combined_region(ax, ay):
    if not MAPPED_TARGETS:
        return None
    xs = [ax + (t["x"] - ANCHOR_LEFT) for t in MAPPED_TARGETS]
    ys = [ay + (t["y"] - ANCHOR_TOP) for t in MAPPED_TARGETS]
    min_x = int(min(xs)); min_y = int(min(ys))
    max_x = int(max(x + t["w"] for x, t in zip(xs, MAPPED_TARGETS)))
    max_y = int(max(y + t["h"] for y, t in zip(ys, MAPPED_TARGETS)))
    if max_x <= min_x or max_y <= min_y:
        return None
    return min_x, min_y, max_x - min_x, max_y - min_y


def _find_under_once(page, ax, ay):
    region = _combined_region(ax, ay)
    if region is None:
        return None
    min_x, min_y, rw, rh = region
    t0 = time.perf_counter()
    try:
        raw = page.screenshot(
            clip={"x": min_x, "y": min_y, "width": rw, "height": rh},
            type="png", scale="css")
        img = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_GRAYSCALE)
        if img is None:
            return None

        tpl = UNDER_TEMPLATE.get("img")
        if (tpl is not None
                and tpl.shape[0] <= img.shape[0]
                and tpl.shape[1] <= img.shape[1]):
            res = cv2.matchTemplate(img, tpl, cv2.TM_CCOEFF_NORMED)
            _, mv, _, ml = cv2.minMaxLoc(res)
            if mv >= TEMPLATE_MATCH_THRESHOLD:
                cx = min_x + ml[0] + tpl.shape[1] / 2
                cy = min_y + ml[1] + tpl.shape[0] / 2
                dbg(f"find_under: template conf={mv:.2f} {round((time.perf_counter()-t0)*1000)}ms")
                return (cx, cy)

        big = cv2.resize(img, None, fx=OCR_UPSCALE, fy=OCR_UPSCALE,
                         interpolation=cv2.INTER_CUBIC)
        data = pytesseract.image_to_data(
            big, config=OCR_CFG_MULTI, output_type=pytesseract.Output.DICT)

        best = None
        best_score = 0.0
        for i, word in enumerate(data['text']):
            if not _is_under_word(word):
                continue
            wx = data['left'][i] / OCR_UPSCALE
            wy = data['top'][i] / OCR_UPSCALE
            ww = data['width'][i] / OCR_UPSCALE
            wh = data['height'][i] / OCR_UPSCALE
            score = float(data['conf'][i]) if data['conf'][i] != '-1' else 0.0
            if score >= best_score:
                best_score = score
                best = (wx, wy, ww, wh)

        if best is None:
            dbg(f"find_under: OCR miss {round((time.perf_counter()-t0)*1000)}ms")
            return None

        wx, wy, ww, wh = best
        pad = 2
        tx1 = max(0, int(wx) - pad); ty1 = max(0, int(wy) - pad)
        tx2 = min(img.shape[1], int(wx + ww) + pad)
        ty2 = min(img.shape[0], int(wy + wh) + pad)
        if tx2 > tx1 and ty2 > ty1 and (tx2 - tx1) >= 10 and (ty2 - ty1) >= 6:
            UNDER_TEMPLATE["img"] = img[ty1:ty2, tx1:tx2].copy()

        cx = min_x + wx + ww / 2
        cy = min_y + wy + wh / 2
        dbg(f"find_under: OCR hit conf={best_score:.0f} "
            f"{round((time.perf_counter()-t0)*1000)}ms")
        return (cx, cy)
    except Exception as e:
        log(f"[WARN] find_under error: {e}")
    return None


def do_click_xy(page, cx, cy):
    try:
        page.mouse.click(cx, cy, delay=random.randint(1, 3))
    except Exception as e:
        log(f"   Click error: {e}")
        return None, None
    return cx, cy


def do_click_box(page, box, ax, ay):
    x = ax + (box["x"] - ANCHOR_LEFT)
    y = ay + (box["y"] - ANCHOR_TOP)
    return do_click_xy(page, x + box["w"] / 2, y + box["h"] / 2)


def handle_wicket(page):
    global _last_wicket_time, UNDER_REL_POS, _wicket_click_count
    t0 = time.perf_counter()

    if time.time() - _last_wicket_time < WICKET_THROTTLE_S:
        dbg("wicket throttle")
        return False

    _wicket_click_count += 1
    need_verify = (VERIFY_EVERY > 0 and _wicket_click_count % VERIFY_EVERY == 0)

    if UNDER_REL_POS is not None and not need_verify:
        ax, ay = find_anchor_fast(page)
        if ax is not None:
            cx = ax + UNDER_REL_POS[0]
            cy = ay + UNDER_REL_POS[1]
            r = do_click_xy(page, cx, cy)
            if r[0] is not None:
                _last_wicket_time = time.time()
                ms = round((time.perf_counter() - t0) * 1000)
                log(f"[CLICK-INSTANT] UNDER @({cx:.0f},{cy:.0f}) [+{ms}ms]")
                return True
        UNDER_REL_POS = None

    ax, ay = find_anchor_fast(page)
    if ax is None:
        log("[NO-ANCHOR]")
        return False

    pos = _find_under_once(page, ax, ay)
    if pos is not None:
        new_rel = (pos[0] - ax, pos[1] - ay)
        if need_verify and UNDER_REL_POS is not None:
            drift = ((new_rel[0] - UNDER_REL_POS[0]) ** 2
                     + (new_rel[1] - UNDER_REL_POS[1]) ** 2) ** 0.5
            log(f"[VERIFY] drift={drift:.1f}px old={UNDER_REL_POS} new={new_rel}")
        UNDER_REL_POS = new_rel

        r = do_click_xy(page, pos[0], pos[1])
        if r[0] is not None:
            _last_wicket_time = time.time()
            ms = round((time.perf_counter() - t0) * 1000)
            log(f"[CLICK-SCAN] UNDER @({pos[0]:.0f},{pos[1]:.0f}) "
                f"rel={UNDER_REL_POS} [+{ms}ms]")
            return True

    under_box = next((t for t in MAPPED_TARGETS if t["side"] == "under"), None)
    if under_box:
        r = do_click_box(page, under_box, ax, ay)
        if r[0] is not None:
            _last_wicket_time = time.time()
            ms = round((time.perf_counter() - t0) * 1000)
            log(f"[FALLBACK] configured UNDER box [+{ms}ms]")
            return True

    log("[FAIL] no Under available")
    return False


def ball_info(d):
    ev = str(d.get("ev") or "").strip()
    ev_low = ev.lower()
    if not ev or is_intermediate(ev):
        return (ev, "intermediate", 0)
    if re.match(r'^(wicket|out|w)[.!]*$', ev_low):
        return (ev, "wicket", 0)
    if re.match(r'^(bowled|lbw|caught|c ?& ?b|run ?out|stumped?|hit ?wicket|'
                r'retired|handled|obstruct)', ev_low):
        return (ev, "wicket", 0)
    return (ev, "intermediate", 0)


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
                            dbg("event_q full - dropping")
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
    global _last_dedup_key

    if len(sys.argv) < 2:
        print("Usage: python clicker_wicket.py <match_key> [multi]")
        return
    match_filter = sys.argv[1].strip()
    mode = "single"
    if len(sys.argv) >= 3:
        arg2 = sys.argv[2].strip().lower()
        if arg2 == "multi":
            mode = "multi"
        elif arg2 in ("single", "strict"):
            mode = "single"

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

        match_url = (os.environ.get("CLICKER_MATCH_URL", "").strip()
                     or safe_input("Match URL: ", ""))
        if not match_url:
            log("[!] no match URL — abort")
            browser.close()
            return
        page.goto(match_url)
        log(f"Match page ready: {page.url}")

        print("\n" + "=" * 65)
        print("Go to browser and select 'Next Wicket Fall' market")
        print("Adjust red boxes: TOP = Over, BOTTOM = Under")
        print("Click 'DONE' when ready")
        print("=" * 65 + "\n")

        while not _stop_event.is_set():
            try:
                page.evaluate("() => { window.__pw_action = null; }")
                page.evaluate(SHOW_TARGETS_JS)
            except Exception as e:
                log(f"Calibration setup error: {e}")
                break
            print("Waiting for calibration (DONE)...")
            action = None
            while not _stop_event.is_set():
                try:
                    action = page.evaluate("() => window.__pw_action")
                except Exception as e:
                    log(f"Calibration poll error: {e}")
                    break
                if action == 'done':
                    break
                page.wait_for_timeout(200)
            if action == 'done':
                try:
                    final_targets = page.evaluate("() => window.__pw_final_targets")
                    setup_from_done(page, final_targets)
                except Exception as e:
                    log(f"Calibration save error: {e}")
            break

        if not MAPPED_TARGETS:
            log("No targets mapped - aborting.")
            browser.close()
            return

        log("[PREWARM] scanning for 'Under' button...")
        prewarm_under_position(page, retries=3)

        start_ws_thread()
        log(f"[WS] waiting... (match_key={match_filter}, mode={mode})")
        log(f"   v27 - FAST wicket click")
        if UNDER_REL_POS is not None:
            log(f"   Under prewarmed rel={UNDER_REL_POS} → first wicket INSTANT")
        else:
            log(f"   Under NOT prewarmed → first wicket will scan (~300ms)")
        log(f"   Throttle={WICKET_THROTTLE_S}s  Verify every {VERIFY_EVERY} clicks")

        _last_dedup_key = None

        try:
            while not _stop_event.is_set():
                check_pending_reset()

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
                    continue

                if is_intermediate(ev):
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

                log(f"[WICKET] over={m.get('over')} ball={m.get('ball')} "
                    f"ev='{text}'")
                handle_wicket(page)

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
