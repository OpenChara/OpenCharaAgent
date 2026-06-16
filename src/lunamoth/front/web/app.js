/* LunaMoth Desktop renderer — board/deck/settings/workshop half.
   Chat (ChatController + right panel + works/term pages) lives in chat.js.
   One hub connection for the board; one CharaClient per open chat. */
"use strict";

/* ---------- tiny DOM helpers ---------- */
function el(tag, attrs, ...children) {
  const node = document.createElement(tag);
  if (attrs) {
    for (const [k, v] of Object.entries(attrs)) {
      if (k === "class") node.className = v;
      else if (k === "style") node.style.cssText = v;
      else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
      else if (v !== null && v !== undefined) node.setAttribute(k, v);
    }
  }
  for (const c of children) {
    if (c === null || c === undefined) continue;
    node.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
  }
  return node;
}
const $ = (id) => document.getElementById(id);

/* The one "AI is thinking / generating" node — shared by card & avatar generation
   and per-field AI rewrites so every wait speaks the same visual language as the
   chat-thinking indicator (a breathing dot + label). opts.block = field-fill box. */
function thinkingEl(label, opts) {
  const o = opts || {};
  return el("div", { class: "lm-thinking" + (o.block ? " block" : "") },
    el("span", { class: "lm-think-label" }, label || t("ai-thinking")));
}

/* Per-field AI rewrite: a "✦ AI" button on an editable field. Click → a small
   popover for an instruction (empty = free rephrase) → the field becomes a
   shared loading box → the rewritten text fills it. Used by the card editor,
   the wake content step, and the create-shape review. */
/* `ctx` is either a name string OR a function returning a rich context string
   (the card's other fields) — so a single-field rewrite stays in character
   instead of going OOC. Resolved lazily at rewrite time (snapshots live edits). */
function cardCtxString(o) {
  o = o || {};
  const bits = [];
  if (o.name) bits.push("Name: " + o.name);
  if (o.description) bits.push("Description: " + String(o.description).slice(0, 1200));
  if (o.personality) bits.push("Personality: " + String(o.personality).slice(0, 600));
  if (o.scenario) bits.push("Scenario: " + String(o.scenario).slice(0, 600));
  if (o.tagline) bits.push("Tagline: " + String(o.tagline).slice(0, 200));
  return bits.join("\n");
}

function aiEditButton(fieldNode, fieldKey, ctx) {
  const btn = el("button", { class: "ai-edit-btn", type: "button", title: t("ai-edit-title") }, t("ai-edit"));
  btn.addEventListener("click", (ev) => { ev.stopPropagation(); openAiFieldEdit(fieldNode, fieldKey, ctx); });
  return btn;
}

function openAiFieldEdit(fieldNode, fieldKey, ctx) {
  const input = el("textarea", { class: "ai-edit-input", placeholder: t("ai-edit-ph") });
  const go = el("button", { class: "btn primary", type: "button" }, t("ai-edit-go"));
  const cancel = el("button", { class: "btn text", type: "button" }, t("cancel"));
  const overlay = el("div", { class: "ai-edit-overlay" },
    el("div", { class: "ai-edit-pop" },
      el("h4", null, t("ai-edit-title")), input,
      el("div", { class: "acts" }, cancel, go)));
  const close = () => overlay.remove();
  overlay.addEventListener("click", (ev) => { if (ev.target === overlay) close(); });
  cancel.addEventListener("click", close);
  go.addEventListener("click", () => { const instr = input.value.trim(); close(); runFieldRewrite(fieldNode, fieldKey, ctx, instr); });
  document.body.appendChild(overlay);
  input.focus();
}

/* Reusable editable card field + labelled block (mirrors the card editor's local
   field()/block()), so the wake content step renders the same way as the editor. */
function cardFieldEl(value, phKey, editable) {
  const div = el("div", { class: "cve-text" }, value || "");
  if (editable) {
    div.setAttribute("contenteditable", "plaintext-only");
    if (phKey) div.dataset.ph = t(phKey);
  }
  return div;
}
function cardBlockEl(labelKey, node, fieldKey, ctx) {
  const h = el("h4", null, t(labelKey));
  if (fieldKey) h.appendChild(aiEditButton(node, fieldKey, ctx));
  return el("div", { class: "cv-block" }, h, node);
}

async function runFieldRewrite(fieldNode, fieldKey, ctx, instruction) {
  const original = fieldNode.textContent;
  const context = typeof ctx === "function" ? ctx() : (ctx ? "Name: " + ctx : "");
  const loading = thinkingEl(t("ai-thinking"), { block: true });
  fieldNode.style.display = "none";
  fieldNode.parentNode.insertBefore(loading, fieldNode);
  try {
    const r = await hub.call("card.rewrite_field", {
      field: fieldKey, value: original, instruction: instruction || "", context,
    }, 180000);
    fieldNode.textContent = (r && r.text) || original;
  } catch (e) {
    toast(rpcErrText(e) || t("ai-edit-failed"), true);
  } finally {
    loading.remove();
    fieldNode.style.display = "";
  }
}

function toast(msg, isErr) {
  const node = el("div", { class: "toast" + (isErr ? " err" : "") }, msg);
  $("toasts").appendChild(node);
  setTimeout(() => node.remove(), isErr ? 5200 : 3200);
}

// A sticky toast with a spinner for a slow API call (e.g. export). Returns a
// dismiss fn — call it the moment the call resolves so the wait is never silent.
function workingToast(msg) {
  const node = el("div", { class: "toast" }, el("span", { class: "spin" }), " " + msg);
  $("toasts").appendChild(node);
  return () => node.remove();
}

/* ---------- global error backstop（Hermes error-boundary 的 vanilla 版） ----------
   只上浮，不处理：完整细节进 console.error，toast 限流一条/3s（错误风暴不刷屏，
   也不会因 toast 自身出错而循环）。绝不吞错。 */
let lastErrToastAt = 0;
function surfaceUncaught(msg, detail) {
  console.error("[lunamoth] uncaught:", detail !== undefined && detail !== null ? detail : msg);
  const now = Date.now();
  if (now - lastErrToastAt < 3000) return;
  lastErrToastAt = now;
  toast(String(msg || t("err-unexpected")), true);
}
window.addEventListener("error", (ev) => surfaceUncaught(ev.message, ev.error));
window.addEventListener("unhandledrejection", (ev) => {
  const r = ev.reason;
  surfaceUncaught(r && r.message ? r.message : String(r), r);
});

function timeAgo(ts) {
  if (!ts) return "";
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 90) return t("ago-just");
  if (s < 3600) return `${Math.round(s / 60)} ${t("ago-min")}`;
  if (s < 86400) return `${Math.round(s / 3600)} ${t("ago-hour")}`;
  return `${Math.round(s / 86400)} ${t("ago-day")}`;
}

function fmtClock(epoch) {
  return new Date(epoch * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
}

function fmtSize(bytes) {
  const n = Number(bytes) || 0;
  if (n >= 1048576) return (n / 1048576).toFixed(1) + " MB";
  if (n >= 1024) return Math.round(n / 1024) + " KB";
  return n + " B";
}

function estimateTokens(text) {
  const s = String(text || "");
  let cjk = 0;
  for (const ch of s) if (ch >= "一" && ch <= "鿿") cjk++;
  const other = Math.max(0, s.length - cjk);
  return cjk + Math.floor(other / 4);
}

function durationText(seconds) {
  const s = Math.max(0, Number(seconds) || 0);
  if (s >= 60) return `${Math.floor(s / 60)}m${Math.round(s % 60)}s`;
  if (s >= 10) return `${Math.round(s)}s`;
  if (s >= 1) return `${s.toFixed(1)}s`;
  return "<1s";
}

function modeLabel(mode) {
  return t(mode === "chat" ? "mode-chat" : "mode-live");
}

/* minimal markdown for character prose: fences, inline code, bold, italic.
   Streaming shows raw text; blocks are formatted when the turn finalizes. */
function mdRender(text) {
  const escaped = text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  const parts = escaped.split(/```[a-zA-Z]*\n?/);
  let html = "";
  for (let i = 0; i < parts.length; i++) {
    if (i % 2 === 1) {
      html += `<pre class="md-pre">${parts[i].replace(/\n$/, "")}</pre>`;
    } else {
      html += parts[i]
        .replace(/`([^`\n]+)`/g, '<code class="md-code">$1</code>')
        .replace(/\*\*([^*\n]+)\*\*/g, "<b>$1</b>")
        .replace(/(^|[^*])\*([^*\n]+)\*(?!\*)/g, "$1<i>$2</i>");
    }
  }
  return html;
}

function paletteClass(name) {
  let h = 0;
  for (const ch of String(name)) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return "p-" + (h % 6);
}
const glyphOf = (name) => (name || "?").trim().slice(0, 1).toUpperCase();

/* ---------- error language (reasons are human words) ---------- */
function errText(err) {
  const kind = err && err.kind ? err.kind : "provider";
  const key = { auth: "err-auth", credit: "err-credit", network: "err-network",
                model: "err-model", ratelimit: "err-ratelimit",
                draft_json: "err-draft-json", draft_schema: "err-draft-schema" }[kind] || "err-provider";
  return t(key);
}

function rpcErrText(e) {
  const data = e && e.data;
  if (data && data.kind) return errText(data) + (data.detail ? ` · ${data.detail}` : "");
  return e && e.message ? e.message : t("err-provider");
}

function dataUriSvg(svg) {
  return "data:image/svg+xml;charset=utf-8," + encodeURIComponent(svg);
}

/* The dual theme (presentation, not soul). Reads the new {primary,secondary}
   and falls back to the legacy single theme_color (= primary). */
function themeOf(card) {
  const th = card && card.theme && typeof card.theme === "object" ? card.theme : {};
  const primary = String(th.primary || (card && card.theme_color) || "");
  const secondary = String(th.secondary || "");
  return { primary, secondary };
}
function themeStyle(card) {
  const { primary, secondary } = themeOf(card);
  if (!primary) return "";
  return `--card-theme:${primary}` + (secondary ? `;--card-theme-2:${secondary}` : "");
}
/* The avatar image source: sidecar data-URI first, then a portable inline SVG. */
function avatarSrc(card) {
  if (card && card.avatar_uri) return String(card.avatar_uri);
  if (card && card.avatar_svg) return dataUriSvg(String(card.avatar_svg));
  return "";
}

/* Shared avatar: image when the card has one, letter glyph otherwise. */
function avatarNode(name, card, cls) {
  const style = themeStyle(card);
  const src = avatarSrc(card);
  const attrs = { class: (cls || "avatar-s") + (style || src ? "" : " " + paletteClass(name)) };
  if (style) attrs.style = style;
  const node = el("div", attrs);
  if (src) node.appendChild(el("img", { src, alt: "" }));
  else node.appendChild(document.createTextNode(glyphOf(name)));
  return node;
}

function cardVisual(c, cls) {
  const style = themeStyle(c);
  const src = avatarSrc(c);
  const attrs = { class: cls || "face" };
  if (style) attrs.style = style;
  const children = [];
  // The 立绘 (full-body sprite) sits UNDER the chibi avatar and fades in on hover
  // (a theme-color scrim keeps it legible). Default state = the Q-version avatar
  // on top; hover reveals the sprite. Degrades to nothing when no sprite.
  const spriteUrl = c && (c.sprite_url || c.keyvisual_url);
  if (spriteUrl) {
    const sp = el("div", { class: "face-sprite" });
    sp.style.backgroundImage = `url("${String(spriteUrl).replace(/"/g, "%22")}")`;
    children.push(sp, el("div", { class: "face-sprite-scrim" }));
  }
  if (src) children.push(el("img", { class: "avatar-svg", src, alt: "" }));
  else children.push(el("div", { class: "glyph" }, glyphOf(c && c.name)));
  return el("div", attrs, ...children);
}

/* The deck card behind a living session (frozen cards keep their deck entry). */
function cardForSession(name) {
  const cards = (state.hub && state.hub.cards) || [];
  return cards.find((c) => (c.used_by || []).includes(name)) || null;
}

/* ---------- global state ---------- */
const hub = new HubClient();
const state = {
  hub: null,            // last hub.state result
  view: "board",
  sort: "recent",
  models: null,         // models.list cache
  chat: null,           // active ChatController (chat.js)
  pendingChatOpts: null,
  boardTimer: null,
  display: localStorage.getItem("lm-display") || "product",
  pendingDrafts: [],    // client-side card-draft generations in flight (see startDraftGeneration)
  pendingTimer: null,   // 1s ticker that updates pending placeholders in place
};

/* ============================ THEME & LANG & DISPLAY ============================ */
function applyTheme(pref) {
  const dark = pref === "dark" ||
    (pref !== "light" && window.matchMedia("(prefers-color-scheme: dark)").matches);
  document.body.classList.toggle("dark", dark);
  try { localStorage.setItem("lm-theme", pref); } catch (e) { /* ok */ }
  document.querySelectorAll("#theme-seg span").forEach((s) =>
    s.classList.toggle("on", s.dataset.th === pref));
}
window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
  applyTheme(localStorage.getItem("lm-theme") || "system");
});

function setLang(code, persist) {
  setLangCode(code);
  document.querySelectorAll("#lang-seg span, #fr-langseg span").forEach((s) =>
    s.classList.toggle("on", s.dataset.lang === code));
  renderBoard();
  renderDeck();
  if (persist && hub.sock.open) hub.call("defaults.set", { ui_lang: code }).catch(() => {});
}

/* Product hides raw payloads (OC creators); Technical shows full I/O. */
function applyDisplayMode(mode) {
  state.display = mode === "technical" ? "technical" : "product";
  try { localStorage.setItem("lm-display", state.display); } catch (e) { /* ok */ }
  document.body.classList.toggle("technical", state.display === "technical");
  document.querySelectorAll("#display-seg span").forEach((s) =>
    s.classList.toggle("on", s.dataset.disp === state.display));
  if (state.chat) state.chat.onDisplayModeChanged();
}
const isTechnical = () => state.display === "technical";

/* ---- per-card chat visuals: background + 立绘 opacity / sprite position ----
   Operator presentation prefs (localStorage), applied as CSS vars on :root + a
   sprite position class. Pure presentation; degrades to nothing when a card has
   no bg_url/sprite_url. Defaults: bg 18, sprite 16, position "right". */
const VISUAL_DEFAULTS = { bgOn: true, veilOpacity: 80, spriteOpacity: 16, spritePos: "right" };
/* Visuals are PER-CHARA, not global: each session keeps its own. Scope the
   storage key by the open chat's name; fall back to the bare key only when no
   chat is open (a harmless default).
   Model: the background IMAGE is a plain on/off toggle (shown at full strength
   on the sides); the adjustable opacity is the centred readability VEIL behind
   the chat column. 立绘 sprite opacity + position stay as they were. */
