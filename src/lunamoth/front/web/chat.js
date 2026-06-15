/* LunaMoth Desktop renderer — the chara page: chat stream, the persistent
   right panel (tabbed: status | skills | goals | memory | gateway | settings),
   works / terminal sibling pages and the avatar editor.
   Idle driving is SERVER-SIDE only (supervisor) — this file never calls idle. */
"use strict";

/* Line icons for the attach menu (16×16, currentColor stroke — match the theme). */
const ICON_IMAGE = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="2.5"/><circle cx="8.5" cy="8.5" r="1.6"/><path d="M21 15l-5-5L5 21"/></svg>';
const ICON_FILE = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M14 3H6.5A1.5 1.5 0 0 0 5 4.5v15A1.5 1.5 0 0 0 6.5 21h11a1.5 1.5 0 0 0 1.5-1.5V8z"/><path d="M14 3v5h5"/></svg>';
const ICON_CLIP = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><rect x="8" y="2" width="8" height="4" rx="1"/><path d="M16 4h2a2 2 0 0 1 2 2v13a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2"/></svg>';
const ATTACH_ACCEPT_ALL = "image/*,.pdf,.txt,.md,.json,.csv,.docx,.doc,.xlsx,.zip,.log";

/* The avatar/theme editor (openAvatarEditor) now lives in app.js — it edits
   the presentation only (sidecar avatar + dual theme), soul untouched. */
/* Encode text into a QR image data-URL locally (vendored qrcode-generator).
   Returns "" if the encoder is unavailable or the text won't fit. */
function qrDataUrl(text) {
  try {
    if (typeof qrcode !== "function") return "";
    const q = qrcode(0, "M");   // type 0 = auto-size, error-correction M
    q.addData(String(text));
    q.make();
    return q.createDataURL(6, 8);  // cellSize, margin → data:image/gif;base64,…
  } catch (e) {
    return "";
  }
}


/* ============================ ATTACHMENTS（多模态：图片/文件） ============================ */
const ATTACH_MAX_BYTES = 25 * 1024 * 1024;   // client-side guard: don't base64-bloat huge uploads

/* Read a File to RAW base64 (NO "data:<mime>;base64," prefix) for the `data`
   wire field, plus a data-URL we keep for the local thumbnail preview. */
function readAttachment(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(reader.error || new Error("read failed"));
    reader.onload = () => {
      const url = String(reader.result || "");
      const comma = url.indexOf(",");           // "data:<mime>;base64,<DATA>"
      const data = comma >= 0 ? url.slice(comma + 1) : url;
      resolve({
        name: file.name || "file",
        mime: file.type || "application/octet-stream",
        size: file.size || 0,
        data,                                    // raw base64, prefix stripped
        url,                                     // full data: URL (preview only)
        isImage: (file.type || "").startsWith("image/"),
      });
    };
    reader.readAsDataURL(file);
  });
}

function humanSize(n) {
  n = Number(n) || 0;
  if (n < 1024) return n + " B";
  if (n < 1024 * 1024) return (n / 1024).toFixed(0) + " KB";
  return (n / 1024 / 1024).toFixed(1) + " MB";
}

/* ============================ GATEWAY（右侧面板「网关」页） ============================ */
const GW_MASK = "••••••••"; // hub.py _SECRET_MASK：后端给秘密字段回显的掩码
/* GW_PLATFORMS — 纯数据注册表（Hermes field-copy 三件套：label/help/placeholder）。
   字段形状：{key, label, secret, help, ph}。label/help/ph 一律过 t()：是 i18n key
   就翻译，是字面量（如 "CorpID"）原样显示。help 渲染为 label 下那行 .why——一句
   大白话讲清「为什么/在哪拿」。
   平台级 pending=<i18n key>：后端 adapter 未落地——渲染琥珀横幅、禁用启用开关与
   启动按钮；保存仍可用（hub.py messaging.save 对 adapters 做通用字段级合并，
   预存配置安全且落地即用）。
   注意：allowed_senders 是顶层共享字段（每个平台的「建议」区都渲染那一行，带
   gw-allowed-why 的安全理由），不要重复进 per-platform 字段列表。 */
// While we test WeChat, the gateway page shows ONLY WeChat (iLink). The other
// adapters (weixinpad/qq/telegram) still exist in the backend — they're
// just hidden from the deck for now; re-add their GW_PLATFORMS entries to bring
// them back. Platform key stays "weixin" (the backend adapter name).
const GW_PLATFORMS = {
  weixin: {
    label: "gw-weixin-label",   // 微信 / WeChat (bilingual via i18n)
    blurb: "gw-weixin-blurb",
    qr: true,
    note: "gw-weixin-note",
    required: [],
    recommended: [],
    advanced: [
      { key: "base_url", label: "base_url", secret: false, help: "gw-h-wx-base", ph: "https://ilinkai.weixin.qq.com" },
      { key: "bot_type", label: "bot_type", secret: false, help: "gw-h-wx-bot-type", ph: "3" },
      { key: "long_poll_timeout_ms", label: "long_poll_timeout_ms", secret: false, help: "gw-h-wx-poll", ph: "35000" },
      { key: "api_timeout_ms", label: "api_timeout_ms", secret: false, help: "gw-h-wx-api-timeout", ph: "15000" },
    ],
  },
};

/* The form itself lives on ChatController.renderGatewayPane (panel「网关」tab). */

/* ============================ CHAT CONTROLLER ============================ */
function lifeAttr(life) {
  if (!life || !life.state) return "";
  if (life.state === "idle_countdown") return "working"; // 机制不是情绪：与 working 同一 register
  return life.state;
}

class ChatController {
  constructor(name, opts) {
    this.name = name;
    this.opts = opts || {};
    // Open straight to a given right-panel tab once (e.g. the global gateway
    // view's "manage" deep-links to the gateway tab), then default to status.
    this._initialPanelTab = (opts && opts.panelTab) || null;
    this.client = new CharaClient(name);
    this.charName = name;
    this.deckCard = cardForSession(name);
    this.disposed = false;
    this.cur = { kind: null, node: null, textNode: null };
    this.toolChips = null;
    this.activeTools = new Map();
    this.pendingSuper = false;
    this.turnThink = null;
    this.work = { active: false, phase: "idle", thinkTokens: 0, toolName: "" };
    this.life = null;
    this.snap = null;
    this.superReadTs = 0;
    this.pendingSupers = [];   // {node, ts} bright bubbles awaiting the fade
    this.page = "chat";
    this.works = null;
    this.worksFilter = "all";
    this.lifeTimer = null;
    this.snapTimer = null;
    this.worksTimer = null;
    this.sbTimer = null;
    this.sessionStart = Date.now();
    this._panelSig = "";
    this.panelTab = "status";
    this.term = null;          // xterm Terminal（首次进入终端页才建）
    this.termFit = null;
    this.termWs = null;
    this._termCode = null;
    this._termClosedBar = null;
    this._termResize = null;
    this._termThemeObs = null;
    this._qrTimer = null;      // weixin QR 轮询（离开网关页即停）
    this._qrBusy = false;
    this.staged = [];          // pending attachments: {name,mime,size,data,url,isImage,node}
    this._dragDepth = 0;       // dragenter/leave counter so nested elements don't flicker the overlay
    try {
      this.thinkExpanded = localStorage.getItem("lm-chat-thinking-expanded") === "1";
    } catch (e) {
      this.thinkExpanded = false;
    }
    try {
      this.worksSeen = Number(localStorage.getItem(`lm-works-seen:${name}`) || 0);
    } catch (e) { this.worksSeen = 0; }
    const entry = (state.hub && state.hub.sessions.find((s) => s.name === name)) || null;
    if (entry) this.charName = entry.char_name;
  }

  entry() {
    return (state.hub && state.hub.sessions.find((s) => s.name === this.name)) || null;
  }

  /* ---- identity: avatar + accent from the deck card behind the session ---- */
  refreshIdentity() {
    this.deckCard = cardForSession(this.name);
    const card = this.deckCard;
    const root = $("chat-root");
    root.style.setProperty("--chara-accent", card && card.theme_color ? card.theme_color : "var(--accent)");
    const btn = $("chat-avatar");
    btn.querySelectorAll("img,.glyph-txt").forEach((n) => n.remove());
    btn.classList.remove("p-0", "p-1", "p-2", "p-3", "p-4", "p-5");
    const style = themeStyle(card);
    const src = avatarSrc(card);   // PNG/SVG data-URI, or "" → glyph fallback
    if (src) {
      btn.style.cssText = style;
      btn.insertBefore(el("img", { src, alt: "" }), btn.firstChild);
    } else if (style) {
      btn.style.cssText = style;
      btn.insertBefore(el("span", { class: "glyph-txt" }, glyphOf(this.charName)), btn.firstChild);
    } else {
      btn.style.cssText = "";
      btn.classList.add(paletteClass(this.charName));
      btn.insertBefore(el("span", { class: "glyph-txt" }, glyphOf(this.charName)), btn.firstChild);
    }
    this.applyCharVisuals(card);   // background image + 立绘 sprite (graceful when absent)
    const empty = $("stream-inner").querySelector(".chat-empty");
    if (empty) {
      const old = empty.querySelector(".avatar-s");
      if (old) old.replaceWith(this.bigAvatar());
    }
  }

  /* The optional per-card visual layers: full background + 立绘 sprite. Both are
     additive and degrade to nothing when the card carries no URL. We also (re)apply
     the operator's saved opacity/position prefs so a freshly opened chat is correct
     even if the global startup hook ran before this page existed. */
  applyCharVisuals(card) {
    applyVisualPrefs();   // re-assert --chat-bg-opacity / --chat-sprite-opacity / sprite pos
    const bg = $("chat-bg");
    if (bg) {
      const url = card && card.bg_url;
      bg.style.backgroundImage = url ? `url("${String(url).replace(/"/g, "%22")}")` : "";
    }
    const img = $("chat-sprite-img");
    if (img) {
      const url = card && card.sprite_url;
      // Hide the layer on load failure rather than show a broken-image glyph
      // (a silent broken visual is a bug; a missing sprite just means no sprite).
      img.onerror = () => { img.style.display = "none"; };
      if (url) { img.style.display = ""; img.src = String(url); }
      else { img.removeAttribute("src"); img.style.display = "none"; }
    }
  }

  msgAvatar() {
    return avatarNode(this.charName, this.deckCard, "avatar-s");
  }
  bigAvatar() {
    return avatarNode(this.charName, this.deckCard, "avatar-s");
  }

