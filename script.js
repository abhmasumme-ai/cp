/* ========= CONFIG & STATE ========= */
const SERVED = location.protocol === "http:" || location.protocol === "https:";
const _urlParams = new URLSearchParams(location.search || "");
const WS_TOKEN = _urlParams.get("token") || "";
const OVERS_OVERRIDE = Number(_urlParams.get("overs")) || 0;

const API_BASE = SERVED ? location.origin : "http://127.0.0.1:5000";
const WS_URL = (SERVED
  ? (location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws"
  : "ws://127.0.0.1:5000/ws") + (WS_TOKEN ? `?token=${encodeURIComponent(WS_TOKEN)}` : "");

const TELEGRAM_ID = "@abc_trc123";
const MAX_LOG = 200;
const MAX_MS = 1000;
const RECONNECT_MS = 3000;
const RECONNECT_JITTER = 0.6;
const SETTINGS_KEY = "cricketPanelSettings";
const FALLBACK_QUIET_MS = 8000;
const FIXTURE_POLL_MS = 15000;
const BASELINE_RETRY_MS = 3000;
const BASELINE_MAX_TRIES = 5;
const WICKET_HOLD_MS = 700;
const FETCH_TIMEOUT_MS = 10000;
const PING_INTERVAL_MS = 30000;
const MAX_CACHED_MATCHES = 100;

const $ = id => document.getElementById(id);
const esc = s => String(s).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));

const EL = {
  score: $("score"), overTxt: $("overTxt"), team: $("team"),
  status: $("status"), ball: $("ball"),
  overNo: $("overNo"), overDots: $("overDots"),
  lgR: $("lgR"), lgB: $("lgB"),
  fixture: $("fixture"), terminal: $("terminal"),
  barClick: $("barClick"), barClickTxt: $("barClickTxt"),
  barOdds: $("barOdds"), barOddsTxt: $("barOddsTxt"),
};

const S = {
  started: false, ws: null, ping: null, reconnect: null,
  prevMs: null, label: "", matchKey: "", active: false, auto: false, idle: true,
  fmt: "", cur: null, isFreeHitNext: false, unk: new Set(),
  oversOverride: OVERS_OVERRIDE,
  pendingKey: "", pendingLabel: "", pendingFmt: "",
};

let _evSeq = 0;
let _baselineTimer = null;
let _baselineAbort = null;
const _dec = new TextDecoder();

/* ========= Logging ========= */
function log(msg, err) {
  const t = EL.terminal;
  if (!t) return;
  const li = document.createElement("li");
  const time = new Date().toLocaleTimeString();
  if (err) { li.className = "err"; li.textContent = `[${time}] ${msg}`; }
  else li.innerHTML = `[${time}] ${msg}`;
  const atBottom = t.scrollTop + t.clientHeight >= t.scrollHeight - 20;
  t.appendChild(li);
  while (t.children.length > MAX_LOG) t.firstChild.remove();
  if (atBottom) t.scrollTop = t.scrollHeight;
}
function matchLabel() {
  return S.label || "match";
}

/* ========= Bars ========= */
const BAR_MIN = 100, BAR_MAX = 5000;
function setBar(id, ms) {
  const el = id === "barClick" ? EL.barClick : EL.barOdds;
  const t = id === "barClick" ? EL.barClickTxt : EL.barOddsTxt;
  if (!el) return;
  const out = ms > BAR_MAX;
  const h = 20 + (Math.min(BAR_MAX, Math.max(BAR_MIN, ms)) - BAR_MIN) / (BAR_MAX - BAR_MIN) * 20;
  el.style.height = h + "px";
  if (t) {
    t.textContent = out ? "OUT TO LIMIT" : Math.round(ms) + "ms";
    t.style.color = out ? "var(--red)" : "";
  }
}

/* ========= Pie chart ========= */
const GREY = "#555";
let chart = null;

function setPie(vals, cols) {
  if (!chart) return;
  try {
    chart.data.datasets[0].data = vals;
    chart.data.datasets[0].backgroundColor = cols;
    chart.update("none");
  } catch (e) {}
}

let _pieData = { r: 0, b: 0, dirty: false };
let _pieRaf = 0;
let _pieLastMs = 0;

function pieOff() {
  _pieData = { r: 0, b: 0, dirty: false };
  if (_pieRaf) { cancelAnimationFrame(_pieRaf); _pieRaf = 0; }
  setPie([1, 1], [GREY, GREY]);
  if (EL.lgR) EL.lgR.textContent = "0ms";
  if (EL.lgB) EL.lgB.textContent = "0ms";
}

function _pieRenderNow() {
  _pieRaf = 0;
  if (!_pieData.dirty) return;
  if (!chart) return;
  if (document.visibilityState !== "visible") return;
  _pieData.dirty = false;
  _pieLastMs = performance.now();
  const r = Math.min(Math.max(0, _pieData.r), MAX_MS) || 1;
  const b = Math.min(Math.max(0, _pieData.b), MAX_MS) || 1;
  setPie([r, b], ["#ff2a2a", "#17f0e0"]);
}

function _pieScheduleRaf() {
  if (_pieRaf) return;
  _pieRaf = requestAnimationFrame(_pieRenderNow);
}

function pieOnFast(r, b) {
  _pieData.r = Math.max(0, r);
  _pieData.b = Math.max(0, b);
  _pieData.dirty = true;
  if (EL.lgR) EL.lgR.textContent = Math.round(_pieData.r) + "ms";
  if (EL.lgB) EL.lgB.textContent = Math.round(_pieData.b) + "ms";
  _pieScheduleRaf();
}

/* ========= Rings ========= */
const RINGS = [
  {key:"0.5 over", label:"0.5 OVER", color:"#e5102a", n:5, hl:"background:#bdbdf0;color:#000"},
  {key:"0.5 under", label:"0.5 UNDER", color:"#bdbdf0", n:3, hl:"background:#ff2a2a"},
  {key:"wicket in fall", label:"WICKET", color:"#e5102a", n:3, hl:"background:#ff2a2a"},
  {key:"2 run over", label:"2/3 OVER", color:"#e0962c", n:5, hl:"background:#17f0e0;color:#000"},
  {key:"4 run over", label:"4/6 OVER", color:"#e020e0", n:3, hl:"background:#16f055;color:#000"},
  {key:"batsman under", label:"BATSMAN", color:"#1ee0f5", n:3, hl:"background:#2a2af0"}
];
function initRings() {
  const container = $("rings");
  if (!container) return;
  container.innerHTML = "";
  RINGS.forEach(r => {
    const b = document.createElement("button");
    b.type = "button"; b.className = "ring";
    b.innerHTML = `${r.label}<svg viewBox="0 0 110 110"><circle cx="55" cy="55" r="38" fill="none" stroke="${r.color}" stroke-width="16" stroke-dasharray="190 50" transform="rotate(-30 55 55)"/><text x="55" y="65" text-anchor="middle">${r.n}</text></svg>`;
    b.onclick = () => clickAction(r);
    container.appendChild(b);
  });
}