function visualKey(base) {
  const n = state.chat && state.chat.name;
  return n ? `${base}:${n}` : base;
}
function readVisualPrefs() {
  const num = (k, d) => {
    const v = Number(localStorage.getItem(visualKey(k)));
    return Number.isFinite(v) && v >= 0 && v <= 100 ? v : d;
  };
  let pos = localStorage.getItem(visualKey("lm-sprite-pos")) || VISUAL_DEFAULTS.spritePos;
  if (!["off", "left", "center", "right"].includes(pos)) pos = VISUAL_DEFAULTS.spritePos;
  const bgRaw = localStorage.getItem(visualKey("lm-chat-bg-on"));
  return {
    bgOn: bgRaw === null ? VISUAL_DEFAULTS.bgOn : bgRaw === "1",
    veilOpacity: num("lm-chat-veil-opacity", VISUAL_DEFAULTS.veilOpacity),
    spriteOpacity: num("lm-sprite-opacity", VISUAL_DEFAULTS.spriteOpacity),
    spritePos: pos,
  };
}
function applyVisualPrefs() {
  const p = readVisualPrefs();
  const root = document.documentElement;
  // bg image: full strength when on, gone when off (the veil handles legibility)
  root.style.setProperty("--chat-bg-opacity", p.bgOn ? "1" : "0");
  root.style.setProperty("--chat-veil-opacity", String(p.veilOpacity / 100));
  root.style.setProperty("--chat-sprite-opacity", String(p.spriteOpacity / 100));
  const sprite = $("chat-sprite");
  if (sprite) {
    sprite.classList.remove("pos-left", "pos-center", "pos-right");
    if (p.spritePos !== "off") sprite.classList.add("pos-" + p.spritePos);
  }
  // reflect in the settings controls (instant/optimistic)
  const bgsw = $("bg-on-switch"); if (bgsw) bgsw.classList.toggle("on", p.bgOn);
  const veil = $("veil-opacity"); if (veil) veil.value = String(p.veilOpacity);
  const so = $("sprite-opacity"); if (so) so.value = String(p.spriteOpacity);
  document.querySelectorAll("#sprite-pos-seg span").forEach((s) =>
    s.classList.toggle("on", s.dataset.pos === p.spritePos));
}

/* ============================ ROUTER ============================
   #/  #/deck  #/settings  #/chara/<name>[/works|/term] — refresh/back work. */
function navTo(hash) {
  if (location.hash === hash) route();
  else location.hash = hash;
}

function show(view) {
  state.view = view;
  document.querySelectorAll(".view").forEach((v) => v.classList.remove("active"));
  $(`view-${view}`).classList.add("active");
  document.querySelectorAll(".nav-item").forEach((n) =>
    n.classList.toggle("active", n.dataset.view === (view === "chat" ? "board" : view)));
  if (view === "board" || view === "deck") refreshHub();
  if (view === "gateways") renderGateways();
}