  /* ---- lifecycle ---- */
  async open(page) {
    $("stream-inner").innerHTML = "";
    this.setWorkState(false);
    $("chat-name").textContent = this.charName;
    this.setStatusWord(t("st-connecting"));
    $("chat-dot").className = "mini-dot off";
    $("composer-input").placeholder = t("composer-ph", { name: this.charName });
    $("chat-root").removeAttribute("data-life");
    this.refreshIdentity();
    this.bindUI();
    this.resetPanel();
    this.showPage(page || "chat");
    this.startSessionTimer();
    try {
      await this.client.connect();
      this.client.onProtocolEvent = (ev) => this.onEvent(ev);
      this.client.onPermissionAsk = (p) => this.onPermission(p);
      this.client.onClarifyAsk = (p) => this.onClarify(p);
      this.client.onPeerMessage = (p) => this.onPeerMessage(p);
      this.client.onTurnEnd = () => this.onTurnEnd();
      this.client.onLifeState = (p) => this.onLifeState(p);
      this.client.onRejoinGap = () => {
        // Ring couldn't replay (child restarted → seq reset). Just forget the
        // stale seq; do NOT clear the stream — open() already cleared it and
        // the attach() below renders the full restored history. Clearing here
        // races with that render and wiped the whole history (empty-history bug).
        this.client.clearRejoin();
      };
      this.client.onClose = (ev) => {
        if (!this.disposed) {
          this.note((ev && ev.reason) || t("conn-lost"));
          $("chat-dot").className = "mini-dot off";
        }
      };
      try {
        const r = await hub.call("superchat.read", { name: this.name, ts: 0 }, 10000);
        this.superReadTs = Number(r && r.read_ts) || 0;
      } catch (e) { /* keep 0 */ }
      const info = await this.client.attach();
      if (this.disposed) return;
      this.charName = info.char_name || this.charName;
      $("chat-name").textContent = this.charName;
      $("chat-dot").className = "mini-dot";
      this.refreshIdentity();
      this.renderRestored(info.restored || []);
      this.flushSuperReads();
      this.refreshSnapshot().then(() => {
        // attach ≠ 唤醒：resting 时保持沉睡氛围，不宣称"它知道你来了"
        const resting = this.snap && this.snap.rest_until * 1000 > Date.now();
        const hasOpening = info.opening && info.opening !== "none" && info.opening_text;
        if (!resting && !hasOpening) this.systemLine(t("st-arrived"), "arrived");
        this.maybeEmptyState();
      });
      this.renderStatusPane();
      this.snapTimer = setInterval(() => { if (!document.hidden) this.refreshSnapshot(); }, 6000);
      this.worksTimer = setInterval(() => { if (!document.hidden) this.pollWorks(); }, 45000);
      this.pollWorks();
      if (this.opts.netOn) await this.command("/net on", true);
      await this.handleOpening(info);
    } catch (e) {
      if (!this.disposed) this.note(e.message);
    }
  }

  dispose() {
    this.disposed = true;
    if (this._visHandler) document.removeEventListener("visibilitychange", this._visHandler);
    clearInterval(this.lifeTimer);
    clearInterval(this.snapTimer);
    clearInterval(this.worksTimer);
    clearInterval(this.sbTimer);
    this.stopQrPoll();
    this.disposeTerm();
    $("sb-timer").textContent = "";
    $("chat-root").removeAttribute("data-life");
    const c = this.client;
    (async () => {
      try { if (c.streaming) await c.interrupt(); } catch (e) { /* gone */ }
      try { await c.detach(); } catch (e) { /* gone */ }
      c.close();
    })();
    setTimeout(refreshHub, 600);
  }

  startSessionTimer() {
    const tick = () => {
      const s = Math.floor((Date.now() - this.sessionStart) / 1000);
      const mm = Math.floor((s % 3600) / 60).toString().padStart(2, "0");
      const ss = (s % 60).toString().padStart(2, "0");
      $("sb-timer").textContent = s >= 3600 ? `${Math.floor(s / 3600)}:${mm}:${ss}` : `${mm}:${ss}`;
    };
    tick();
    this.sbTimer = setInterval(tick, 1000);
  }

  /* ---- 对话|作品|终端：常驻不卸载，display 切换（终端的滚动缓冲因此存活） ---- */
  showPage(page) {
    this.page = page;
    document.querySelectorAll("#chat-tabs span").forEach((s2) =>
      s2.classList.toggle("on", s2.dataset.page === page));
    document.querySelectorAll(".chat-page").forEach((p) =>
      p.classList.toggle("on", p.id === `page-${page}`));
    if (page === "works") {
      this.renderWorks();
      this.markWorksSeen();
    }
    if (page === "term") {
      if (!this.term) this.initTerm();
      requestAnimationFrame(() => this.fitTerm());
    }
  }

  hashFor(page) {
    const base = `#/chara/${encodeURIComponent(this.name)}`;
    return page === "chat" ? base : `${base}/${page}`;
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

  /* ---- empty chat = the card's brand moment ---- */
  maybeEmptyState() {
    const inner = $("stream-inner");
    if (inner.children.length > 1) return;
    const onlyArrived = inner.children.length === 1 && inner.firstChild.classList.contains("arrived");
    if (inner.children.length === 0 || onlyArrived) {
      const card = this.deckCard;
      const tagline = (card && card.tagline) || "";
      const av = this.bigAvatar();
      inner.insertBefore(el("div", { class: "chat-empty" },
        av, el("b", null, this.charName),
        tagline ? el("div", { class: "tagline" }, tagline) : null), inner.firstChild);
    }
  }
  clearEmpty() {
    const node = $("stream-inner").querySelector(".chat-empty");
    if (node) node.remove();
  }

  /* ---- restored history ----
     The restored messages now carry the full forensic shape (reasoning_content,
     tool_calls, and role:"tool" results — display-only; the model's context is
     unchanged). Mirror the LIVE renderers so history reads the same as a turn:
     reasoning → a collapsed think block, tool_calls → compact tool-call lines,
     a tool result → a result line under its call. */
  renderRestored(messages) {
    this._restoreChips = new Map();  // tool_call_id -> {item, detail} so results fold into their call
    for (const m of messages.slice(-80)) {
      if (!m) continue;
      const content = typeof m.content === "string" ? m.content : "";
      const hasText = content.trim().length > 0;
      if (m.role === "user") {
        if (!hasText) continue;
        $("stream-inner").appendChild(el("div", { class: "user-msg" }, el("div", { class: "bubble" }, m.content)));
      } else if (m.role === "system") {
        if (hasText && m.kind !== "summary") this.systemLine(content);
      } else if (m.role === "tool") {
        if (hasText) this.restoreToolResult(m);
      } else if (m.role === "assistant") {
        if (m.kind === "think") {
          if (hasText) {
            this.appendMuseText(content);
            this.closeCurrent();
          }
          continue;
        }
        // Reasoning first (it preceded the visible reply/tool calls live).
        const reasoning = typeof m.reasoning_content === "string" ? m.reasoning_content.trim() : "";
        if (reasoning) this.restoreThinkBlock(reasoning);
        if (hasText) {
          this.appendCharText(content);
          this.closeCurrent();
        }
        // Tool calls: speak surfaces as a super-chat bubble (existing behavior);
        // every other call renders as a compact static tool-call line.
        const calls = Array.isArray(m.tool_calls) ? m.tool_calls : [];
        for (const tc of calls) {
          const fn = tc && tc.function;
          if (!fn) continue;
          if (fn.name === "speak") continue; // handled below as a super-chat bubble
          this.restoreToolCall(fn, tc.id);
        }
        for (const speak of speakTextsFromMessage(m)) {
          this.appendCharText(speak, { superChat: true, ts: m.ts || Date.now() / 1000 });
          this.closeCurrent();
        }
      }
    }
    this.scrollDown(true);
  }

  /* A finalized (collapsed) think block — same markup as the live ThinkDelta
     path (appendThinking), but static and already done. */
  restoreThinkBlock(text) {
    this.closeCurrent();
    this.breakToolGroup();
    const head = el("button", { class: "think-head" });
    const body = el("div", { class: "think-body" });
    const node = el("div", { class: "think-block" }, head, body);
    body.textContent = text;
    node.dataset.tokens = String(Math.max(1, estimateTokens(text)));
    head.onclick = () => this.toggleThinkingExpanded();
    $("stream-inner").appendChild(node);
    this.applyThinkState(node, false);
  }

  /* A compact static tool-call chip — mirrors the live tool row. The args
     (and, once it arrives, the result) live in the collapsible detail: nothing
     shows until you click the chip. */
  restoreToolCall(fn, callId) {
    const name = fn.name || "?";
    const group = this.ensureToolGroup();
    const detail = el("div", { class: "tool-detail" });
    if (isTechnical()) {
      const args = toolArgsSummary(fn.arguments);
      if (args) detail.textContent = args;
    }
    const button = el("button", { class: "tool-chip ok" }, `⚙ ${name}`);
    const item = el("div", { class: "tool-chip-item" }, button, detail);
    item.classList.toggle("has-detail", !!detail.textContent);
    button.onclick = () => item.classList.toggle("open");
    group.appendChild(item);
    if (callId) this._restoreChips.set(callId, { item, detail });
  }

  /* A tool result folds into ITS call's collapsible detail (matched by
     tool_call_id) — same as the live path, so it never leaks uncollapsed.
     An orphan result (no matching call in the window) gets its own chip. */
  restoreToolResult(m) {
    const text = abbreviate(typeof m.content === "string" ? m.content : "", 600);
    const rec = m.tool_call_id && this._restoreChips && this._restoreChips.get(m.tool_call_id);
    if (rec) {
      const sep = rec.detail.textContent ? "\n\n→ " : "→ ";
      rec.detail.textContent = rec.detail.textContent + sep + text;
      rec.item.classList.add("has-detail");
      return;
    }
    const group = this.ensureToolGroup();
    const detail = el("div", { class: "tool-detail" }, text);
    const button = el("button", { class: "tool-chip ok" }, "⚙ result");
    const item = el("div", { class: "tool-chip-item has-detail" }, button, detail);
    button.onclick = () => item.classList.toggle("open");
    group.appendChild(item);
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
    } else if (ev.type === "attachment") {
      this.appendCharAttachment(ev);
    }
    this.scrollDown();
  }

