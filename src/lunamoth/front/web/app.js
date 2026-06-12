/* LunaMoth Desktop renderer — design: docs/desktop/design.md.
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

function toast(msg, isErr) {
  const node = el("div", { class: "toast" + (isErr ? " err" : "") }, msg);
  $("toasts").appendChild(node);
  setTimeout(() => node.remove(), isErr ? 5200 : 3200);
}

function timeAgo(ts) {
  if (!ts) return "";
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 90) return t("ago-just");
  if (s < 3600) return `${Math.round(s / 60)} ${t("ago-min")}`;
  if (s < 86400) return `${Math.round(s / 3600)} ${t("ago-hour")}`;
  return `${Math.round(s / 86400)} ${t("ago-day")}`;
}

function estimateTokens(text) {
  const s = String(text || "");
  let cjk = 0;
  for (const ch of s) if (ch >= "\u4e00" && ch <= "\u9fff") cjk++;
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

/* ---------- error language (design §3.2: reasons are human words) ---------- */
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

function cardVisual(c, cls) {
  const color = c && c.theme_color ? String(c.theme_color) : "";
  const svg = c && c.avatar_svg ? String(c.avatar_svg) : "";
  const attrs = { class: cls || "face" };
  if (color) attrs.style = `--card-theme:${color}`;
  const children = [];
  if (svg) children.push(el("img", { class: "avatar-svg", src: dataUriSvg(svg), alt: "" }));
  else children.push(el("div", { class: "glyph" }, glyphOf(c && c.name)));
  return el("div", attrs, ...children);
}

/* ---------- global state ---------- */
const hub = new HubClient();
const state = {
  hub: null,            // last hub.state result
  view: "board",
  sort: "recent",
  models: null,         // models.list cache
  chat: null,           // active ChatController
  boardTimer: null,
};

/* ============================ THEME & LANG ============================ */
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

/* ============================ NAVIGATION ============================ */
function show(view) {
  if (state.view === "chat" && view !== "chat" && state.chat) {
    state.chat.dispose();
    state.chat = null;
  }
  state.view = view;
  document.querySelectorAll(".view").forEach((v) => v.classList.remove("active"));
  $(`view-${view}`).classList.add("active");
  document.querySelectorAll(".nav-item").forEach((n) =>
    n.classList.toggle("active", n.dataset.view === (view === "chat" ? "board" : view)));
  if (view === "board") refreshHub();
  if (view === "deck") refreshHub();
}
document.querySelectorAll(".nav-item").forEach((n) =>
  n.addEventListener("click", () => show(n.dataset.view)));

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
  } catch (e) { /* transient */ }
}

hub.onReady = async () => {
  $("conn-dot").classList.remove("bad");
  await refreshHub();
  const d = (state.hub && state.hub.defaults) || {};
  const savedLang = localStorage.getItem("lm-lang") || d.ui_lang || (navigator.language.startsWith("zh") ? "zh" : "en");
  setLang(savedLang, false);
  applyTheme(localStorage.getItem("lm-theme") || d.ui_theme || "system");
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
  // one line per card, exception first (design §3.2)
  if (s.status === "new") return { dot: "off", line: t("st-new"), cls: "" };
  if (s.status === "crashed") return { dot: "err", line: s.error || "crashed", cls: "err" };
  if (s.error && (s.error_kind === "auth" || (s.status !== "attached" && s.status !== "running")))
    return { dot: "err", line: t("st-error"), cls: "err" };
  if (s.status === "idle") return { dot: "off", line: `${t("st-offline")} · ${timeAgo(s.last_active)}`, cls: "" };
  if (s.preview && s.preview.awaiting)
    return { dot: "live", line: s.preview.text, cls: "msg" };
  if (s.life && s.life.state) return { dot: "live", line: lifeText(s.life), cls: "" };
  return { dot: "live", line: t("st-idle-live"), cls: "" };
}