/* ========= Fetch helpers ========= */
async function _fetchJson(url, opts) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), FETCH_TIMEOUT_MS);
  try {
    const res = await fetch(url, { ...(opts || {}), signal: ctrl.signal });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return await res.json();
  } finally { clearTimeout(timer); }
}
async function postApi(path, body) {
  const t0 = performance.now();
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), FETCH_TIMEOUT_MS);
  try {
    const res = await fetch(`${API_BASE}${path}`, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(body),
      signal: ctrl.signal,
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return performance.now() - t0;
  } finally { clearTimeout(timer); }
}

/* ★ NEW: postJson helper for clicker control */
async function postJson(path, body) {
  const ctrl = new AbortController();
  const t = setTimeout(() => ctrl.abort(), FETCH_TIMEOUT_MS);
  try {
    const res = await fetch(`${API_BASE}${path}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
      signal: ctrl.signal,
    });
    const j = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(j.detail || j.message || `HTTP ${res.status}`);
    return j;
  } finally { clearTimeout(t); }
}

/* ★ NEW: clicker kind map */
const CLICKER_KIND = {
  "Delivery Run Over/Under": "delivery",
  "Wicket in Fall":          "wicket",
  "Batsman Under":           "batsman",
  "Custom Selection":        "custom",
};

async function clickAction(r) {
  try {
    const ms = await postApi("/api/click", { action: r.key, telegram: TELEGRAM_ID, match: matchLabel() });
    setBar("barClick", ms);
    log(`clicked on <span class="hl" style="${r.hl}">${esc(r.key)}</span> on ${esc(matchLabel())}`);
  } catch (e) {
    log(`# ERROR click "${esc(r.key)}" failed: ${esc(e.message)}`, true);
  }
}

/* ========= Fixtures ========= */
let _warnedEmpty = false;
let _lastFixtureKeys = "";

async function loadFixtures(quiet) {
  try {
    const result = await _fetchJson(`${API_BASE}/api/matches`);
    let matches = [];
    if (result.data && result.data.matches) matches = result.data.matches;
    else if (result.matches) matches = result.matches;
    else if (Array.isArray(result)) matches = result;
    else if (result.data && Array.isArray(result.data)) matches = result.data;

    if (matches.length > 0) {
      const keys = matches.map(m => String(m.key || m.id || "")).join(",");
      if (keys !== _lastFixtureKeys) {
        renderMatches(matches);
        _lastFixtureKeys = keys;
        if (!quiet) log(`Loaded ${matches.length} matches into fixtures.`);
      }
      _warnedEmpty = false;
      _evictMatchCaches(matches);
    } else if (!_warnedEmpty) {
      _warnedEmpty = true;
      log("# Fixture empty: no data from push/REST yet");
    }
  } catch (err) {
    log(`# Error fetching fixtures: ${esc(err.message)}`, true);
  }
}

function _evictMatchCaches(matches) {
  const keep = new Set(matches.map(m => String(m.key || m.id || "")));
  keep.add(S.matchKey);
  keep.add(S.pendingKey);
  [MS, TEAMS, STATUS, START].forEach(store => {
    Object.keys(store).forEach(k => { if (!keep.has(k)) delete store[k]; });
  });
  if (Object.keys(MS).length > MAX_CACHED_MATCHES) {
    Object.keys(MS).forEach(k => { if (k !== S.matchKey && k !== S.pendingKey) delete MS[k]; });
  }
}

function renderMatches(matches) {
  const tbody = EL.fixture;
  if (!tbody) return;
  const order = { started: 0, not_started: 1, completed: 2, cancelled: 3 };
  matches = [...matches].sort((a, b) => (order[a.status] ?? 9) - (order[b.status] ?? 9));
  const scrollTop = tbody.parentElement ? tbody.parentElement.scrollTop : 0;

  let html = "";
  matches.forEach(m => {
    const matchId = String(m.key || m.id || "N/A");
    TEAMS[matchId] = { a: m.teams?.a?.name, b: m.teams?.b?.name };
    const title = m.title || m.name ||
      `${m.teams?.a?.name || "Team 1"} vs ${m.teams?.b?.name || "Team 2"}`;
    const format = String(m.format || m.type || "-").toUpperCase();
    const status = String(m.status || "not_started").toLowerCase();
    STATUS[matchId] = status;
    if (m.start_at) START[matchId] = m.start_at;
    const [stTxt, stCol] = statusView(status);
    const isActive = S.matchKey === matchId || S.pendingKey === matchId;
    const rowStyle = isActive ? "cursor:pointer;background:rgba(23,240,224,.08)" : "cursor:pointer";

    html += `<tr style="${rowStyle}" data-key="${esc(matchId)}" data-title="${esc(title)}" data-type="${esc(format)}" data-status="${esc(status)}">
      <td><code>${esc(matchId)}</code></td>
      <td><strong>${esc(title)}</strong></td>
      <td>${esc(format)}</td>
      <td><span style="color:${stCol}">${esc(stTxt)}</span></td>
    </tr>`;
  });
  tbody.innerHTML = html;
  if (tbody.parentElement) tbody.parentElement.scrollTop = scrollTop;

  tbody.querySelectorAll("tr").forEach(tr =>
    tr.addEventListener("click", () => selectMatch(tr.dataset.key, tr.dataset.title, tr.dataset.type))
  );
}

function highlightFixtureRow(key) {
  document.querySelectorAll("#fixture tr").forEach(tr => {
    if (tr.dataset.key === key) tr.style.background = "rgba(23,240,224,.08)";
    else if (tr.style.background && tr.style.background.includes("rgba(23,240,224")) tr.style.background = "";
  });
}

/* ========= Viewport blank state ========= */
function blankViewport() {
  if (EL.score) EL.score.textContent = "--";
  if (EL.overTxt) EL.overTxt.textContent = "-- OVER";
  if (EL.team) EL.team.textContent = "--";
  if ($("matchTeams")) $("matchTeams").textContent = "-- vs --";
  if (EL.status) EL.status.textContent = "Click ACTIVE to load";
  if (EL.overNo) EL.overNo.textContent = "-- OVER";
  if (EL.overDots) { EL.overDots.dataset.h = ""; EL.overDots.innerHTML = ""; }
  paintBall({ text: "--", kind: "msg" });
}

/* ========= Match selection / activation ========= */
function selectMatch(key, title, type) {
  S.pendingKey = key;
  S.pendingLabel = title;
  S.pendingFmt = type || "";

  if ($("matchKey")) $("matchKey").value = key;
  if ($("type")) $("type").value = type || "";

  if (S.matchKey === key && S.active) {
    log(`Selected: ${esc(title)} (already active)`);
    highlightFixtureRow(key);
    return;
  }

  S.matchKey = "";
  S.label = "";
  S.fmt = "";
  S.active = false;
  setActive(false);
  sendWatch();
  resetBall();
  blankViewport();

  log(`Selected: ${esc(title)} — click ACTIVE to load`);
  highlightFixtureRow(key);
}

function activateMatch() {
  if (!S.pendingKey) {
    log("# Select a match from Fixture first", true);
    return;
  }
  if (S.active && S.matchKey === S.pendingKey) {
    log("# Already active for this match");
    return;
  }

  S.matchKey = S.pendingKey;
  S.label = S.pendingLabel;
  S.fmt = S.pendingFmt;

  const st = ms(S.matchKey);
  st.dots = []; st.dotsOver = null; st.order = null; st.seen.clear();
  st.objTs = 0; st.lastCommId = null; st.evAt = 0;
  st.has = false; st.runs = 0; st.wk = 0; st.ov = "0.0"; st.bat = null;
  st.result = ""; st.toss = ""; st.brk = ""; st.sum = [];
  st.day = null; st.session = null; st.dayLine = ""; st.rawState = ""; st.maxOv = 0;
  if (st._dismissTimer) { clearTimeout(st._dismissTimer); st._dismissTimer = null; }
  S.isFreeHitNext = false;

  if ($("matchTeams")) $("matchTeams").textContent = S.label;

  resetBall();
  startAll();
  setActive(true);
  resetViewport();
  showStatus(S.matchKey);
  sendWatch();
  renderLive(S.matchKey);
  fetchBaseline(S.matchKey, 0);
  log(`Activated: ${esc(S.label)}`);
}

/* ========= WebSocket ========= */
function sendWatch() {
  if (S.ws && S.ws.readyState === WebSocket.OPEN) S.ws.send("watch:" + (S.matchKey || ""));
}

function _handleRawMessage(raw) {
  if (raw === "pong") return;
  let m;
  try { m = JSON.parse(raw); }
  catch { log("# Data parse error", true); return; }

  if (m.t === "reset") {
    log(`# reset ${esc(String(m.from || ""))} -> ${esc(String(m.to || ""))}`);
    return;
  }
  if (S.matchKey && m.id && String(m.id) !== S.matchKey) return;

  let delay = 10;
  if (m._server_ts) delay = Math.max(1, Math.round(Date.now() - m._server_ts));

  updateViewport(m);
  setBar("barOdds", delay);

  if (typeof m._lag_ms === "number") {
    pieOnFast(m._lag_ms, delay);
  }
}

function startLiveListener() {
  if (S.ws) { S.ws.onclose = null; try { S.ws.close(); } catch (e) {} }
  clearInterval(S.ping);
  clearTimeout(S.reconnect);

  S.ws = new WebSocket(WS_URL);
  S.ws.binaryType = "arraybuffer";

  S.ws.onopen = () => {
    log("# WebSocket connected.");
    clearInterval(S.ping);
    S.ping = setInterval(() => {
      if (S.ws && S.ws.readyState === WebSocket.OPEN) S.ws.send("ping");
    }, PING_INTERVAL_MS);
    sendWatch();
  };

  S.ws.onmessage = (event) => {
    const d = event.data;
    if (typeof d === "string") { _handleRawMessage(d); return; }
    if (d instanceof ArrayBuffer) {
      try { _handleRawMessage(_dec.decode(d)); }
      catch { log("# binary decode error", true); }
      return;
    }
    if (d instanceof Blob) {
      d.text().then(_handleRawMessage).catch(() => log("# blob decode error", true));
      return;
    }
  };

  S.ws.onerror = () => log("# WebSocket Error", true);
  S.ws.onclose = () => {
    clearInterval(S.ping);
    log("# WebSocket Disconnected");
    if (S.started) {
      const jitter = RECONNECT_MS * (0.7 + Math.random() * RECONNECT_JITTER);
      S.reconnect = setTimeout(startLiveListener, jitter);
    }
  };
}

function stopLiveListener() {
  clearInterval(S.ping);
  clearTimeout(S.reconnect);
  if (S.ws) {
    S.ws.onclose = null;
    try { S.ws.close(); } catch (e) {}
    S.ws = null;
    log("# Live Listener Stopped.");
  }
}

/* ========= Per-match state ========= */
const TEAMS = {};
const MS = {};
function ms(key) {
  return (MS[key] ||= {
    runs: 0, wk: 0, ov: "0.0", bat: null,
    seen: new Set(), has: false, lastBallAt: 0, evAt: 0, objTs: 0, lastCommId: null,
    phase: "cold",
    cur: null, curOv: null, curBall: null,
    timers: [], overNo: 0, dots: [], dotsOver: null, order: null,
    toss: "", result: "", sum: [], brk: "",
    day: null, session: null, dayLine: "", rawState: "", maxOv: 0,
    _dismissTimer: null,
  });
}

const BREAKS = [
  { re: /innings break/, label: "INNINGS BREAK" },
  { re: /lunch/,         label: "LUNCH BREAK" },
  { re: /\btea\b/,       label: "TEA BREAK" },
  { re: /dinner/,        label: "DINNER BREAK" },
  { re: /drink/,         label: "DRINKS BREAK" },
  { re: /stump/,         label: null },
  { re: /time ?out|strategic/, label: "TIME OUT", skipTest: true },
  { re: /\brain\b/,      label: "RAIN DELAY" },
  { re: /bad ?light/,    label: "BAD LIGHT" },
  { re: /delay|interrupt|suspend/, label: "PLAY DELAYED" }
];
const isTest = () => /test|first[\s-]?class/i.test(S.fmt || "");

function breakLabel(txt, st) {
  for (const b of BREAKS) {
    if (!b.re.test(txt)) continue;
    if (b.skipTest && isTest()) continue;
    return b.label ?? (st.day !== null ? `Day ${st.day}: Stumps` : "STUMPS");
  }
  return "";
}

function applyPlayState(m, key) {
  const st = ms(key);
  const dayN = Number(m.day);
  if (dayN > 0) st.day = dayN;
  const sesN = Number(m.session);
  if (sesN > 0) st.session = sesN;
  if (st.day !== null) st.dayLine = `Day ${st.day}${st.session === null ? "" : ` · Session ${st.session}`}`;

  const sig = String(m.game || "");
  if (!sig) return;
  if (sig !== st.rawState) { st.rawState = sig; log(`# play state: ${esc(sig)}`); }
  const brk = breakLabel(sig.toLowerCase().replace(/[_-]+/g, " "), st);
  st.brk = (brk && Date.now() - st.lastBallAt < 10000) ? "" : brk;
}

function normOv(ov) {
  const [o, b] = String(ov).split(".");
  const oi = parseInt(o, 10) || 0, bi = parseInt(b, 10) || 0;
  return bi >= 6 ? `${oi + 1}.0` : `${oi}.${bi}`;
}
const fmtOvers = ov => normOv(ov);
const ovVal = ov => {
  const [o, b] = String(ov).split(".");
  return (parseInt(o, 10) || 0) * 6 + (parseInt(b, 10) || 0);
};

function showStatus(key) {
  if (key !== S.matchKey) return;
  const st = ms(key);
  const el = EL.status;
  if (el) {
    const lines = [];
    if (st.toss && (st.has || STATUS[key] === "not_started")) lines.push(`<div>${esc(st.toss)}</div>`);
    if (STATUS[key] === "not_started" && START[key]) lines.push(`<div class="sub">Starts: ${esc(fmtStart(START[key]))}</div>`);
    if (STATUS[key] === "completed") (st.sum || []).forEach(t => lines.push(`<div class="sub">${esc(t)}</div>`));
    if (isTest() && st.dayLine && !st.result) lines.push(`<div class="sub">${esc(st.dayLine)}</div>`);
    el.innerHTML = lines.join("");
  }
  renderBall(key);
}

function renderLive(key) {
  const st = ms(key);
  const teams = TEAMS[key] || {};
  const aName = teams.a || "Team A";
  const bName = teams.b || "Team B";
  const status = STATUS[key] || "started";

  if (EL.team) {
    if (st.has && st.bat) EL.team.textContent = teams[st.bat] || "--";
    else if (status === "not_started") EL.team.textContent = `${aName} vs ${bName}`;
    else EL.team.textContent = aName !== "Team A" ? `${aName} vs ${bName}` : "--";
  }

  if (EL.score) {
    if (st.has) EL.score.textContent = `${st.runs}-${st.wk}`;
    else if (status === "not_started") EL.score.textContent = "UPCOMING";
    else if (status === "completed") EL.score.textContent = "FINISHED";
    else EL.score.textContent = "--";
  }

  if (EL.overTxt) {
    if (st.has) EL.overTxt.textContent = `${st.ov} OVER`;
    else EL.overTxt.textContent = "-- OVER";
  }
}

/* ========= Ball box ========= */
function paintBall(info) {
  const el = EL.ball;
  if (!el) return;
  const k = info.idle ? "idle" : `${info.kind || ""}|${info.text}|${_evSeq}`;
  if (el.dataset.k === k) return;
  if (info.idle && el.dataset.k === "idle") return;
  el.dataset.k = k;
  S.cur = info;

  el.style.animation = "none";
  el.style.background = "";
  el.style.color = "";

  if (info.idle) {
    el.className = "ball idle";
    el.innerHTML = `<span>Ball<i>.</i><i>.</i><i>.</i><i>.</i></span>`;
  } else {
    el.className = `ball ${info.kind || "raw"}`;
    el.innerHTML = `<span>${esc(info.text)}</span>`;
  }

  void el.offsetHeight;
  el.style.animation = "";
}

function maxOvers() {
  if (S.oversOverride > 0) return S.oversOverride;
  const mo = S.matchKey ? ms(S.matchKey).maxOv : 0;
  if (mo > 0) return mo;
  if (/T10/i.test(S.fmt)) return 10;
  if (/T20/i.test(S.fmt)) return 20;
  if (/ONE[\s_-]?DAY|ODI/i.test(S.fmt)) return 50;
  return 0;
}
function inningsEnded(st, prov) {
  const wk = prov ? prov.wk : st.wk;
  const ov = prov ? prov.ov : st.ov;
  if (wk >= 10) return true;
  const mo = maxOvers();
  return mo > 0 && ovVal(ov) >= mo * 6;
}
function breakApplies(st) {
  if (isTest()) return st.order == null || st.order < 4;
  return st.order == null || st.order === 1;
}

function ballView(key) {
  const st = ms(key);
  if (st.result) return { text: st.result, kind: "msg" };
  if (st.brk) return { text: st.brk, kind: "brk" };
  switch (st.phase) {
    case "wait":    return { idle: true };
    case "freehit": return { text: "FREE HIT", kind: "freehit" };
    case "result":  if (st.cur) return st.cur; break;
    case "over":    return { text: `${st.overNo} OVER COMPLETE`, kind: "overdone" };
    case "inn":     return { text: "INNINGS BREAK", kind: "brk" };
  }
  if (STATUS[key] === "not_started" && !st.has) return { text: "UPCOMING", kind: "msg" };
  if (STATUS[key] === "completed") return { text: "MATCH FINISHED", kind: "msg" };
  if (!st.has && st.toss) return { text: st.toss, kind: "msg" };
  return { text: "--", kind: "msg" };
}

function renderBall(key) {
  if (!S.matchKey || key !== S.matchKey) return;
  paintBall(ballView(key));
  renderDots(key);
}
function setBallIdle() {
  if (!S.matchKey) { paintBall({ text: "--", kind: "msg" }); renderDots(null); return; }
  renderBall(S.matchKey);
}
function clearBallTimers(st) { (st.timers || []).forEach(clearTimeout); st.timers = []; }
function resetBall() {
  const st = S.matchKey ? ms(S.matchKey) : null;
  if (st) {
    clearBallTimers(st);
    if (st._dismissTimer) { clearTimeout(st._dismissTimer); st._dismissTimer = null; }
    st.phase = "cold"; st.cur = null; st.curOv = null; st.curBall = null;
  }
  setBallIdle();
}

/* ========= Over dots ========= */
function dotEntry(info, legal) {
  let t = info.chipText || info.text, c = "r";
  switch (info.kind) {
    case "wicket":  t = info.chipText || "W"; c = "w"; break;
    case "six":     t = "6"; c = "s"; break;
    case "four":    t = "4"; c = "f"; break;
    case "noball":  t = "NB"; c = "x"; break;
    case "wide":    t = "WD"; c = "x"; break;
    case "dot":     t = "0"; c = "d"; break;
    case "raw":
      if (String(info.text) === "0") { t = "0"; c = "d"; }
      break;
  }
  return { t, c, legal };
}

function makeDotId(ovIdx, ballNo, legal, codeOrKey, xballNo) {
  if (legal) return `p${ovIdx}.${ballNo}`;
  const xb = Number(xballNo);
  if (!isNaN(xb) && xb > 0) return `p${ovIdx}.x${xb}`;
  const key = String(codeOrKey || "").replace(/\s+/g, "_").slice(0, 24);
  return `p${ovIdx}.${ballNo}_${key || Math.random().toString(36).slice(2, 10)}`;
}

function ordinal(n) {
  const v = n % 100;
  if (v >= 11 && v <= 13) return n + "th";
  return n + (["th", "st", "nd", "rd"][n % 10] || "th");
}
function curOverNo(st) {
  if (st.dotsOver != null) return Number(st.dotsOver) + 1;
  return Math.floor(ovVal(st.ov) / 6) + 1;
}
function renderDots(key) {
  const no = EL.overNo;
  if (no) {
    const t = key && isLiveKey(key) ? `${ordinal(curOverNo(ms(key)))} OVER` : "-- OVER";
    if (no.textContent !== t) no.textContent = t;
  }
  const el = EL.overDots;
  if (!el) return;
  const dots = key && isLiveKey(key) ? ms(key).dots : [];
  const legalCount = dots.filter(x => x.legal).length;
  let h = dots.map(x => `<span class="od ${x.c}">${esc(x.t)}</span>`).join("");
  for (let i = legalCount; i < 6; i++) h += `<span class="od e">&middot;</span>`;
  if (el.dataset.h !== h) { el.dataset.h = h; el.innerHTML = h; }
}

/* ========= Parser ========= */
const DISMISS_RE = /^(bowled|lbw|caught|c ?& ?b|caught behind|caught and bowled|run ?out|stumped?|hit ?wicket|retired|handled|obstruct|timed ?out|hit the ball twice)/i;

function wicketChipText(e) {
  const r = Number(e.run) || 0;
  return r > 0 ? `W+${r}` : "W";
}

function resultFromEvent(low, ev) {
  if (/^(dot|0)( ball)?[.!]*$/.test(low)) return { legal: true, info: { text: "0", kind: "dot" } };
  const num = /^(\d+)(?:\s+(?:runs?))?[.!]*$/.exec(low);
  if (num) {
    const n = Number(num[1]);
    if (n === 4) return { legal: true, info: { text: "4", kind: "four" } };
    if (n === 6) return { legal: true, info: { text: "6", kind: "six" } };
    return { legal: true, info: { text: String(n), kind: "run" } };
  }
  if (/^four/.test(low)) return { legal: true, info: { text: "4", kind: "four" } };
  if (/^six/.test(low))  return { legal: true, info: { text: "6", kind: "six" } };
  if (/^(wicket|out|w)[.!]*$/.test(low)) return { legal: true, info: { text: "W", kind: "wicket" } };
  if (DISMISS_RE.test(low)) return { legal: true, info: { text: "W", kind: "wicket" } };
  if (/^wide/.test(low)) return { legal: false, info: { text: "WD", kind: "wide" } };
  if (/^no[\s_-]?ball|^nb\b/.test(low)) return { legal: false, info: { text: "NB", kind: "noball" } };
  if (/^leg[\s_-]?bye/i.test(low)) {
    const m = /(\d+)/.exec(ev);
    const cnt = m ? m[1] : "1";
    return { legal: true, info: { text: "Legbye", kind: "raw", chipText: `${cnt}LB` } };
  }
  if (/^bye/i.test(low)) {
    const m = /(\d+)/.exec(ev);
    const cnt = m ? m[1] : "1";
    return { legal: true, info: { text: "bye", kind: "raw", chipText: `${cnt}B` } };
  }
  return null;
}

function wicketLabel(t) {
  const s = t ? String(t).replace(/[_-]+/g, " ").trim().toUpperCase() : "";
  if (s === "RUNOUT") return "RUN OUT";
  if (s === "HITWICKET") return "HIT WICKET";
  if (s === "C & B" || s === "C AND B") return "CAUGHT & BOWLED";
  return s;
}

function classifyEvent(ev) {
  const low = ev.toLowerCase().trim();
  if (/^ball\b/i.test(low) && /\b(chalu|start|chalo|shuru|shuro)\b/i.test(low)) return { idle: true };
  if (/^confirm(ing)?\b|^checking\b/i.test(low)) return { text: "Confirming", kind: "raw" };
  if (/^no[\s_-]?ball|^nb\b/.test(low)) return { text: "NB", kind: "noball" };
  if (DISMISS_RE.test(low)) return { text: "W", kind: "wicket", sub: wicketLabel(ev) };
  if (/^(wicket|out|w)[.!]*$/.test(low)) return { text: "W", kind: "wicket" };
  if (/^four/.test(low) || /^4(?:\s+(?:runs?))?[.!]*$/.test(low)) return { text: "4", kind: "four" };
  if (/^six/.test(low) || /^6(?:\s+(?:runs?))?[.!]*$/.test(low)) return { text: "6", kind: "six" };
  if (/^(dot|0)( ball)?[.!]*$/.test(low)) return { text: "0", kind: "dot" };
  if (/^wide/.test(low)) return { text: "WIDE", kind: "wide" };
  if (/^leg[\s_-]?bye/i.test(low)) return { text: "legbye", kind: "raw" };
  if (/^bye/i.test(low)) return { text: "bye", kind: "raw" };
  const num = /^(\d+)(?:\s+(?:runs?))?[.!]*$/.exec(low);
  if (num) return { text: num[1], kind: "raw" };
  return { text: ev, kind: "raw" };
}

function addWicketToChip(st, rawOv, rawBall, code, xballNo) {
  let ovIdx = null, ballNo = null;

  if (rawOv != null && String(rawOv).includes(".")) {
    const parts = String(rawOv).split(".");
    ovIdx = parseInt(parts[0], 10);
    ballNo = parseInt(parts[1], 10);
  } else if (rawBall != null) {
    if (st.dotsOver != null) ovIdx = st.dotsOver;
    else if (rawOv != null && !isNaN(parseInt(rawOv, 10))) ovIdx = parseInt(rawOv, 10);
    ballNo = parseInt(rawBall, 10);
  }

  if (isNaN(ovIdx) || isNaN(ballNo)) return;

  if (st.dotsOver == null || ovIdx > st.dotsOver) {
    st.dots = [];
    st.dotsOver = ovIdx;
  }
  if (ovIdx === st.dotsOver) {
    const dotId = makeDotId(ovIdx, ballNo, true, code || "W", xballNo);
    const existing = st.dots.findIndex(d => d.id === dotId);
    const entry = { id: dotId, ballNo, xball: xballNo, ...dotEntry({ text: "W", kind: "wicket" }, true) };
    if (existing >= 0) {
      st.dots[existing] = entry;
    } else {
      const legalCount = st.dots.filter(x => x.legal).length;
      if (legalCount < ballNo) {
        st.dots.push(entry);
      } else {
        let seen = 0, replaced = false;
        for (let i = 0; i < st.dots.length; i++) {
          if (st.dots[i].legal) {
            seen++;
            if (seen === ballNo) { st.dots[i] = entry; replaced = true; break; }
          }
        }
        if (!replaced) st.dots.push(entry);
      }
    }
  }
}

/* ========= Ball event handler ========= */
function handleEvent(key, e) {
  if (!S.matchKey || key !== S.matchKey) return;
  const st = ms(key);
  const ev = String(e.ev || "").trim();
  if (!ev) return;

  const info = classifyEvent(ev);

  if (info.idle) {
    if (st._dismissTimer) { clearTimeout(st._dismissTimer); st._dismissTimer = null; }
    st.phase = "wait"; st.cur = null; st.curOv = null; st.curBall = null; st.brk = "";
    renderBall(key);
    return;
  }

  if (info.kind === "wicket") {
    if (st.cur && st.cur.kind === "wicket" && info.sub) {
      if (st._dismissTimer) { clearTimeout(st._dismissTimer); st._dismissTimer = null; }
      _evSeq++;
      st.cur = { text: info.sub, kind: "raw" };
      renderBall(key);
      return;
    }

    _evSeq++;
    st.lastBallAt = Date.now();
    st.evAt = Date.now();
    st.brk = "";

    const rawOv = e.over != null ? String(e.over) : "";
    const ballRef = e.ball != null ? e.ball : e.xball;
    addWicketToChip(st, rawOv, ballRef, e.code, e.xball);

    st.phase = "result";
    st.cur = { text: "W", kind: "wicket" };
    const ovIdx = parseInt(rawOv, 10);
    const ballNo = rawOv.includes(".")
      ? parseInt(rawOv.split(".")[1], 10)
      : (ballRef != null ? parseInt(ballRef, 10) : NaN);
    st.curOv = !isNaN(ovIdx) ? ovIdx : null;
    st.curBall = !isNaN(ballNo) ? ballNo : null;

    if (e.batsman_out) {
      log(`# out: <b>${esc(e.batsman_out)}</b>${e.bowler ? ` (b ${esc(e.bowler)})` : ""}`);
    } else if (e.striker) {
      log(`# wicket :: striker ${esc(e.striker)}${e.bowler ? ` / bowler ${esc(e.bowler)}` : ""}`);
    }

    renderBall(key);

    if (info.sub) {
      if (st._dismissTimer) clearTimeout(st._dismissTimer);
      const sub = info.sub;
      st._dismissTimer = setTimeout(() => {
        st._dismissTimer = null;
        if (st.cur && st.cur.kind === "wicket") {
          _evSeq++;
          st.cur = { text: sub, kind: "raw" };
          renderBall(key);
        }
      }, WICKET_HOLD_MS);
    }
    return;
  }

  if (st._dismissTimer) { clearTimeout(st._dismissTimer); st._dismissTimer = null; }

  _evSeq++;
  st.lastBallAt = Date.now();
  st.evAt = Date.now();
  st.brk = "";

  const rawOv = e.over != null ? String(e.over) : "";
  const ballRef = e.ball != null ? e.ball : e.xball;
  const low = ev.toLowerCase();
  const parsed = resultFromEvent(low, ev);
  let ovIdx = parseInt(rawOv, 10);
  let ballNo = rawOv.includes(".")
    ? parseInt(rawOv.split(".")[1], 10)
    : (ballRef != null ? parseInt(ballRef, 10) : NaN);

  if (isNaN(ovIdx) && st.dotsOver != null) ovIdx = Number(st.dotsOver);
  if (isNaN(ovIdx) && st.ov) ovIdx = Math.floor(ovVal(st.ov) / 6);
  if (isNaN(ballNo)) {
    const legalCount = st.dots.filter(x => x.legal).length;
    ballNo = legalCount + (parsed && parsed.legal ? 1 : 0);
    if (ballNo < 1) ballNo = 1;
  }

  if (parsed && !isNaN(ovIdx) && !isNaN(ballNo)) {
    if (st.dotsOver == null || ovIdx > st.dotsOver) {
      st.dots = [];
      st.dotsOver = ovIdx;
    }
    if (ovIdx === st.dotsOver) {
      const dotId = makeDotId(ovIdx, ballNo, parsed.legal, e.code || ev, e.xball);
      const existing = st.dots.findIndex(d => d.id === dotId);
      const entry = { id: dotId, ballNo, xball: e.xball, ...dotEntry(parsed.info, parsed.legal) };
      if (existing >= 0) {
        const old = st.dots[existing];
        st.dots[existing] = entry;
        if (old.c === "w" && entry.c !== "w") {
          log(`# wicket corrected at ${esc(rawOv)}: "${esc(ev)}"`);
        } else if (old.t !== entry.t) {
          log(`# correction ball ${esc(rawOv)}: "${esc(ev)}"`);
        }
      } else {
        const legalCount = st.dots.filter(x => x.legal).length;
        if (parsed.legal ? (legalCount < ballNo) : true) st.dots.push(entry);
      }
    }
  }

  st.phase = "result";
  st.cur = { text: info.text, kind: info.kind };
  st.curOv = !isNaN(ovIdx) ? ovIdx : null;
  st.curBall = !isNaN(ballNo) ? ballNo : null;
  renderBall(key);
}

/* ========= Match object handler ========= */
function newInnings(st) {
  st.wk = 0; st.runs = 0; st.ov = "0.0"; st.has = false;
  st.result = ""; st.brk = "";
  st.seen.clear(); st.dots = []; st.dotsOver = null; st.lastCommId = null;
  st.curOv = null; st.curBall = null;
  if (st._dismissTimer) { clearTimeout(st._dismissTimer); st._dismissTimer = null; }
  S.isFreeHitNext = false;
}

function infoFromComm(e) {
  if (e.w) {
    return {
      text: "W",
      kind: "wicket",
      chipText: wicketChipText(e),
      sub: e.dis ? wicketLabel(e.dis) : ""
    };
  }
  if (e.nb > 0) return { text: e.bat > 0 ? `NO BALL +${e.bat}` : "NO BALL", kind: "noball" };
  if (e.wd > 0) {
    const wr = Math.max(0, e.run - 1);
    return { text: wr > 0 ? `WIDE +${wr}` : "WIDE", kind: "wide" };
  }
  if (e.six)  return { text: "6", kind: "six" };
  if (e.four) return { text: "4", kind: "four" };
  if (e.by > 0) return { text: "bye", kind: "raw", chipText: `${e.by}B` };
  if (e.lb > 0) return { text: "legbye", kind: "raw", chipText: `${e.lb}LB` };
  if (e.run === 0) return { text: "0", kind: "dot" };
  return { text: String(e.run), kind: "raw" };
}
const commLegal = e => !(e.wd > 0 || e.nb > 0);

function applyComm(key, m) {
  const st = ms(key);
  const list = (m.comm || []).slice().sort((a, b) => {
    const na = Number(a.id), nb = Number(b.id);
    if (!isNaN(na) && !isNaN(nb)) return na - nb;
    return String(a.id || "").localeCompare(String(b.id || ""));
  });
  if (!list.length) return;

  const maxOv = Math.max(...list.map(e => e.ov));
  const commLegalCount = list.filter(e => e.ov === maxOv && commLegal(e)).length;
  const chipLegalCount = st.dots.filter(x => x.legal).length;
  const chipOver = st.dotsOver;

  let hasValueDiff = false;
  if (chipOver === maxOv) {
    const chipByBall = {};
    st.dots.forEach(d => {
      if (!d.legal || d.ballNo == null) return;
      chipByBall[d.ballNo] = d;
    });
    for (const e of list) {
      if (e.ov !== maxOv || !commLegal(e)) continue;
      const cb = chipByBall[e.b];
      if (cb) {
        const expect = e.w ? wicketChipText(e)
                           : (e.six ? "6" : e.four ? "4" : String(e.run));
        if (cb.t !== expect) { hasValueDiff = true; break; }
      }
    }
  }

  const shouldRebuild = (chipOver == null
    || maxOv > chipOver
    || (maxOv === chipOver && commLegalCount > chipLegalCount)
    || hasValueDiff);

  if (shouldRebuild) {
    const prevWs = (st.dotsOver === maxOv)
      ? st.dots.filter(d => d.c === "w")
      : [];

    st.dots = list.filter(e => e.ov === maxOv)
      .map(e => ({
        id: makeDotId(e.ov, e.b, commLegal(e), e.id, e.x),
        ballNo: e.b,
        xball: e.x,
        ord: e.x || 0,
        ...dotEntry(infoFromComm(e), commLegal(e))
      }));

    prevWs.forEach(w => {
      const idx = st.dots.findIndex(d => d.id === w.id);
      if (idx >= 0) {
        if (st.dots[idx].c !== "w") {
          log(`# wicket corrected by commentary at ${w.id}`);
        }
      } else {
        const ballNo = (w.ballNo != null) ? w.ballNo : NaN;
        let insertAt = st.dots.length;
        if (!isNaN(ballNo) && ballNo > 0) {
          let seen = 0;
          for (let i = 0; i < st.dots.length; i++) {
            if (st.dots[i].legal) {
              seen++;
              if (seen >= ballNo) { insertAt = i; break; }
            }
          }
        }
        st.dots.splice(insertAt, 0, w);
      }
    });

    st.dotsOver = maxOv;

    if (st.phase === "result" && st.cur
        && st.cur.text !== "legbye"
        && st.cur.text !== "bye"
        && st.curOv !== null && st.curBall !== null) {
      const match = list.find(e => e.ov === st.curOv && e.b === st.curBall);
      if (match) {
        const chipInfo = infoFromComm(match);
        if (chipInfo.text.toLowerCase() !== st.cur.text.toLowerCase()) {
          _evSeq++;
          st.cur = chipInfo;
          st.lastBallAt = Date.now();
          renderBall(key);
          log(`# commentary sync ball ${st.curOv}.${st.curBall}: "${esc(st.cur.text)}"`);
        }
      }
    }
  }

  const newest = list[list.length - 1];
  const isNewBall = newest.id !== st.lastCommId;
  st.lastCommId = newest.id;

  if (isNewBall && !isTest()) {
    if (newest.nb > 0) S.isFreeHitNext = true;
    else if (newest.wd === 0) S.isFreeHitNext = false;
  }

  if (isNewBall && (!st.evAt || Date.now() - st.evAt > FALLBACK_QUIET_MS)) {
    _evSeq++;
    const info = infoFromComm(newest);
    st.lastBallAt = Date.now();
    st.brk = "";
    st.cur = info;
    st.curOv = newest.ov;
    st.curBall = newest.b;
    st.phase = "result";
    renderBall(key);

    if (info.kind === "wicket" && info.sub) {
      if (st._dismissTimer) clearTimeout(st._dismissTimer);
      const sub = info.sub;
      st._dismissTimer = setTimeout(() => {
        st._dismissTimer = null;
        if (st.cur && st.cur.kind === "wicket") {
          _evSeq++;
          st.cur = { text: sub, kind: "raw" };
          renderBall(key);
        }
      }, WICKET_HOLD_MS);
    }
  }
}

function applySummary(key, m) {
  const st = ms(key);
  const parts = [];
  let last = null;
  ["a", "b"].forEach(side => {
    const t = m[side];
    if (!t || !t.sc) return;
    parts.push(`${t.name || side.toUpperCase()} ${String(t.sc).replace(/^\*/, "")}`);
    const re = /(\d+)\s*\/\s*(\d+)\s*\(\s*([\d.]+)\s*ov/gi;
    let mm, lm = null;
    while ((mm = re.exec(t.sc))) lm = mm;
    if (lm) last = { side, runs: Number(lm[1]), wk: Number(lm[2]), ov: fmtOvers(lm[3]) };
  });
  st.sum = parts;
  if (last && !(m.inn > 0 && m.score)) {
    st.runs = last.runs; st.wk = last.wk; st.ov = last.ov;
    st.bat = last.side; st.has = true;
  }
}

function handleMatch(key, m) {
  if (!key || (m.id && String(m.id) !== key)) return;
  const st = ms(key);
  if (m.ts && st.objTs && m.ts < st.objTs) return;
  if (m.ts) st.objTs = m.ts;

  if (m.a || m.b) TEAMS[key] = { a: m.a?.name || TEAMS[key]?.a, b: m.b?.name || TEAMS[key]?.b };
  if (m.fmt && !S.fmt) { S.fmt = m.fmt; if ($("type")) $("type").value = m.fmt; }
  if (m.start) START[key] = m.start;
  if (m.status) setMatchStatus(key, m.status);

  const mo = Number(m.max) || 0;
  if (mo > 0 && mo !== st.maxOv) { st.maxOv = mo; log(`# overs limit: ${mo}`); }

  if (m.toss) st.toss = String(m.toss);
  if (m.result) {
    if (m.result !== st.result && key === S.matchKey) log(`# result: ${esc(m.result)}`);
    st.result = String(m.result);
  }
  applySummary(key, m);
  applyPlayState(m, key);

  const inn = Number(m.inn) || 0;
  if (inn > 0 && m.score) {
    if (st.order != null && inn !== st.order) newInnings(st);
    st.order = inn;
    st.runs = m.score.runs;
    st.wk = m.score.wk;
    st.ov = fmtOvers(m.score.ov);
    if (m.bat) st.bat = m.bat;
    st.has = true;
  }

  applyComm(key, m);
  showStatus(key);
  renderLive(key);
}

/* ========= Match status ========= */
const STATUS = {};
const START = {};
const STATUS_VIEW = {
  started:     ["LIVE",     "var(--green)"],
  not_started: ["UPCOMING", "var(--yellow)"],
  completed:   ["FINISHED", "var(--text-muted)"],
  cancelled:   ["CANCELLED", "var(--red)"]
};
const statusView = s => STATUS_VIEW[s] || [String(s || "-").toUpperCase(), "var(--cyan)"];
const isLiveKey = key => !STATUS[key] || STATUS[key] === "started";

function fmtStart(v) {
  const n = Number(v);
  if (!n) return String(v);
  const d = new Date(n > 1e11 ? n : n * 1000);
  return isNaN(d) ? String(v) : d.toLocaleString();
}
function setMatchStatus(key, s) {
  s = String(s || "").toLowerCase();
  if (!STATUS_VIEW[s] || STATUS[key] === s) return;
  STATUS[key] = s;
  const [txt, col] = STATUS_VIEW[s];
  document.querySelectorAll("#fixture tr").forEach(tr => {
    if (tr.dataset.key !== key) return;
    tr.dataset.status = s;
    const sp = tr.querySelector("td:last-child span");
    if (sp) { sp.textContent = txt; sp.style.color = col; }
  });
  if (key === S.matchKey) log(`# status: ${esc(txt)}`);
}

function resetViewport() {
  if (EL.score) EL.score.textContent = "0-0";
  if (EL.overTxt) EL.overTxt.textContent = "0.0 OVER";
  if (EL.team) EL.team.textContent = "--";
  if (EL.status) EL.status.innerHTML = "";
}

async function fetchBaseline(key, tries) {
  tries = tries || 0;
  if (!S.matchKey || key !== S.matchKey) return;
  if (_baselineTimer) { clearTimeout(_baselineTimer); _baselineTimer = null; }
  if (_baselineAbort) { _baselineAbort.abort(); _baselineAbort = null; }
  const ctrl = new AbortController();
  _baselineAbort = ctrl;
  const timer = setTimeout(() => ctrl.abort(), FETCH_TIMEOUT_MS);
  try {
    const res = await fetch(`${API_BASE}/api/match/${encodeURIComponent(key)}`, { signal: ctrl.signal });
    clearTimeout(timer);
    if (!res.ok) {
      const errMsg = res.status === 404 ? "backend cache miss (set REST_TOKEN)" : `backend HTTP ${res.status}`;
      log(`# baseline fail: ${errMsg}`, true);
      if (tries < BASELINE_MAX_TRIES) _baselineTimer = setTimeout(() => fetchBaseline(key, tries + 1), BASELINE_RETRY_MS);
      return;
    }
    const json = await res.json();
    if (!json.data) { log(`# baseline empty for ${esc(key)}`, true); return; }
    if (key === S.matchKey) {
      log(`# baseline loaded (${tries} retries)`);
      handleMatch(key, json.data);
    }
  } catch (e) {
    clearTimeout(timer);
    if (e.name === "AbortError") return;
    log(`# baseline error: ${esc(e.message)}`, true);
  } finally {
    if (_baselineAbort === ctrl) _baselineAbort = null;
  }
}

let _hinted = false;
function updateViewport(m) {
  if (!S.matchKey) {
    if (S.pendingKey) return;
    if (!_hinted) { log("# Select a match from Fixture."); _hinted = true; }
    if (EL.status) EL.status.textContent = "Select a match from Fixture";
    setBallIdle();
    return;
  }
  if (m.id && String(m.id) !== S.matchKey) return;
  if (m.t === "ev") handleEvent(S.matchKey, m);
  else if (m.t === "match") handleMatch(S.matchKey, m);
}

/* ========= Controls ========= */
function startAll() {
  if (S.started) return;
  S.started = true;
  startLiveListener();
  log("# Live listener started.");
}
function stopAll() {
  S.started = false;
  stopLiveListener();
  pieOff();
  S.prevMs = null;
  if (_baselineTimer) { clearTimeout(_baselineTimer); _baselineTimer = null; }
  if (_baselineAbort) { _baselineAbort.abort(); _baselineAbort = null; }
  setActive(false);
  resetBall();
  log("# Live listener stopped.");
}

function setActive(on) {
  const b = $("activeBtn");
  if (!b) return;
  S.active = !!on;
  b.classList.toggle("on", !!on);
  b.textContent = on ? "ACTIVE MATCH" : "ACTIVE";
}

/* ========= Dropdown settings persistence ========= */
function _readSettings() {
  try { return JSON.parse(localStorage.getItem(SETTINGS_KEY) || '{}'); }
  catch (e) { return {}; }
}
function _writeSettings(data) {
  try { localStorage.setItem(SETTINGS_KEY, JSON.stringify(data)); } catch (e) {}
}
function saveDropdownSetting(name, url, percent) {
  const data = _readSettings();
  data[name] = { url, percent, ts: Date.now() };
  _writeSettings(data);
}
function removeDropdownSetting(name) {
  const data = _readSettings();
  delete data[name];
  _writeSettings(data);
}
function loadDropdownSettings() {
  const data = _readSettings();
  document.querySelectorAll(".option-group").forEach(group => {
    const name = group.dataset.group;
    if (data[name]?.url) {
      const inp = group.querySelector(".url-input");
      if (inp) inp.value = data[name].url;
    }
  });
}

/* ========= Init ========= */
window.addEventListener("DOMContentLoaded", () => {
  initRings();

  const canvasEl = $("latChart");
  if (canvasEl && window.Chart) {
    chart = new Chart(canvasEl, {
      type: "pie",
      data: {
        labels: ["Latency A", "Latency B"],
        datasets: [{ data: [1, 1], backgroundColor: [GREY, GREY], borderColor: "#fff", borderWidth: 1 }]
      },
      options: { responsive: false, animation: false, plugins: { legend: { display: false }, tooltip: { enabled: false } } }
    });
  } else if (canvasEl && !window.Chart) {
    log("# Chart.js not loaded — pie chart disabled", true);
  }

  $("activeBtn")?.addEventListener("click", activateMatch);

  document.querySelectorAll("input.radio-look[data-target]").forEach(cb => {
    cb.addEventListener("change", () => {
      const target = document.getElementById(cb.dataset.target);
      if (!target) return;
      if (cb.checked) {
        target.hidden = false;
        document.querySelectorAll("input.radio-look[data-target]").forEach(other => {
          if (other === cb) return;
          other.checked = false;
          const ot = document.getElementById(other.dataset.target);
          if (ot) ot.hidden = true;
        });
      } else {
        target.hidden = true;
      }
    });
  });

  /* ★ PATCHED: clicker control buttons */
  document.querySelectorAll(".dropdown").forEach(dd => {
    const group = dd.closest(".option-group");
    const groupName = group?.dataset.group || "";
    const kind = CLICKER_KIND[groupName];
    const urlInput = dd.querySelector(".url-input");

    if (urlInput) {
      urlInput.addEventListener("input", () => urlInput.classList.remove("need"));
    }

    dd.querySelectorAll(".btns button").forEach(btn => {
      btn.addEventListener("click", async (e) => {
        e.preventDefault();

        if (btn.classList.contains("submit")) {
          if (!S.active || !S.matchKey) {
            log("# Activate a match first, then start the clicker", true);
            return;
          }
          const siteUrl = ($("site")?.value || "").trim();
          if (!siteUrl) {
            log("# Website URL required", true);
            $("site")?.classList.add("need");
            return;
          }
          const url = (urlInput?.value || "").trim();
          if (!url) {
            log(`# Match URL required for ${esc(groupName)}`, true);
            urlInput?.classList.add("need");
            return;
          }
          urlInput.classList.remove("need");

          const modeLabel = btn.textContent.trim();
          const mode = /hyb/i.test(modeLabel) ? "hybrid" : "solo";
          saveDropdownSetting(groupName, url, modeLabel);

          try {
            const r = await postJson("/api/clicker/start", {
              type: kind,
              mode,
              match_key: S.matchKey,
              site_url: siteUrl,
              match_url: url,
            });
            log(`# ${esc(groupName)} [${esc(modeLabel)}] started pid=${r.pid} match=${esc(S.matchKey)}`);
          } catch (err) {
            log(`# Start failed (${esc(groupName)}): ${esc(err.message)}`, true);
          }
        }
        else if (btn.classList.contains("edit")) {
          try {
            const r = await postJson("/api/clicker/stop", { type: kind, reset: true });
            log(`# Reset: ${esc(groupName)} → ` + (r.reset ? "restarted" : "nothing running"));
          } catch (err) {
            log(`# Reset failed: ${esc(err.message)}`, true);
          }
          if (urlInput) urlInput.value = "";
          removeDropdownSetting(groupName);
        }
        else if (btn.classList.contains("stop")) {
          try {
            const r = await postJson("/api/clicker/stop", { type: kind });
            log(`# Stop: ${esc(groupName)} → ` + (r.stopped ? "killed" : "not running"));
          } catch (err) {
            log(`# Stop failed: ${esc(err.message)}`, true);
          }
        }
      });
    });
  });

  $("bStop")?.addEventListener("click", stopAll);
  $("bRestart")?.addEventListener("click", () => { stopAll(); setTimeout(startAll, 300); });
  $("bAuto")?.addEventListener("click", () => {
    S.auto = !S.auto;
    const btn = $("bAuto");
    if (btn) {
      btn.style.background = S.auto ? "var(--cyan)" : "";
      btn.style.color = S.auto ? "#000" : "";
    }
    if (S.auto && !S.started && S.pendingKey) activateMatch();
  });

  loadDropdownSettings();
  loadFixtures();
  setInterval(() => loadFixtures(true), FIXTURE_POLL_MS);

  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") {
      loadFixtures(true);
      if (_pieData.dirty) _pieScheduleRaf();
      if (S.started && (!S.ws || S.ws.readyState !== WebSocket.OPEN)) startLiveListener();
    }
  });

  setBallIdle();
});