  appendCharText(text, opts) {
    this.clearEmpty();
    const kind = opts && opts.superChat ? "super" : "say";
    const ts = opts && opts.ts ? Number(opts.ts) : Date.now() / 1000;
    if (this.cur.kind !== kind) {
      this.closeCurrent();
      this.breakToolGroup();
      const textDiv = el("div", { class: "text" });
      const nameLine = el("div", { class: "name" }, this.charName);
      if (kind === "super") nameLine.appendChild(superBadge());
      else if (isTechnical()) nameLine.appendChild(el("span", { class: "chan-badge" }, t("channel-say")));
      const node = el("div", { class: "char-msg" + (kind === "super" ? " super-chat" : "") },
        this.msgAvatar(),
        el("div", { class: "body" }, nameLine, textDiv));
      if (kind === "super") {
        node.dataset.speakTs = String(ts);
        if (ts <= this.superReadTs) node.classList.add("read"); // 淡的看过了
        else this.pendingSupers.push({ node, ts });             // 亮的没看过
      }
      $("stream-inner").appendChild(node);
      this.cur = { kind, node, textNode: textDiv, raw: "" };
    }
    this.cur.raw = (this.cur.raw || "") + text;
    this.cur.textNode.textContent = this.cur.raw;
  }

  /* An inline attachment the chara sent: {url, mime, name, caption, channel}.
     Images render as a centered .wp-img; anything else becomes a small file chip
     with a download link. muse-channel attachments use the muse register; say is a
     normal char message. Always its own row (closes any open text/tool group). */
  appendCharAttachment(ev) {
    this.clearEmpty();
    this.closeCurrent();
    this.breakToolGroup();
    const isImage = String(ev.mime || "").startsWith("image/");
    const name = ev.name || (isImage ? "image" : "file");
    let media;
    if (isImage) {
      const img = el("img", { alt: name, loading: "lazy" });
      // If the image can't load, degrade to a download chip instead of a broken glyph.
      img.onerror = () => media.replaceWith(
        el("a", { class: "file-chip", href: String(ev.url || ""), download: name }, name));
      img.src = String(ev.url || "");   // set after creation so layout never blocks
      media = el("div", { class: "wp-img" }, img);
    } else {
      const link = el("a", { class: "file-chip", href: String(ev.url || ""), download: name }, name);
      media = link;
    }
    const caption = ev.caption ? el("div", { class: "attach-cap" }, ev.caption) : null;
    if (ev.channel === "muse") {
      const body = el("div", { class: "muse-text" }, media, caption);
      const node = el("div", { class: "muse-msg" },
        el("div", { class: "muse-label" }, t("muse-label")), body);
      $("stream-inner").appendChild(node);
    } else {
      const nameLine = el("div", { class: "name" }, this.charName);
      if (isTechnical()) nameLine.appendChild(el("span", { class: "chan-badge" }, t("channel-say")));
      const node = el("div", { class: "char-msg" },
        this.msgAvatar(),
        el("div", { class: "body" }, nameLine, media, caption));
      $("stream-inner").appendChild(node);
    }
    this.cur = { kind: null, node: null, textNode: null };
  }

  /* 已读 = 淡化：页面可见地渲染过 → superchat.read → 整体淡下去 */
  flushSuperReads() {
    if (document.visibilityState !== "visible") return;
    const pend = this.pendingSupers.splice(0);
    if (!pend.length) return;
    const maxTs = Math.max(...pend.map((p) => p.ts));
    setTimeout(() => {
      hub.call("superchat.read", { name: this.name, ts: maxTs }, 10000)
        .then((r) => {
          this.superReadTs = Math.max(this.superReadTs, Number(r && r.read_ts) || maxTs);
          for (const p of pend) p.node.classList.add("read");
          refreshHub();
        })
        .catch(() => { this.pendingSupers.push(...pend); });
    }, 1600);
  }