function lifeText(life) {
  if (!life) return "";
  const now = Date.now() / 1000;
  if (life.state === "working") return t("life-working");
  if (life.state === "waiting") return t("life-waiting");
  if (life.state === "resting") return `${t("life-resting")} · ${timeAgo(life.rest_until)}`;
  if (life.state === "backoff") return `${t("life-backoff")} · ${life.detail || ""}`;
  if (life.state === "idle_countdown" && life.next_cycle_at) {
    return `${t("life-countdown")} · ${durationText(Math.max(0, life.next_cycle_at - now))}`;
  }
  return t("st-idle-live");
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
  for (const s of list) {
    const live = s.status === "running" || s.status === "attached";
    const st = statusOf(s);
    const card = el("div", { class: "chara-card" + (st.dot === "off" ? " offline" : ""), onclick: () => openChat(s.name) },
      el("div", { class: `portrait ${paletteClass(s.char_name)}` },
        el("div", { class: "glyph" }, glyphOf(s.char_name)),
        el("span", { class: `dot ${st.dot}` }),
        el("div", { class: "hover-acts" },
          el("button", {
            title: live ? t("act-sleep") : t("act-wake-up"),
            onclick: async (ev) => {
              ev.stopPropagation();
              try {
                await hub.call(live ? "session.stop" : "session.start", { name: s.name }, 30000);
                refreshHub();
              } catch (e) { toast(e.message, true); }
            },
          }, "⏻"),
          el("button", { title: "⋯", onclick: (ev) => { ev.stopPropagation(); cardMenu(ev, s); } }, "⋯"))),
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
              onclick: (ev) => { ev.stopPropagation(); show("settings"); },
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
  // minimal ⋯ menu as a one-off palette near the cursor
  closeMenus();
  const menu = el("div", { class: "palette open", style: `position:fixed;left:${Math.min(ev.clientX, innerWidth - 240)}px;top:${ev.clientY + 8}px;bottom:auto;transform:none;width:220px;z-index:90` },
    el("div", { class: "row", onclick: async () => {
      closeMenus();
      try {
        const r = await hub.call("session.export", { name: s.name }, 120000);
        toast(t("exported", { path: r.path }));
        hub.call("open.path", { path: r.path, reveal: true }).catch(() => {});
      } catch (e) { toast(rpcErrText(e), true); }
    } }, t("menu-export")),
    el("div", { class: "row danger", onclick: () => { closeMenus(); openDeleteModal(s); } }, t("menu-delete")));
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

/* ---------- delete (the heaviest friction in the app, deliberately) ---------- */
function openDeleteModal(s) {
  const phrase = `${t("del-word")} ${s.char_name}`;
  const input = el("input", { class: "confirm-input", placeholder: t("del-ph", { name: s.char_name }) });
  const delBtn = el("button", { class: "btn danger", disabled: "" }, t("del-go"));
  input.addEventListener("input", () => {
    if (input.value.trim() === phrase) delBtn.removeAttribute("disabled");
    else delBtn.setAttribute("disabled", "");
  });
  delBtn.addEventListener("click", async () => {
    try {
      await hub.call("session.delete", { name: s.name, confirm: s.name }, 30000);
      closeModal();
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
      el("button", { class: "btn soft", style: "width:100%;margin-bottom:14px", onclick: async () => {
        try {
          const r = await hub.call("session.export", { name: s.name }, 120000);
          toast(t("exported", { path: r.path }));
        } catch (e) { toast(e.message, true); }
      } }, t("del-export")),
      input,
      el("div", { class: "acts" },
        el("button", { class: "btn text", onclick: closeModal }, t("cancel")),
        el("div", { class: "grow" }),
        delBtn)));
}

/* ============================ MODALS ============================ */
function openModal(content, wide) {
  const box = $("modal-box");
  box.classList.toggle("sheet", !!wide);
  box.innerHTML = "";
  box.appendChild(content);
  $("modal-layer").classList.add("open");
}
function closeModal() { $("modal-layer").classList.remove("open"); }
$("modal-layer").addEventListener("click", (ev) => { if (ev.target === $("modal-layer")) closeModal(); });
document.addEventListener("keydown", (ev) => { if (ev.key === "Escape") { closeModal(); closeMenus(); } });

/* ============================ DECK ============================ */
function renderDeck() {
  if (!state.hub) return;
  const q = ($("deck-search").value || "").toLowerCase();
  const cards = state.hub.cards.filter((c) => !q || c.name.toLowerCase().includes(q));
  $("deck-count").textContent = cards.length ? `· ${cards.length}` : "";
  const grid = $("deck-grid");
  grid.innerHTML = "";
  for (const c of cards) {
    const badge = c.frozen
      ? el("div", { class: "lock-badge" }, t("deck-readonly"))
      : (c.draft ? el("div", { class: "draft-badge" }, t("deck-draft")) : null);
    const acts = el("div", { class: "spine-acts" },
      el("button", { class: "wake", onclick: (ev) => { ev.stopPropagation(); ensureModel(() => openWakeSheet(c)); } }, t("deck-wake")),
      el("button", { onclick: (ev) => { ev.stopPropagation(); viewCard(c); } }, t("deck-view")),
      el("button", { onclick: async (ev) => {
        ev.stopPropagation();
        try {
          const full = await hub.call("card.read", { path: c.path }, 20000);
          if (!full.raw) { toast("PNG cards: duplicate not supported yet", true); return; }
          await hub.call("card.save", { data: full.raw }, 20000);
          toast(t("copied"));
          refreshHub();
        } catch (e) { toast(e.message, true); }
      } }, t("deck-copy")));
    const face = cardVisual(c, `face ${paletteClass(c.name)}`);
    if (badge) face.appendChild(badge);
    face.appendChild(acts);
    grid.appendChild(el("div", { class: "spine", onclick: () => viewCard(c) },
      face,
      el("div", { class: "sbody" },
        el("div", { class: "sname" }, el("b", null, c.name), el("span", { class: "chip" }, c.lang)),
        el("div", { class: "sworld" }, [c.tagline || c.world, c.builtin ? t("deck-builtin") : "", ...(c.tags || [])].filter(Boolean).slice(0, 3).join(" · ")))));
  }
}
$("deck-search").addEventListener("input", renderDeck);
$("deck-new").addEventListener("click", () => ensureModel(openCreateFlow));
$("deck-import").addEventListener("click", () => $("file-input").click());

async function viewCard(c) {
  try {
    const full = await hub.call("card.read", { path: c.path }, 20000);
    const ext = full.extensions && full.extensions.lunamoth ? full.extensions.lunamoth : {};
    const avatar = ext.avatar_svg ? el("img", { class: "view-avatar", src: dataUriSvg(ext.avatar_svg), alt: "" }) : null;
    openModal(el("div", null,
      el("h2", null, full.name),
      el("div", { class: "sub" }, c.frozen ? t("card-frozen-by", { names: c.used_by.join("、") }) : (ext.tagline || c.world || "")),
      avatar,
      el("div", { class: "memory-text", style: "max-height:46vh;overflow:auto" },
        [full.description, full.personality, full.scenario, full.first_mes ? "—\n" + full.first_mes : ""]
          .filter(Boolean).join("\n\n")),
      el("div", { class: "acts", style: "margin-top:16px" },
        el("button", { class: "btn text", onclick: closeModal }, t("cancel")),
        el("div", { class: "grow" }),
        (!c.builtin && !c.frozen) ? el("button", { class: "btn soft", onclick: async () => {
          if (!confirm(t("deck-delete-q"))) return;
          try { await hub.call("card.delete", { path: c.path }, 10000); closeModal(); refreshHub(); }
          catch (e) { toast(e.message, true); }
        } }, t("menu-delete")) : null,
        el("button", { class: "btn primary", onclick: () => { closeModal(); ensureModel(() => openWakeSheet(c)); } }, t("deck-wake")))));
  } catch (e) { toast(e.message, true); }
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
    show("deck");
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
  root.appendChild(provRow("OpenRouter", "OpenRouter", t("or-desc"), true));
  root.appendChild(provRow("OpenAI", "OpenAI", "", false));
  const moreWrap = el("div", { style: "display:none" },
    provRow("Ollama (local)", "Ollama", "local", false),
    provRow("_custom", "OpenAI-compatible", "", false));
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
    ? el("button", { class: "btn text", onclick: () => { closeFirstRun(); show("board"); } }, t("later"))
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
$("reveal-home").addEventListener("click", () => {
  if (state.hub) hub.call("open.path", { path: state.hub.home, reveal: true }).catch((e) => toast(e.message, true));
});

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
  $("fr-dots").innerHTML = "<i class='on'></i><i></i>";
}
function frShowSetup() {
  $("fr-welcome").style.display = "none";
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
  const wantZh = getLangCode() === "zh";
  return cards.find((c) => c.builtin && c.lang === (wantZh ? "zh" : "en") &&
           (c.name === "月蛾" || c.name.toLowerCase() === "lunamoth")) ||
         cards.find((c) => c.builtin && (c.name === "月蛾" || c.name.toLowerCase() === "lunamoth"));
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
$("fr-try").addEventListener("click", () => { frPendingAction = wakeDefaultLuna; frShowSetup(); });
$("fr-create").addEventListener("click", () => { frPendingAction = () => { closeFirstRun(); openCreateFlow(); }; frShowSetup(); });
$("fr-import").addEventListener("click", () => $("file-input").click());

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
  let wantNet = false;
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
  const netSwitch = el("button", { class: "switch", onclick: () => {
    wantNet = !wantNet;
    netSwitch.classList.toggle("on", wantNet);
  } });

  let toolpack = "sandbox";
  try {
    const full = await hub.call("card.read", { path: card.path }, 20000);
    const ext = full.raw && full.raw.data && full.raw.data.extensions && full.raw.data.extensions.lunamoth;
    if (ext && ext.toolpack) toolpack = String(ext.toolpack);
  } catch (e) { /* keep default */ }
  const toolNames = ["terminal", "memory", "files", "goals", "skills", "speak", "rest"];

  const adv = el("div", { class: "adv" },
    el("div", { class: "adv-head", onclick: () => adv.classList.toggle("open") }, t("wake-adv")),
    el("div", { class: "adv-body" },
      el("div", { class: "field-row" }, el("label", null, t("wake-iso")), isoSeg),
      el("div", { class: "field-row" },
        el("div", { class: "switch-row", style: "font-size:12.5px" },
          el("b", { style: "font-weight:550" }, t("d-net")),
          el("small", null, t("d-net-sub")),
          netSwitch)),
      el("div", { class: "field-row" },
        el("label", null, t("wake-tools") + ` · ${toolpack}`),
        el("div", { class: "tool-chips" }, ...toolNames.map((n) => el("span", { class: "chip" }, n))))));

  const goBtn = el("button", { class: "btn primary big", onclick: async () => {
    goBtn.disabled = true;
    try {
      const entry = await hub.call("session.wake", {
        card: card.path, name: nameInput.value.trim(), isolation,
        model: modelInput.value.trim(), toolpack,
      }, 60000);
      closeModal();
      await refreshHub();
      openChat(entry.name, { netOn: wantNet });
    } catch (e) {
      toast(e.message, true);
      goBtn.disabled = false;
    }
  } }, t("wake-go"));

  openModal(el("div", null,
    el("h2", null, t("wake-title", { name: card.name })),
    el("div", { class: "sub" }, t("wake-sub")),
    el("div", { class: "field-row" }, el("label", null, t("wake-name")), el("div", { class: "input-like" }, nameInput)),
    el("div", { class: "field-row" }, el("label", null, t("wake-model")),
      el("div", { class: "input-like" }, modelInput), capLine, warnLine),
    adv,
    el("div", { class: "acts", style: "margin-top:18px" },
      el("button", { class: "btn text", onclick: closeModal }, t("cancel")),
      el("div", { class: "grow" }),
      goBtn)), true);
  refreshCaps();
}

/* ============================ CHAT ============================ */
function openChat(name, opts) {
  if (state.chat) { state.chat.dispose(); state.chat = null; }
  show("chat");
  state.chat = new ChatController(name, opts || {});
  state.chat.open();
}

class ChatController {
  constructor(name, opts) {
    this.name = name;
    this.opts = opts;
    this.client = new CharaClient(name);
    this.charName = name;
    this.mode = "live";
    this.showThinking = false;
    this.disposed = false;
    this.cur = { kind: null, node: null, textNode: null };
    this.toolChips = null;
    this.activeTools = new Map();
    this.pendingSuper = false;
    this.turnThink = null;
    this.work = { active: false, phase: "idle", thinkTokens: 0, toolName: "" };
    this.life = null;
    this.lifeTimer = null;
    this.snapTimer = null;
    this.drawerLoaded = {};
    try {
      this.thinkExpanded = localStorage.getItem("lm-chat-thinking-expanded") === "1";
    } catch (e) {
      this.thinkExpanded = false;
    }
    const entry = (state.hub && state.hub.sessions.find((s) => s.name === name)) || null;
    if (entry) this.charName = entry.char_name;
  }

  /* ---- lifecycle ---- */
  async open() {
    $("stream-inner").innerHTML = "";
    this.setWorkState(false);
    $("chat-name").textContent = this.charName;
    $("chat-statusword").textContent = t("st-connecting");
    $("chat-avatar").className = "avatar-s " + paletteClass(this.charName);
    $("chat-avatar-glyph").textContent = glyphOf(this.charName);
    $("chat-dot").className = "mini-dot off";
    $("composer-input").placeholder = t("composer-ph", { name: this.charName });
    $("drawer").classList.remove("open");
    this.bindUI();
    try {
      await this.client.connect();
      this.client.onProtocolEvent = (ev) => this.onEvent(ev);
      this.client.onPermissionAsk = (p) => this.onPermission(p);
      this.client.onLifeState = (p) => this.onLifeState(p);
      this.client.onRejoinGap = () => {
        this.client.clearRejoin();
        $("stream-inner").innerHTML = "";
      };
      this.client.onClose = (ev) => {
        if (!this.disposed) {
          this.note((ev && ev.reason) || t("conn-lost"));
          $("chat-dot").className = "mini-dot off";
        }
      };
      const info = await this.client.attach();
      if (this.disposed) return;
      this.lastUserAt = Date.now(); // arriving counts as engagement
      this.charName = info.char_name || this.charName;
      this.mode = info.mode || "live";
      this.showThinking = !!info.show_thinking;
      $("chat-name").textContent = this.charName;
      $("chat-dot").className = "mini-dot";
      this.setStatusWord(t("st-listening"));
      this.renderModeSeg();
      this.renderRestored(info.restored || []);
      this.renderPalette();
      this.refreshSnapshot();
      this.snapTimer = setInterval(() => { if (!document.hidden) this.refreshSnapshot(); }, 6000);
      if (this.opts.netOn) await this.command("/net on", true);
      await this.handleOpening(info);
    } catch (e) {
      if (!this.disposed) this.note(e.message);
    }
  }

  dispose() {
    this.disposed = true;
    clearInterval(this.lifeTimer);
    clearInterval(this.snapTimer);
    const c = this.client;
    (async () => {
      try { if (c.streaming) await c.interrupt(); } catch (e) { /* gone */ }
      try { await c.detach(); } catch (e) { /* gone */ }
      c.close();
    })();
    setTimeout(refreshHub, 600);
  }

  /* ---- opening decision tree (AttachInfo) ---- */
  async handleOpening(info) {
    if (info.opening === "greeting" && info.opening_text) {
      this.appendCharText(info.opening_text);
      this.finalize();
      try { await this.client.sock.call("greet", { text: info.opening_text }, 10000); } catch (e) { /* older server */ }
    } else if (info.opening === "arrival" && info.opening_text) {
      await this.runStream(() => this.client.sock.call("event", { text: info.opening_text }));
    } else if (info.opening === "probe" && info.opening_text) {
      await this.runStream(() => this.client.send(info.opening_text));
    }
  }

  /* ---- restored history ---- */
  renderRestored(messages) {
    const inner = $("stream-inner");
    for (const m of messages.slice(-80)) {
      if (!m) continue;
      const content = typeof m.content === "string" ? m.content : "";
      const hasText = content.trim().length > 0;
      if (m.role === "user") {
        if (!hasText) continue;
        inner.appendChild(el("div", { class: "user-msg" }, el("div", { class: "bubble" }, m.content)));
      } else if (m.role === "system") {
        if (hasText && m.kind !== "summary") this.systemLine(content);
      } else if (m.role === "assistant") {
        if (m.kind === "think") {
          if (hasText) {
            this.appendMuseText(content);
            this.closeCurrent();
          }
          continue;
        }
        if (hasText) {
          this.appendCharText(content);
          this.closeCurrent();
        }
        for (const speak of speakTextsFromMessage(m)) {
          this.appendCharText(speak, { superChat: true, ts: m.ts || Date.now() / 1000 });
          this.closeCurrent();
        }
      }
    }
    this.scrollDown(true);
  }

  /* ---- streaming protocol events ---- */
  onEvent(ev) {
    if (!ev || this.disposed) return;
    if (ev.type === "text") {
      this.setWorkState(true, "generate");
      const isSuper = ev.channel === "say" && this.pendingSuper;
      if (ev.channel === "say") this.pendingSuper = false;
      if (ev.channel === "muse") {
        this.appendMuseText(ev.text);
      } else {
        this.appendCharText(ev.text, { superChat: isSuper });
        // Electron shell: collect say-channel text for a system notification.
        this.pendingNotify = (this.pendingNotify || "") + ev.text;
      }
      this.setStatusWord(t("st-creating"));
    } else if (ev.type === "think") {
      this.appendThinking(ev.text);
    } else if (ev.type === "tool_start") {
      this.showToolStart(ev);
    } else if (ev.type === "tool_end") {
      this.showToolEnd(ev);
    } else if (ev.type === "notice") {
      this.note(ev.text || ev.kind);
    }
    this.scrollDown();
  }

  appendCharText(text, opts) {
    const kind = opts && opts.superChat ? "super" : "say";
    const ts = opts && opts.ts ? Number(opts.ts) : Date.now() / 1000;
    if (this.cur.kind !== kind) {
      this.closeCurrent();
      this.breakToolGroup();
      const textDiv = el("div", { class: "text" });
      const nameLine = el("div", { class: "name" }, this.charName);
      if (kind === "super") nameLine.appendChild(superBadge());
      const node = el("div", { class: "char-msg" + (kind === "super" ? " super-chat" : "") },
        el("div", { class: "avatar-s " + paletteClass(this.charName), style: "font-size:12px" }, glyphOf(this.charName)),
        el("div", { class: "body" },
          nameLine,
          textDiv,
          kind === "super" ? el("div", { class: "read-mark" }, "") : null));
      if (kind === "super") node.dataset.speakTs = String(ts);
      $("stream-inner").appendChild(node);
      this.cur = { kind, node, textNode: textDiv, raw: "" };
    }
    this.cur.raw = (this.cur.raw || "") + text;
    this.cur.textNode.textContent = this.cur.raw;
    if (kind === "super") this.markSuperRead(this.cur.node, ts);
  }

  markSuperRead(node, ts) {
    if (!node || document.visibilityState !== "visible") return;
    const mark = node.querySelector(".read-mark");
    if (mark && mark.textContent) return;
    hub.call("superchat.read", { name: this.name, ts: Number(ts) || Date.now() / 1000 }, 10000)
      .then(() => { if (mark) mark.textContent = "✓"; refreshHub(); })
      .catch(() => {});
  }

  appendMuseText(text) {
    if (this.cur.kind !== "muse") {
      this.closeCurrent();
      this.breakToolGroup();
      const textDiv = el("div", { class: "muse-text" });
      const node = el("div", { class: "muse-msg" },
        el("div", { class: "muse-label" }, t("muse-label")),
        textDiv);
      $("stream-inner").appendChild(node);
      this.cur = { kind: "muse", node, textNode: textDiv, raw: "" };
    }
    this.cur.raw = (this.cur.raw || "") + text;
    this.cur.textNode.textContent = this.cur.raw;
  }

  appendThinking(text) {
    if (this.cur.kind !== "think") {
      this.closeCurrent();
      this.breakToolGroup();
      if (this.turnThink && this.turnThink.node && this.turnThink.node.isConnected) {
        this.cur = this.turnThink;
      } else {
        const head = el("button", { class: "think-head streaming" });
        const body = el("div", { class: "think-body" });
        const node = el("div", { class: "think-block streaming" }, head, body);
        head.onclick = () => this.toggleThinkingExpanded();
        $("stream-inner").appendChild(node);
        this.cur = { kind: "think", node, head, body, raw: "", tokens: 0 };
        this.turnThink = this.cur;
      }
    }
    this.cur.raw = (this.cur.raw || "") + text;
    this.cur.tokens = this.cur.raw ? Math.max(1, estimateTokens(this.cur.raw)) : 0;
    this.cur.node.dataset.tokens = String(this.cur.tokens);
    this.cur.body.textContent = this.cur.raw;
    this.applyThinkState(this.cur.node, true);
    this.setWorkState(true, "think", { thinkTokens: this.cur.tokens });
  }

  closeCurrent() {
    if ((this.cur.kind === "say" || this.cur.kind === "super") && this.cur.raw) {
      this.cur.textNode.innerHTML = mdRender(this.cur.raw);
      if (this.cur.kind === "super") this.markSuperRead(this.cur.node, Number(this.cur.node.dataset.speakTs || 0));
    }
    this.cur = { kind: null, node: null, textNode: null };
  }

  applyThinkState(node, streaming) {
    if (!node) return;
    const tokens = Number(node.dataset.tokens || 0) || 0;
    const head = node.querySelector(".think-head");
    const body = node.querySelector(".think-body");
    node.classList.toggle("streaming", !!streaming);
    if (head) {
      head.classList.toggle("streaming", !!streaming);
      head.textContent = streaming
        ? `✶ ${t("thinking-live", { n: tokens })}`
        : `${t("thinking-done", { n: tokens })} ${this.thinkExpanded ? "▾" : "▸"}`;
    }
    if (body) body.style.display = this.thinkExpanded ? "block" : "none";
  }

  toggleThinkingExpanded() {
    this.thinkExpanded = !this.thinkExpanded;
    try { localStorage.setItem("lm-chat-thinking-expanded", this.thinkExpanded ? "1" : "0"); } catch (e) { /* ok */ }
    this.updateThinkingBlocks();
  }

  updateThinkingBlocks() {
    $("stream-inner").querySelectorAll(".think-block").forEach((node) =>
      this.applyThinkState(node, node.classList.contains("streaming")));
  }

  finalizeThinkingBlocks() {
    $("stream-inner").querySelectorAll(".think-block.streaming").forEach((node) => {
      node.classList.remove("streaming");
      this.applyThinkState(node, false);
    });
  }

  ensureToolGroup() {
    if (this.toolChips && this.toolChips.isConnected) return this.toolChips;
    this.closeCurrent();
    const node = el("div", { class: "tool-chip-line" });
    $("stream-inner").appendChild(node);
    this.toolChips = node;
    return node;
  }

  breakToolGroup() {
    this.toolChips = null;
  }

  toolKey(ev) {
    return `${Number(ev.index || 0)}:${ev.name || "?"}`;
  }

  showToolStart(ev) {
    this.closeCurrent();
    const name = ev.name || "?";
    const group = this.ensureToolGroup();
    const detail = el("div", { class: "tool-detail" }, ev.preview || "");
    const button = el("button", { class: "tool-chip running" },
      el("span", { class: "spin" }), el("span", null, `⚙ ${name}`));
    const item = el("div", { class: "tool-chip-item" }, button, detail);
    button.onclick = () => item.classList.toggle("open");
    group.appendChild(item);
    this.activeTools.set(this.toolKey(ev), { item, button, detail, name });
    this.setWorkState(true, "tool", { toolName: name });
    this.setStatusWord(t("st-creating"));
  }

  showToolEnd(ev) {
    const name = ev.name || "?";
    const key = this.toolKey(ev);
    let rec = this.activeTools.get(key);
    if (!rec) {
      const group = this.ensureToolGroup();
      const detail = el("div", { class: "tool-detail" });
      const button = el("button", { class: "tool-chip" });
      const item = el("div", { class: "tool-chip-item" }, button, detail);
      button.onclick = () => item.classList.toggle("open");
      group.appendChild(item);
      rec = { item, button, detail, name };
    }
    const ok = ev.ok !== false;
    rec.button.className = "tool-chip " + (ok ? "ok" : "err");
    rec.button.textContent = `⚙ ${name} ${ok ? "✓" : "✗"} · ${durationText(ev.duration)}`;
    rec.detail.textContent = ev.summary || t("tool-no-summary");
    rec.item.classList.toggle("has-detail", !!(ev.summary || "").trim());
    this.activeTools.delete(key);
    if (name === "speak" && ok) this.pendingSuper = true;
    if (this.activeTools.size) {
      const next = this.activeTools.values().next().value;
      this.setWorkState(true, "tool", { toolName: next ? next.name : "" });
    } else {
      this.setWorkState(true, "generate");
    }
  }

  systemLine(text, cls) {
    if (!text) return;
    this.closeCurrent();
    this.breakToolGroup();
    $("stream-inner").appendChild(el("div", { class: "sys-note" + (cls ? " " + cls : "") }, String(text).slice(0, 240)));
  }

  idleDivider() {
    this.systemLine(t("idle-divider"), "idle-divider");
  }

  setWorkState(active, phase, detail) {
    const node = $("work-status");
    if (!node) return;
    if (!active) {
      this.work = { active: false, phase: "idle", thinkTokens: 0, toolName: "" };
      node.hidden = true;
      node.textContent = "";
      node.className = "work-status";
      return;
    }
    this.work = {
      active: true,
      phase: phase || this.work.phase || "generate",
      thinkTokens: detail && "thinkTokens" in detail ? detail.thinkTokens : (this.work.thinkTokens || 0),
      toolName: detail && "toolName" in detail ? detail.toolName : (this.work.toolName || ""),
      lifeText: detail && "lifeText" in detail ? detail.lifeText : (this.work.lifeText || ""),
    };
    node.hidden = false;
    node.className = "work-status " + this.work.phase;
    if (this.work.phase === "think") {
      node.textContent = t("work-thinking", { n: this.work.thinkTokens || 0 });
    } else if (this.work.phase === "tool") {
      node.textContent = t("work-tool", { name: this.work.toolName || "tool" });
    } else if (this.work.phase === "life") {
      node.textContent = (detail && detail.lifeText) || this.work.lifeText || t("life-countdown");
    } else {
      node.textContent = t("work-generating");
    }
  }

  finalize() {
    this.closeCurrent();
    this.finalizeThinkingBlocks();
    this.activeTools.clear();
    this.breakToolGroup();
    this.pendingSuper = false;
    this.turnThink = null;
    if (!(this.life && this.life.state === "working")) this.setWorkState(false);
    // Electron shell: surface what was said while the window wasn't watched.
    if (this.pendingNotify && window.lunamothNative && !document.hasFocus())
      window.lunamothNative.notify(this.charName, this.pendingNotify.trim().slice(0, 200));
    this.pendingNotify = "";
    this.setStatusWord(t("st-listening"));
    this.scrollDown();
  }

  note(text) {
    if (!text) return;
    this.systemLine(text);
  }

  scrollDown(force) {
    const sc = $("stream");
    const nearBottom = sc.scrollHeight - sc.scrollTop - sc.clientHeight < 160;
    if (force || nearBottom) sc.scrollTop = sc.scrollHeight;
  }

  setStatusWord(word) { $("chat-statusword").textContent = word; }

  /* ---- driving turns ---- */
  async runStream(fn) {
    this.setSending(true);
    this.turnThink = null;
    this.setWorkState(true, "generate");
    try {
      await fn();
    } catch (e) {
      if (!this.disposed) this.note(e.message);
    } finally {
      if (!this.disposed) {
        this.finalize();
        this.setSending(false);
        this.refreshSnapshot();
      }
    }
  }

  async sendUser(text) {
    this.lastUserAt = Date.now();
    $("stream-inner").appendChild(el("div", { class: "user-msg" }, el("div", { class: "bubble" }, text)));
    this.scrollDown(true);
    await this.runStream(() => this.client.send(text));
  }

  onLifeState(life) {
    this.life = life || null;
    this.renderLifeState();
    if (!this.lifeTimer) this.lifeTimer = setInterval(() => this.renderLifeState(), 1000);
  }

  renderLifeState() {
    if (!this.life) return;
    if (this.life.state === "working") {
      this.setWorkState(true, "generate");
      this.setStatusWord(t("life-working"));
      return;
    }
    this.setWorkState(true, "life", { lifeText: lifeText(this.life) });
    this.setStatusWord(lifeText(this.life));
  }

  async command(line, quiet) {
    try {
      const reply = await this.client.command(line);
      if (!quiet && reply && reply.text) this.note(reply.text);
      this.refreshSnapshot();
      return reply;
    } catch (e) {
      if (!quiet) this.note(e.message);
      return null;
    }
  }

  /* ---- snapshot -> header + drawer ---- */
  async refreshSnapshot() {
    if (!this.client.open || this.client.streaming) return;
    let snap;
    try { snap = await this.client.snapshot(); } catch (e) { return; }
    if (this.disposed) return;
    this.snap = snap;
    this.mode = snap.mode || this.mode;
    this.showThinking = !!snap.show_thinking;
    this.renderModeSeg();
    $("net-btn").style.display = snap.net_on ? "none" : "flex";
    $("net-btn").title = t("net-off-tip");
    if (snap.rest_until && snap.rest_until * 1000 > Date.now()) this.setStatusWord(t("st-resting"));
    if ($("drawer").classList.contains("open")) this.renderDrawerStatus();
  }

  renderModeSeg() {
    document.querySelectorAll("#mode-seg span").forEach((s) =>
      s.classList.toggle("on", s.dataset.mode === this.mode));
  }

  /* ---- drawer ---- */
  async openDrawerTab(tab) {
    document.querySelectorAll("#drawer-tabs span").forEach((s) => s.classList.toggle("on", s.dataset.p === tab));
    document.querySelectorAll(".drawer-pane").forEach((p) => p.classList.toggle("on", p.id === tab));
    if (tab === "d-status") this.renderDrawerStatus();
    if (tab === "d-works") this.renderDrawerWorks();
    if (tab === "d-memory" || tab === "d-skills") this.renderDrawerExtras(tab);
  }

  renderDrawerStatus() {
    const snap = this.snap;
    const pane = $("d-status");
    pane.innerHTML = "";
    if (!snap) return;
    const pct = snap.context_max ? Math.min(100, Math.round(100 * snap.context_tokens / snap.context_max)) : 0;
    pane.appendChild(el("div", { class: "dsec" },
      el("h4", null, t("d-context")),
      el("div", { class: "ctx-big" },
        el("div", { class: "ring", style: `--p:${pct}` }),
        el("div", { class: "nums" },
          el("b", null, `${pct}%`),
          el("div", null, `${(snap.context_tokens / 1000).toFixed(1)}k / ${(snap.context_max / 1000).toFixed(0)}k`)),
        el("button", { class: "btn soft", onclick: () => this.command("/compact") }, t("d-tidy")))));
    const memPct = snap.memory_max ? Math.min(100, Math.round(100 * snap.memory_chars / snap.memory_max)) : 0;
    pane.appendChild(el("div", { class: "dsec" },
      el("h4", null, t("d-memory")),
      el("div", { class: "membar" },
        el("div", { class: "lbl" }, el("span", null, t("d-memory")), el("span", null, `${snap.memory_chars} / ${snap.memory_max}`)),
        el("div", { class: "bar" }, el("i", { style: `width:${memPct}%` })))));
    pane.appendChild(el("div", { class: "dsec" },
      el("h4", null, t("d-sandbox")),
      el("div", { class: "sandbox-card" },
        el("div", { class: "top" }, isoGlyph(snap.isolation), el("small", null, snap.model)),
        el("div", { class: "paths" },
          el("code", null, snap.workspace_root || ""),
          el("code", null, snap.sandbox_root || "")))));
    const netSwitch = el("button", { class: "switch" + (snap.net_on ? " on" : ""), onclick: async () => {
      await this.command(snap.net_on ? "/net off" : "/net on", true);
    } });
    pane.appendChild(el("div", { class: "dsec" },
      el("h4", null, t("d-net")),
      el("div", { class: "switch-row" },
        el("b", { style: "font-weight:550" }, t("d-net")),
        el("small", null, t("d-net-sub")),
        netSwitch)));
  }

  async renderDrawerWorks() {
    const pane = $("d-works");
    let works = [];
    try { works = await hub.call("works.list", { name: this.name }, 20000); } catch (e) { /* */ }
    pane.innerHTML = "";
    if (!works.length) {
      pane.appendChild(el("div", { class: "placeholder-pane" }, t("d-empty-works")));
    } else {
      let lastDay = "";
      const icons = { image: "▣", web: "❖", audio: "♪", text: "≣", code: "⌨", file: "▢" };
      for (const w of works) {
        const day = new Date(w.mtime * 1000).toLocaleDateString();
        if (day !== lastDay) {
          lastDay = day;
          const today = new Date().toLocaleDateString();
          const yest = new Date(Date.now() - 86400000).toLocaleDateString();
          pane.appendChild(el("div", { class: "day-label" }, day === today ? t("today") : day === yest ? t("yesterday") : day));
        }
        pane.appendChild(el("div", { class: "work-row", onclick: () => hub.call("works.open", { path: w.path }).catch((e) => toast(e.message, true)) },
          el("div", { class: "wicon" }, icons[w.kind] || "▢"),
          el("div", { class: "winfo" },
            el("b", null, w.name),
            el("span", null, new Date(w.mtime * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }))),
          el("button", { class: "reveal", title: "Finder", onclick: (ev) => {
            ev.stopPropagation();
            hub.call("works.open", { path: w.path, reveal: true }).catch((e) => toast(e.message, true));
          } }, "⌖")));
      }
    }
    pane.appendChild(el("button", { class: "drawer-foot-link", onclick: () => {
      if (this.snap) hub.call("open.path", { path: this.snap.sandbox_root }).catch((e) => toast(e.message, true));
    } }, t("open-sandbox")));
  }

  async renderDrawerExtras(tab) {
    let extras = null;
    try { extras = await hub.call("chara.extras", { name: this.name }, 20000); } catch (e) { return; }
    if (tab === "d-memory") {
      const pane = $("d-memory");
      pane.innerHTML = "";
      pane.appendChild(el("div", { class: "dsec" },
        el("h4", null, t("d-mem-own")),
        el("div", { class: "memory-text" }, extras.memory || t("d-empty-mem"))));
      pane.appendChild(el("div", { class: "dsec" },
        el("h4", null, t("d-mem-user")),
        el("div", { class: "memory-text" }, extras.user_memory || t("d-empty-mem"))));
    } else {
      const pane = $("d-skills");
      pane.innerHTML = "";
      const snap = this.snap || {};
      pane.appendChild(el("div", { class: "dsec" },
        el("h4", null, t("d-toolpack")),
        el("div", { class: "tool-chips" },
          ...["terminal", "memory", "files", "goals", "skills", "speak", "rest"].map((n) => el("span", { class: "chip" }, n)))));
      const goals = (extras.goals && (Array.isArray(extras.goals) ? extras.goals : extras.goals.goals)) || [];
      pane.appendChild(el("div", { class: "dsec" },
        el("h4", null, t("d-goals")),
        ...(goals.length
          ? goals.slice(0, 12).map((g) => el("div", { class: "goal" }, el("i"), el("span", null,
              typeof g === "string" ? g : (g.text || g.title || JSON.stringify(g)).slice(0, 120))))
          : [el("div", { class: "placeholder-pane" }, t("d-empty-goals"))])));
    }
  }

  /* ---- permission requests, inline ---- */
  onPermission(p) {
    const box = el("div", { class: "sec", style: "max-width:430px;margin-left:40px" },
      el("h3", null, `🔐 ${p.kind}`),
      el("div", { class: "memory-text" }, p.reason || p.detail || ""),
      el("div", { class: "acts", style: "margin-top:10px" },
        el("button", { class: "btn soft", onclick: () => { this.client.permissionReply(p.id, false).catch(() => {}); box.remove(); } }, "✗"),
        el("div", { class: "grow" }),
        el("button", { class: "btn primary", onclick: () => { this.client.permissionReply(p.id, true).catch(() => {}); box.remove(); } }, "✓")));
    $("stream-inner").appendChild(box);
    this.scrollDown();
  }

  /* ---- composer & UI bindings ---- */
  setSending(streaming) {
    $("send-btn").textContent = streaming ? "■" : "↑";
    $("send-btn").className = streaming ? "stop" : "send";
  }

  renderPalette() {
    const rows = [
      [t("cmd-compact"), "/compact", () => this.command("/compact")],
      [t("cmd-quiet"), "/quiet 600", () => this.command("/quiet 600")],
      [t("cmd-reasoning"), "/reasoning", () => this.command("/reasoning")],
      [t("cmd-thinking"), "/thinking", async () => {
        this.showThinking = !this.showThinking;
        await this.command(`/thinking ${this.showThinking ? "on" : "off"}`);
      }],
      [t("cmd-net"), "/net", async () => {
        const on = this.snap && this.snap.net_on;
        await this.command(on ? "/net off" : "/net on");
      }],
    ];
    const pal = $("palette");
    pal.innerHTML = "";
    for (const [label, raw, fn] of rows) {
      pal.appendChild(el("div", { class: "row", onclick: () => { pal.classList.remove("open"); fn(); } },
        el("span", null, label), el("span", { class: "raw" }, raw)));
    }
    pal.appendChild(el("div", { class: "row danger", onclick: () => {
      pal.classList.remove("open");
      if (confirm(t("reset-confirm"))) {
        this.command("/reset").then(() => { $("stream-inner").innerHTML = ""; });
      }
    } }, el("span", null, t("cmd-reset")), el("span", { class: "raw" }, "/reset")));
    pal.appendChild(el("div", { class: "hint" }, t("cmd-hint")));
  }

  bindUI() {
    const input = $("composer-input");
    input.value = "";
    input.oninput = () => {
      input.style.height = "auto";
      input.style.height = Math.min(input.scrollHeight, 130) + "px";
    };
    input.onkeydown = (ev) => {
      if (ev.key === "Enter" && !ev.shiftKey && !ev.isComposing) {
        ev.preventDefault();
        this.submit();
      }
    };
    $("send-btn").onclick = () => {
      if (this.client.streaming) this.client.interrupt().catch(() => {});
      else this.submit();
    };
    $("chat-back").onclick = () => show("board");
    $("cmd-btn").onclick = () => $("palette").classList.toggle("open");
    $("drawer-btn").onclick = () => {
      $("drawer").classList.toggle("open");
      $("drawer-btn").classList.toggle("on");
      if ($("drawer").classList.contains("open")) this.openDrawerTab(document.querySelector("#drawer-tabs span.on").dataset.p);
    };
    $("drawer-tabs").onclick = (ev) => {
      const s = ev.target.closest("span");
      if (s) this.openDrawerTab(s.dataset.p);
    };
    $("mode-seg").onclick = async (ev) => {
      const s = ev.target.closest("span");
      if (!s || s.dataset.mode === this.mode) return;
      this.mode = s.dataset.mode;
      this.renderModeSeg();
      await this.command(`/mode ${this.mode}`, true);
    };
    $("net-btn").onclick = () => this.command("/net on");
  }

  async submit() {
    const input = $("composer-input");
    const text = input.value.trim();
    if (!text || this.client.streaming) return;
    input.value = "";
    input.style.height = "auto";
    $("palette").classList.remove("open");
    if (text.startsWith("/")) {
      const reply = await this.command(text);
      if (reply && reply.text) this.note(reply.text);
      return;
    }
    await this.sendUser(text);
  }
}

/* ============================ CREATE FLOW ============================ */
const SECTION_DEFS = [
  ["name", "sec-name"],
  ["description", "sec-description"],
  ["first_mes", "sec-first"],
  ["world_entries", "sec-world"],
  ["seed_goals", "sec-goals"],
  ["tagline", "sec-tagline"],
];

function normalizeDraft(d) {
  const draft = Object.assign({}, d || {});
  draft.name = String(draft.name || "");
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
  draft.theme_color = /^#[0-9a-fA-F]{6}$/.test(String(draft.theme_color || "")) ? String(draft.theme_color).toUpperCase() : "#5B9FD4";
  draft.avatar_svg = String(draft.avatar_svg || "");
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

function renderTellStep(root, flow) {
  root.innerHTML = "";
  root.appendChild(flowSteps(0));
  const box = el("textarea", { class: "tell-box", placeholder: t("tell-ph") });
  box.value = flow.origin;
  const inner = el("div", { class: "flow-inner" }, box);

  // writing-star hint: gentle, only when the default model lacks ★
  modelsCached().then((models) => {
    const d = (state.hub && state.hub.defaults) || {};
    const m = models.find((x) => x.id === d.model);
    if (m && !m.writing) inner.appendChild(el("div", { class: "cap-hint", style: "margin-top:10px" }, t("tell-star-hint")));
  });

  const goBtn = el("button", { class: "btn primary big", onclick: async () => {
    flow.origin = box.value.trim();
    if (!flow.origin) return;
    if (flow.lastDraftAt && !confirm(t("draft-overwrite-q"))) return;
    inner.querySelectorAll(".draft-error,.transcribing").forEach((n) => n.remove());
    const progress = el("div", { class: "transcribing" }, el("i"), t("transcribing"));
    inner.appendChild(progress);
    goBtn.disabled = true;
    try {
      flow.draft = normalizeDraft(await hub.call("cards.draft", { inspiration: flow.origin }, 240000));
      flow.lastDraftAt = Date.now();
      flow.versions = {};
      for (const [key] of SECTION_DEFS) flow.versions[key] = [sectionText(flow.draft, key)];
      renderShapeStep(root, flow);
    } catch (e) {
      goBtn.disabled = false;
      progress.remove();
      inner.appendChild(el("div", { class: "draft-error" },
        el("b", null, rpcErrText(e)),
        el("button", { class: "btn soft", onclick: () => goBtn.click() }, t("retry"))));
    }
  } }, t("tell-go"));

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
      flow.versions[key][flow.versions[key].length - 1] = text;
    });
  }

  // the telling never disappears — that is this step's core reassurance
  const origin = el("div", { class: "origin-panel", onclick: () => origin.classList.toggle("expanded") },
    el("div", { class: "oh" }, t("origin-title"), el("span", { class: "cue" }, t("origin-cue"))),
    el("div", { class: "ox" }, flow.origin));
  inner.appendChild(origin);

  if (flow.draft.notes && flow.draft.notes.length) {
    inner.appendChild(el("div", { class: "draft-note" }, flow.draft.notes.join(" · ")));
  }

  const avatarBox = el("div", { class: "avatar-preview" });
  function refreshAvatarBox() {
    avatarBox.innerHTML = "";
    avatarBox.appendChild(safeSvgForPreview(flow.draft.avatar_svg)
      ? el("img", { src: dataUriSvg(flow.draft.avatar_svg), alt: "" })
      : el("span", null, glyphOf(flow.draft.name)));
  }
  refreshAvatarBox();
  const svgText = el("textarea", { class: "svg-edit", placeholder: "<svg …" });
  svgText.value = flow.draft.avatar_svg || "";
  svgText.addEventListener("input", () => {
    flow.draft.avatar_svg = svgText.value;
    refreshAvatarBox();
  });
  const themeChip = el("div", { class: "theme-chip", style: `--card-theme:${flow.draft.theme_color || "#5B9FD4"}` }, t("theme-preview"));
  const colorInput = el("input", { type: "color", value: flow.draft.theme_color || "#5B9FD4" });
  colorInput.addEventListener("input", () => {
    flow.draft.theme_color = colorInput.value.toUpperCase();
    themeChip.style.cssText = `--card-theme:${flow.draft.theme_color}`;
  });
  const visualSec = el("div", { class: "sec visual-sec" },
    el("h3", null, t("sec-visual")),
    el("div", { class: "visual-row" },
      avatarBox,
      el("label", null, t("theme-color"), colorInput),
      themeChip),
    el("label", { class: "svg-label" }, t("avatar-svg-field"), svgText));
  inner.appendChild(visualSec);

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
    const rewriteBtn = el("button", { class: "rewrite", onclick: async () => {
      rewriteBtn.disabled = true;
      rewriteBtn.textContent = "…";
      try {
        const note = getLangCode() === "zh"
          ? `\n\n（请只为「${t(labelKey)}」部分换一种写法，其余保持原意；返回完整 JSON。）`
          : `\n\n(Rewrite only the "${t(labelKey)}" part differently; keep everything else; return full JSON.)`;
        collect();
        const fresh = normalizeDraft(await hub.call("cards.draft", { inspiration: flow.origin + note }, 240000));
        putSection(flow.draft, key, sectionText(fresh, key));
        versions.push(sectionText(fresh, key));
        flow.edited[key] = false;
        renderShapeStep(root, flow);
      } catch (e) {
        toast(rpcErrText(e), true);
        rewriteBtn.disabled = false;
        rewriteBtn.textContent = t("rewrite");
      }
    } }, t("rewrite"));
    inner.appendChild(el("div", { class: "sec", "data-sec": key },
      el("h3", null, t(labelKey), verLabel, revertBtn, rewriteBtn),
      textDiv));
  }

  root.appendChild(inner);
  root.appendChild(el("div", { class: "flow-bar" },
    el("button", { class: "btn text", onclick: () => { collect(); renderTellStep(root, flow); } }, t("back")),
    el("div", { class: "grow" }),
    el("button", { class: "btn soft", onclick: async () => {
      collect();
      try {
        await hub.call("card.from_draft", { draft: flow.draft, origin: flow.origin, as_draft: true }, 30000);
        toast(t("saved"));
        refreshHub();
      } catch (e) { toast(rpcErrText(e), true); }
    } }, t("save-draft")),
    el("button", { class: "btn primary", onclick: async () => {
      collect();
      try {
        const r = await hub.call("card.from_draft", { draft: flow.draft, origin: flow.origin }, 30000);
        await refreshHub();
        closeCreateFlow();
        const card = (state.hub.cards || []).find((c) => c.path === r.path) ||
          { path: r.path, name: flow.draft.name, lang: "zh" };
        openModal(el("div", null,
          el("h2", null, t("card-made")),
          el("div", { class: "sub" }, t("wake-now-q")),
          el("div", { class: "acts", style: "margin-top:14px" },
            el("button", { class: "btn text", onclick: () => { closeModal(); show("deck"); } }, t("later-deck")),
            el("div", { class: "grow" }),
            el("button", { class: "btn primary big", onclick: () => { closeModal(); openWakeSheet(card); } }, t("deck-wake")))));
      } catch (e) { toast(rpcErrText(e), true); }
    } }, t("next-card"))));
}

/* boot text */
applyTheme(localStorage.getItem("lm-theme") || "system");
setLangCode(localStorage.getItem("lm-lang") || (navigator.language.startsWith("zh") ? "zh" : "en"));
applyI18n();