function route() {
  const h = location.hash || "#/";
  const m = h.match(/^#\/chara\/([^/]+)(?:\/(works|term))?$/);
  if (m) {
    const name = decodeURIComponent(m[1]);
    const page = m[2] || "chat";
    if (state.chat && state.chat.name === name && !state.chat.disposed) {
      show("chat");
      state.chat.showPage(page);
    } else {
      if (state.chat) { state.chat.dispose(); state.chat = null; }
      show("chat");
      state.chat = new ChatController(name, state.pendingChatOpts || {});
      state.pendingChatOpts = null;
      state.chat.open(page);
    }
    return;
  }
  if (state.chat) { state.chat.dispose(); state.chat = null; }
  show(h === "#/deck" ? "deck" : h === "#/gateways" ? "gateways" : h === "#/settings" ? "settings" : "board");
}
window.addEventListener("hashchange", route);

document.querySelectorAll(".nav-item").forEach((n) =>
  n.addEventListener("click", () => navTo(n.dataset.view === "board" ? "#/" : `#/${n.dataset.view}`)));

function openChat(name, opts) {
  state.pendingChatOpts = opts || null;
  navTo(`#/chara/${encodeURIComponent(name)}`);
}

/* ============================ 网关总览（所有角色 × 网关，与角色面板同数据） ============================ */
function gwPlatLabel(platform) {
  if (!platform) return t("gw-none");
  if (typeof GW_PLATFORMS !== "undefined" && GW_PLATFORMS[platform]) return t(GW_PLATFORMS[platform].label);
  return platform;
}

function gwStatusBits(gw) {
  const st = (gw && gw.state) || "stopped";
  return {
    text: st === "running" ? t("gw-running") : st === "needs_login" ? t("gw-needs-login") : t("gw-stopped"),
    cls: st === "running" ? "ok" : st === "needs_login" ? "warn" : "",
  };
}

async function renderGateways() {
  const host = $("gw-overview");
  if (!host) return;
  if (!state.hub) { try { await refreshHub(); } catch (e) { /* surfaced below */ } }
  host.innerHTML = "";
  let data;
  try { data = await hub.call("gateways.list", {}, 20000); }
  catch (e) { host.appendChild(el("div", { class: "gw-error" }, rpcErrText(e))); return; }
  const rows = (data && data.gateways) || [];
  const byName = {};
  for (const s of (state.hub ? state.hub.sessions : [])) byName[s.name] = s;
  const configured = rows.filter((r) => r.enabled || (r.gateway && r.gateway.platform));
  $("gw-count").textContent = configured.length ? String(configured.length) : "";
  if (!configured.length) {
    host.appendChild(el("div", { class: "empty-state" },
      el("p", null, t("gw-empty")),
      el("button", { class: "btn primary", onclick: openNewGateway }, t("gw-new"))));
    return;
  }
  for (const r of configured) host.appendChild(gatewayCard(r, byName[r.name] || { char_name: r.name }));
}

function gatewayCard(r, sess) {
  const gw = r.gateway || {};
  const bits = gwStatusBits(gw);
  const sw = el("button", { class: "switch" + (r.enabled ? " on" : "") });
  sw.onclick = async () => {
    if (sw.disabled) return;
    sw.disabled = true;
    const turnOn = !r.enabled;
    sw.classList.toggle("on", turnOn);   // optimistic
    try { await hub.call(turnOn ? "gateway.start" : "gateway.stop", { name: r.name }, 30000); renderGateways(); }
    catch (e) { sw.classList.toggle("on", !turnOn); sw.disabled = false; toast(rpcErrText(e), true); }
  };
  return el("div", { class: "gw-card" },
    el("div", { class: "gw-card-head" },
      el("span", { class: "gw-plat-name" }, gwPlatLabel(gw.platform)),
      el("span", { class: "gw-chip " + bits.cls }, bits.text)),
    el("div", { class: "gw-card-sub" }, t("gw-bound") + "：" + (sess.char_name || r.name)),
    gw.detail ? el("div", { class: "gw-card-detail" }, gw.detail) : null,
    el("div", { class: "gw-card-foot" },
      sw, el("span", { class: "enable-lbl" }, r.enabled ? t("gw-enabled") : t("gw-disabled")),
      el("div", { class: "grow" }),
      el("button", { class: "btn soft", onclick: () => openChat(r.name, { panelTab: "gateway" }) }, t("gw-manage"))));
}

function openNewGateway() {
  // Step 1: a new gateway always binds to a chara — pick one, then deep-link to
  // its gateway tab where login/config (and, per platform, the QR) live.
  closePopovers();
  const sessions = (state.hub && state.hub.sessions) || [];
  if (!sessions.length) { toast(t("gw-no-chara"), true); return; }
  const pop = el("div", { class: "popover gw-newpop" });
  pop.appendChild(el("h4", null, t("gw-pick-chara")));
  for (const s of sessions) {
    pop.appendChild(el("button", { class: "gw-pick-row", onclick: () => { pop.remove(); openChat(s.name, { panelTab: "gateway" }); } },
      s.char_name || s.name));
  }
  document.body.appendChild(pop);
  const btn = $("gw-new");
  if (btn) {
    const rc = btn.getBoundingClientRect();
    pop.style.position = "fixed";
    pop.style.top = `${rc.bottom + 6}px`;
    pop.style.right = `${window.innerWidth - rc.right}px`;
  }
}

if ($("gw-refresh")) $("gw-refresh").addEventListener("click", renderGateways);
if ($("gw-new")) $("gw-new").addEventListener("click", openNewGateway);

/* ============================ SPLITTERS（左右栏都可拖宽） ============================ */
function makeSplit(handle, target, cssVar, storeKey, opts) {
  const min = opts.min, max = opts.max, rtl = !!opts.rtl;
  const saved = Number(localStorage.getItem(storeKey) || 0);
  if (saved) document.documentElement.style.setProperty(cssVar, `${Math.min(max, Math.max(min, saved))}px`);
  handle.addEventListener("mousedown", (ev) => {
    ev.preventDefault();
    const startX = ev.clientX;
    const startW = target.getBoundingClientRect().width;
    handle.classList.add("dragging");
    document.body.classList.add("col-resizing");
    const move = (e) => {
      const dx = rtl ? startX - e.clientX : e.clientX - startX;
      const w = Math.min(max, Math.max(min, startW + dx));
      document.documentElement.style.setProperty(cssVar, `${w}px`);
    };
    const up = () => {
      handle.classList.remove("dragging");
      document.body.classList.remove("col-resizing");
      document.removeEventListener("mousemove", move);
      document.removeEventListener("mouseup", up);
      const w = Math.round(target.getBoundingClientRect().width);
      try { localStorage.setItem(storeKey, String(w)); } catch (e) { /* ok */ }
    };
    document.addEventListener("mousemove", move);
    document.addEventListener("mouseup", up);
  });
}
makeSplit($("sidebar-split"), $("sidebar"), "--sidebar-w", "lm-w-sidebar", { min: 150, max: 320 });
makeSplit($("panel-split"), $("panel"), "--panel-w", "lm-w-panel", { min: 240, max: 460, rtl: true });

/* ============================ HUB LIFECYCLE ============================ */
async function refreshHub() {
  if (!hub.sock.open) return;
  try {
    state.hub = await hub.call("hub.state", {}, 20000);
    $("version").textContent = "v" + state.hub.version;
    $("about-version").textContent = "LunaMoth v" + state.hub.version;
    $("home-path").textContent = state.hub.home;
    renderBoard();
    renderDeck();
    renderModelPane();
    if (state.chat) state.chat.onHubState();
  } catch (e) { /* transient */ }
}

let _routedOnce = false;
hub.onReady = async () => {
  $("conn-dot").classList.remove("bad");
  await refreshHub();
  const d = (state.hub && state.hub.defaults) || {};
  const savedLang = localStorage.getItem("lm-lang") || d.ui_lang || (navigator.language.startsWith("zh") ? "zh" : "en");
  setLang(savedLang, false);
  applyTheme(localStorage.getItem("lm-theme") || d.ui_theme || "system");
  applyDisplayMode(state.display);
  applyVisualPrefs();
  if (!_routedOnce) { _routedOnce = true; route(); }
  if (state.hub && state.hub.first_run) openFirstRun();
};
hub.onDown = () => { $("conn-dot").classList.add("bad"); };
hub.start();

if (!state.boardTimer) {
  state.boardTimer = setInterval(() => {
    if (state.view === "board" && !document.hidden) refreshHub();
  }, 10000);
}

/* ============================ BOARD ============================ */
function sessionsSorted() {
  const q = ($("board-search").value || "").toLowerCase();
  let list = (state.hub ? state.hub.sessions : []).filter(
    (s) => !q || s.name.toLowerCase().includes(q) || s.char_name.toLowerCase().includes(q));
  list.sort((a, b) => state.sort === "created"
    ? b.created_at - a.created_at
    : (b.last_active || 0) - (a.last_active || 0));
  return list;
}

function statusOf(s) {
  // one line per card, exception first
  if (s.status === "new") return { dot: "off", line: t("st-new"), cls: "" };
  if (s.status === "crashed") return { dot: "err", line: s.error || "crashed", cls: "err" };
  if (s.error && (s.error_kind === "auth" || (s.status !== "attached" && s.status !== "running")))
    return { dot: "err", line: t("st-error"), cls: "err" };
  // Autonomous running turned off by the operator — a deliberate, calm state.
  if (s.paused) return { dot: "off", line: t("st-paused"), cls: "" };
  if (s.status === "idle") return { dot: "off", line: `${t("st-offline")} · ${timeAgo(s.last_active)}`, cls: "" };
  if (s.preview && s.preview.awaiting)
    return { dot: "live", line: s.preview.text, cls: "msg" };
  if (s.life && s.life.state) return { dot: "live", line: lifeText(s.life), cls: "" };
  return { dot: "live", line: t("st-idle-live"), cls: "" };
}

/* Status words are factual statements only — the platform does not roleplay.
   `working` is an LLM turn actually in flight; `idle_countdown` is the gap
   between self-paced cycles (it is NOT working — earlier it was mislabeled the
   same as working, which read as "always busy"). */
function lifeText(life) {
  if (!life) return "";
  if (life.state === "working") return t("life-working");
  if (life.state === "idle_countdown") {
    const n = life.next_cycle_at ? Math.max(0, Math.round((life.next_cycle_at - Date.now() / 1000) / 60)) : null;
    return n && n >= 1 ? t("life-idle-next", { n }) : t("life-idle");
  }
  if (life.state === "waiting") return t("life-waiting");
  if (life.state === "resting" && life.rest_until) return t("life-resting-until", { time: fmtClock(life.rest_until) });
  if (life.state === "resting") return t("st-resting");
  if (life.state === "backoff") return `${t("life-backoff")}${life.detail ? " · " + life.detail : ""}`;
  return t("life-idle");
}

function isoGlyph(iso) {
  return { dir: t("iso-dir"), sandbox: t("iso-sandbox"), docker: t("iso-docker") }[iso] || iso;
}

function superBadge(cls) {
  return el("span", { class: "super-badge" + (cls ? " " + cls : ""), title: t("superchat-tip") }, "⚡ Super Chat");
}

function parseToolArguments(raw) {
  if (raw && typeof raw === "object") return raw;
  if (typeof raw !== "string" || !raw.trim()) return null;
  try {
    const parsed = JSON.parse(raw);
    return parsed && typeof parsed === "object" ? parsed : null;
  } catch (e) {
    return null;
  }
}

/* Collapse text to one short line — the JS twin of agent.py's _abbrev, used by
   the restored-history tool lines. */
function abbreviate(text, limit) {
  const one = String(text || "").split(/\s+/).join(" ").trim();
  return one.length <= limit ? one : one.slice(0, limit - 1) + "…";
}

/* A compact "key=value, …" summary of a tool call's arguments, for the
   restored tool-call detail (technical mode only). */
function toolArgsSummary(raw) {
  const args = parseToolArguments(raw);
  if (!args) return typeof raw === "string" ? abbreviate(raw, 200) : "";
  const parts = [];
  for (const [k, v] of Object.entries(args)) {
    const val = typeof v === "string" ? v : JSON.stringify(v);
    parts.push(`${k}=${val}`);
  }
  return abbreviate(parts.join(", "), 200);
}

function speakTextsFromMessage(msg) {
  const out = [];
  const calls = Array.isArray(msg && msg.tool_calls) ? msg.tool_calls : [];
  for (const tc of calls) {
    const fn = tc && tc.function;
    if (!fn || fn.name !== "speak") continue;
    const args = parseToolArguments(fn.arguments);
    const text = args && typeof args.text === "string" ? args.text.trim() : "";
    if (text) out.push(text);
  }
  return out;
}

function renderBoard() {
  if (!state.hub) return;
  const list = sessionsSorted();
  $("board-count").textContent = list.length ? `· ${list.length}` : "";
  $("board-sort").textContent = t(state.sort === "created" ? "sort-created" : "sort-recent") + " ⌄";
  const grid = $("board-grid");
  grid.innerHTML = "";
  $("board-empty").style.display = list.length ? "none" : "flex";
  if (!list.length) decorateDefaultCard();
  for (const s of list) {
    const live = (s.status === "running" || s.status === "attached") && !s.paused;
    const st = statusOf(s);
    const deckCard = cardForSession(s.name);
    const portraitSrc = avatarSrc(deckCard);
    const portraitStyle = themeStyle(deckCard);
    const portrait = el("div", {
      class: `portrait ${portraitSrc || portraitStyle ? "" : paletteClass(s.char_name)}`,
      style: portraitStyle,
    });
    if (portraitSrc) {
      portrait.appendChild(el("img", { class: "avatar-svg", src: portraitSrc, alt: "" }));
    } else {
      portrait.appendChild(el("div", { class: "glyph" }, glyphOf(s.char_name)));
    }
    portrait.appendChild(el("span", { class: `dot ${st.dot}` }));
    portrait.appendChild(el("div", { class: "hover-acts" },
      el("button", {
        title: live ? t("act-sleep") : t("act-wake-up"),
        onclick: async (ev) => {
          ev.stopPropagation();
          const btn = ev.currentTarget;
          if (btn.disabled) return;
          btn.disabled = true;                       // no dead double-click
          btn.textContent = "";
          btn.appendChild(el("span", { class: "spin" }));  // working state (start/stop can take seconds)
          try {
            await hub.call(live ? "session.stop" : "session.start", { name: s.name }, 30000);
            refreshHub();                            // re-renders the card with the new power state
          } catch (e) {
            btn.disabled = false; btn.textContent = "⏻";   // revert on failure
            toast(rpcErrText(e), true);
          }
        },
      }, "⏻"),
      el("button", { title: "⋯", onclick: (ev) => { ev.stopPropagation(); cardMenu(ev, s); } }, "⋯")));
    const card = el("div", { class: "chara-card" + (st.dot === "off" ? " offline" : ""), onclick: () => openChat(s.name) },
      portrait,
      el("div", { class: "card-body" },
        el("div", { class: "card-name" },
          el("b", null, s.char_name),
          el("div", { class: "chips" },
            el("span", { class: "chip" }, s.lang),
            el("span", { class: "chip" }, modeLabel(s.mode)))),
        (() => {
          const line = el("div", { class: "status-line " + st.cls }, st.line);
          if (st.cls === "err") {
            line.textContent = "";
            line.title = s.error_kind === "auth" ? t("board-key-tip") : (s.error || "");
            line.append(boardErrText(s), " · ");
            line.appendChild(el("a", {
              title: s.error_kind === "auth" ? t("board-key-tip") : "",
              style: "cursor:pointer;text-decoration:underline",
              onclick: (ev) => { ev.stopPropagation(); navTo("#/settings"); },
            }, t("go-settings")));
          }
          return line;
        })(),
        (() => {
          const speaks = Array.isArray(s.speaks) ? s.speaks : [];
          if (!speaks.length) return null;
          return el("div", { class: "speak-feed" },
            ...speaks.slice(0, 3).map((sp) => el("div", { class: "speak-item" },
              el("div", { class: "speak-top" }, superBadge("mini"),
                s.superchat_unread ? el("span", { class: "chip" }, String(s.superchat_unread)) : null,
                el("time", null, timeAgo(sp.ts))),
              el("div", { class: "speak-text" }, sp.text || ""))));
        })(),
        el("div", { class: "meta-line" },
          el("span", null, s.model || ""),
          el("span", { class: "sep" }, "·"),
          el("span", null, isoGlyph(s.isolation)))));
    grid.appendChild(card);
  }
}

function shortErr(line) {
  const low = line.toLowerCase();
  if (low.includes("credit") || low.includes("balance") || low.includes("402")) return t("err-credit");
  if (low.includes("401") || low.includes("403") || low.includes("auth")) return t("err-auth");
  if (low.includes("timeout") || low.includes("connect") || low.includes("network") || low.includes("unreachable")) return t("err-network");
  return t("err-provider");
}

function boardErrText(s) {
  const kind = s && s.error_kind ? s.error_kind : "";
  if (kind === "auth") return t("board-key-invalid");
  if (kind === "credit") return t("err-credit");
  if (kind === "model") return t("err-model");
  if (kind === "ratelimit") return t("err-ratelimit");
  if (kind === "network") return t("err-network");
  return s && s.error ? shortErr(s.error) : t("st-error");
}

function cardMenu(ev, s) {
  // minimal ⋯ menu as a one-off floating list near the cursor.
  // Deletion is intentionally NOT here — it lives only in the per-chara
  // settings panel's danger zone (chat.js), never reachable from a status menu.
  closeMenus();
  const menu = el("div", { class: "palette open", style: `position:fixed;left:${Math.min(ev.clientX, innerWidth - 240)}px;top:${ev.clientY + 8}px;bottom:auto;transform:none;width:220px;z-index:90` },
    el("div", { class: "row", onclick: async () => {
      closeMenus();
      const done = workingToast(t("exporting"));
      try {
        const r = await hub.call("session.export", { name: s.name }, 120000);
        done();
        toast(t("exported", { path: r.path }));
        hub.call("open.path", { path: r.path, reveal: true }).catch(() => {});
      } catch (e) { done(); toast(rpcErrText(e), true); }
    } }, t("menu-export")));
  menu.dataset.menu = "1";
  document.body.appendChild(menu);
  setTimeout(() => document.addEventListener("click", closeMenus, { once: true }), 0);
}
function closeMenus() { document.querySelectorAll("[data-menu]").forEach((m) => m.remove()); }

$("board-sort").addEventListener("click", () => {
  state.sort = state.sort === "recent" ? "created" : "recent";
  renderBoard();
});
$("board-search").addEventListener("input", renderBoard);
$("board-new").addEventListener("click", () => ensureModel(openCreateFlow));
$("empty-meet").addEventListener("click", () => ensureModel(wakeDefaultLuna));
$("empty-create").addEventListener("click", () => ensureModel(openCreateFlow));

/* ---------- delete (the heaviest friction in the app, deliberately) ----------
   Three sequential gates before session.delete fires: (1) type the chara's
   name, (2) tick an explicit understanding checkbox, (3) type the final
   "delete {name}" phrase. The Delete button only enables once all three pass.
   The backend still gates on confirm === name (RpcError -32034 otherwise). */
function openDeleteModal(s) {
  const phrase = `${t("del-word")} ${s.char_name}`;
  const gate = { name: false, ack: false, phrase: false };
  const delBtn = el("button", { class: "btn danger", disabled: "" }, t("del-go"));
  function refreshGate() {
    if (gate.name && gate.ack && gate.phrase) delBtn.removeAttribute("disabled");
    else delBtn.setAttribute("disabled", "");
  }

  // step 1 — type its name
  const nameInput = el("input", { class: "confirm-input", placeholder: t("del-step1-ph", { name: s.char_name }) });
  nameInput.addEventListener("input", () => {
    gate.name = nameInput.value.trim() === s.char_name;
    step1.classList.toggle("ok", gate.name);
    refreshGate();
  });
  const step1 = el("div", { class: "del-step" },
    el("label", null, t("del-step1", { name: s.char_name })), nameInput);

  // step 2 — explicit understanding checkbox
  const ackBox = el("input", { type: "checkbox" });
  const ackLabel = el("label", { class: "del-check" }, ackBox,
    el("span", null, t("del-step2")));
  ackBox.addEventListener("change", () => {
    gate.ack = ackBox.checked;
    step2.classList.toggle("ok", gate.ack);
    refreshGate();
  });
  const step2 = el("div", { class: "del-step" }, ackLabel);

  // step 3 — type the final phrase
  const phraseInput = el("input", { class: "confirm-input", placeholder: t("del-ph", { name: s.char_name }) });
  phraseInput.addEventListener("input", () => {
    gate.phrase = phraseInput.value.trim() === phrase;
    step3.classList.toggle("ok", gate.phrase);
    refreshGate();
  });
  const step3 = el("div", { class: "del-step" },
    el("label", null, t("del-step3")), phraseInput);

  delBtn.addEventListener("click", async () => {
    if (!(gate.name && gate.ack && gate.phrase)) return;
    try {
      await hub.call("session.delete", { name: s.name, confirm: s.name }, 30000);
      closeModal();
      navTo("#/");
      refreshHub();
    } catch (e) { toast(e.message, true); }
  });
  openModal(
    el("div", null,
      el("h2", null, t("del-title", { name: s.char_name })),
      el("div", { class: "sub" }, t("del-sub")),
      el("div", { class: "consequences" },
        el("div", null, el("i", null, "⏻"), el("div", null, t("del-1"))),
        el("div", null, el("i", null, "▣"), el("div", null, el("b", null, t("del-2")))),
        el("div", null, el("i", null, "✕"), el("div", null, t("del-3"))),
        el("div", null, el("i", null, "≋"), el("div", null, t("del-4")))),
      el("div", { class: "soften" }, t("del-soften", { name: s.char_name })),
      el("button", { class: "btn soft", style: "width:100%;margin-bottom:14px", onclick: async (ev) => {
        const btn = ev.currentTarget;
        if (btn.disabled) return;
        btn.disabled = true;
        const done = workingToast(t("exporting"));
        try {
          const r = await hub.call("session.export", { name: s.name }, 120000);
          done();
          toast(t("exported", { path: r.path }));
        } catch (e) { done(); btn.disabled = false; toast(rpcErrText(e), true); }
      } }, t("del-export")),
      step1, step2, step3,
      el("div", { class: "acts" },
        el("button", { class: "btn text", onclick: closeModal }, t("cancel")),
        el("div", { class: "grow" }),
        delBtn)));
}

/* ============================ MODALS ============================ */
/* wide: falsy = narrow, true = sheet, "wide" = sheet + .wide (the card editor) */
function openModal(content, wide) {
  const box = $("modal-box");
  box.classList.toggle("sheet", !!wide);
  box.classList.toggle("wide", wide === "wide");
  // The card view (R5) is a fixed-height flex column: tag the box so it drops the
  // modal's own padding/scroll and lets only its inner pane scroll.
  box.classList.toggle("cardview", !!(content && content.classList && content.classList.contains("cardview")));
  box.innerHTML = "";
  box.appendChild(content);
  $("modal-layer").classList.add("open");
}
function closeModal() { $("modal-layer").classList.remove("open"); }
$("modal-layer").addEventListener("click", (ev) => { if (ev.target === $("modal-layer")) closeModal(); });
document.addEventListener("keydown", (ev) => { if (ev.key === "Escape") { closeModal(); closeMenus(); closePopovers(); } });

function closePopovers() {
  document.querySelectorAll(".popover, .attach-menu").forEach((p) => p.remove());
  document.querySelectorAll(".attach-btn.on").forEach((b) => b.classList.remove("on"));
}

/* ============================ DECK ============================ */
/* Duplicate must NOT shadow the original: the backend dedupes by name+lang and
   scans the user deck before the bundled one, so a same-name copy would hide a
   builtin/frozen card and the unlocked copy would take its place. Rename the
   copy up front (副本 / copy, numbered when taken). */
function duplicateName(name, lang) {
  const zh = String(lang || "").toLowerCase().startsWith("zh");
  const taken = new Set(((state.hub && state.hub.cards) || []).map((c) => c.name));
  const base = `${name} ${zh ? "副本" : "copy"}`;
  if (!taken.has(base)) return base;
  for (let n = 2; ; n++) {
    if (!taken.has(`${base} ${n}`)) return `${base} ${n}`;
  }
}

async function duplicateCard(c) {
  const full = await hub.call("card.read", { path: c.path }, 20000);
  if (!full.raw) throw new Error(t("dup-png"));
  const name = duplicateName(full.name || c.name, full.language || c.lang);
  if (full.raw.data) full.raw.data.name = name;
  full.raw.name = name;
  await hub.call("card.save", { data: full.raw }, 20000);
  return name;
}

/* ---------- background card-draft generation ----------
   cards.draft can take ~a minute. We never trap the user in the modal: a
   submission spawns a client-side "pending" placeholder that renders at the
   top of the deck while the call runs in the background, then becomes a real
   draft card on success (card.from_draft as_draft) or shows retry/dismiss on
   error. Multiple generations may run at once (keyed by client id). */

let _pendingSeq = 0;

/* Card drafting (and avatar/rewrite) always use the system default model — there
   are no per-task aux models. The override lives only on wake / chara settings. */
function effectiveDraftModel() {
  const d = (state.hub && state.hub.defaults) || {};
  return String(d.model || "");
}

function tentativeName(inspiration) {
  const first = String(inspiration || "").trim().split(/\s+/).slice(0, 4).join(" ");
  return first.slice(0, 28) || "…";
}

function ensurePendingTimer() {
  if (state.pendingTimer) return;
  state.pendingTimer = setInterval(() => {
    if (!state.pendingDrafts.length) {
      clearInterval(state.pendingTimer);
      state.pendingTimer = null;
      return;
    }
    const now = Date.now();
    for (const pd of state.pendingDrafts) {
      if (pd.error) continue;
      const span = document.querySelector(`.pending-spine[data-pid="${pd.id}"] .pending-elapsed`);
      if (span) span.textContent = pendingStatusText(pd, now);
    }
  }, 1000);
}

function pendingStatusText(pd, now) {
  const n = Math.max(0, Math.floor(((now || Date.now()) - pd.startedAt) / 1000));
  return t("pending-gen", { n: String(n), model: pd.model || "—" });
}

/* Start one background generation. Renders a placeholder immediately. */
function startDraftGeneration(inspiration, opts) {
  const o = opts || {};
  const id = "pd" + (++_pendingSeq);
  const pd = {
    id,
    inspiration: String(inspiration || "").trim(),
    model: effectiveDraftModel(),
    name: tentativeName(inspiration),
    startedAt: Date.now(),
    error: null,
  };
  state.pendingDrafts.unshift(pd);
  ensurePendingTimer();
  renderDeck();
  runDraftGeneration(pd);
  return id;
}

async function runDraftGeneration(pd) {
  pd.error = null;
  pd.startedAt = Date.now();
  renderDeck();
  try {
    const raw = await hub.call("cards.draft", { inspiration: pd.inspiration }, 240000);
    const draft = normalizeDraft(raw);
    await hub.call("card.from_draft", { draft, origin: pd.inspiration, as_draft: true }, 30000);
    removePending(pd.id);
    await refreshHub();   // the new draft card now rides cards.list with its badge
    toast(t("draft-ready", { name: draft.name || pd.name }));
  } catch (e) {
    pd.error = rpcErrText(e);
    renderDeck();
  }
}

function removePending(id) {
  const i = state.pendingDrafts.findIndex((p) => p.id === id);
  if (i >= 0) state.pendingDrafts.splice(i, 1);
  if (!state.pendingDrafts.length && state.pendingTimer) {
    clearInterval(state.pendingTimer);
    state.pendingTimer = null;
  }
  renderDeck();
}

/* A pending-generation placeholder card for the deck grid. */
function pendingSpine(pd) {
  const face = el("div", { class: "face pending-face " + paletteClass(pd.name) },
    el("div", { class: "glyph" }, glyphOf(pd.name)));
  face.appendChild(el("div", { class: "draft-badge" }, t("deck-generating")));
  const spine = el("div", { class: "spine pending-spine", "data-pid": pd.id }, face);
  if (pd.error) {
    spine.appendChild(el("div", { class: "sbody" },
      el("div", { class: "sname" }, el("b", null, pd.name)),
      el("div", { class: "pending-err" }, pd.error),
      el("div", { class: "pending-acts" },
        el("button", { class: "btn soft sm", onclick: (ev) => { ev.stopPropagation(); runDraftGeneration(pd); } }, t("retry")),
        el("button", { class: "btn text sm", onclick: (ev) => { ev.stopPropagation(); removePending(pd.id); } }, t("dismiss")))));
  } else {
    spine.appendChild(el("div", { class: "sbody" },
      el("div", { class: "sname" }, el("b", null, pd.name)),
      el("div", { class: "pending-status" },
        el("i", { class: "pending-pulse" }),
        el("span", { class: "pending-elapsed" }, pendingStatusText(pd)))));
  }
  return spine;
}

/* Regenerate a draft card from its stored origin inspiration. The origin is
   persisted by the draft pipeline (card.from_draft -> ext.lunamoth.origin), so
   regenerate re-runs cards.draft with it. If a card has no stored origin
   (e.g. an old draft or an imported one), we fall back to its description as
   the inspiration — the simplest honest behavior. Backgrounds like a fresh
   generation: a new placeholder, the old draft card stays until it lands. */
async function regenerateDraftCard(c) {
  let full;
  try {
    full = await hub.call("card.read", { path: c.path }, 20000);
  } catch (e) { toast(rpcErrText(e), true); return; }
  const ext = full.extensions && full.extensions.lunamoth ? full.extensions.lunamoth : {};
  const origin = String(ext.origin || "").trim() || String(full.description || "").trim();
  if (!origin) { toast(t("regen-no-origin"), true); return; }
  startDraftGeneration(origin, {});
  toast(t("regen-started"));
}

// Deck filter (R8b): "unwoken" = your own wakeable+editable OCs (drafts +
// non-builtin templates); "woken" = living charas' cards, all read-only (locked).
// Built-in recommended cards aren't in either list — they live behind the
// ✨默认 button (the carousel). This naturally splits read-only from editable.
let deckFilter = "unwoken";
function deckMatchesFilter(c) {
  if (deckFilter === "woken") return !!c.locked;       // a living chara's read-only card
  return !c.locked && !c.builtin;                       // your own editable OCs / drafts
}
function renderDeck() {
  if (!state.hub) return;
  const q = ($("deck-search").value || "").toLowerCase();
  const cards = state.hub.cards.filter(
    (c) => deckMatchesFilter(c) && (!q || c.name.toLowerCase().includes(q)));
  // pending generations belong to the "unwoken" (your own) view only
  const pending = deckFilter === "unwoken" ? state.pendingDrafts : [];
  const total = cards.length + pending.length;
  $("deck-count").textContent = total ? `· ${total}` : "";
  const grid = $("deck-grid");
  grid.innerHTML = "";
  if (!total) {
    grid.appendChild(el("div", { class: "deck-empty" },
      t(deckFilter === "woken" ? "deck-empty-woken" : "deck-empty-unwoken")));
    return;
  }
  // pending generations render first — they are NOT real cards yet
  for (const pd of pending) grid.appendChild(pendingSpine(pd));
  for (const c of cards) {
    const copyBtn = el("button", { onclick: async (ev) => {
      ev.stopPropagation();
      try { await duplicateCard(c); toast(t("copied")); refreshHub(); }
      catch (e) { toast(e.message, true); }
    } }, t("deck-copy"));
    const wakeBtn = el("button", { class: "wake", onclick: (ev) => { ev.stopPropagation(); ensureModel(() => openWakeSheet(c)); } }, t("deck-wake"));
    // Locked card (a living chara's own card): browse + copy + wake (wake copies);
    // never edit in place. Unlocked template: wake + view/edit + copy.
    const badge = c.locked
      ? el("div", { class: "lock-badge" }, c.owner ? t("deck-owned", { name: c.owner }) : t("deck-readonly"))
      : (c.draft ? el("div", { class: "draft-badge" }, t("deck-draft")) : null);
    const acts = c.locked
      ? el("div", { class: "spine-acts" }, wakeBtn, copyBtn)
      : el("div", { class: "spine-acts" },
          wakeBtn,
          el("button", { onclick: (ev) => { ev.stopPropagation(); viewCard(c); } }, t("deck-view")),
          c.draft ? el("button", { onclick: (ev) => { ev.stopPropagation(); ensureModel(() => regenerateDraftCard(c)); } }, t("deck-regen")) : null,
          copyBtn);
    const face = cardVisual(c, `face ${paletteClass(c.name)}`);
    if (badge) face.appendChild(badge);
    face.appendChild(acts);
    const sub = c.locked && c.owner
      ? t("deck-owned", { name: c.owner })
      : [c.tagline || c.world, c.builtin ? t("deck-builtin") : "", ...(c.tags || [])].filter(Boolean).slice(0, 3).join(" · ");
    grid.appendChild(el("div", { class: "spine" + (c.locked ? " locked" : ""), onclick: () => viewCard(c) },
      face,
      el("div", { class: "sbody" },
        el("div", { class: "sname" }, el("b", null, c.name), el("span", { class: "chip" }, c.lang)),
        el("div", { class: "sworld" }, sub))));
  }
}
$("deck-search").addEventListener("input", renderDeck);
$("deck-filterseg").addEventListener("click", (ev) => {
  const s = ev.target.closest("span[data-mode]");
  if (!s || s.classList.contains("on")) return;
  deckFilter = s.dataset.mode;
  // optimistic: flip the segmented control immediately, then re-render
  for (const sp of $("deck-filterseg").querySelectorAll("span")) sp.classList.toggle("on", sp === s);
  renderDeck();
});
$("deck-new").addEventListener("click", () => ensureModel(openCreateFlow));
$("deck-import").addEventListener("click", () => $("file-input").click());

/* 卡片视图 = 卡片编辑器：非内置 JSON 卡每一段都可直接改；内置卡只读（复制副本
   再编辑）；冻结卡可编辑——改动写回卡册，活着的 chara 保留唤醒时的冻结副本。
   原始 JSON 收在底部折叠（开发者向）。 */
async function viewCard(c) {
  let full;
  try {
    full = await hub.call("card.read", { path: c.path }, 20000);
  } catch (e) { toast(rpcErrText(e), true); return; }
  const ext = full.extensions && full.extensions.lunamoth ? full.extensions.lunamoth : {};
  const isJson = !!full.raw;
  // Editable = a user template. Builtins are read-only (copy to edit); a LOCKED
  // card (a living chara's own card) is browse-only — duplicate or re-wake to change.
  const editable = !c.builtin && !c.locked && isJson;
  const card = { name: full.name, theme: c.theme || (ext.theme || null),
                 theme_color: c.theme_color || ext.theme_color || "",
                 avatar_uri: c.avatar_uri || "", avatar_svg: c.avatar_svg || ext.avatar_svg || "" };
  const avatar = avatarNode(full.name, card, "avatar-s");
  avatar.style.cursor = "pointer";
  avatar.title = t("av-title");
  avatar.addEventListener("click", () => { closeModal(); openAvatarEditor(c); });
  const badges = el("div", { class: "cv-badges" },
    el("span", { class: "chip" }, full.language || c.lang),
    c.builtin ? el("span", { class: "chip" }, t("deck-builtin")) : null,
    ext.embodiment ? el("span", { class: "chip" }, ext.embodiment) : null,
    ext.toolpack ? el("span", { class: "chip" }, `⚒ ${ext.toolpack}`) : null,
    c.frozen ? el("span", { class: "chip" }, t("card-frozen-by", { names: (c.used_by || []).join("、") })) : null);

  function field(value, phKey) {
    const div = el("div", { class: "cve-text" }, value || "");
    if (editable) {
      div.setAttribute("contenteditable", "plaintext-only");
      if (phKey) div.dataset.ph = t(phKey);
    }
    return div;
  }
  // Context for in-character AI rewrites: snapshot the core identity fields at
  // rewrite time (the field nodes exist by the time the button is clicked).
  const editorCtx = () => cardCtxString({
    name: c.name || full.name,
    description: descField.textContent, personality: persField.textContent,
    scenario: scenField.textContent, tagline: taglineField.textContent,
  });
  const block = (labelKey, node, has, fieldKey) => {
    if (!(editable || has)) return null;
    const h = el("h4", null, t(labelKey));
    if (editable && fieldKey) h.appendChild(aiEditButton(node, fieldKey, editorCtx));
    return el("div", { class: "cv-block" }, h, node);
  };

  const nameField = field(full.name);
  nameField.classList.add("cve-name");
  const taglineValue = String(ext.tagline || c.tagline || "");
  const taglineField = field(taglineValue, "sec-tagline");
  taglineField.classList.add("tagline");
  const descField = field(full.description);
  const persField = field(full.personality);
  const scenField = field(full.scenario);
  const firstField = field(full.first_mes);
  const book = full.character_book && Array.isArray(full.character_book.entries) ? full.character_book : null;
  const worldText = sectionText({
    world_entries: (book ? book.entries : []).map((e2) => ({
      keys: e2.keys || [], content: e2.content || "", constant: !!e2.constant })),
  }, "world_entries");
  const worldField = field(worldText);
  // Seed wishes: read the new `wishes` key first, fall back to legacy `goals`.
  const wishesSrc = Array.isArray(ext.wishes) ? ext.wishes : (Array.isArray(ext.goals) ? ext.goals : []);
  const goalsText = wishesSrc.map(String).join("\n");
  const goalsField = field(goalsText);
  const notesField = field(full.creator_notes);
  // Advanced: override the neutral enter/leave conversation markers (passive fact
  // lines, {{user}}/{{char}} macros apply). Empty = the engine's neutral default.
  const onAttachField = field(String(ext.on_attach || ""), "cve-presence-ph");
  const onDetachField = field(String(ext.on_detach || ""), "cve-presence-ph");
  const advanced = (editable || ext.on_attach || ext.on_detach)
    ? el("details", { class: "cv-raw" },
        el("summary", null, t("cve-advanced")),
        el("div", { class: "cv-note" }, t("cve-presence-help")),
        block("cve-on-attach", onAttachField, true, "on_attach"),
        block("cve-on-detach", onDetachField, true, "on_detach"))
    : null;

  const note = c.builtin ? t("cv-builtin-note")
    : !isJson ? t("cv-png-note")
    : c.frozen ? t("av-frozen-note", { names: (c.used_by || []).join("、") })
    : "";

  const dupBtn = (c.builtin && isJson) ? el("button", { class: "btn soft", onclick: async () => {
    dupBtn.disabled = true;
    try {
      await duplicateCard(c);
      toast(t("copied"));
      closeModal();
      refreshHub();
    } catch (e) { dupBtn.disabled = false; toast(rpcErrText(e), true); }
  } }, t("deck-copy")) : null;

  const saveBtn = editable ? el("button", { class: "btn primary", onclick: async () => {
    saveBtn.disabled = true;
    try {
      const data = full.raw.data = full.raw.data || {};
      const newName = nameField.textContent.trim() || full.name;
      data.name = newName;
      full.raw.name = newName;
      data.description = descField.textContent;
      data.personality = persField.textContent;
      data.scenario = scenField.textContent;
      data.first_mes = firstField.textContent;
      data.creator_notes = notesField.textContent;
      data.extensions = data.extensions || {};
      const lm = data.extensions.lunamoth = data.extensions.lunamoth || {};
      const tagline = taglineField.textContent.trim();
      if (tagline) lm.tagline = tagline; else delete lm.tagline;
      const wishes = goalsField.textContent.split("\n").map((s) => s.trim()).filter(Boolean);
      if (wishes.length) lm.wishes = wishes; else delete lm.wishes;
      delete lm.goals;  // migrate the legacy key on save
      // Advanced: enter/leave conversation marker overrides (empty → neutral default)
      const onAttach = onAttachField.textContent.trim();
      if (onAttach) lm.on_attach = onAttach; else delete lm.on_attach;
      const onDetach = onDetachField.textContent.trim();
      if (onDetach) lm.on_detach = onDetach; else delete lm.on_detach;
      // world book: rebuild entries from the line format, keep the book's name
      const tmp = {};
      putSection(tmp, "world_entries", worldField.textContent);
      const entries = (tmp.world_entries || []).map((w, i) => ({
        keys: w.keys, content: w.content, constant: w.constant, enabled: true, insertion_order: i,
      }));
      const oldBook = data.character_book;
      if (entries.length || (oldBook && oldBook.name)) {
        data.character_book = { name: (oldBook && oldBook.name) || newName, entries };
      } else {
        delete data.character_book;
      }
      await hub.call("card.save", { data: full.raw, path: c.path }, 20000);
      toast(t("saved"));
      closeModal();
      refreshHub();
    } catch (e) {
      saveBtn.disabled = false;
      toast(rpcErrText(e), true);
    }
  } }, t("save")) : null;

  // ---- 设定 pane: the core identity fields + Advanced + raw JSON ----
  const setPane = el("div", { class: "cv-pane", "data-pane": "set" },
    block("cve-description", descField, !!full.description, "description"),
    block("cve-personality", persField, !!full.personality, "personality"),
    block("cve-scenario", scenField, !!full.scenario, "scenario"),
    block("cv-first", firstField, !!full.first_mes, "first_mes"),
    block("cve-goals", goalsField, !!goalsText, "goals"),
    block("cve-notes", notesField, !!full.creator_notes),
    advanced,
    full.raw ? el("details", { class: "cv-raw" },
      el("summary", null, t("cv-raw")),
      el("pre", null, JSON.stringify(full.raw, null, 2))) : null);

  // ---- 视觉 pane: 4 art tiles + theme swatches; themed placeholder fallback ----
  const artTile = (labelKey, url, sq, onClick) => {
    const art = el("div", { class: "cv-art" + (url ? "" : " empty " + paletteClass(full.name)) });
    if (url) art.style.backgroundImage = `url("${String(url).replace(/"/g, "%22")}")`;
    else art.appendChild(el("div", { class: "cv-art-glyph" }, glyphOf(full.name)));
    const cap = el("div", { class: "cv-art-cap" }, el("b", null, t(labelKey)),
      url ? null : el("span", { class: "cv-art-none" }, t("cv-art-none")));
    const tile = el("div", { class: "cv-tile" + (sq ? " sq" : "") + (onClick ? " clickable" : "") }, art, cap);
    if (onClick) tile.addEventListener("click", onClick);
    return tile;
  };
  const th = themeOf(c);
  const hasAnyArt = !!(c.sprite_url || c.keyvisual_url || c.bg_url || avatarSrc(card));
  const swatch = (labelKey, color) => color
    ? el("div", { class: "cv-swatch" }, el("i", { style: `background:${color}` }),
        el("span", null, `${t(labelKey)} ${color}`))
    : null;
  const themebar = (th.primary || th.secondary)
    ? el("div", { class: "cv-themebar" }, el("b", null, t("cv-theme-label")),
        swatch("cv-theme-primary", th.primary), swatch("cv-theme-secondary", th.secondary),
        el("span", { class: "cv-theme-note" }, t("cv-theme-note")))
    : null;
  const visPane = el("div", { class: "cv-pane", "data-pane": "vis", style: "display:none" });
  if (hasAnyArt) {
    visPane.appendChild(el("div", { class: "cv-tiles" },
      artTile("cv-art-sprite", c.sprite_url, false, null),
      artTile("cv-art-keyvisual", c.keyvisual_url, false, null),
      artTile("cv-art-bg", c.bg_url, true, null),
      artTile("cv-art-avatar", avatarSrc(card), true, () => { closeModal(); openAvatarEditor(c); })));
    if (themebar) visPane.appendChild(themebar);
  } else {
    // No art at all: one friendly themed placeholder + a neutral note.
    const ph = el("div", { class: "cv-empty-art " + paletteClass(full.name) },
      el("div", { class: "cv-art-glyph" }, glyphOf(full.name)));
    visPane.appendChild(el("div", { class: "cv-empty" }, ph, el("div", { class: "cv-empty-note" }, t("cv-no-art"))));
    if (themebar) visPane.appendChild(themebar);
  }

  // ---- 表情 pane: sticker thumbnails (display-only v1) or fallback note ----
  const emoPane = el("div", { class: "cv-pane", "data-pane": "emo", style: "display:none" });
  const stickers = Array.isArray(c.stickers_urls) ? c.stickers_urls.filter(Boolean) : [];
  if (stickers.length) {
    const grid = el("div", { class: "cv-emos" });
    for (const url of stickers) {
      const pic = el("div", { class: "cv-emo-pic" });
      pic.style.backgroundImage = `url("${String(url).replace(/"/g, "%22")}")`;
      grid.appendChild(el("div", { class: "cv-emo" }, pic));
    }
    emoPane.appendChild(grid);
  } else {
    emoPane.appendChild(el("div", { class: "cv-empty" },
      el("div", { class: "cv-empty-art " + paletteClass(full.name) }, el("div", { class: "cv-art-glyph" }, glyphOf(full.name))),
      el("div", { class: "cv-empty-note" }, t("cv-no-emo"))));
  }

  // ---- 世界 pane: editable text round-trip OR pretty read-only entries ----
  const worldPane = el("div", { class: "cv-pane", "data-pane": "world", style: "display:none" });
  if (editable) {
    worldPane.appendChild(block("cve-world", worldField, true, "world_entries"));
  } else {
    const entries = book ? book.entries : [];
    if (!entries.length) {
      worldPane.appendChild(el("div", { class: "cv-empty-note" }, t("cv-world-empty")));
    } else {
      entries.forEach((e2, i) => {
        const keys = (e2.keys || []).slice(0, 5).join(" · ");
        worldPane.appendChild(el("details", { class: "cv-we", open: i === 0 ? "" : null },
          el("summary", null,
            el("span", { class: "cv-st " + (e2.constant ? "const" : "kw") }, t(e2.constant ? "cv-world-const" : "cv-world-kw")),
            keys ? el("span", { class: "cv-we-keys" }, keys) : null),
          el("div", { class: "cv-we-body" }, String(e2.content || ""))));
      });
    }
  }

  // ---- tab bar (theme-color active underline) ----
  const panes = { set: setPane, vis: visPane, emo: emoPane, world: worldPane };
  const tabDefs = [["set", "cv-tab-set"], ["vis", "cv-tab-vis"], ["emo", "cv-tab-emo"], ["world", "cv-tab-world"]];
  const tabBar = el("div", { class: "cv-tabs" });
  const tabEls = {};
  for (const [key, labelKey] of tabDefs) {
    const tab = el("div", { class: "cv-tab" + (key === "set" ? " on" : ""), "data-tab": key }, t(labelKey));
    tabEls[key] = tab;
    tabBar.appendChild(tab);
  }
  tabBar.addEventListener("click", (ev) => {
    const tab = ev.target.closest(".cv-tab");
    if (!tab) return;
    const key = tab.dataset.tab;
    for (const [k, el2] of Object.entries(tabEls)) el2.classList.toggle("on", k === key);
    for (const [k, p] of Object.entries(panes)) p.style.display = (k === key) ? "" : "none";
  });

  const footer = el("div", { class: "cv-foot" },
    el("button", { class: "btn text", onclick: closeModal }, t("cancel")),
    el("div", { class: "grow" }),
    (!c.builtin && !c.frozen) ? el("button", { class: "btn soft", onclick: async () => {
      if (!confirm(t("deck-delete-q"))) return;
      try { await hub.call("card.delete", { path: c.path }, 10000); closeModal(); refreshHub(); }
      catch (e) { toast(e.message, true); }
    } }, t("menu-delete")) : null,
    dupBtn,
    saveBtn,
    el("button", { class: "btn primary go", onclick: () => { closeModal(); ensureModel(() => openWakeSheet(c)); } }, t("deck-wake")));

  const header = el("div", { class: "cv-header" }, avatar,
    el("div", { class: "cv-id" },
      nameField,
      (editable || taglineValue) ? taglineField : null,
      badges));

  const root = el("div", { class: "cardview" },
    note ? el("div", { class: "cv-note cv-note-top" }, note) : null,
    header, tabBar,
    el("div", { class: "cv-scroll" }, setPane, visPane, emoPane, worldPane),
    footer);
  const themeCss = themeStyle(c);
  if (themeCss) root.setAttribute("style", themeCss);
  openModal(root, "wide");
}

/* card import: file picker + whole-window drag-drop */
async function importCardFile(file) {
  try {
    const buf = await file.arrayBuffer();
    const resp = await fetch(`/upload?token=${encodeURIComponent(BOOT.token)}`, {
      method: "POST", body: buf, headers: { "X-Filename": file.name },
    });
    if (!resp.ok) throw new Error(await resp.text());
    toast(t("imported", { name: file.name }));
    await refreshHub();
    closeFirstRun();
    navTo("#/deck");
  } catch (e) { toast(e.message, true); }
}
$("file-input").addEventListener("change", () => {
  const f = $("file-input").files[0];
  if (f) importCardFile(f);
  $("file-input").value = "";
});
let dragDepth = 0;
document.addEventListener("dragenter", (ev) => { ev.preventDefault(); if (++dragDepth) document.body.classList.add("dragging"); });
document.addEventListener("dragleave", () => { if (--dragDepth <= 0) { dragDepth = 0; document.body.classList.remove("dragging"); } });
document.addEventListener("dragover", (ev) => ev.preventDefault());
document.addEventListener("drop", (ev) => {
  ev.preventDefault();
  dragDepth = 0;
  document.body.classList.remove("dragging");
  const f = ev.dataTransfer.files && ev.dataTransfer.files[0];
  if (f && (f.name.endsWith(".json") || f.name.endsWith(".png"))) importCardFile(f);
});

/* ============================ MODEL SETUP (first-run step 2 + settings·模型) ============================ */
function setupPane(opts) {
  // opts: {firstrun: bool, onDone: fn}
  const d = (state.hub && state.hub.defaults) || {};
  const presets = (state.hub && state.hub.presets) || {};
  const local = { provider: d.provider || "openrouter", base_url: d.base_url || "https://openrouter.ai/api/v1",
                  model: d.model || "deepseek/deepseek-v4-flash", api_key: "" };
  const root = el("div", null);
  root.appendChild(el("h1", null, t("setup-title")));
  root.appendChild(el("div", { class: "sub" }, t("setup-sub")));
  if (!opts.firstrun) {
    // Hermes 的措辞：默认 vs 热切换分清楚；Fallback 的位置渲染成不回退声明。
    root.appendChild(el("div", { class: "scope-note" }, t("default-scope")));
    root.appendChild(el("div", { class: "no-fallback-row" }, "⊘ ", t("no-fallback")));
  }

  const provRows = [];
  function pickProvider(key) {
    const p = presets[key] || {};
    local.provider = p.provider || "openai_compatible";
    local.base_url = p.base_url || "";
    if (p.model) local.model = p.model;
    provRows.forEach((r) => r.node.classList.toggle("on", r.key === key));
    baseRow.style.display = key === "_custom" ? "flex" : "none";
    modelInput.value = local.model;
    result.classList.remove("show");
  }
  function provRow(key, label, desc, rec) {
    const node = el("button", { class: "provider", onclick: () => pickProvider(key) },
      el("div", null,
        el("div", { class: "pname" }, label, rec ? el("span", { class: "rec" }, t("rec")) : null),
        desc ? el("div", { class: "pdesc" }, desc) : null),
      el("div", { class: "radio" }));
    provRows.push({ key, node });
    return node;
  }
  // 每行一句认证机制说明（Hermes providers-settings 的风格），灰字走现有 .pdesc。
  root.appendChild(provRow("OpenRouter", "OpenRouter", t("or-desc"), true));
  root.appendChild(provRow("OpenAI", "OpenAI", t("prov-openai-desc"), false));
  const moreWrap = el("div", { style: "display:none" },
    provRow("Ollama (local)", "Ollama", t("prov-ollama-desc"), false),
    provRow("_custom", "OpenAI-compatible", t("prov-compat-desc"), false));
  const moreBtn = el("button", { class: "provider more", onclick: () => {
    moreWrap.style.display = moreWrap.style.display === "none" ? "block" : "none";
  } }, t("more-providers"));
  root.appendChild(moreBtn);
  root.appendChild(moreWrap);

  const baseInput = el("input", { placeholder: t("base-ph"), value: "" });
  const baseRow = el("div", { class: "keyrow", style: "display:none" }, el("div", { class: "input-like" }, baseInput));
  root.appendChild(baseRow);

  const keyInput = el("input", { type: "password", placeholder: d.has_key ? "••••••••  (已保存 / saved)" : t("key-ph") });
  const testBtn = el("button", { class: "btn soft" }, t("test"));
  root.appendChild(el("div", { class: "keyrow" }, el("div", { class: "input-like" }, keyInput), testBtn));

  const okline = el("div", { class: "okline" });
  const modelInput = el("input", { value: local.model, list: "model-list" });
  const modelRow = el("div", { class: "modelrow" }, el("span", null, t("default-model")),
    el("div", { class: "input-like", style: "flex:1" }, modelInput));
  const badges = el("div", { class: "capbadges" });
  const result = el("div", { class: "test-result" }, okline, modelRow, badges,
    el("div", { class: "cap-hint" }, t("cap-hint")));
  root.appendChild(result);

  let lastTest = null;
  async function runTest() {
    testBtn.textContent = t("testing");
    testBtn.disabled = true;
    result.classList.add("show");
    okline.className = "okline";
    okline.textContent = t("testing");
    badges.innerHTML = "";
    const params = {
      provider: local.provider,
      base_url: baseRow.style.display !== "none" && baseInput.value ? baseInput.value : local.base_url,
      api_key: keyInput.value.trim(),
      model: modelInput.value.trim() || local.model,
    };
    if (params.base_url) local.base_url = params.base_url;
    try {
      const r = await hub.call("key.test", params, 60000);
      lastTest = r;
      if (r.ok) {
        okline.textContent = t("connected");
        renderCaps(badges, r.capabilities);
      } else {
        okline.className = "okline bad";
        okline.textContent = "✗ " + errText(r.error);
      }
    } catch (e) {
      okline.className = "okline bad";
      okline.textContent = "✗ " + rpcErrText(e);
    } finally {
      testBtn.textContent = t("test");
      testBtn.disabled = false;
    }
  }
  testBtn.addEventListener("click", runTest);

  const laterBtn = opts.firstrun
    ? el("button", { class: "btn text", onclick: () => { closeFirstRun(); navTo("#/"); } }, t("later"))
    : el("span");
  const goBtn = el("button", { class: "btn primary big", onclick: async () => {
    const payload = {
      provider: local.provider, base_url: local.base_url,
      model: modelInput.value.trim() || local.model, ui_lang: getLangCode(),
    };
    if (keyInput.value.trim()) payload.api_key = keyInput.value.trim();
    try {
      const saved = await hub.call("defaults.set", payload, 15000);
      await refreshHub();
      toast(t("saved"));
      if (saved && saved.key_update_candidates && saved.key_update_candidates.length) {
        promptKeyUpdate(saved.key_update_candidates);
      }
      if (opts.onDone) opts.onDone();
    } catch (e) { toast(e.message, true); }
  } }, t("continue"));
  root.appendChild(el("div", { class: "setup-acts" }, laterBtn, el("div", { class: "grow" }), goBtn));
  pickProvider("OpenRouter");
  return root;
}

function renderCaps(container, caps) {
  container.innerHTML = "";
  if (!caps) return;
  if (caps.tools === false) container.appendChild(el("span", { class: "capbadge warn" }, t("cap-tool-no")));
  else if (caps.tools) container.appendChild(el("span", { class: "capbadge" }, t("cap-tool")));
  if (caps.writing) container.appendChild(el("span", { class: "capbadge star" }, t("cap-write")));
  if (caps.vision) container.appendChild(el("span", { class: "capbadge off" }, t("cap-mm")));
}

function renderModelPane() {
  const pane = $("pane-model");
  pane.innerHTML = "";
  pane.appendChild(setupPane({ firstrun: false, onDone: null }));
}

function promptKeyUpdate(candidates) {
  const rows = (candidates || []).map((c) => ({ entry: c, checked: true }));
  if (!rows.length) return;
  const list = el("div", { class: "key-update-list" });
  function refreshCount() {
    const n = rows.filter((r) => r.checked).length;
    applyBtn.textContent = t("key-update-apply", { n });
    applyBtn.disabled = n ? false : true;
  }
  const allBox = el("input", { type: "checkbox", checked: "" });
  const applyBtn = el("button", { class: "btn primary big" });
  allBox.addEventListener("change", () => {
    rows.forEach((r) => { r.checked = allBox.checked; });
    list.querySelectorAll("input[type=checkbox]").forEach((box) => { box.checked = allBox.checked; });
    refreshCount();
  });
  for (const row of rows) {
    const box = el("input", { type: "checkbox", checked: "" });
    box.addEventListener("change", () => {
      row.checked = box.checked;
      allBox.checked = rows.every((r) => r.checked);
      refreshCount();
    });
    list.appendChild(el("label", { class: "check-row" },
      box,
      el("span", null, el("b", null, row.entry.char_name || row.entry.name), " ", el("small", null, row.entry.name)),
      row.entry.model ? el("code", null, row.entry.model) : null));
  }
  applyBtn.addEventListener("click", async () => {
    const names = rows.filter((r) => r.checked).map((r) => r.entry.name);
    if (!names.length) return;
    applyBtn.disabled = true;
    try {
      const r = await hub.call("defaults.apply_key", { names }, 30000);
      closeModal();
      toast(t("key-update-done", { n: (r.updated || []).length }));
      await refreshHub();
    } catch (e) {
      applyBtn.disabled = false;
      toast(rpcErrText(e), true);
    }
  });
  openModal(el("div", null,
    el("h2", null, t("key-update-title")),
    el("div", { class: "sub" }, t("key-update-sub")),
    el("label", { class: "check-row select-all" }, allBox, el("b", null, t("select-all"))),
    list,
    el("div", { class: "acts", style: "margin-top:16px" },
      el("button", { class: "btn text", onclick: closeModal }, t("later")),
      el("div", { class: "grow" }),
      applyBtn)), true);
  refreshCount();
}

/* settings interactions */
$("settings-nav").addEventListener("click", (ev) => {
  const btn = ev.target.closest("button");
  if (!btn) return;
  document.querySelectorAll("#settings-nav button").forEach((b) => b.classList.toggle("on", b === btn));
  document.querySelectorAll(".settings-pane").forEach((p) => p.classList.toggle("on", p.id === "pane-" + btn.dataset.pane));
});
$("theme-seg").addEventListener("click", (ev) => {
  const s = ev.target.closest("span");
  if (s) { applyTheme(s.dataset.th); hub.call("defaults.set", { ui_theme: s.dataset.th }).catch(() => {}); }
});
$("lang-seg").addEventListener("click", (ev) => {
  const s = ev.target.closest("span");
  if (s) setLang(s.dataset.lang, true);
});
$("display-seg").addEventListener("click", (ev) => {
  const s = ev.target.closest("span");
  if (s) applyDisplayMode(s.dataset.disp);
});
$("reveal-home").addEventListener("click", () => {
  if (state.hub) hub.call("open.path", { path: state.hub.home, reveal: true }).catch((e) => toast(e.message, true));
});
// The bg/sprite/position controls now live in the chat panel's per-session
// settings pane (chat.js renderSettingsPane), wired there per-chara. applyVisualPrefs()
// (called on load and on chat open) still applies the CSS vars and reflects the
// controls when they exist.

/* ============================ FIRST RUN ============================ */
let frPendingAction = null;

function openFirstRun() {
  $("overlay-firstrun").classList.add("open");
  frShowWelcome();
}
function closeFirstRun() { $("overlay-firstrun").classList.remove("open"); }
function frShowWelcome() {
  $("fr-welcome").style.display = "flex";
  $("fr-setup").style.display = "none";
  const pk = $("fr-picker");
  if (pk) pk.style.display = "none";
  $("fr-dots").innerHTML = "<i class='on'></i><i></i>";
  decorateDefaultCard();
}

// Render the resolved default card's name + tagline beside the generic
// "try the default character" labels (first-run + board empty-state).
function decorateDefaultCard() {
  const card = defaultLunaCard();
  // The "try" button now opens the recommended-character carousel (not a single
  // default), so it keeps its generic label — no per-card name/tagline append.
  const trySpan = document.querySelector("#fr-try span[data-i18n='btn-try']");
  const trySub = document.querySelector("#fr-try small[data-i18n='btn-try-sub']");
  if (trySpan) trySpan.textContent = t("btn-try");
  if (trySub) trySub.textContent = "";
  const meet = $("empty-meet");
  if (meet) {
    const base = t("meet-luna");
    meet.textContent = card ? `${base} · ${card.name}` : base;
  }
}
function frShowSetup() {
  $("fr-welcome").style.display = "none";
  const pk = $("fr-picker");
  if (pk) pk.style.display = "none";
  const setup = $("fr-setup");
  setup.style.display = "block";
  setup.innerHTML = "";
  setup.appendChild(setupPane({ firstrun: true, onDone: () => {
    closeFirstRun();
    const act = frPendingAction;
    frPendingAction = null;
    if (act) act();
  } }));
  $("fr-dots").innerHTML = "<i class='done'></i><i class='on'></i>";
}

function ensureModel(action) {
  const d = (state.hub && state.hub.defaults) || {};
  if (d.has_key && d.base_url) { action(); return; }
  frPendingAction = action;
  $("overlay-firstrun").classList.add("open");
  frShowSetup();
}

function defaultLunaCard() {
  const cards = (state.hub && state.hub.cards) || [];
  // The default card is the bundled one whose tags contain "default".
  // Resolve dynamically so the welcome follows whichever card carries the tag.
  // Backend surfaces an explicit `default` flag (the "default" tag can sit past
  // the 4-tag display cap); fall back to a tag scan for older hubs.
  const tagged = cards.find((c) => c.builtin && (c.default || (c.tags || []).includes("default")));
  if (tagged) return tagged;
  // No tag (older hub): fall back to the first bundled card, preferring the
  // shell's language — no character name is hard-coded here.
  const wantZh = getLangCode() === "zh";
  return cards.find((c) => c.builtin && c.lang === (wantZh ? "zh" : "en")) ||
         cards.find((c) => c.builtin);
}

async function wakeDefaultLuna() {
  const card = defaultLunaCard();
  if (!card) { toast("default card missing", true); return; }
  try {
    const entry = await hub.call("session.wake", { card: card.path }, 60000);
    closeFirstRun();
    await refreshHub();
    openChat(entry.name);
  } catch (e) { toast(e.message, true); }
}

$("fr-langseg").addEventListener("click", (ev) => {
  const s = ev.target.closest("span");
  if (s) setLang(s.dataset.lang, false);
});
// "Pick a recommended character" → the character-select carousel (R8). It
// replaces the old single "try the default" button: the carousel renders all
// eight built-ins; tapping a tile routes through ensureModel → openWakeSheet
// (so model setup still happens when there's no key — see selectBuiltin).
$("fr-try").addEventListener("click", () => frShowPicker());
$("fr-create").addEventListener("click", () => { frPendingAction = () => { closeFirstRun(); openCreateFlow(); }; frShowSetup(); });
$("fr-import").addEventListener("click", () => $("file-input").click());

// Show the carousel inside the first-run overlay (replacing the welcome column).
function frShowPicker() {
  $("fr-welcome").style.display = "none";
  $("fr-setup").style.display = "none";
  const host = $("fr-picker");
  host.style.display = "block";
  host.innerHTML = "";
  host.appendChild(buildBuiltinPicker());
  applyI18n(host);
  $("fr-dots").innerHTML = "<i class='done'></i><i class='on'></i>";
}

// The standalone picker overlay (reopened from the card deck).
function openBuiltinPicker() {
  const host = $("picker-host");
  host.innerHTML = "";
  host.appendChild(buildBuiltinPicker());
  applyI18n(host);
  $("overlay-picker").classList.add("open");
}
function closeBuiltinPicker() { $("overlay-picker").classList.remove("open"); }
$("picker-close").addEventListener("click", closeBuiltinPicker);
$("overlay-picker").addEventListener("click", (ev) => {
  if (ev.target === $("overlay-picker")) closeBuiltinPicker();
});
$("deck-picker").addEventListener("click", openBuiltinPicker);

/* ============================ WAKE SHEET ============================ */
async function modelsCached() {
  if (state.models) return state.models;
  try {
    state.models = await hub.call("models.list", {}, 30000);
  } catch (e) { state.models = []; }
  // datalist for model autocomplete everywhere
  let dl = $("model-list");
  if (!dl) { dl = el("datalist", { id: "model-list" }); document.body.appendChild(dl); }
  dl.innerHTML = "";
  for (const m of state.models.slice(0, 400)) dl.appendChild(el("option", { value: m.id }));
  return state.models;
}

async function openWakeSheet(card) {
  const d = (state.hub && state.hub.defaults) || {};
  let isolation = "sandbox";
  // Network ON by default at wake — matches the runtime default (state.py
  // DEFAULT_STATUS) and lets a fresh chara reach the web (e.g. to research and
  // build its own site). The operator can still toggle it off here.
  let wantNet = true;
  const models = await modelsCached();

  const nameInput = el("input", { value: card.name });
  const modelInput = el("input", { value: d.model || "", list: "model-list" });
  const capLine = el("div", { class: "capbadges", style: "margin:6px 0 0" });
  const warnLine = el("div", { class: "amber-note", style: "display:none" }, t("wake-no-tools"));
  function refreshCaps() {
    const m = models.find((x) => x.id === modelInput.value.trim());
    renderCaps(capLine, m ? { tools: m.tools, writing: m.writing, vision: m.vision } : null);
    warnLine.style.display = m && m.tools === false ? "block" : "none";
  }
  modelInput.addEventListener("input", refreshCaps);

  const isoOptions = [
    ["dir", t("iso-dir"), t("iso-dir-d")],
    ["sandbox", t("iso-sandbox"), t("iso-sandbox-d")],
    ["docker", t("iso-docker"), t("iso-docker-d")],
  ];
  const isoSeg = el("div", { class: "iso-seg" });
  for (const [key, label, desc] of isoOptions) {
    const opt = el("div", { class: key === isolation ? "on" : "", onclick: () => {
      isolation = key;
      isoSeg.querySelectorAll(":scope>div").forEach((n, i) => n.classList.toggle("on", isoOptions[i][0] === key));
    } }, el("b", null, label), el("span", null, desc));
    isoSeg.appendChild(opt);
  }
  const netSwitch = el("button", { class: "switch" + (wantNet ? " on" : ""), onclick: () => {
    wantNet = !wantNet;
    netSwitch.classList.toggle("on", wantNet);
  } });

  // toolpack：卡片提的是期望，操作员在此授予（toolpacks.list 缺席时退回 datalist）
  let cardPack = "";
  try {
    const full = await hub.call("card.read", { path: card.path }, 20000);
    const ext = full.raw && full.raw.data && full.raw.data.extensions && full.raw.data.extensions.lunamoth;
    if (ext && ext.toolpack) cardPack = String(ext.toolpack);
  } catch (e) { /* keep default */ }
  let packs = null;
  try {
    packs = await hub.call("toolpacks.list", {}, 15000);
    if (!Array.isArray(packs)) packs = null;
  } catch (e) {
    packs = null;
    if (e && e.code !== -32601) toast(rpcErrText(e), true);
  }
  const packInput = el("input", { value: cardPack || "sandbox" });
  let packPicker = null;
  if (packs && packs.length) {
    packPicker = el("div", { class: "pack-list" });
    const syncPicked = () => {
      const v = packInput.value.trim();
      packPicker.querySelectorAll(".pack-option").forEach((n) =>
        n.classList.toggle("on", n.dataset.pack === v));
    };
    for (const p of packs) {
      const tools = Array.isArray(p.tools) ? p.tools : [];
      packPicker.appendChild(el("div", {
        class: "pack-option", "data-pack": p.name,
        onclick: () => { packInput.value = p.name; syncPicked(); },
      },
        el("div", { class: "pack-head" },
          el("b", null, p.name),
          p.description ? el("span", null, p.description) : null),
        tools.length ? el("div", { class: "tool-chips" },
          ...tools.slice(0, 10).map((tn) => el("span", { class: "chip" }, String(tn))),
          tools.length > 10 ? el("span", { class: "chip" }, `+${tools.length - 10}`) : null) : null));
    }
    packInput.addEventListener("input", syncPicked);
    syncPicked();
  } else {
    let tpl = $("toolpack-list");
    if (!tpl) { tpl = el("datalist", { id: "toolpack-list" }); document.body.appendChild(tpl); }
    tpl.innerHTML = "";
    for (const v of new Set(["sandbox", cardPack].filter(Boolean))) tpl.appendChild(el("option", { value: v }));
    packInput.setAttribute("list", "toolpack-list");
  }

  // embodiment：唤醒时一次定下、整段生命不热切换（保护提示词缓存）；默认随卡
  let emb = card.embodiment === "actor" ? "actor" : "literal";
  const embGrid = el("div", { class: "embodiment-grid" },
    ...["literal", "actor"].map((mode) => {
      const opt = el("div", { class: "emb-option" + (emb === mode ? " on" : "") },
        el("b", null, mode),
        el("span", null, t("emb-" + mode)));
      opt.addEventListener("click", () => {
        emb = mode;
        embGrid.querySelectorAll(".emb-option").forEach((n) => n.classList.remove("on"));
        opt.classList.add("on");
      });
      return opt;
    }));

  // ---- STEP 1: content editor (wake ≈ edit). Load the card and render the same
  // editable fields as the card editor, each with a per-field ✦ AI rewrite. ----
  let fullCard = null;
  try { fullCard = await hub.call("card.read", { path: card.path }, 20000); } catch (e) { fullCard = null; }
  const rawCard = (fullCard && fullCard.raw) || { version: "1.0", data: {} };
  if (!rawCard.data) rawCard.data = {};
  const ext0 = (rawCard.data.extensions && rawCard.data.extensions.lunamoth) || {};
  const charName = (fullCard && fullCard.name) || card.name;

  const fName = cardFieldEl(charName, null, true); fName.classList.add("cve-name");
  const fUserName = cardFieldEl(String(ext0.user_name || ""), "sec-user-name", true);
  const fUserPersona = cardFieldEl(String(ext0.user_persona || ""), null, true);
  const fDesc = cardFieldEl((fullCard && fullCard.description) || "", null, true);
  const fPers = cardFieldEl((fullCard && fullCard.personality) || "", null, true);
  const fScen = cardFieldEl((fullCard && fullCard.scenario) || "", null, true);
  const fFirst = cardFieldEl((fullCard && fullCard.first_mes) || "", null, true);
  const fTagline = cardFieldEl(String(ext0.tagline || card.tagline || ""), "sec-tagline", true);
  const wishesSrc0 = Array.isArray(ext0.wishes) ? ext0.wishes : (Array.isArray(ext0.goals) ? ext0.goals : []);
  const goalsText = wishesSrc0.map(String).join("\n");
  const fGoals = cardFieldEl(goalsText, null, true);
  const book = (fullCard && fullCard.character_book && Array.isArray(fullCard.character_book.entries)) ? fullCard.character_book : null;
  const worldText = sectionText({ world_entries: (book ? book.entries : []).map((e2) => ({ keys: e2.keys || [], content: e2.content || "", constant: !!e2.constant })) }, "world_entries");
  const fWorld = cardFieldEl(worldText, null, true);
  const fOnAttach = cardFieldEl(String(ext0.on_attach || ""), "cve-presence-ph", true);
  const fOnDetach = cardFieldEl(String(ext0.on_detach || ""), "cve-presence-ph", true);

  const wakeCtx = () => cardCtxString({
    name: fName.textContent, description: fDesc.textContent,
    personality: fPers.textContent, scenario: fScen.textContent, tagline: fTagline.textContent,
  });
  const step1Body = el("div", { class: "wake-content" },
    cardBlockEl("sec-name", fName, null, wakeCtx),
    cardBlockEl("sec-user-name", fUserName, "user_name", wakeCtx),
    cardBlockEl("sec-user-persona", fUserPersona, "user_persona", wakeCtx),
    cardBlockEl("cve-description", fDesc, "description", wakeCtx),
    cardBlockEl("cve-personality", fPers, "personality", wakeCtx),
    cardBlockEl("cve-scenario", fScen, "scenario", wakeCtx),
    cardBlockEl("cv-first", fFirst, "first_mes", wakeCtx),
    cardBlockEl("sec-tagline", fTagline, "tagline", wakeCtx),
    cardBlockEl("cve-goals", fGoals, "goals", wakeCtx),
    cardBlockEl("cve-world", fWorld, "world_entries", wakeCtx),
    cardBlockEl("cve-on-attach", fOnAttach, "on_attach", wakeCtx),
    cardBlockEl("cve-on-detach", fOnDetach, "on_detach", wakeCtx));

  function collectCardData() {
    const data = rawCard.data;
    data.name = fName.textContent.trim() || charName;
    rawCard.name = data.name;
    data.description = fDesc.textContent;
    data.personality = fPers.textContent;
    data.scenario = fScen.textContent;
    data.first_mes = fFirst.textContent;
    data.extensions = data.extensions || {};
    const lm = data.extensions.lunamoth = data.extensions.lunamoth || {};
    const setOrDel = (k, node) => { const v = node.textContent.trim(); if (v) lm[k] = v; else delete lm[k]; };
    setOrDel("user_name", fUserName);
    setOrDel("user_persona", fUserPersona);
    setOrDel("tagline", fTagline);
    setOrDel("on_attach", fOnAttach);
    setOrDel("on_detach", fOnDetach);
    const wishes = fGoals.textContent.split("\n").map((s) => s.trim()).filter(Boolean);
    if (wishes.length) lm.wishes = wishes; else delete lm.wishes;
    delete lm.goals;  // migrate the legacy key on save
    if (packInput.value.trim()) lm.toolpack = packInput.value.trim();
    const tmp = {};
    putSection(tmp, "world_entries", fWorld.textContent);
    const entries = (tmp.world_entries || []).map((w, i) => ({ keys: w.keys, content: w.content, constant: w.constant, enabled: true, insertion_order: i }));
    if (entries.length || (data.character_book && data.character_book.name)) {
      data.character_book = { name: (data.character_book && data.character_book.name) || data.name, entries };
    } else { delete data.character_book; }
    return rawCard;
  }

  // ---- STEP 2: settings — what TA lives in / thinks with (frozen at wake) ----
  const goBtn = el("button", { class: "btn primary big" }, t("wake-go"));
  goBtn.addEventListener("click", async () => {
    goBtn.disabled = true;
    try {
      const entry = await hub.call("session.wake", {
        card: card.path, name: nameInput.value.trim(), isolation,
        model: modelInput.value.trim(), toolpack: packInput.value.trim() || "sandbox",
        embodiment: emb, card_data: collectCardData(),
      }, 60000);
      closeModal();
      await refreshHub();
      openChat(entry.name, { netOn: wantNet });
    } catch (e) {
      toast(e.message, true);
      goBtn.disabled = false;
    }
  });

  const step2Body = el("div", { class: "wake-settings" },
    el("div", { class: "field-row" }, el("label", null, t("wake-name")), el("div", { class: "input-like" }, nameInput)),
    el("div", { class: "field-row" }, el("label", null, t("wake-model")),
      el("div", { class: "input-like" }, modelInput), capLine, warnLine),
    el("div", { class: "field-row" },
      el("label", null, t("wake-toolpack")),
      el("div", { class: "input-like" }, packInput,
        cardPack ? el("span", { class: "cue" }, t("wake-toolpack-card", { name: cardPack })) : null),
      packPicker),
    el("div", { class: "field-row" }, el("label", null, t("wake-emb")), embGrid),
    el("div", { class: "field-row" }, el("label", null, t("wake-iso")), isoSeg),
    el("div", { class: "field-row" },
      el("div", { class: "switch-row", style: "font-size:12.5px" },
        el("b", { style: "font-weight:550" }, t("p-net")),
        el("small", null, t("p-net-sub")),
        netSwitch)));

  // ---- two-step shell ----
  const container = el("div");
  function showStep(n) {
    container.innerHTML = "";
    const dots = el("div", { class: "wake-steps" },
      el("i", { class: n === 1 ? "on" : "done" }), el("i", { class: n === 2 ? "on" : "" }));
    if (n === 1) {
      container.append(
        el("h2", null, t("wake-edit-title", { name: charName })), dots,
        el("div", { class: "sub" }, t("wake-edit-sub")), step1Body,
        el("div", { class: "acts", style: "margin-top:18px" },
          el("button", { class: "btn text", onclick: closeModal }, t("cancel")),
          el("div", { class: "grow" }),
          el("button", { class: "btn primary big", onclick: () => showStep(2) }, t("wake-continue"))));
    } else {
      container.append(
        el("h2", null, t("wake-title", { name: charName })), dots,
        el("div", { class: "sub" }, t("wake-sub")), step2Body,
        el("div", { class: "acts", style: "margin-top:18px" },
          el("button", { class: "btn text", onclick: () => showStep(1) }, t("wake-back")),
          el("div", { class: "grow" }),
          goBtn));
      refreshCaps();
    }
  }
  showStep(1);
  openModal(container, true);
}

/* ============================ CREATE FLOW（工坊：讲述 → 成形 → 落卡） ============================ */
/* AI 重写链只跟这些段；名字对/用户设定/视觉/embodiment 单独渲染。 */
const SECTION_DEFS = [
  ["description", "sec-description"],
  ["personality", "cve-personality"],
  ["scenario", "cve-scenario"],
  ["first_mes", "sec-first"],
  ["world_entries", "sec-world"],
  ["seed_goals", "sec-goals"],
  ["tagline", "sec-tagline"],
];

function normalizeDraft(d) {
  const draft = Object.assign({}, d || {});
  draft.name = String(draft.name || "");
  draft.user_name = String(draft.user_name || "");
  draft.user_persona = String(draft.user_persona || "");
  draft.description = String(draft.description || draft.appearance || "");
  draft.first_mes = String(draft.first_mes || "");
  if (!Array.isArray(draft.world_entries)) {
    draft.world_entries = (draft.world || []).map((w) => ({
      keys: w.keys || (w.key ? [w.key] : []),
      content: w.content || w.desc || "",
      constant: !!w.constant,
    }));
  }
  if (!Array.isArray(draft.seed_goals)) draft.seed_goals = Array.isArray(draft.goals) ? draft.goals : [];
  draft.tagline = String(draft.tagline || "");
  const isHex = (v) => /^#[0-9a-fA-F]{6}$/.test(String(v || ""));
  const th = draft.theme && typeof draft.theme === "object" ? draft.theme : {};
  const primary = isHex(th.primary) ? String(th.primary).toUpperCase()
    : (isHex(draft.theme_color) ? String(draft.theme_color).toUpperCase() : "#5B9FD4");
  const secondary = isHex(th.secondary) ? String(th.secondary).toUpperCase() : "";
  draft.theme = { primary, secondary };
  delete draft.theme_color;
  draft.avatar_svg = String(draft.avatar_svg || "");
  // A pending sidecar upload (raster avatar chosen before the card exists).
  draft.pending_avatar = draft.pending_avatar || null;
  draft.embodiment = draft.embodiment === "actor" ? "actor" : "literal";
  return draft;
}

function openCreateFlow() {
  const root = $("flow-root");
  const flow = {
    origin: "",
    draft: null,
    lastDraftAt: 0,
    versions: {},   // section -> [v0, v1, ...] (strings)
    edited: {},
  };
  $("overlay-flow").classList.add("open");
  renderTellStep(root, flow);
}
function closeCreateFlow() { $("overlay-flow").classList.remove("open"); }
$("overlay-flow").addEventListener("click", (ev) => {
  if (ev.target === $("overlay-flow")) closeCreateFlow();
});

function flowSteps(active) {
  const names = [t("flow-tell"), t("flow-shape")];
  const bar = el("div", { class: "flow-steps" });
  names.forEach((n, i) => {
    if (i) bar.appendChild(el("i"));
    if (i < active) bar.appendChild(el("span", { class: "done" }, "✓ " + n));
    else if (i === active) bar.appendChild(el("b", null, n));
    else bar.appendChild(el("span", null, n));
  });
  return bar;
}

function sectionText(draft, key) {
  if (key === "world_entries") {
    return (draft.world_entries || []).map((w) =>
      `${(w.keys || []).join(", ")} — ${w.content || ""}${w.constant ? " [constant]" : ""}`).join("\n");
  }
  if (key === "seed_goals") return (draft.seed_goals || []).join("\n");
  return draft[key] || "";
}
function putSection(draft, key, text) {
  if (key === "world_entries") {
    draft.world_entries = text.split("\n").map((line) => {
      const constant = /\[(constant|常驻)\]/i.test(line);
      const clean = line.replace(/\[(constant|常驻)\]/ig, "").trim();
      const m = clean.split("—");
      return m.length > 1 ? {
        keys: m[0].split(/[,，]/).map((s) => s.trim()).filter(Boolean),
        content: m.slice(1).join("—").trim(),
        constant,
      } : null;
    }).filter(Boolean);
  } else if (key === "seed_goals") {
    draft.seed_goals = text.split(/\n|·/).map((s) => s.trim()).filter(Boolean);
  } else {
    draft[key] = text;
    if (key === "name") draft.name = draft.name.trim();
  }
}

function safeSvgForPreview(svg) {
  const s = String(svg || "").trim();
  const low = s.toLowerCase();
  return s.length <= 1500 && low.startsWith("<svg") && /\bviewbox\s*=\s*["']0\s+0\s+64\s+64["']/i.test(s) &&
    !/<\s*\/?\s*script(?:\s|>|\/)/i.test(s) && !/<\s*\/?\s*foreignobject(?:\s|>|\/)/i.test(s) &&
    !/<\s*\/?\s*text(?:\s|>|\/)/i.test(s) &&
    !/\son[a-zA-Z0-9_.:-]*\s*=/.test(s) &&
    !/\b(?:href|xlink:href)\s*=\s*["']\s*(?!#)[^"']+["']|url\(\s*["']?\s*(?!#)[^)]+/i.test(s);
}

/* ---------- shared avatar controls (presentation editor) ----------
   ONE builder for both the create flow and the deck editor. `work` is the
   live model: { name, avatar_uri, avatar_svg, pending_avatar, theme }.
   pending_avatar = a chosen raster file not yet on disk: {data_b64, ext, mime}.
   No raw-SVG textarea — just preview, Upload, AI 生成 (loading → confirm/cancel),
   and two theme-color pickers. opts.cardPath (optional) gives the generator the
   character's persona for context; opts.disabled greys the editor (builtin). */
const AVATAR_UPLOAD_MAX = 1024 * 1024;
const AVATAR_EXTS = ["png", "jpg", "jpeg", "svg"];

function buildAvatarControls(work, opts) {
  opts = opts || {};
  const disabled = !!opts.disabled;
  work.theme = work.theme || { primary: "", secondary: "" };

  const preview = el("div", { class: "av-preview" });
  function previewSrc() {
    if (work.pending_avatar) return `data:${work.pending_avatar.mime};base64,${work.pending_avatar.data_b64}`;
    if (work.avatar_uri) return String(work.avatar_uri);
    if (safeSvgForPreview(work.avatar_svg)) return dataUriSvg(work.avatar_svg);
    return "";
  }
  function refresh() {
    preview.innerHTML = "";
    preview.style.cssText = themeStyle(work) || "";
    const src = previewSrc();
    if (src) preview.appendChild(el("img", { src, alt: "" }));
    else preview.appendChild(document.createTextNode(glyphOf(work.name)));
  }
  refresh();

  // ---- Upload (png/jpg/jpeg/svg) ----
  const fileInput = el("input", { type: "file", accept: ".png,.jpg,.jpeg,.svg,image/png,image/jpeg,image/svg+xml",
    style: "display:none" });
  const uploadBtn = el("button", { class: "btn soft" }, t("av-upload"));
  const upNote = el("div", { class: "av-note", style: "margin-top:6px" });
  uploadBtn.disabled = disabled;
  uploadBtn.addEventListener("click", () => { if (!disabled) fileInput.click(); });
  fileInput.addEventListener("change", () => {
    upNote.className = "av-note"; upNote.textContent = "";
    const f = fileInput.files && fileInput.files[0];
    fileInput.value = "";
    if (!f) return;
    const ext = (f.name.split(".").pop() || "").toLowerCase();
    if (!AVATAR_EXTS.includes(ext)) {
      upNote.className = "av-note err"; upNote.textContent = t("av-up-type"); return;
    }
    if (f.size > AVATAR_UPLOAD_MAX) {
      upNote.className = "av-note err"; upNote.textContent = t("av-up-size"); return;
    }
    const reader = new FileReader();
    reader.onload = () => {
      const b64 = String(reader.result || "").split(",")[1] || "";
      const mime = ext === "svg" ? "image/svg+xml" : (ext === "png" ? "image/png" : "image/jpeg");
      // SVG safety is verified server-side on save; raster goes through as-is.
      work.pending_avatar = { data_b64: b64, ext, mime };
      work.avatar_uri = ""; work.avatar_svg = "";
      refresh();
    };
    reader.onerror = () => { upNote.className = "av-note err"; upNote.textContent = t("av-up-read"); };
    reader.readAsDataURL(f);
  });

  // ---- AI 生成 → loading (思考 Ns) → confirm/cancel ----
  const aiDesc = el("input", { placeholder: t("av-ai-desc-ph") });
  const aiBtn = el("button", { class: "btn soft" }, t("av-ai-go"));
  const aiNote = el("div", { class: "av-note", style: "margin-top:6px" });
  const aiActs = el("div", { class: "av-ai-confirm" });
  aiBtn.disabled = disabled; aiDesc.disabled = disabled;
  let thinkTimer = null;
  function stopThinking() { if (thinkTimer) { clearInterval(thinkTimer); thinkTimer = null; } }
  aiBtn.addEventListener("click", async () => {
    if (disabled) return;
    aiBtn.disabled = true; aiDesc.disabled = true;
    aiActs.innerHTML = "";
    aiNote.className = "av-note thinking";
    const startedAt = Date.now();
    const tick = () => { aiNote.textContent = t("av-ai-thinking", { n: Math.max(1, Math.round((Date.now() - startedAt) / 1000)) }); };
    tick(); stopThinking(); thinkTimer = setInterval(tick, 1000);
    let r;
    try {
      r = await hub.call("card.avatar_generate",
        { card_path: opts.cardPath || "", description: aiDesc.value.trim() }, 180000);
    } catch (e) {
      stopThinking();
      aiNote.className = "av-note err"; aiNote.textContent = rpcErrText(e);
      aiBtn.disabled = false; aiDesc.disabled = false;
      return;
    }
    stopThinking();
    aiBtn.disabled = false; aiDesc.disabled = false;
    const svg = String((r && r.avatar_svg) || "");
    if (!safeSvgForPreview(svg)) {
      aiNote.className = "av-note err"; aiNote.textContent = t("av-ai-bad"); return;
    }
    // Stage the result for confirm/cancel (do NOT touch work yet).
    aiNote.className = "av-note"; aiNote.textContent = t("av-ai-confirm-q");
    const thumb = el("div", { class: "av-cand", style: themeStyle(work) }, el("img", { src: dataUriSvg(svg), alt: "" }));
    const confirmBtn = el("button", { class: "btn primary" }, t("av-ai-confirm-yes"));
    const cancelBtn = el("button", { class: "btn text" }, t("av-ai-confirm-no"));
    confirmBtn.addEventListener("click", () => {
      // Confirmed: an SVG result is stored inline; the card editor will save it
      // as the sidecar (uploads svg → sidecar) on save.
      work.avatar_svg = svg; work.avatar_uri = ""; work.pending_avatar = null;
      aiActs.innerHTML = ""; aiNote.textContent = ""; refresh();
    });
    cancelBtn.addEventListener("click", () => { aiActs.innerHTML = ""; aiNote.textContent = ""; });
    aiActs.innerHTML = "";
    aiActs.appendChild(thumb);
    aiActs.appendChild(el("div", { class: "av-ai-confirm-acts" }, confirmBtn, cancelBtn));
  });

  // ---- two theme color pickers (applied LIVE) ----
  function picker(slot, fallback) {
    const cur = /^#[0-9a-fA-F]{6}$/.test(String(work.theme[slot] || ""))
      ? String(work.theme[slot]).toUpperCase() : fallback;
    const input = el("input", { type: "color", value: cur });
    input.disabled = disabled;
    if (!work.theme[slot] && slot === "primary") work.theme.primary = fallback;
    input.addEventListener("input", () => { work.theme[slot] = input.value.toUpperCase(); refresh(); });
    return input;
  }
  const primaryPick = picker("primary", "#5B9FD4");
  const secondaryPick = picker("secondary", "#FFFFFF");
  // Secondary starts blank unless the card already declares one.
  if (!work.theme.secondary) secondaryPick.classList.add("av-color-unset");
  const clearSecondary = el("button", { class: "btn text tiny", title: t("av-color-clear") }, "×");
  clearSecondary.disabled = disabled;
  clearSecondary.addEventListener("click", () => {
    work.theme.secondary = ""; secondaryPick.classList.add("av-color-unset"); refresh();
  });
  secondaryPick.addEventListener("input", () => secondaryPick.classList.remove("av-color-unset"));

  const node = el("div", { class: "av-controls" },
    el("div", { class: "av-top" }, preview,
      el("div", { class: "av-side" },
        el("div", { class: "av-sec" }, el("h4", null, t("av-image")),
          el("div", { class: "av-row" }, uploadBtn), upNote, fileInput),
        el("div", { class: "av-sec" }, el("h4", null, t("av-ai")),
          el("div", { class: "av-ai-row" }, aiDesc, aiBtn), aiNote, aiActs))),
    el("div", { class: "av-sec" }, el("h4", null, t("av-colors")),
      el("div", { class: "av-color-row" },
        el("label", { class: "av-color" }, el("span", null, t("av-color-primary")), primaryPick),
        el("label", { class: "av-color" }, el("span", null, t("av-color-secondary")), secondaryPick, clearSecondary))));
  return { node, refresh };
}

/* The new avatar/theme editor for a deck card. Soul untouched: only the avatar
   (sidecar) and the dual theme. Upload → sidecar; AI 生成 → confirm → sidecar.
   Builtin cards are read-only (copy first). */
async function openAvatarEditor(deckCard) {
  let full;
  try {
    full = await hub.call("card.read", { path: deckCard.path }, 20000);
  } catch (e) { toast(rpcErrText(e), true); return; }
  if (!full.raw || !full.raw.data) { toast(t("av-png-note"), true); return; }
  const data = full.raw.data;
  const ext0 = (data.extensions && data.extensions.lunamoth) || {};
  const theme0 = themeOf({ theme: ext0.theme, theme_color: ext0.theme_color });
  const work = {
    name: full.name,
    avatar_uri: String(deckCard.avatar_uri || ""),
    avatar_svg: "",            // sidecar/inline is authoritative; staged only on change
    pending_avatar: null,
    theme: { primary: theme0.primary || "", secondary: theme0.secondary || "" },
  };
  const builtin = !!deckCard.builtin;
  const { node } = buildAvatarControls(work, { cardPath: deckCard.path, disabled: builtin });

  const saveBtn = el("button", { class: "btn primary" }, t("save"));
  saveBtn.disabled = builtin;
  saveBtn.addEventListener("click", async () => {
    saveBtn.disabled = true;
    try {
      // 1) avatar: a new file (upload or confirmed SVG) goes to the sidecar.
      if (work.pending_avatar) {
        await hub.call("card.avatar_upload",
          { path: deckCard.path, data_b64: work.pending_avatar.data_b64, ext: work.pending_avatar.ext }, 30000);
      } else if (work.avatar_svg) {
        const b64 = btoa(unescape(encodeURIComponent(work.avatar_svg)));
        await hub.call("card.avatar_upload", { path: deckCard.path, data_b64: b64, ext: "svg" }, 30000);
      }
      // 2) theme: write the dual theme back into the card (re-read to avoid
      //    clobbering the avatar_file the upload just set).
      const fresh = await hub.call("card.read", { path: deckCard.path }, 20000);
      const fdata = (fresh.raw && fresh.raw.data) || data;
      const lm = (fdata.extensions = fdata.extensions || {}).lunamoth = (fdata.extensions.lunamoth || {});
      const th = { primary: work.theme.primary || "", secondary: work.theme.secondary || "" };
      if (th.primary || th.secondary) lm.theme = th; else delete lm.theme;
      delete lm.theme_color;
      await hub.call("card.save", { data: fresh.raw, path: deckCard.path }, 20000);
      toast(t("saved"));
      closeModal();
      await refreshHub();
      if (state.chat) state.chat.refreshIdentity();
    } catch (e) {
      saveBtn.disabled = false;
      toast(rpcErrText(e), true);
    }
  });

  openModal(el("div", null,
    el("h2", null, `${full.name} · ${t("av-title")}`),
    builtin ? el("div", { class: "av-note amber", style: "margin-bottom:12px" }, t("av-builtin-note")) : null,
    (!builtin && deckCard.frozen) ? el("div", { class: "av-note", style: "margin-bottom:12px" },
      t("av-frozen-note", { names: (deckCard.used_by || []).join("、") })) : null,
    node,
    el("div", { class: "acts", style: "margin-top:14px" },
      el("button", { class: "btn text", onclick: closeModal }, t("cancel")),
      el("div", { class: "grow" }),
      saveBtn)), true);
}

/* user_name / user_persona ride extensions.lunamoth (engine support: 需求单 #9) */
async function injectUserFields(path, flow) {
  if (!flow.draft.user_name && !flow.draft.user_persona) return;
  try {
    const full = await hub.call("card.read", { path }, 20000);
    if (!full.raw || !full.raw.data) return;
    const ext = full.raw.data.extensions = full.raw.data.extensions || {};
    const lm = ext.lunamoth = ext.lunamoth || {};
    lm.user_name = flow.draft.user_name;
    lm.user_persona = flow.draft.user_persona;
    await hub.call("card.save", { data: full.raw, path }, 20000);
  } catch (e) { /* the card itself is saved; user fields are best-effort */ }
}

/* A raster avatar chosen during creation becomes the card's sidecar once the
   card exists on disk. (SVG avatars ride the draft inline and are sidecar'd by
   from_draft's avatar_svg path; raster needs this upload step.) */
async function injectPendingAvatar(path, flow) {
  const pa = flow.draft.pending_avatar;
  if (!pa || !pa.data_b64) return;
  try {
    await hub.call("card.avatar_upload", { path, data_b64: pa.data_b64, ext: pa.ext }, 30000);
  } catch (e) { toast(rpcErrText(e), true); }
}

function renderTellStep(root, flow) {
  root.innerHTML = "";
  root.appendChild(flowSteps(0));
  const guide = el("div", { class: "tell-guide" }, t("tell-guide"));
  const box = el("textarea", { class: "tell-box", placeholder: t("tell-ph") });
  box.value = flow.origin;
  const inner = el("div", { class: "flow-inner" }, guide, box);

  // which model will do the generation (always the system default model)
  const model = effectiveDraftModel();
  inner.appendChild(el("div", { class: "gen-model" }, t("gen-with", { model: model || "—" })));

  // writing-star hint: gentle, only when the default model lacks ★
  modelsCached().then((models) => {
    const d = (state.hub && state.hub.defaults) || {};
    const m = models.find((x) => x.id === d.model);
    if (m && !m.writing) inner.appendChild(el("div", { class: "cap-hint", style: "margin-top:10px" }, t("tell-star-hint")));
  });

  // Default path: background the generation, drop a placeholder in the deck,
  // and let the user out of the modal immediately — never trapped waiting.
  // ONE generate path: draft → the shape/edit step (so the user reviews & edits
  // before the card lands). No background "into the deck" button (it surfaced raw
  // draft-schema errors with no chance to fix).
  const goBtn = el("button", { class: "btn primary big", onclick: async () => {
    flow.origin = box.value.trim();
    if (!flow.origin) return;
    if (flow.lastDraftAt && !confirm(t("draft-overwrite-q"))) return;
    inner.querySelectorAll(".draft-error,.transcribing").forEach((n) => n.remove());
    const started = Date.now();
    const progress = el("div", { class: "transcribing" }, el("i"),
      el("span", { class: "think-elapsed" }, t("thinking-n", { n: "0" })));
    const tick = setInterval(() => {
      const span = progress.querySelector(".think-elapsed");
      if (span) span.textContent = t("thinking-n", { n: String(Math.floor((Date.now() - started) / 1000)) });
    }, 1000);
    inner.appendChild(progress);
    goBtn.disabled = true;
    try {
      flow.draft = normalizeDraft(await hub.call("cards.draft", { inspiration: flow.origin }, 240000));
      flow.lastDraftAt = Date.now();
      flow.versions = {};
      for (const [key] of SECTION_DEFS) flow.versions[key] = [sectionText(flow.draft, key)];
      clearInterval(tick);
      renderShapeStep(root, flow);
    } catch (e) {
      clearInterval(tick);
      goBtn.disabled = false;
      progress.remove();
      inner.appendChild(el("div", { class: "draft-error" },
        el("b", null, rpcErrText(e)),
        el("button", { class: "btn soft", onclick: () => goBtn.click() }, t("retry"))));
    }
  } }, t("tell-go-edit"));

  root.appendChild(inner);
  root.appendChild(el("div", { class: "flow-bar" },
    el("button", { class: "btn text", onclick: closeCreateFlow }, t("cancel")),
    el("div", { class: "grow" }),
    goBtn));
  box.focus();
}

function renderShapeStep(root, flow) {
  root.innerHTML = "";
  root.appendChild(flowSteps(1));
  const inner = el("div", { class: "flow-inner" });

  function collect() {
    inner.querySelectorAll(".sec[data-sec]").forEach((secEl) => {
      const key = secEl.dataset.sec;
      const text = secEl.querySelector(".sec-text").textContent;
      putSection(flow.draft, key, text);
      if (flow.versions[key]) flow.versions[key][flow.versions[key].length - 1] = text;
    });
    inner.querySelectorAll("[data-plain]").forEach((node) => {
      flow.draft[node.dataset.plain] = node.textContent.trim();
    });
    if (flow._syncAvatar) flow._syncAvatar();
  }

  // the telling never disappears — that is this step's core reassurance
  const origin = el("div", { class: "origin-panel", onclick: () => origin.classList.toggle("expanded") },
    el("div", { class: "oh" }, t("origin-title"), el("span", { class: "cue" }, t("origin-cue"))),
    el("div", { class: "ox" }, flow.origin));
  inner.appendChild(origin);

  if (flow.draft.notes && flow.draft.notes.length) {
    inner.appendChild(el("div", { class: "draft-note" }, flow.draft.notes.join(" · ")));
  }

  // 1. 名字对：chara | 用户（鼓励改写的放最上）
  function plainSec(key, labelKey, value) {
    return el("div", { class: "sec" },
      el("h3", null, t(labelKey)),
      el("div", { class: "sec-text", contenteditable: "plaintext-only", "data-plain": key }, value || ""));
  }
  inner.appendChild(el("div", { class: "name-pair" },
    plainSec("name", "sec-name", flow.draft.name),
    plainSec("user_name", "sec-user-name", flow.draft.user_name)));
  // 2. 用户自己的设定
  inner.appendChild(plainSec("user_persona", "sec-user-persona", flow.draft.user_persona));

  // 3+4. chara 设定与其余（带 AI 版本链：原文→AI 稿→v2→手改，可回退）
  for (const [key, labelKey] of SECTION_DEFS) {
    const versions = flow.versions[key];
    const current = versions[versions.length - 1];
    const verLabel = el("span", { class: "ver" },
      flow.edited[key] ? t("edited") : versions.length > 1 ? t("ai-draft-n", { n: versions.length }) : t("ai-draft"));
    const textDiv = el("div", { class: "sec-text", contenteditable: "plaintext-only" }, current);
    textDiv.addEventListener("input", () => {
      flow.edited[key] = true;
      verLabel.textContent = t("edited");
    });
    const revertBtn = el("button", { class: "revert", style: versions.length > 1 ? "" : "display:none", onclick: () => {
      if (versions.length > 1) versions.pop();
      flow.edited[key] = false;
      renderShapeStep(root, flow);
    } }, t("revert"));
    // Directed per-field AI rewrite (natural-language instruction, or free
    // rephrase when empty) — the same affordance as the editor and the wake step.
    const aiBtn = aiEditButton(textDiv, key, () => { collect(); return cardCtxString(flow.draft); });
    inner.appendChild(el("div", { class: "sec", "data-sec": key },
      el("h3", null, t(labelKey), verLabel, revertBtn, aiBtn),
      textDiv));
  }

  // 视觉（头像 + 双主题色）：与卡片编辑器共用一套控件。上传/AI 生成在卡片落地
  // 后才能写 sidecar——这里把选择暂存在 draft 上，from_draft 之后再上传。
  const avWork = {
    name: flow.draft.name,
    avatar_uri: "",
    avatar_svg: flow.draft.avatar_svg || "",
    pending_avatar: flow.draft.pending_avatar || null,
    theme: flow.draft.theme || { primary: "#5B9FD4", secondary: "" },
  };
  const { node: avNode } = buildAvatarControls(avWork, {});
  // Mirror the live edits back onto the draft so collect()/save sees them.
  flow._syncAvatar = () => {
    flow.draft.avatar_svg = avWork.avatar_svg || "";
    flow.draft.pending_avatar = avWork.pending_avatar || null;
    flow.draft.theme = { primary: avWork.theme.primary || "", secondary: avWork.theme.secondary || "" };
  };
  inner.appendChild(el("div", { class: "sec visual-sec" },
    el("h3", null, t("sec-visual")),
    avNode));

  const embodiment = el("div", { class: "sec embodiment-sec" },
    el("h3", null, t("sec-embodiment")),
    el("div", { class: "embodiment-grid" },
      ...["literal", "actor"].map((mode) => {
        const opt = el("div", { class: "emb-option" + (flow.draft.embodiment === mode ? " on" : "") },
          el("b", null, mode === "literal" ? "literal" : "actor"),
          el("span", null, t("emb-" + mode)));
        opt.addEventListener("click", () => {
          flow.draft.embodiment = mode;
          inner.querySelectorAll(".emb-option").forEach((n) => n.classList.remove("on"));
          opt.classList.add("on");
        });
        return opt;
      })));
  inner.appendChild(embodiment);

  root.appendChild(inner);
  root.appendChild(el("div", { class: "flow-bar" },
    el("button", { class: "btn text", onclick: () => { collect(); renderTellStep(root, flow); } }, t("back")),
    el("div", { class: "grow" }),
    el("button", { class: "btn soft", onclick: async () => {
      collect();
      try {
        const r = await hub.call("card.from_draft", { draft: flow.draft, origin: flow.origin, as_draft: true }, 30000);
        await injectUserFields(r.path, flow);
        await injectPendingAvatar(r.path, flow);
        toast(t("saved"));
        refreshHub();
      } catch (e) { toast(rpcErrText(e), true); }
    } }, t("save-draft")),
    el("button", { class: "btn primary", onclick: async () => {
      collect();
      try {
        const r = await hub.call("card.from_draft", { draft: flow.draft, origin: flow.origin }, 30000);
        await injectUserFields(r.path, flow);
        await injectPendingAvatar(r.path, flow);
        await refreshHub();
        closeCreateFlow();
        const card = (state.hub.cards || []).find((c) => c.path === r.path) ||
          { path: r.path, name: flow.draft.name, lang: "zh" };
        openModal(el("div", null,
          el("h2", null, t("card-made")),
          el("div", { class: "sub" }, t("wake-now-q")),
          el("div", { class: "acts", style: "margin-top:14px" },
            el("button", { class: "btn text", onclick: () => { closeModal(); navTo("#/deck"); } }, t("later-deck")),
            el("div", { class: "grow" }),
            el("button", { class: "btn primary big", onclick: () => { closeModal(); openWakeSheet(card); } }, t("deck-wake")))));
      } catch (e) { toast(rpcErrText(e), true); }
    } }, t("next-card"))));
}

/* boot text */
applyTheme(localStorage.getItem("lm-theme") || "system");
setLangCode(localStorage.getItem("lm-lang") || (navigator.language.startsWith("zh") ? "zh" : "en"));
applyI18n();

// applyI18n rewrites data-i18n nodes by innerHTML on every language switch,
// which wipes the dynamic default-card decoration — re-apply it afterwards.
document.addEventListener("lm-lang-changed", () => decorateDefaultCard());