  appendMuseText(text) {
    this.clearEmpty();
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
    this.clearEmpty();
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
    this.clearEmpty();
    this.closeCurrent();
    const name = ev.name || "?";
    const group = this.ensureToolGroup();
    const detail = el("div", { class: "tool-detail" }, isTechnical() ? (ev.preview || "") : "");
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

  setWorkState(active, phase, detail) {
    const node = $("work-status");
    if (!node) return;
    if (!active) {
      this.work = { active: false, phase: "idle", thinkTokens: 0, toolName: "" };
      // The turn is over — hand the single transient-status slot back to the
      // life-state word (resting/idle/waiting/…), which lives in the same node.
      this.setStatusWord(this._lastLifeWord || "");
      return;
    }
    this.work = {
      active: true,
      phase: phase || this.work.phase || "generate",
      thinkTokens: detail && "thinkTokens" in detail ? detail.thinkTokens : (this.work.thinkTokens || 0),
      toolName: detail && "toolName" in detail ? detail.toolName : (this.work.toolName || ""),
    };
    node.hidden = false;
    node.className = "work-status " + this.work.phase;
    if (this.work.phase === "think") {
      node.textContent = t("work-thinking", { n: this.work.thinkTokens || 0 });
    } else if (this.work.phase === "tool") {
      node.textContent = t("work-tool", { name: this.work.toolName || "tool" });
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
    this.setWorkState(false);
    this.flushSuperReads();
    // Electron shell: surface what was said while the window wasn't watched.
    if (this.pendingNotify && window.lunamothNative && !document.hasFocus())
      window.lunamothNative.notify(this.charName, this.pendingNotify.trim().slice(0, 200));
    this.pendingNotify = "";
    this.renderLifeState();
    this.scrollDown();
    if (this.page !== "works") this.pollWorks();
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

  /* The header stays STABLE (name + static dot only). All transient status —
     life-state AND work/thinking phase — renders in the single #work-status
     slot above the composer. During an active turn the work phase owns that
     slot; setStatusWord only paints the life word when no turn is running. */
  setStatusWord(word) {
    this._lastLifeWord = word || "";
    if (this.work && this.work.active) return;
    const node = $("work-status");
    if (!node) return;
    if (word) {
      node.hidden = false;
      node.className = "work-status life";
      node.textContent = word;
    } else {
      node.hidden = true;
      node.className = "work-status";
      node.textContent = "";
    }
  }

  /* ---- driving turns ---- */
  async runStream(fn) {
    this.setSending(true);
    this.turnThink = null;
    this._appTurn = true;   // app-driven: this runStream owns finalize()
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
      this._appTurn = false;
    }
    this.flushQueue();   // the chara is free now — deliver any staged message
  }

  // A turn the app did NOT drive (self-work / WeChat / idle) streamed events
  // that switched the "generating…" indicator on; without an app-side runStream
  // to call finalize(), it would stick forever (the "still generating after
  // rest" bug). The backend's turn_end signal is that missing completion.
  onTurnEnd() {
    if (this.disposed) return;
    if (!this._appTurn && this.work && this.work.active) this.finalize();
    this.flushQueue();   // a self-work/WeChat turn just ended — deliver staged msg
  }

  // Send-anytime: while the chara is busy, a typed message is STAGED (not sent,
  // not an interrupt) and shown as a pending bubble; it's delivered as a normal
  // turn the moment the chara finishes what it's doing.
  queueMessage(text, atts) {
    this.clearEmpty();
    this._queue = this._queue || [];
    const row = el("div", { class: "user-msg queued" },
      this.attachmentRow(atts),
      text ? el("div", { class: "bubble" }, text) : null,
      el("div", { class: "via-tag" }, t("queued-hint")));
    $("stream-inner").appendChild(row);
    this.scrollDown(true);
    this._queue.push({ text, atts: atts || [], node: row });
  }

  flushQueue() {
    if (this.disposed || !this._queue || !this._queue.length) return;
    if (this.client.streaming || this._appTurn) return;   // still busy
    const item = this._queue.shift();
    if (item.node && item.node.isConnected) item.node.remove();
    this.sendUser(item.text, item.atts);   // a normal turn; its completion flushes the next
  }

  async sendUser(text, atts) {
    this.clearEmpty();
    atts = atts || [];
    $("stream-inner").appendChild(el("div", { class: "user-msg" },
      this.attachmentRow(atts),
      text ? el("div", { class: "bubble" }, text) : null));
    this.scrollDown(true);
    const wire = atts.map((a) => ({ name: a.name, mime: a.mime, size: a.size, data: a.data }));
    await this.runStream(() => this.client.send(text, wire));
  }

  // A message that arrived from another channel (WeChat): show it as an incoming
  // user bubble with a small "via 微信" tag, ahead of the chara's streamed reply.
  onPeerMessage(p) {
    const text = (p && p.text) || "";
    if (!text) return;
    this.clearEmpty();
    this.closeCurrent();
    const row = el("div", { class: "user-msg" }, el("div", { class: "bubble" }, text));
    const src = (p && p.source) || "";
    if (src) {
      const label = (typeof gwPlatLabel === "function") ? gwPlatLabel(src) : src;
      row.appendChild(el("div", { class: "via-tag" }, t("via-tag") + " " + label));
    }
    $("stream-inner").appendChild(row);
    this.scrollDown(true);
  }

  /* ---- mood layer v2：安静的在场 ----
     状态由事实文字承载（分钟数逐分钟更新），视觉只做静态基色变化。 */
  onLifeState(life) {
    this.life = life || null;
    this.renderLifeState();
    if (!this.lifeTimer) this.lifeTimer = setInterval(() => this.renderLifeState(), 1000);
  }

  renderLifeState() {
    const root = $("chat-root");
    if (this.client.streaming) {
      root.setAttribute("data-life", "working");
      return;
    }
    // Autonomy off (the board/in-chat switch): the chara never self-works, so
    // any stale autonomous life state (e.g. a past "backoff") must not linger —
    // show a calm, factual "autonomy off".
    const entry = this.entry();
    if (entry && entry.paused) {
      root.setAttribute("data-life", "");
      this.setStatusWord(t("st-paused"));
      $("composer-input").placeholder = t("composer-ph", { name: this.charName });
      return;
    }
    const life = this.life;
    if (!life) return;
    root.setAttribute("data-life", lifeAttr(life));
    this.setStatusWord(this.lifeWord(life));
    const resting = life.state === "resting";
    $("composer-input").placeholder = resting
      ? t("composer-resting-ph")
      : t("composer-ph", { name: this.charName });
    if (isTechnical() && life.next_cycle_at) {
      const rowVal = $("p-next-cycle-val");
      if (rowVal) rowVal.textContent = t("next-cycle-at", { time: fmtClock(life.next_cycle_at) });
    }
  }

  /* 比 board 版 lifeText 多一层事实：等你回复时，说清还有约几分钟回去做自己的事。 */
  lifeWord(life) {
    if (life.state === "waiting" && life.engaged_until) {
      const leftMin = Math.ceil((life.engaged_until - Date.now() / 1000) / 60);
      if (leftMin >= 1) return t("life-waiting-back", { n: leftMin });
    }
    return lifeText(life);
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

  /* ---- 整理记忆 (compaction): confirm → loading button → progress line ---- */
  confirmCompaction(btn) {
    if (this.client.streaming || this._appTurn) { toast(t("busy-cmd")); return; }
    const body = el("div", { class: "confirm-box" },
      el("h4", null, t("compact-title")),
      el("p", { class: "confirm-body" }, t("compact-body")));
    const cancel = el("button", { class: "btn soft" }, t("cancel"));
    cancel.onclick = () => closeModal();
    const ok = el("button", { class: "btn primary" }, t("compact-ok"));
    ok.onclick = () => { closeModal(); this.runCompaction(btn); };
    body.appendChild(el("div", { class: "confirm-acts" }, cancel, ok));
    openModal(body);
  }

  async runCompaction(btn) {
    if (this._compacting) return;
    this._compacting = true;
    if (btn) { btn.disabled = true; btn.classList.add("loading"); }
    // A progress line in the conversation (compaction is one summary pass —
    // we show it running, then replace it with the result).
    const note = el("div", { class: "sys-note compacting" }, t("compact-running"));
    $("stream-inner").appendChild(note);
    this.scrollDown(true);
    try {
      const reply = await this.client.command("/compact");
      note.classList.remove("compacting");
      note.textContent = (reply && reply.text) || t("compact-done");
      this.refreshSnapshot();
    } catch (e) {
      note.classList.remove("compacting");
      note.classList.add("err");
      note.textContent = rpcErrText(e);
    } finally {
      this._compacting = false;
      if (btn) { btn.disabled = false; btn.classList.remove("loading"); }
    }
  }

  /* ---- snapshot -> header + panel ---- */
  async refreshSnapshot() {
    if (!this.client.open || this.client.streaming) return;
    let snap;
    try { snap = await this.client.snapshot(); } catch (e) { return; }
    if (this.disposed) return;
    this.snap = snap;
    $("net-btn").style.display = snap.net_on ? "none" : "flex";
    $("net-btn").title = t("net-off-tip");
    if (!this.life && snap.rest_until && snap.rest_until * 1000 > Date.now()) {
      $("chat-root").setAttribute("data-life", "resting");
      this.setStatusWord(t("life-resting-until", { time: fmtClock(snap.rest_until) }));
      $("composer-input").placeholder = t("composer-resting-ph");
    }
    if (this._termCode && snap.sandbox_root) this._termCode.textContent = snap.sandbox_root;
    this.renderStatusPane();
  }

  onHubState() {
    this.renderStatusPane();
  }

  onDisplayModeChanged() {
    this._panelSig = "";
    this.renderStatusPane();
  }

  /* ============================ RIGHT PANEL ============================ */
  prow(opts) {
    // opts: {label, sub, val, valNode, click, switchOn, onSwitch, dot, chev, cls, tidy}
    const row = el("div", { class: "prow" + (opts.click || opts.onSwitch ? " click" : "") + (opts.cls ? " " + opts.cls : "") });
    const main = el("div", { class: "pmain" }, el("span", { class: "plbl" }, opts.label));
    if (opts.sub) main.appendChild(el("span", { class: "psub" }, opts.sub));
    if (opts.bar !== undefined) {
      const pct = Math.max(0, Math.min(100, opts.bar));
      main.appendChild(el("div", { class: "pbar" }, el("i", { class: pct > 85 ? "hot" : "", style: `width:${pct}%` })));
    }
    row.appendChild(main);
    if (opts.dot) row.appendChild(el("span", { class: "pdot " + opts.dot }));
    if (opts.valNode) row.appendChild(opts.valNode);
    else if (opts.val !== undefined) row.appendChild(el("span", { class: "pval" }, opts.val));
    if (opts.tidy) row.appendChild(el("button", { class: "tidy-link", onclick: (ev) => { ev.stopPropagation(); opts.tidy(); } }, t("p-tidy")));
    if (opts.onSwitch) {
      const sw = el("button", { class: "switch" + (opts.switchOn ? " on" : ""), onclick: (ev) => {
        ev.stopPropagation();
        sw.classList.toggle("on");  // optimistic: flip immediately, the re-render reconciles
        opts.onSwitch();
      } });
      row.appendChild(sw);
    }
    if (opts.chev) row.appendChild(el("span", { class: "chev" }, "›"));
    if (opts.click) row.addEventListener("click", opts.click);
    return row;
  }

  /* 标签页骨架：状态 | 能力 | 记忆 | 网关 | 设置。
     懒渲染：首次打开才渲染；每次回访都刷新（状态页由 snapshot 循环驱动）。 */
  resetPanel() {
    this.stopQrPoll();
    this._panelSig = "";
    document.querySelectorAll(".panel-pane").forEach((p) => { p.innerHTML = ""; });
    this.showPanelTab(this._initialPanelTab || "status");
    this._initialPanelTab = null;
  }

  showPanelTab(which) {
    if (this.panelTab === "gateway" && which !== "gateway") this.stopQrPoll();
    this.panelTab = which;
    document.querySelectorAll("#panel-tabs span").forEach((s2) =>
      s2.classList.toggle("on", s2.dataset.ptab === which));
    document.querySelectorAll(".panel-pane").forEach((p) =>
      p.classList.toggle("on", p.id === `ppane-${which}`));
    this.renderPanelPane(which);
  }

  renderPanelPane(which) {
    if (which === "status") {
      this._panelSig = "";
      this.renderStatusPane();
      return;
    }
    const body = $(`ppane-${which}`);
    body.innerHTML = "";
    if (which === "skills") this.renderSkillsPage(body);
    else if (which === "goals") this.renderGoalsPage(body);
    else if (which === "memory") this.renderMemoryPage(body);
    else if (which === "gateway") this.renderGatewayPane(body);
    else if (which === "settings") this.renderSettingsPane(body);
  }

  /* —— 状态页：现有的 prow 行，原样保留 —— */
  renderStatusPane() {
    if (this.disposed) return;
    const snap = this.snap;
    const entry = this.entry();
    const pane = $("ppane-status");
    const sig = JSON.stringify([
      snap && [snap.model, snap.reasoning, snap.context_tokens, snap.memory_chars, snap.net_on,
               snap.mode, snap.show_thinking, snap.isolation, snap.quiet, snap.patience, snap.embodiment],
      entry && entry.gateway && [entry.gateway.state, entry.gateway.platform],
      getLangCode(), isTechnical(),
    ]);
    if (sig === this._panelSig) return;
    this._panelSig = sig;
    pane.innerHTML = "";
    if (!snap) return;

    // —— 状态区：高频、温和、一眼可读，点击即改（标签页本身就叫「状态」，不再加组头） ——
    const st = el("div", { class: "pgroup" });
    st.appendChild(this.prow({
      label: t("p-model"),
      valNode: el("span", { class: "pval" }, el("code", null, snap.model || "—")),
      chev: true,
      click: (ev) => this.openModelPopover(ev),
    }));
    st.appendChild(this.prow({
      label: t("p-effort"),
      val: t("eff-" + (snap.reasoning || "medium")) + (snap.reasoning_supported ? "" : " ⌀"),
      chev: true,
      click: (ev) => this.openModelPopover(ev),
    }));
    // Context — the ring the owner likes (only this block differs from a prow).
    const pctCtx = snap.context_max ? Math.round(100 * snap.context_tokens / snap.context_max) : 0;
    const ring = el("div", { class: "ctx-ring" + (pctCtx >= 75 ? " hot" : "") });
    ring.style.setProperty("--p", String(pctCtx));
    const tidyBtn = el("button", { class: "btn soft ctx-tidy" }, t("tidy-mem"));
    tidyBtn.onclick = () => this.confirmCompaction(tidyBtn);
    st.appendChild(el("div", { class: "ctx-sec" },
      el("div", { class: "ctx-sec-label" }, t("p-context")),
      el("div", { class: "ctx-big" },
        ring,
        el("div", { class: "ctx-nums" },
          el("b", null, `${pctCtx}%`),
          el("div", null, `${(snap.context_tokens / 1000).toFixed(1)}k / ${(snap.context_max / 1000).toFixed(0)}k tokens`)),
        tidyBtn)));
    const pctMem = snap.memory_max ? Math.round(100 * snap.memory_chars / snap.memory_max) : 0;
    st.appendChild(this.prow({
      label: t("p-memory"),
      bar: pctMem,
      val: `${snap.memory_chars} / ${snap.memory_max}`,
      chev: true,
      click: () => this.showPanelTab("memory"),
    }));
    st.appendChild(this.prow({
      label: t("p-sandbox"),
      valNode: el("span", { class: "pval" }, `${isoGlyph(snap.isolation)} `,
        el("code", null, (snap.workspace_root || "").split("/").slice(-2).join("/"))),
      click: () => hub.call("open.path", { path: snap.workspace_root || snap.sandbox_root }).catch((e) => toast(e.message, true)),
    }));
    st.appendChild(this.prow({
      label: t("p-net"), sub: t("p-net-sub"),
      switchOn: !!snap.net_on,
      onSwitch: () => this.command(snap.net_on ? "/net off" : "/net on", true),
    }));
    // Autonomy = the SAME persisted on/off the board shows (the pause marker),
    // not the old live/chat mode — so inner and outer never disagree. Toggling
    // here keeps the chat alive (it doesn't stop the child).
    const autonomyOn = !(entry && entry.paused);
    st.appendChild(this.prow({
      label: t("p-autonomy"), sub: t("p-autonomy-sub"),
      switchOn: autonomyOn,
      onSwitch: async () => {
        try {
          await hub.call("chara.set_autonomy", { name: this.name, on: !autonomyOn }, 15000);
          await refreshHub();
          this.renderStatusPane();
        } catch (e) { toast(rpcErrText(e), true); }
      },
    }));
    // No show-thinking toggle: the thinking block is always rendered and
    // expandable in-line (click to reveal), so an on/off switch is redundant.
    const gw = entry && entry.gateway;
    st.appendChild(this.prow({
      label: t("p-gateway"),
      dot: gw && gw.state === "running" ? "live" : "",
      val: gw && gw.state === "running" ? t("gw-running") : t("gw-stopped"),
      chev: true,
      click: () => this.showPanelTab("gateway"),
    }));
    if (isTechnical() && this.life && this.life.next_cycle_at) {
      const row = this.prow({ label: t("p-next-cycle"), val: "" });
      row.querySelector(".pval").id = "p-next-cycle-val";
      row.querySelector(".pval").textContent = t("next-cycle-at", { time: fmtClock(this.life.next_cycle_at) });
      st.appendChild(row);
    }
    pane.appendChild(st);
  }

  numField(body, labelKey, whyKey, value, onSave) {
    const input = el("input", { type: "number", value: String(Math.round(value)) });
    const btn = el("button", { class: "btn soft", onclick: async () => {
      btn.disabled = true;
      await onSave(input.value.trim());
      btn.disabled = false;
    } }, t("save"));
    body.appendChild(el("div", { class: "pfield" },
      el("label", null, t(labelKey)),
      el("div", { class: "why" }, t(whyKey)),
      el("div", { class: "ctl" }, input, btn)));
  }

  /* —— 设置页：节奏（quiet/patience）、安静一会儿、embodiment 事实行、reset ——
     节奏只有 patience：时间流速（tempo）已在产品层移除。
     embodiment 是唤醒时的一次性选择（保护 prompt cache），这里只陈述事实。 */
  renderSettingsPane(body) {
    const snap = this.snap || {};
    // quiet =「等你多久」；patience =「它自己生活的节拍」（owner 的文案语义）
    this.numField(body, "p-quiet", "p-quiet-sub", snap.quiet || 300,
      (v) => this.command(`/quiet ${v}`, false));
    this.numField(body, "p-patience", "p-patience-sub", snap.patience || 600,
      (v) => this.command(`/patience ${v}`, false));
    body.appendChild(this.prow({
      label: t("p-quiet-act"),
      click: () => { this.command("/quiet 600", false); },
    }));
    const emb = snap.embodiment === "actor" ? "actor" : "literal";
    body.appendChild(el("div", { class: "pfield", style: "margin-top:16px" },
      el("label", null, t("p-embodiment")),
      el("div", { class: "why" }, t("emb-" + emb)),
      el("div", { class: "ctl" },
        el("span", { class: "fact" }, emb),
        el("span", { class: "fact-hint" }, t("emb-fact-hint")))));
    const danger = el("div", { class: "pgroup", style: "margin-top:22px" });
    danger.appendChild(this.prow({
      label: t("p-reset"), cls: "danger click",
      click: () => {
        if (confirm(t("reset-confirm"))) {
          this.command("/reset").then(() => { $("stream-inner").innerHTML = ""; this.maybeEmptyState(); });
        }
      },
    }));
    body.appendChild(danger);

    // Danger zone — deletion lives ONLY here (the last thing in the settings
    // pane), never in a status/⋯ menu. Opens the triple-confirm flow.
    const dz = el("div", { class: "del-danger-zone" },
      el("h4", null, t("danger-zone")),
      el("div", { class: "dz-sub" }, t("del-open-sub")),
      el("button", { class: "btn danger dz-del", onclick: () => {
        openDeleteModal({ name: this.name, char_name: this.charName });
      } }, t("del-open")));
    body.appendChild(dz);
  }

  async renderSkillsPage(body) {
    let pack = "";
    const deckCard = this.deckCard;
    if (deckCard) {
      try {
        const full = await hub.call("card.read", { path: deckCard.path }, 20000);
        const ext = full.extensions && full.extensions.lunamoth;
        if (ext && ext.toolpack) pack = String(ext.toolpack);
      } catch (e) { /* fine */ }
    }
    body.appendChild(el("div", { class: "dsec" },
      el("h4", null, t("p-toolpack")),
      el("div", { class: "tool-chips" },
        el("span", { class: "chip" }, pack || "sandbox"))));
    const skillsReply = await this.command("/skills", true);
    body.appendChild(el("div", { class: "dsec" },
      el("h4", null, "Skills"),
      (skillsReply && skillsReply.text)
        ? el("div", { class: "memory-text" }, skillsReply.text.slice(0, 2000))
        : el("div", { class: "placeholder-pane" }, t("d-empty-skills"))));
  }

  async renderGoalsPage(body) {
    let extras = null;
    try { extras = await hub.call("chara.extras", { name: this.name }, 20000); } catch (e) { /* */ }
    const goals = (extras && extras.goals && (Array.isArray(extras.goals) ? extras.goals : extras.goals.goals)) || [];
    if (!goals.length) {
      body.appendChild(el("div", { class: "placeholder-pane" }, t("d-empty-goals")));
      return;
    }
    // 进行中的排在前，已完成 / 已放弃的归档其后并淡化、加状态标。
    const rank = { active: 0, done: 1, dropped: 2 };
    const ordered = goals.slice().sort((a, b) =>
      (rank[(a && a.status) || "active"] ?? 0) - (rank[(b && b.status) || "active"] ?? 0));
    body.appendChild(el("div", { class: "dsec" },
      ...ordered.slice(0, 30).map((g) => {
        const status = (typeof g === "object" && g.status) || "active";
        const text = typeof g === "string" ? g : (g.text || g.title || JSON.stringify(g));
        const row = el("div", { class: "goal goal-" + status },
          el("i"),
          el("span", null, String(text).slice(0, 200)));
        if (status !== "active") row.appendChild(el("span", { class: "goal-badge " + status }, t("goal-" + status)));
        return row;
      })));
  }

  async renderMemoryPage(body) {
    let extras = null;
    try { extras = await hub.call("chara.extras", { name: this.name }, 20000); } catch (e) { return; }
    body.appendChild(el("div", { class: "dsec" },
      el("h4", null, t("d-mem-own")),
      el("div", { class: "memory-text" }, extras.memory || t("d-empty-mem"))));
    body.appendChild(el("div", { class: "dsec" },
      el("h4", null, t("d-mem-user")),
      el("div", { class: "memory-text" }, extras.user_memory || t("d-empty-mem"))));
  }

  /* ============================ GATEWAY PANE（面板「网关」页） ============================ */
  stopQrPoll() {
    if (this._qrTimer) clearInterval(this._qrTimer);
    this._qrTimer = null;
    this._qrBusy = false;
  }

  async renderGatewayPane(body) {
    this.stopQrPoll();
    const name = this.name;
    // messaging.get → {config: <masked>, path}；错误原样展示，不做占位回退。
    let cfg = null;
    try {
      const r = await hub.call("messaging.get", { name }, 15000);
      cfg = (r && r.config) || {};
    } catch (e) {
      if (this.disposed || this.panelTab !== "gateway") return;
      body.appendChild(el("div", { class: "gw-error" }, rpcErrText(e)));
      return;
    }
    let gwStatus = { state: "stopped", platform: "", detail: "" };
    try { gwStatus = await hub.call("gateway.status", { name }, 15000); } catch (e) { /* keep */ }
    if (this.disposed || this.panelTab !== "gateway") return;

    const ctrl = this;
    const platKeys = Object.keys(GW_PLATFORMS);
    let plat = platKeys.find((k) => (cfg.adapters || {})[k]) || platKeys[0];
    let enabled = !!cfg.enabled;
    const inputs = {};       // field -> input element (per render)
    const initial = {};      // field -> 渲染时的初值（含掩码），用于字段级合并
    let allowedInput = null;
    const root = el("div", null);
    body.appendChild(root);

    function adaptersOf() { return (cfg && cfg.adapters) || {}; }

    function requiredFilled(p) {
      const spec = GW_PLATFORMS[p];
      const a = adaptersOf()[p] || {};
      return spec.required.length === 0
        ? Object.keys(a).length > 0 || p === "weixin"
        : spec.required.every((fd) => String(a[fd.key] ?? "").length > 0);
    }

    function chips() {
      const st = (gwStatus && gwStatus.state) || "stopped";
      const runText = st === "running" ? t("gw-running")
        : st === "needs_login" ? t("gw-needs-login") : t("gw-stopped");
      const runCls = st === "running" ? "ok" : st === "needs_login" ? "warn" : "";
      return el("div", { class: "gw-chips" },
        el("span", { class: "gw-chip " + (enabled ? "ok" : "") }, enabled ? t("gw-enabled") : t("gw-disabled")),
        el("span", { class: "gw-chip " + (requiredFilled(plat) ? "ok" : "warn") },
          requiredFilled(plat) ? t("gw-configured") : t("gw-needs-setup")),
        el("span", { class: "gw-chip " + runCls }, runText));
    }

    function fieldRow(plat2, fd) {
      // fd = {key, label, secret, help, ph}（GW_PLATFORMS 注册表条目；label/help/ph 过 t()）
      const a = adaptersOf()[plat2] || {};
      const f = fd.key;
      const value = a[f] !== undefined && a[f] !== null ? String(a[f]) : "";
      const input = el("input", {
        value,
        placeholder: fd.ph ? t(fd.ph) : (fd.secret ? GW_MASK : ""),
        type: fd.secret ? "password" : "text",
      });
      inputs[f] = input;
      initial[f] = value;
      return el("div", { class: "gw-field" },
        el("label", null, fd.label ? t(fd.label) : f),
        fd.help ? el("div", { class: "why" }, t(fd.help)) : null,
        input,
        fd.secret ? el("div", { class: "why" }, t("gw-secret-keep")) : null);
    }

    /* —— weixin 扫码登录：weixin.qr → 轮询 weixin.qr_status（~2.5s，有上限；
       离开本页 / 销毁即停） —— */
    function qrSection() {
      const box = el("div", { class: "gw-qr-box" });
      const idle = (msgNode, refetch) => {
        ctrl.stopQrPoll();
        box.innerHTML = "";
        if (msgNode) box.appendChild(msgNode);
        box.appendChild(el("button", { class: "btn soft", onclick: fetchQr },
          t(refetch ? "gw-qr-refetch" : "gw-qr-get")));
      };
      async function fetchQr() {
        ctrl.stopQrPoll();
        box.innerHTML = "";
        box.appendChild(el("div", { class: "gw-blurb" }, t("gw-qr-fetching")));
        let qr;
        try {
          qr = await hub.call("weixin.qr", { name }, 30000);
        } catch (e) {
          if (!box.isConnected) return;
          idle(el("div", { class: "gw-qr-err" }, rpcErrText(e)), true);
          return;
        }
        if (!box.isConnected || ctrl.disposed || ctrl.panelTab !== "gateway") return;
        box.innerHTML = "";
        // Encode the SCANNABLE login payload (scan_content = qrcode_img_content)
        // into a QR locally — never ship the login payload to a third party.
        // `qrcode` is only the polling token, NOT what the phone scans.
        const scan = qr.scan_content || qr.img || "";
        const dataUrl = scan ? qrDataUrl(scan) : "";
        if (dataUrl) {
          box.appendChild(el("img", { class: "gw-qr-img", src: dataUrl, alt: "QR" }));
        } else if (qr.fallback_url) {
          // Last resort only (no local encoder): the external QR image service.
          box.appendChild(el("img", { class: "gw-qr-img", src: qr.fallback_url, alt: "QR" }));
        }
        const stLine = el("div", { class: "gw-blurb", style: "margin:4px 0 0" }, t("gw-qr-waiting"));
        box.appendChild(stLine);
        let polls = 0;
        ctrl._qrTimer = setInterval(async () => {
          if (ctrl.disposed || ctrl.panelTab !== "gateway" || !box.isConnected) { ctrl.stopQrPoll(); return; }
          if (ctrl._qrBusy) return;
          if (++polls > 96) { idle(el("div", { class: "gw-qr-err" }, t("gw-qr-timeout")), true); return; }
          ctrl._qrBusy = true;
          let r;
          try {
            r = await hub.call("weixin.qr_status", { name, qrcode: qr.qrcode }, 10000);
          } catch (e) {
            if (box.isConnected) idle(el("div", { class: "gw-qr-err" }, rpcErrText(e)), true);
            else ctrl.stopQrPoll();
            return;
          } finally {
            ctrl._qrBusy = false;
          }
          if (r.status === "confirmed") {
            ctrl.stopQrPoll();
            box.innerHTML = "";
            box.appendChild(el("div", { class: "gw-qr-ok" },
              t("gw-qr-confirmed", { id: r.account_id || "" })));
            // Login saved + the weixin adapter is now ensured (backend). Start
            // the gateway so it actually connects and receives messages — a
            // scanned-but-never-started gateway was why nothing arrived.
            box.appendChild(el("div", { class: "gw-blurb", style: "margin-top:6px" }, t("gw-qr-starting")));
            try {
              await hub.call("gateway.stop", { name }, 15000).catch(() => {});
              await hub.call("gateway.start", { name }, 30000);
            } catch (e) { /* the pane refresh below surfaces the state */ }
            // Re-render through renderPanelPane (it fetches + clears the pane
            // body); calling renderGatewayPane() with no body crashed on
            // body.appendChild ("Cannot read properties of undefined").
            if (!ctrl.disposed && ctrl.panelTab === "gateway") ctrl.renderPanelPane("gateway");
          } else if (r.status === "expired") {
            idle(el("div", { class: "gw-qr-err" }, t("gw-qr-expired")), true);
          }
        }, 2500);
      }
      idle(null, false);
      return box;
    }

    function render() {
      ctrl.stopQrPoll();
      root.innerHTML = "";
      for (const k of Object.keys(inputs)) delete inputs[k];
      for (const k of Object.keys(initial)) delete initial[k];
      const spec = GW_PLATFORMS[plat];
      root.appendChild(el("div", { class: "sub", style: "margin-bottom:12px" }, t("gw-sub")));
      root.appendChild(el("div", { class: "gw-plats" },
        ...platKeys.map((k) => el("button", { class: k === plat ? "on" : "", onclick: () => { plat = k; render(); } },
          t(GW_PLATFORMS[k].label)))));
      root.appendChild(chips());
      root.appendChild(el("div", { class: "gw-blurb" }, t(spec.blurb)));
      if (spec.pending) {
        // 等待后端的视觉模式：琥珀横幅。配置可填可存，启用/启动在下面禁掉。
        root.appendChild(el("div", { class: "gw-banner draft-note" }, t(spec.pending)));
      }

      if (spec.steps) {
        root.appendChild(el("div", { class: "gw-sec" },
          el("h4", null, t("gw-creds")),
          el("ol", { class: "gw-steps" }, ...spec.steps.map((s) => el("li", null, t(s))))));
      }
      // The QR is the login path; once running there's nothing to scan, so
      // show it only while not connected.
      if (spec.qr && (!gwStatus || gwStatus.state !== "running")) {
        root.appendChild(qrSection());
        root.appendChild(el("div", { class: "gw-blurb" }, t(spec.note)));
      }
      if (spec.required.length) {
        root.appendChild(el("div", { class: "gw-sec" },
          el("h4", null, t("gw-required")),
          ...spec.required.map((fd) => fieldRow(plat, fd))));
      }
      const recSec = el("div", { class: "gw-sec" }, el("h4", null, t("gw-recommended")));
      for (const fd of spec.recommended) recSec.appendChild(fieldRow(plat, fd));
      // allowed_senders 顶层，带安全理由
      allowedInput = el("input", { value: (Array.isArray(cfg.allowed_senders) ? cfg.allowed_senders.join(", ") : "") });
      recSec.appendChild(el("div", { class: "gw-field" },
        el("label", null, t("gw-f-allowed")),
        el("div", { class: "why" }, t("gw-allowed-why")),
        allowedInput));
      root.appendChild(recSec);

      if (spec.advanced.length) {
        root.appendChild(el("details", { class: "gw-adv" },
          el("summary", null, `${t("gw-advanced")} (${spec.advanced.length})`),
          ...spec.advanced.map((fd) => fieldRow(plat, fd))));
      }

      // Config fields auto-save on change — no separate Save button. The field
      // merge: untouched fields (incl. unchanged masks) omitted, cleared → null.
      const saveConfig = async () => {
        const fields = {};
        const spec2 = GW_PLATFORMS[plat];
        for (const fd of [...spec2.required, ...spec2.recommended, ...spec2.advanced]) {
          const f = fd.key;
          if (!inputs[f]) continue;
          const v = inputs[f].value.trim();
          const init = initial[f] ?? "";
          if (v === init) continue;
          fields[f] = v === "" ? null : v;
        }
        const config = {
          enabled,
          allowed_senders: allowedInput.value.split(/[,，]/).map((s) => s.trim()).filter(Boolean),
          adapters: { [plat]: fields },
        };
        const r = await hub.call("messaging.save", { name, config }, 20000);
        cfg = (r && r.config) || cfg;
      };
      for (const f of Object.keys(inputs)) {
        inputs[f].addEventListener("change", () => saveConfig().then(() => toast(t("saved"))).catch((e) => toast(rpcErrText(e), true)));
      }
      allowedInput.addEventListener("change", () => saveConfig().then(() => toast(t("saved"))).catch((e) => toast(rpcErrText(e), true)));

      // ONE immediate control: the switch connects (start) / disconnects (stop)
      // right away. No separate Save or Stop button. For a QR platform, turning
      // it on shows the QR above (needs_login) until you scan; once scanned it
      // runs. Turning it off stops it.
      const enableSwitch = el("button", { class: "switch" + (enabled ? " on" : "") });
      enableSwitch.onclick = async () => {
        if (enableSwitch.disabled) return;
        enableSwitch.disabled = true;
        const turnOn = !enabled;
        enabled = turnOn;
        enableSwitch.classList.toggle("on", enabled);   // optimistic
        try {
          await saveConfig();                            // persist edits + enabled
          gwStatus = await hub.call(turnOn ? "gateway.start" : "gateway.stop", { name }, 30000);
          render();
        } catch (e) {
          enabled = !turnOn;
          enableSwitch.classList.toggle("on", enabled);
          enableSwitch.disabled = false;
          toast(rpcErrText(e), true);
        }
      };
      if (spec.pending) enableSwitch.disabled = true;

      root.appendChild(el("div", { class: "gw-foot" },
        enableSwitch, el("span", { class: "enable-lbl" }, enabled ? t("gw-enabled") : t("gw-disabled"))));
    }
    render();
  }

  /* ============================ MODEL POPOVER ============================ */
  openModelPopover(ev) {
    closePopovers();
    const snap = this.snap || {};
    const pop = el("div", { class: "popover" });
    pop.appendChild(el("h4", null, t("p-model")));
    pop.appendChild(el("div", { class: "model-id" }, snap.model || "—"));
    // 防御式热切换：/model 落地即点亮；未知命令则显示等待文案，输入框保留。
    const modelInput = el("input", { value: snap.model || "", list: "model-list", spellcheck: "false" });
    const applyBtn = el("button", { class: "btn soft" }, t("apply"));
    const note = el("div", { class: "dim-note" });
    modelsCached(); // 填充 #model-list datalist（app.js 缓存）
    applyBtn.addEventListener("click", async () => {
      const id = modelInput.value.trim();
      if (!id) return;
      applyBtn.disabled = true;
      let reply = null;
      try {
        reply = await this.client.command(`/model ${id}`);
      } catch (e) {
        note.textContent = e.message;
        applyBtn.disabled = false;
        return;
      }
      applyBtn.disabled = false;
      if (reply && reply.ok) {
        toast(t("saved"));
        pop.remove();
        this.refreshSnapshot();
      } else if (reply && /unknown command/i.test(reply.text || "")) {
        note.textContent = t("p-hotswap-wait");
      } else {
        note.textContent = (reply && reply.text) || t("p-hotswap-wait");
      }
    });
    pop.appendChild(el("div", { class: "model-row" }, modelInput, applyBtn));
    pop.appendChild(note);
    pop.appendChild(el("h4", { style: "margin-top:12px" }, t("p-effort")));
    const seg = el("div", { class: "seg" });
    for (const lvl of ["off", "low", "medium", "high"]) {
      seg.appendChild(el("span", { class: (snap.reasoning || "medium") === lvl ? "on" : "", onclick: async (e2) => {
        await this.command(`/reasoning ${lvl}`, true);
        seg.querySelectorAll("span").forEach((s2) => s2.classList.toggle("on", s2 === e2.target));
      } }, t("eff-" + lvl)));
    }
    pop.appendChild(seg);
    if (!snap.reasoning_supported) pop.appendChild(el("div", { class: "dim-note" }, t("p-effort-ignored")));
    const rect = ev.currentTarget.getBoundingClientRect();
    pop.style.top = Math.min(rect.top, innerHeight - 300) + "px";
    pop.style.left = Math.max(8, rect.left - 296) + "px";
    document.body.appendChild(pop);
    setTimeout(() => document.addEventListener("click", function close(e2) {
      if (!pop.contains(e2.target)) { pop.remove(); document.removeEventListener("click", close); }
    }), 0);
  }

  /* ============================ WORKS PAGE ============================ */
  async pollWorks() {
    if (this.disposed) return;
    let works;
    try { works = await hub.call("works.list", { name: this.name }, 20000); } catch (e) { return; }
    if (this.disposed) return;
    this.works = works;
    const maxM = works.length ? Math.max(...works.map((w) => w.mtime)) : 0;
    if (this.page === "works") {
      this.renderWorksList();
      this.markWorksSeen();
    } else {
      $("works-unread").classList.toggle("show", maxM > this.worksSeen);
    }
  }

  markWorksSeen() {
    const maxM = this.works && this.works.length ? Math.max(...this.works.map((w) => w.mtime)) : 0;
    this.worksSeen = Math.max(this.worksSeen, maxM);
    try { localStorage.setItem(`lm-works-seen:${this.name}`, String(this.worksSeen)); } catch (e) { /* ok */ }
    $("works-unread").classList.remove("show");
  }

  worksGroup(kind) {
    if (kind === "image") return "img";
    if (kind === "text" || kind === "code") return "text";
    return "other";
  }

  async renderWorks() {
    if (!this.works) await this.pollWorks();
    this.renderWorksList();
  }

  renderWorksList() {
    const works = this.works || [];
    const chips = $("works-chips");
    const listEl = $("works-list");
    const counts = { all: works.length, img: 0, text: 0, other: 0 };
    for (const w of works) counts[this.worksGroup(w.kind)]++;
    chips.innerHTML = "";
    for (const [key, labelKey] of [["all", "works-all"], ["img", "works-img"], ["text", "works-text"], ["other", "works-other"]]) {
      chips.appendChild(el("button", {
        class: "fchip" + (this.worksFilter === key ? " on" : ""),
        onclick: () => { this.worksFilter = key; this.renderWorksList(); },
      }, `${t(labelKey)} ${counts[key]}`));
    }
    listEl.innerHTML = "";
    const filtered = this.worksFilter === "all" ? works : works.filter((w) => this.worksGroup(w.kind) === this.worksFilter);
    if (!filtered.length) {
      listEl.appendChild(el("div", { class: "works-empty" }, t("works-empty")));
    } else {
      let lastDay = "";
      const icons = { image: "▣", web: "❖", audio: "♪", text: "≣", code: "⌨", file: "▢" };
      for (const w of filtered) {
        const day = new Date(w.mtime * 1000).toLocaleDateString();
        if (day !== lastDay) {
          lastDay = day;
          const today = new Date().toLocaleDateString();
          const yest = new Date(Date.now() - 86400000).toLocaleDateString();
          listEl.appendChild(el("div", { class: "day-label" }, day === today ? t("today") : day === yest ? t("yesterday") : day));
        }
        listEl.appendChild(el("div", { class: "work-row", onclick: () => this.openWorkPreview(w) },
          el("div", { class: "wicon" }, icons[w.kind] || "▢"),
          el("div", { class: "winfo" },
            el("b", null, w.name),
            el("span", { class: "wrel" }, w.rel || "")),
          el("div", { class: "wmeta" },
            el("span", null, fmtSize(w.size)),
            el("span", null, new Date(w.mtime * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }))),
          el("button", { class: "reveal", title: "Finder", onclick: (ev) => {
            ev.stopPropagation();
            hub.call("works.open", { path: w.path, reveal: true }).catch((e) => toast(e.message, true));
          } }, "⌖")));
      }
    }
    listEl.appendChild(el("button", { class: "drawer-foot-link", onclick: () => {
      if (this.snap) hub.call("open.path", { path: this.snap.sandbox_root }).catch((e) => toast(e.message, true));
    } }, t("open-sandbox")));
  }

  /* 应用内预览：works.read（text → <pre>；image → <img>；二进制/超限 → works.open） */
  async openWorkPreview(w) {
    let r;
    try {
      r = await hub.call("works.read", { name: this.name, rel: w.rel }, 30000);
    } catch (e) { toast(rpcErrText(e), true); return; }
    let bodyNode;
    if (r.kind === "image" && r.data_uri) {
      bodyNode = el("div", { class: "wp-img" }, el("img", { src: r.data_uri, alt: w.name }));
    } else if (r.kind === "text") {
      bodyNode = el("div", null,
        el("pre", { class: "wp-pre" }, r.content || ""),
        r.truncated ? el("div", { class: "wp-note" }, t("wp-truncated")) : null);
    } else {
      bodyNode = el("div", { class: "wp-note" },
        r.kind === "image" && r.truncated ? t("wp-too-big") : t("wp-binary"));
    }
    openModal(el("div", null,
      el("div", { class: "wp-head" },
        el("b", null, w.name),
        el("span", { class: "wp-meta" }, `${w.rel || ""} · ${fmtSize(r.size)}`)),
      bodyNode,
      el("div", { class: "acts", style: "margin-top:14px" },
        el("button", { class: "btn text", onclick: closeModal }, t("cancel")),
        el("div", { class: "grow" }),
        el("button", { class: "btn soft", onclick: (ev2) => {
          ev2.stopPropagation();
          hub.call("works.open", { path: w.path }).catch((e) => toast(e.message, true));
        } }, t("wp-open-system")))), true);
  }

  /* ============================ TERMINAL PAGE ============================
     xterm 直通 supervisor 的 /chara/<name>/pty（双向二进制帧；
     resize = 整条文本帧 \x1b[RESIZE:<cols>;<rows>]）。chara 没在运行也能用。
     首次进入终端页才创建；之后 display 切换、缓冲常驻。 */
  termTheme() {
    const cs = getComputedStyle(document.body);
    const dark = document.body.classList.contains("dark");
    return {
      background: cs.getPropertyValue("--panel").trim() || (dark ? "#232A31" : "#FFFFFF"),
      foreground: cs.getPropertyValue("--text").trim() || (dark ? "#E9EDF0" : "#1D2730"),
      cursor: cs.getPropertyValue("--accent").trim() || "#5B9FD4",
      cursorAccent: cs.getPropertyValue("--panel").trim() || "#FFFFFF",
      selectionBackground: dark ? "rgba(127,182,222,.32)" : "rgba(91,159,212,.28)",
    };
  }

  initTerm() {
    const head = $("term-head");
    const body = $("term-body");
    head.innerHTML = "";
    body.innerHTML = "";
    const code = el("code", null, (this.snap && this.snap.sandbox_root) || "…");
    this._termCode = code;
    head.appendChild(code);
    head.appendChild(el("button", { class: "btn soft", onclick: async () => {
      try {
        await navigator.clipboard.writeText(code.textContent);
        toast(t("copied-path"));
      } catch (e) { /* clipboard denied */ }
    } }, t("copy")));
    const mount = el("div", { class: "term-mount" });
    body.appendChild(mount);
    this._termClosedBar = el("div", { class: "term-closed", hidden: "" });
    body.appendChild(this._termClosedBar);
    this.term = new Terminal({
      fontFamily: getComputedStyle(document.body).getPropertyValue("--mono").trim() || "Menlo, monospace",
      fontSize: 12.5,
      scrollback: 5000,
      cursorBlink: true,
      theme: this.termTheme(),
    });
    this.termFit = new FitAddon.FitAddon();
    this.term.loadAddon(this.termFit);
    this.term.open(mount);
    this.term.onData((d) => {
      if (this.termWs && this.termWs.readyState === WebSocket.OPEN) this.termWs.send(d);
    });
    this._termResize = () => { if (this.page === "term") this.fitTerm(); };
    window.addEventListener("resize", this._termResize);
    // 主题切换（body.dark 翻转）→ 终端配色跟着走
    this._termThemeObs = new MutationObserver(() => {
      if (this.term) this.term.options.theme = this.termTheme();
    });
    this._termThemeObs.observe(document.body, { attributes: true, attributeFilter: ["class"] });
    this.connectTerm();
  }

  connectTerm() {
    if (!this.term || this.disposed) return;
    const cols = this.term.cols || 80;
    const rows = this.term.rows || 24;
    const ws = new WebSocket(
      wsUrl(`/chara/${encodeURIComponent(this.name)}/pty`) + `&cols=${cols}&rows=${rows}`);
    ws.binaryType = "arraybuffer";
    ws.onopen = () => {
      this._termClosedBar.hidden = true;
      this.fitTerm();
      if (this.term) this.term.focus();
    };
    ws.onmessage = (ev2) => {
      if (!this.term) return;
      if (typeof ev2.data === "string") this.term.write(ev2.data); // 服务端的错误文案原样进终端
      else this.term.write(new Uint8Array(ev2.data));
    };
    ws.onclose = (ev2) => {
      if (this.disposed || this.termWs !== ws) return;
      this.showTermClosed((ev2 && ev2.reason) || "");
    };
    this.termWs = ws;
  }

  showTermClosed(reason) {
    const bar = this._termClosedBar;
    if (!bar) return;
    bar.innerHTML = "";
    bar.appendChild(el("span", null, t("term-closed") + (reason ? ` · ${reason}` : "")));
    bar.appendChild(el("button", { class: "btn soft", onclick: () => {
      bar.hidden = true;
      this.connectTerm();
    } }, t("term-reconnect")));
    bar.hidden = false;
  }

  fitTerm() {
    if (!this.term || !this.termFit || this.page !== "term") return;
    try { this.termFit.fit(); } catch (e) { return; } // 容器尚无尺寸
    if (this.termWs && this.termWs.readyState === WebSocket.OPEN) {
      this.termWs.send(`\x1b[RESIZE:${this.term.cols};${this.term.rows}]`);
    }
  }

  disposeTerm() {
    if (this._termResize) window.removeEventListener("resize", this._termResize);
    this._termResize = null;
    if (this._termThemeObs) this._termThemeObs.disconnect();
    this._termThemeObs = null;
    const ws = this.termWs;
    this.termWs = null;
    if (ws) { try { ws.close(); } catch (e) { /* gone */ } }
    if (this.term) { try { this.term.dispose(); } catch (e) { /* already */ } }
    this.term = null;
    this.termFit = null;
    this._termCode = null;
    this._termClosedBar = null;
    $("term-head").innerHTML = "";
    $("term-body").innerHTML = "";
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

  /* ---- clarify questions, inline ---- */
  // Mirrors the permission round-trip: render the question with one button per
  // offered choice plus a free-text "Other" field, and reply with the answer
  // text (the server-side _clarify_hook is blocking the turn until we do).
  onClarify(p) {
    const reply = (answer) => { this.client.clarifyReply(p.id, answer).catch(() => {}); box.remove(); };
    const choices = Array.isArray(p.choices) ? p.choices : [];
    const acts = el("div", { class: "acts", style: "margin-top:10px;flex-wrap:wrap;gap:6px" });
    choices.forEach((c) => acts.appendChild(
      el("button", { class: "btn soft", onclick: () => reply(String(c)) }, String(c))));
    const other = el("input", { class: "clarify-other", type: "text", placeholder: t("clarify-other") });
    other.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && other.value.trim()) reply(other.value.trim());
    });
    const send = el("button", { class: "btn primary", onclick: () => { if (other.value.trim()) reply(other.value.trim()); } }, "→");
    const box = el("div", { class: "sec", style: "max-width:430px;margin-left:40px" },
      el("h3", null, `❓ ${p.question || ""}`),
      acts,
      el("div", { class: "acts", style: "margin-top:8px;gap:6px" }, other, send));
    $("stream-inner").appendChild(box);
    other.focus();
    this.scrollDown();
  }

  /* ---- attachments: stage / render / drop ---- */
  // Read picked/dropped FileList into the staging area. Oversized files are
  // skipped with a friendly inline warning (the WS shouldn't carry a 100MB blob).
  async stageFiles(fileList) {
    const files = Array.from(fileList || []);
    for (const f of files) {
      if (!f) continue;
      if (f.size > ATTACH_MAX_BYTES) {
        this.note(t("attach-too-big", { name: f.name || t("attach-file") }));
        continue;
      }
      try {
        const att = await readAttachment(f);
        if (this.disposed) return;
        this.staged.push(att);
        this.renderStage();
      } catch (e) {
        this.note(rpcErrText(e));
      }
    }
  }

  renderStage() {
    const wrap = $("attach-stage");
    if (!wrap) return;
    wrap.innerHTML = "";
    if (!this.staged.length) { wrap.hidden = true; return; }
    wrap.hidden = false;
    for (const att of this.staged) {
      const rm = el("button", { class: "rm", title: t("attach-remove"), "aria-label": t("attach-remove"),
        onclick: () => this.unstage(att) }, "×");
      let chip;
      if (att.isImage) {
        chip = el("div", { class: "attach-chip", title: att.name },
          el("img", { class: "thumb", src: att.url, alt: att.name }), rm);
      } else {
        chip = el("div", { class: "attach-chip file", title: att.name },
          el("span", { class: "ficon" }, "📄"),
          el("div", { class: "meta" },
            el("span", { class: "fname" }, att.name),
            el("span", { class: "fsize" }, humanSize(att.size))),
          rm);
      }
      wrap.appendChild(chip);
    }
  }

  unstage(att) {
    const i = this.staged.indexOf(att);
    if (i >= 0) this.staged.splice(i, 1);
    this.renderStage();
  }

  // Pop the staged attachments off (for sending). Clears the staging area.
  takeStaged() {
    const out = this.staged.slice();
    this.staged = [];
    this.renderStage();
    return out;
  }

  // The thumbnails/file chips shown in the optimistic user bubble (sent ones).
  attachmentRow(atts) {
    if (!atts || !atts.length) return null;
    const row = el("div", { class: "att-row" });
    for (const a of atts) {
      if (a.isImage) row.appendChild(el("img", { class: "att-thumb", src: a.url, alt: a.name }));
      else row.appendChild(el("div", { class: "att-file" }, "📄 ", a.name));
    }
    return row;
  }

  bindDrop() {
    const surface = $("page-chat");
    const overlay = $("drop-overlay");
    if (!surface || !overlay) return;
    const hint = overlay.querySelector("span");
    if (hint) hint.textContent = t("attach-drop");
    const hasFiles = (ev) => ev.dataTransfer &&
      Array.from(ev.dataTransfer.types || []).includes("Files");
    surface.ondragenter = (ev) => {
      if (!hasFiles(ev)) return;
      ev.preventDefault();
      this._dragDepth++;
      overlay.hidden = false;
    };
    surface.ondragover = (ev) => { if (hasFiles(ev)) ev.preventDefault(); };
    surface.ondragleave = (ev) => {
      if (!hasFiles(ev)) return;
      this._dragDepth = Math.max(0, this._dragDepth - 1);
      if (this._dragDepth === 0) overlay.hidden = true;
    };
    surface.ondrop = (ev) => {
      this._dragDepth = 0;
      overlay.hidden = true;
      if (!ev.dataTransfer || !ev.dataTransfer.files || !ev.dataTransfer.files.length) return;
      ev.preventDefault();   // stop the browser opening the file
      this.stageFiles(ev.dataTransfer.files);
    };
  }

  // The + menu: pick images-only / any file (routes the one hidden input), or
  // paste an image from the clipboard. Only honest options — no dead rows.
  openAttachMenu(btn) {
    closePopovers();
    const input = $("attach-input");
    const pick = (accept) => {
      input.setAttribute("accept", accept);
      input.click();   // optimistic: the OS picker opens at once
    };
    const menu = el("div", { class: "attach-menu" });
    menu.appendChild(el("h4", null, t("attach-menu-title")));
    const row = (icon, label, run) => {
      const r = el("div", { class: "row" }, el("span", null, label));
      const ic = el("span"); ic.innerHTML = icon; r.insertBefore(ic, r.firstChild);
      r.onclick = () => { closePopovers(); run(); };
      return r;
    };
    menu.appendChild(row(ICON_IMAGE, t("attach-images"), () => pick("image/*")));
    menu.appendChild(row(ICON_FILE, t("attach-files"), () => pick(ATTACH_ACCEPT_ALL)));
    if (navigator.clipboard && navigator.clipboard.read)
      menu.appendChild(row(ICON_CLIP, t("attach-paste"), () => this.pasteImage()));
    menu.appendChild(el("div", { class: "tip" }, t("attach-tip")));

    document.body.appendChild(menu);   // append first so offsetHeight is real
    const rect = btn.getBoundingClientRect();
    menu.style.left = rect.left + "px";
    menu.style.top = Math.max(8, rect.top - menu.offsetHeight - 8) + "px";
    btn.classList.add("on");
    setTimeout(() => document.addEventListener("click", function close(e2) {
      if (!menu.contains(e2.target) && e2.target !== btn) {
        closePopovers(); document.removeEventListener("click", close);
      }
    }), 0);
  }

  // Stage an image straight from the clipboard (the "Paste image" row).
  async pasteImage() {
    try {
      const items = await navigator.clipboard.read();
      const files = [];
      for (const it of items) {
        const type = (it.types || []).find((x) => x.startsWith("image/"));
        if (!type) continue;
        const blob = await it.getType(type);
        files.push(new File([blob], `pasted.${(type.split("/")[1] || "png")}`, { type }));
      }
      if (files.length) this.stageFiles(files);
      else this.note(t("attach-no-clip-image"));
    } catch (e) {
      this.note(t("attach-no-clip-image"));
    }
  }

  /* ---- composer & UI bindings ---- */
  setSending(streaming) {
    const btn = $("send-btn");
    btn.textContent = streaming ? "■" : "↑";
    btn.className = streaming ? "stop" : "send";  // clears the transient "stopping" state
    btn.disabled = false;
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
      const hasText = $("composer-input").value.trim().length > 0;
      // Busy + text in the box = stage it (queue), don't interrupt. Busy + empty
      // box = the ■ button means stop, so interrupt. Idle = just send.
      if (this.client.streaming && !hasText) {
        const btn = $("send-btn");
        btn.classList.add("stopping");   // optimistic: interrupt landed
        btn.disabled = true;
        this.client.interrupt().catch(() => {});
      } else this.submit();
    };
    // Attachments: + opens a small menu (Hermes-style); each row routes to the
    // OS picker (images-only / all files) or the clipboard. Drag-and-drop onto
    // the chat surface stages the same way.
    const attachBtn = $("attach-btn");
    const attachInput = $("attach-input");
    attachBtn.title = t("attach-add");
    attachBtn.setAttribute("aria-label", t("attach-add"));
    attachBtn.onclick = (ev) => {
      ev.stopPropagation();
      // Toggle: a second click on an open menu closes it.
      if (attachBtn.classList.contains("on")) { closePopovers(); return; }
      this.openAttachMenu(attachBtn);
    };
    attachInput.onchange = () => {
      this.stageFiles(attachInput.files);
      attachInput.value = "";   // allow re-picking the same file
    };
    this.staged = [];
    this.renderStage();
    this.bindDrop();
    $("chat-back").onclick = () => navTo("#/");
    $("chat-avatar").onclick = () => {
      if (this.deckCard) openAvatarEditor(this.deckCard);
    };
    $("chat-tabs").onclick = (ev) => {
      const s2 = ev.target.closest("span[data-page]");
      if (s2) navTo(this.hashFor(s2.dataset.page));
    };
    $("panel-btn").onclick = () => {
      const collapsed = $("panel").classList.toggle("collapsed");
      $("panel-btn").classList.toggle("on", !collapsed);
      try { localStorage.setItem("lm-panel-open", collapsed ? "0" : "1"); } catch (e) { /* ok */ }
    };
    const panelOpen = localStorage.getItem("lm-panel-open") !== "0";
    $("panel").classList.toggle("collapsed", !panelOpen);
    $("panel-btn").classList.toggle("on", panelOpen);
    $("panel-tabs").onclick = (ev) => {
      const s2 = ev.target.closest("span[data-ptab]");
      if (s2) this.showPanelTab(s2.dataset.ptab);
    };
    $("net-btn").onclick = () => this.command("/net on");
    document.addEventListener("visibilitychange", this._visHandler = () => {
      if (document.visibilityState === "visible") this.flushSuperReads();
    });
  }

  async submit() {
    const input = $("composer-input");
    const text = input.value.trim();
    const hasAttach = this.staged.length > 0;
    // Empty text is fine WHEN files are attached (send the files); otherwise no-op.
    if (!text && !hasAttach) return;
    const busy = this.client.streaming || this._appTurn;
    // A slash command is a control line — never carries attachments.
    if (text.startsWith("/") && !hasAttach) {
      if (busy) { toast(t("busy-cmd")); return; }   // control commands wait for a quiet moment
      input.value = ""; input.style.height = "auto";
      const reply = await this.command(text);
      if (reply && reply.text) this.note(reply.text);
      return;
    }
    input.value = ""; input.style.height = "auto";
    const atts = this.takeStaged();
    if (busy) this.queueMessage(text, atts);   // stage it, don't interrupt
    else await this.sendUser(text, atts);
  }
}
